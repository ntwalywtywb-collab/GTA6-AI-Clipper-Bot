import os
import glob
import tempfile
import subprocess
import threading
import shutil
import traceback
import time
import uuid
import re
import gc
import requests
import urllib.request
import yt_dlp
import imageio_ffmpeg
import cv2
import numpy as np

from flask import Flask, request
from live_stream import start_live_capture, stop_live_capture
from ai_engine import analyze_video, select_best_clips, detect_facecam_region
from chat_analyzer import start_chat_monitor

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
COOKIES_FILE = "/etc/secrets/m.youtube.com_cookies.txt"
DENO_PATH = "/opt/render/project/src/.deno/bin/deno"

CLIP_LENGTH_SECONDS = 60
LIVE_SEGMENT_SECONDS = 180
LIVE_CONVERT_TIMEOUT = 900
RECORDED_CONVERT_TIMEOUT = 1200
LIVE_POLL_SECONDS = 1

PROTOTXT_PATH = "deploy.prototxt"
MODEL_PATH = "mobilenet_iter_730005.caffemodel"

pending_links = {}
pending_lock = threading.Lock()
JOBS = {}
JOBS_LOCK = threading.Lock()

USER_PREFERENCES = {}


def telegram_request(method, payload=None, timeout=30):
    response = requests.post(f"{API}/{method}", json=payload or {}, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result


def send_message(chat_id, message, reply_markup=None):
    payload = {"chat_id": chat_id, "text": message}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        telegram_request("sendMessage", payload)
    except Exception as exc:
        print(f"Telegram message error: {exc}", flush=True)


def answer_callback_query(callback_id, message=None):
    payload = {"callback_query_id": callback_id}
    if message:
        payload["text"] = message
    try:
        requests.post(f"{API}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as exc:
        print(f"Callback answer error (ignored): {exc}", flush=True)


def send_document(chat_id, filename, caption=None):
    if not os.path.exists(filename):
        raise FileNotFoundError(f"الملف غير موجود للإرسال: {filename}")
    
    with open(filename, "rb") as document:
        response = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": chat_id, "caption": caption or ""},
            files={"document": (os.path.basename(filename), document, "application/octet-stream")},
            timeout=(30, 900)
        )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram document upload failed: {result}")


def is_youtube_live_url(url):
    low = url.lower()
    return "youtube.com" in low and "/live/" in low


def is_stopped(chat_id):
    with JOBS_LOCK:
        job = JOBS.get(chat_id)
        return bool(job and job["stop_event"].is_set())


def register_process(chat_id, process):
    with JOBS_LOCK:
        job = JOBS.get(chat_id)
        if job:
            job["process"] = process
            should_stop = job["stop_event"].is_set()
        else:
            should_stop = True
    if should_stop and process.poll() is None:
        process.terminate()


def run_ffmpeg(chat_id, command, timeout=900):
    with tempfile.TemporaryFile(mode="w+b") as error_file:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=error_file
        )
        register_process(chat_id, process)
        deadline = time.monotonic() + timeout

        while process.poll() is None:
            if is_stopped(chat_id):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise InterruptedError("تم إيقاف العمل بطلب المستخدم.")

            if time.monotonic() > deadline:
                process.kill()
                process.wait()
                error_file.seek(0)
                details = error_file.read().decode("utf-8", errors="replace")[-2500:]
                raise TimeoutError(
                    f"انتهت مهلة معالجة الفيديو بعد {timeout} ثانية. التفاصيل: {details}"
                )
            time.sleep(0.5)

        if process.returncode != 0:
            error_file.seek(0)
            details = error_file.read().decode("utf-8", errors="replace")[-3000:]
            raise RuntimeError(f"FFmpeg failed (code {process.returncode}): {details}")


def get_ffmpeg_filter(chat_id, video_path=None):
    """
    يبني مرشح FFmpeg بحسب الزر المختار.
    Facecam mode: يضع نافذة الوجه أعلى اللعب، بعد اكتشاف تقريبي للوجه.
    Fullscreen mode: يحافظ على القص العمودي القديم 0.45 إذا لم تُكتشف Facecam.
    """
    mode = USER_PREFERENCES.get(chat_id, "fullscreen")
    region = None
    frame_w = frame_h = 0
    if video_path:
        try:
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            cap.release()
            region = detect_facecam_region(video_path)
        except Exception as exc:
            print(f"Facecam detection skipped: {exc}", flush=True)

    if region and frame_w > 0 and frame_h > 0:
        cam_x, cam_y, cam_w, cam_h = region

        # نافذة اللعب: نزيل جانب الكاميرا إذا كانت قرب الحافة؛
        # إذا كانت داخل الصورة نختار الجانب الأوسع لتقليل فقدان اللعب.
        left_space = cam_x
        right_space = frame_w - (cam_x + cam_w)
        if cam_x + cam_w >= frame_w * 0.78:
            game_x, game_w = 0, max(1, cam_x)
        elif cam_x <= frame_w * 0.22:
            game_x, game_w = min(frame_w - 1, cam_x + cam_w), max(1, right_space)
        elif left_space >= right_space:
            game_x, game_w = 0, max(1, cam_x)
        else:
            game_x, game_w = min(frame_w - 1, cam_x + cam_w), max(1, right_space)

        if mode == "facecam":
            # 380px للكاميرا بالأعلى و900px للعبة بالأسفل.
            return (
                f"[0:v]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},"
                "scale=720:380:force_original_aspect_ratio=decrease,"
                "pad=720:380:(ow-iw)/2:(oh-ih)/2[cam];"
                f"[0:v]crop={game_w}:{frame_h}:{game_x}:0,"
                "scale=720:900:force_original_aspect_ratio=decrease,"
                "pad=720:900:(ow-iw)/2:(oh-ih)/2[game];"
                "[cam][game]vstack=inputs=2,fps=30[vout]"
            )

        # وضع المعالجة الفائقة: قص منطقة الكاميرا أولاً ثم تكوين 9:16.
        return (
            f"[0:v]crop={game_w}:{frame_h}:{game_x}:0,"
            "scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280:(iw-720)/2:(ih-1280)/2,fps=30[vout]"
        )

    # لا توجد Facecam موثوقة: لا نقص عشوائياً.
    if mode == "facecam":
        return (
            "[0:v]scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,fps=30[vout]"
        )

    # الحفاظ على إعداد القص القديم 0.45 عند عدم اكتشاف كاميرا.
    return (
        "[0:v]scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280:(iw*0.45-360):(ih-1280)/2,fps=30[vout]"
    )


def process_recorded_video(url, chat_id):
    try:
        send_message(chat_id, "⚡ جاري تحميل الفيديو ثم تحليل الحركة لاختيار أفضل اللحظات...")
        with tempfile.TemporaryDirectory() as folder:
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            output = os.path.join(folder, "source.%(ext)s")
            if not (os.path.isfile(DENO_PATH) and os.access(DENO_PATH, os.X_OK)):
                raise RuntimeError(f"Deno missing or not executable: {DENO_PATH}")

            options = {
                "outtmpl": output, "format": "bestvideo*+bestaudio/best",
                "noplaylist": True, "quiet": False, "no_warnings": False,
                "socket_timeout": 60, "retries": 3, "fragment_retries": 3,
                "ffmpeg_location": ffmpeg, "merge_output_format": "mp4",
                "js_runtimes": {"deno": {"path": DENO_PATH}},
                "remote_components": ["ejs:github"],
                "progress_hooks": [
                    lambda data: (_ for _ in ()).throw(
                        yt_dlp.utils.DownloadError("STOP_REQUESTED")
                    ) if is_stopped(chat_id) else None
                ]
            }
            if "youtube.com" in url or "youtu.be" in url:
                if os.path.isfile(COOKIES_FILE):
                    writable_cookies = os.path.join(folder, "cookies.txt")
                    shutil.copyfile(COOKIES_FILE, writable_cookies)
                    options["cookiefile"] = writable_cookies

            if is_stopped(chat_id):
                raise InterruptedError("تم إيقاف العمل.")
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
            if is_stopped(chat_id):
                raise InterruptedError("تم إيقاف العمل.")

            downloaded = [
                f for f in glob.glob(os.path.join(folder, "source.*"))
                if os.path.isfile(f) and not f.endswith((".part", ".ytdl"))
            ]
            if not downloaded:
                raise RuntimeError("لم يتم العثور على ملف الفيديو بعد التحميل.")
            filename = max(downloaded, key=os.path.getsize)
            filter_graph = get_ffmpeg_filter(chat_id, filename)

            send_message(chat_id, "🧠 جاري تحليل الحركة وترتيب أفضل المقاطع...")
            selected = select_best_clips(filename, CLIP_LENGTH_SECONDS, max_clips=10)
            if not selected:
                raise RuntimeError("لم يتمكن المحلل من اختيار مقاطع من الفيديو.")
            send_message(chat_id, f"🎬 تم اختيار {len(selected)} من أفضل اللحظات. بدأ التحويل والإرسال.")

            for index, item in enumerate(selected, 1):
                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف العمل.")
                start = int(item["start"])
                result = os.path.join(folder, f"clip_{index}.mp4")
                command = [
                    ffmpeg, "-y", "-ss", str(start), "-i", filename,
                    "-t", str(CLIP_LENGTH_SECONDS), "-filter_complex", filter_graph, "-map", "[vout]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-threads", "2", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", result
                ]
                run_ffmpeg(chat_id, command, timeout=RECORDED_CONVERT_TIMEOUT)
                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف العمل.")
                send_document(
                    chat_id, result,
                    f"أفضل لحظة {index}/{len(selected)} | من الثانية {start} | 720x1280"
                )
                try:
                    os.remove(result)
                except OSError:
                    pass
                gc.collect()
            send_message(chat_id, "🎉 انتهى تحليل الفيديو وإرسال أفضل المقاطع.")

    except InterruptedError:
        send_message(chat_id, "⏹️ تم إيقاف المعالجة.")
    except Exception as exc:
        error_msg = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
        print(error_msg, flush=True)
        send_message(chat_id, f"❌ حدث خطأ تقني:\n{exc}\n\nالتفاصيل:\n{error_msg}")
    finally:
        gc.collect()
        with JOBS_LOCK:
            JOBS.pop(chat_id, None)

def convert_live_segment(chat_id, segment_file, output_file, ffmpeg, start=0):
    filter_graph = get_ffmpeg_filter(chat_id, segment_file)
    command = [
        ffmpeg, "-y", "-ss", str(start), "-i", segment_file,
        "-t", str(CLIP_LENGTH_SECONDS),
        "-map", "0:v:0", "-map", "0:a?",
        "-filter_complex", filter_graph, "-map", "[vout]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", output_file
    ]
    run_ffmpeg(chat_id, command, timeout=LIVE_CONVERT_TIMEOUT)

def process_youtube_live(url, chat_id):
    capture_process = None
    sent_count = 0
    chat_monitor = None
    try:
        send_message(chat_id, "🔴 جاري الاتصال بالبث وتجميعه في وحدات تحليل مدتها 3 دقائق...")
        with tempfile.TemporaryDirectory() as folder:
            capture_dir = os.path.join(folder, "live_segments")
            os.makedirs(capture_dir, exist_ok=True)
            chat_monitor = start_chat_monitor(url)

            capture = start_live_capture(url, capture_dir, segment_seconds=LIVE_SEGMENT_SECONDS)
            capture_process = capture["process"]
            register_process(chat_id, capture_process)
            send_message(chat_id, f"📡 تم الاتصال بالبث: {capture.get('title', 'YouTube Live')}.")

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            processed_files = set()
            while True:
                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف استقبال البث.")
                segments = sorted(glob.glob(os.path.join(capture_dir, "live_*.ts")))
                capture_finished = capture_process.poll() is not None
                ready_segments = segments if capture_finished else segments[:-1]

                for segment_file in ready_segments:
                    if segment_file in processed_files:
                        continue
                    if is_stopped(chat_id):
                        raise InterruptedError("تم إيقاف استقبال البث.")
                    processed_files.add(segment_file)

                    # اسم الملف live_00000.ts يحدد موضعه الزمني في البث.
                    match = re.search(r"live_(\d+)\.ts$", os.path.basename(segment_file))
                    segment_index = int(match.group(1)) if match else 0
                    timeline_offset = segment_index * LIVE_SEGMENT_SECONDS
                    events = chat_monitor.snapshot() if chat_monitor else []

                    send_message(chat_id, f"🧠 تحليل الوحدة {segment_index + 1}: الحركة والتفاعل...")
                    analysis = analyze_video(
                        segment_file, CLIP_LENGTH_SECONDS,
                        chat_events=events, timeline_offset=timeline_offset
                    )
                    start_at = int(analysis.get("start", 0))
                    output_file = os.path.join(folder, f"live_clip_{segment_index + 1}.mp4")
                    convert_live_segment(chat_id, segment_file, output_file, ffmpeg, start=start_at)
                    if is_stopped(chat_id):
                        raise InterruptedError("تم إيقاف استقبال البث.")

                    sent_count += 1
                    send_document(
                        chat_id, output_file,
                        f"YouTube Live | أفضل 60 ثانية | الوحدة {segment_index + 1} | "
                        f"الحركة {analysis.get('motion', 0):.3f} | تعليقات {analysis.get('chat', 0)}"
                    )
                    for path in (segment_file, output_file):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    gc.collect()

                if capture_finished:
                    remaining = [p for p in segments if p not in processed_files]
                    if not remaining:
                        break
                time.sleep(LIVE_POLL_SECONDS)

        send_message(chat_id, f"✅ انتهى البث. المقاطع المرسلة: {sent_count}.")
    except InterruptedError:
        send_message(chat_id, f"⏹️ تم إيقاف استقبال البث. المقاطع المرسلة: {sent_count}.")
    except Exception as exc:
        error_msg = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
        print(error_msg, flush=True)
        send_message(chat_id, f"❌ خطأ في البث الحي:\n{exc}")
    finally:
        if chat_monitor is not None:
            try:
                chat_monitor.stop()
            except Exception:
                pass
        if capture_process is not None:
            try:
                stop_live_capture(capture_process)
            except Exception:
                pass
        gc.collect()
        with JOBS_LOCK:
            JOBS.pop(chat_id, None)

def process_submitted_url(url, chat_id):
    if is_youtube_live_url(url):
        process_youtube_live(url, chat_id)
    else:
        process_recorded_video(url, chat_id)


def start_pending_link(chat_id, link_id):
    with pending_lock:
        url = pending_links.pop(link_id, None)
    if not url:
        send_message(chat_id, "انتهت صلاحية الرابط أو تم تشغيله مسبقاً.")
        return

    with JOBS_LOCK:
        if chat_id in JOBS:
            send_message(chat_id, "⚠️ يوجد عمل جارٍ بالفعل. أوقفه أولاً.")
            return
        JOBS[chat_id] = {
            "stop_event": threading.Event(),
            "process": None,
            "link_id": link_id
        }

    send_message(chat_id, "▶️ بدأ العمل بالوضع المختار. سيتم اكتشاف Facecam تلقائياً إذا أمكن.")
    threading.Thread(target=process_submitted_url, args=(url, chat_id), daemon=True).start()


def stop_chat_job(chat_id):
    with JOBS_LOCK:
        job = JOBS.get(chat_id)
        if not job:
            return False, None
        job["stop_event"].set()
        process = job.get("process")

    if process is not None:
        try:
            if process.poll() is None:
                process.terminate()
        except Exception as exc:
            print(f"Stop process error: {exc}", flush=True)

    return True, process


def make_control_keyboard(link_id):
    return {
        "inline_keyboard": [
            [
                {"text": "⚡ معالجة فائقة (0.45)", "callback_data": f"mode:fullscreen:{link_id}"},
                {"text": "🎥 توسيط + Facecam", "callback_data": f"mode:facecam:{link_id}"}
            ],
            [
                {"text": "▶️ ابدأ العمل", "callback_data": f"start:{link_id}"},
                {"text": "⏹ إيقاف العمل", "callback_data": f"stop:{link_id}"}
            ]
        ]
    }


@app.route("/", methods=["GET"])
def home():
    return "AI Video Clipper Bot is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    callback = data.get("callback_query")

    if callback:
        callback_id = callback.get("id")
        callback_data = callback.get("data", "")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        message_id = callback.get("message", {}).get("message_id")

        if callback_data.startswith("mode:") and chat_id:
            parts = callback_data.split(":")
            mode = parts[1]
            link_id = parts[2]
            USER_PREFERENCES[chat_id] = mode
            
            mode_names = {
                "fullscreen": "معالجة فائقة (0.45)",
                "facecam": "توسيط + Facecam"
            }
            answer_callback_query(callback_id, f"تم اختيار: {mode_names.get(mode, mode)}")
            
            try:
                requests.post(f"{API}/editMessageReplyMarkup", json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": make_control_keyboard(link_id)
                }, timeout=10)
            except Exception:
                pass

        elif callback_data.startswith("start:") and chat_id:
            answer_callback_query(callback_id, "بدء العمل")
            start_pending_link(chat_id, callback_data.split(":", 1)[1])
        elif callback_data.startswith("stop:") and chat_id:
            answer_callback_query(callback_id, "تم إرسال طلب الإيقاف")
            stopped, _ = st
