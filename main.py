import os
import glob
import tempfile
import subprocess
import threading
import shutil
import traceback
import time
import uuid
import requests
import yt_dlp
import imageio_ffmpeg

from flask import Flask, request
from live_stream import start_live_capture, stop_live_capture

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
COOKIES_FILE = "/etc/secrets/m.youtube.com_cookies.txt"
DENO_PATH = "/opt/render/project/src/.deno/bin/deno"

CLIP_LENGTH_SECONDS = 60
LIVE_SEGMENT_SECONDS = 60
LIVE_CONVERT_TIMEOUT = 900
RECORDED_CONVERT_TIMEOUT = 1200
LIVE_POLL_SECONDS = 1

pending_links = {}
pending_lock = threading.Lock()
JOBS = {}
JOBS_LOCK = threading.Lock()


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
        telegram_request("answerCallbackQuery", payload)
    except Exception as exc:
        print(f"Callback answer error: {exc}", flush=True)


def send_video(chat_id, filename, caption=None):
    with open(filename, "rb") as video:
        response = requests.post(
            f"{API}/sendVideo",
            data={"chat_id": chat_id, "caption": caption or ""},
            files={"video": video},
            timeout=(30, 600)
        )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram upload failed: {result}")


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
    # A temporary file prevents FFmpeg from blocking on a full stderr PIPE.
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
                    f"انتهت مهلة معالجة الفيديو بعد {timeout} ثانية. "
                    f"تفاصيل FFmpeg: {details}"
                )
            time.sleep(0.5)

        if process.returncode != 0:
            error_file.seek(0)
            details = error_file.read().decode("utf-8", errors="replace")[-3000:]
            raise RuntimeError(f"FFmpeg failed (code {process.returncode}): {details}")


def process_recorded_video(url, chat_id):
    try:
        send_message(chat_id, "⏳ بدأت معالجة الفيديو المسجل. مدة كل مقطع 60 ثانية.")
        with tempfile.TemporaryDirectory() as folder:
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            output = os.path.join(folder, "source.%(ext)s")

            if not (os.path.isfile(DENO_PATH) and os.access(DENO_PATH, os.X_OK)):
                raise RuntimeError(f"Deno missing or not executable: {DENO_PATH}")

            options = {
                "outtmpl": output,
                "format": "bestvideo*+bestaudio/best",
                "noplaylist": True,
                "quiet": False,
                "no_warnings": False,
                "socket_timeout": 60,
                "retries": 3,
                "fragment_retries": 3,
                "ffmpeg_location": ffmpeg,
                "merge_output_format": "mp4",
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
                raise RuntimeError("Downloaded video file not found")

            filename = max(downloaded, key=os.path.getsize)
            duration = info.get("duration") or CLIP_LENGTH_SECONDS
            clip_count = min(10, max(1, int(
                (duration + CLIP_LENGTH_SECONDS - 1) // CLIP_LENGTH_SECONDS
            )))
            send_message(
                chat_id,
                f"🎬 اكتمل التحميل. إجمالي المقاطع: {clip_count}.\n"
                f"📋 الحالة: 0 من {clip_count} مكتملة — المتبقي {clip_count}."
            )

            completed = 0
            for i in range(clip_count):
                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف العمل.")

                remaining_before = clip_count - completed
                send_message(
                    chat_id,
                    f"⚙️ جاري تجهيز المقطع {i + 1} من {clip_count}.\n"
                    f"📋 المقاطع المكتملة: {completed} — المتبقية: {remaining_before}."
                )

                start = i * CLIP_LENGTH_SECONDS
                result = os.path.join(folder, f"clip_{i + 1}.mp4")
                command = [
                    ffmpeg, "-y", "-ss", str(start), "-i", filename,
                    "-t", str(CLIP_LENGTH_SECONDS),
                    "-vf",
                    "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                    "-threads", "2", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-movflags", "+faststart", result
                ]
                run_ffmpeg(chat_id, command, timeout=RECORDED_CONVERT_TIMEOUT)

                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف العمل.")

                send_video(chat_id, result, f"المقطع {i + 1} من {clip_count} — 60 ثانية")
                completed += 1
                send_message(
                    chat_id,
                    f"✅ تم إرسال المقطع {completed} من {clip_count}.\n"
                    f"📋 المتبقي: {clip_count - completed} مقطع."
                )

            send_message(chat_id, "🎉 انتهت معالجة الفيديو المسجل وإرسال جميع المقاطع!")

    except InterruptedError:
        send_message(chat_id, "⏹️ تم إيقاف المعالجة.")
    except Exception as exc:
        traceback.print_exc()
        print(f"RECORDED ERROR: {type(exc).__name__}: {exc}", flush=True)
        if is_stopped(chat_id):
            send_message(chat_id, "⏹️ تم إيقاف المعالجة.")
        else:
            send_message(chat_id, f"❌ فشلت معالجة الفيديو المسجل: {type(exc).__name__}: {exc}")
    finally:
        with JOBS_LOCK:
            JOBS.pop(chat_id, None)


def convert_live_segment(chat_id, segment_file, output_file, ffmpeg):
    command = [
        ffmpeg, "-y", "-i", segment_file,
        "-map", "0:v:0", "-map", "0:a?",
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", output_file
    ]
    run_ffmpeg(chat_id, command, timeout=LIVE_CONVERT_TIMEOUT)


def process_youtube_live(url, chat_id):
    capture_process = None
    sent_count = 0
    try:
        send_message(
            chat_id,
            "🔴 جاري الاتصال ببث YouTube Live.\n"
            "سيتم استقبال المقاطع وتحويلها وإرسالها تباعاً، مدة كل مقطع نحو 60 ثانية."
        )

        with tempfile.TemporaryDirectory() as folder:
            capture_dir = os.path.join(folder, "live_segments")
            os.makedirs(capture_dir, exist_ok=True)

            capture = start_live_capture(
                url, capture_dir, segment_seconds=LIVE_SEGMENT_SECONDS
            )
            capture_process = capture["process"]
            register_process(chat_id, capture_process)

            send_message(
                chat_id,
                f"📡 تم الاتصال بالبث: {capture.get('title', 'YouTube Live')}.\n"
                "📋 الحالة: بانتظار اكتمال أول مقطع."
            )

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            processed_files = set()
            last_status_count = -1

            while True:
                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف استقبال البث.")

                segments = sorted(
                    glob.glob(os.path.join(capture_dir, "live_*.ts"))
                )
                capture_finished = capture_process.poll() is not None

                # While capture is running, the newest TS file is still being written.
                # Once another segment appears, all earlier files are closed and safe.
                ready_segments = segments if capture_finished else segments[:-1]

                for segment_file in ready_segments:
                    if segment_file in processed_files:
                        continue
                    if is_stopped(chat_id):
                        raise InterruptedError("تم إيقاف استقبال البث.")

                    processed_files.add(segment_file)
                    segment_number = sent_count + 1
                    output_file = os.path.join(
                        folder, f"live_clip_{segment_number}.mp4"
                    )

                    send_message(
                        chat_id,
                        f"⚙️ اكتمل جزء جديد من البث. جاري تحويل المقطع {segment_number}.\n"
                        f"✅ المقاطع المرسلة: {sent_count}."
                    )

                    convert_live_segment(
                        chat_id, segment_file, output_file, ffmpeg
                    )

                    if is_stopped(chat_id):
                        raise InterruptedError("تم إيقاف استقبال البث.")

                    send_video(
                        chat_id,
                        output_file,
                        f"YouTube Live — المقطع {segment_number} — نحو 60 ثانية"
                    )
                    sent_count += 1

                    try:
                        os.remove(segment_file)
                    except OSError:
                        pass
                    try:
                        os.remove(output_file)
                    except OSError:
                        pass

                    send_message(
                        chat_id,
                        f"✅ تم إرسال المقطع {sent_count} من البث المباشر.\n"
                        "📡 الحالة: استقبال البث مستمر وانتظار الجزء التالي."
                    )

                if capture_finished:
                    # The capture process has ended; the final segment is now closed.
                    remaining_segments = [
                        path for path in segments if path not in processed_files
                    ]
                    if not remaining_segments:
                        break

                time.sleep(LIVE_POLL_SECONDS)

            if sent_count:
                send_message(
                    chat_id,
                    f"🎉 انتهى استقبال البث وإرسال المقاطع.\n"
                    f"إجمالي المقاطع المرسلة: {sent_count}."
                )
            else:
                raise RuntimeError("انتهى البث دون العثور على مقاطع مكتملة.")

    except InterruptedError:
        send_message(
            chat_id,
            f"⏹️ تم إيقاف استقبال البث.\nالمقاطع التي أُرسلت: {sent_count}."
        )
    except Exception as exc:
        traceback.print_exc()
        print(f"LIVE ERROR: {type(exc).__name__}: {exc}", flush=True)
        if is_stopped(chat_id):
            send_message(
                chat_id,
                f"⏹️ تم إيقاف استقبال البث.\nالمقاطع التي أُرسلت: {sent_count}."
            )
        else:
            send_message(
                chat_id,
                f"❌ تعذر استقبال البث: {type(exc).__name__}: {exc}\n"
                f"المقاطع التي أُرسلت: {sent_count}."
            )
    finally:
        if capture_process is not None:
            try:
                stop_live_capture(capture_process)
            except Exception:
                traceback.print_exc()
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

    send_message(chat_id, "▶️ بدأ العمل. يمكنك إيقافه من الزر أدناه.")
    threading.Thread(
        target=process_submitted_url, args=(url, chat_id), daemon=True
    ).start()


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
        "inline_keyboard": [[
            {"text": "▶️ ابدأ العمل", "callback_data": f"start:{link_id}"},
            {"text": "⏹ إيقاف العمل", "callback_data": f"stop:{link_id}"}
        ]]
    }


@app.route("/", methods=["GET"])
def home():
    return "GTA 6 AI Clipper Bot is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    callback = data.get("callback_query")

    if callback:
        callback_id = callback.get("id")
        callback_data = callback.get("data", "")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")

        if callback_data.startswith("start:") and chat_id:
            answer_callback_query(callback_id, "بدء العمل")
            start_pending_link(chat_id, callback_data.split(":", 1)[1])
        elif callback_data.startswith("stop:") and chat_id:
            answer_callback_query(callback_id, "تم إرسال طلب الإيقاف")
            stopped, _ = stop_chat_job(chat_id)
            send_message(
                chat_id,
                "⏹️ تم إرسال طلب إيقاف العمل." if stopped
                else "لا يوجد عمل جارٍ لإيقافه."
            )
        else:
            answer_callback_query(callback_id, "طلب غير معروف")

        return "OK", 200

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return "OK", 200

    if text.startswith("/start"):
        send_message(
            chat_id,
            "🎮 أهلاً بك في GTA 6 AI Clipper Bot!\n"
            "أرسل رابط فيديو مسجل أو رابط YouTube Live، ثم اضغط زر ابدأ العمل.\n"
            "مدة المقاطع: 60 ثانية."
        )
    elif text.startswith(("http://", "https://")):
        link_id = uuid.uuid4().hex[:12]
        with pending_lock:
            pending_links[link_id] = text

        if is_youtube_live_url(text):
            description = "🔴 تم استلام رابط YouTube Live. استخدم الزرين للتحكم."
        else:
            description = "🎬 تم استلام رابط الفيديو. مدة كل مقطع 60 ثانية."

        send_message(
            chat_id,
            description,
            reply_markup=make_control_keyboard(link_id)
        )
    else:
        send_message(chat_id, "أرسل رابط فيديو أو رابط YouTube Live أولاً.")

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
, exist_ok=True)

            capture = start_live_capture(
                url, capture_dir, segment_seconds=LIVE_SEGMENT_SECONDS
            )
            capture_process = capture["process"]
            register_process(chat_id, capture_process)

            send_message(
                chat_id,
                f"📡 تم الاتصال بالبث: {capture.get('title', 'YouTube Live')}.\n"
                "📋 الحالة: بانتظار اكتمال أول مقطع."
            )

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            processed_files = set()
            last_status_count = -1

            while True:
                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف استقبال البث.")

                segments = sorted(
                    glob.glob(os.path.join(capture_dir, "live_*.ts"))
                )
                capture_finished = capture_process.poll() is not None

                # While capture is running, the newest TS file is still being written.
                # Once another segment appears, all earlier files are closed and safe.
                ready_segments = segments if capture_finished else segments[:-1]

                for segment_file in ready_segments:
                    if segment_file in processed_files:
                        continue
                    if is_stopped(chat_id):
                        raise InterruptedError("تم إيقاف استقبال البث.")

                    processed_files.add(segment_file)
                    segment_number = sent_count + 1
                    output_file = os.path.join(
                        folder, f"live_clip_{segment_number}.mp4"
                    )

                    send_message(
                        chat_id,
                        f"⚙️ اكتمل جزء جديد من البث. جاري تحويل المقطع {segment_number}.\n"
                        f"✅ المقاطع المرسلة: {sent_count}."
                    )

                    convert_live_segment(
                        chat_id, segment_file, output_file, ffmpeg
                    )

                    if is_stopped(chat_id):
                        raise InterruptedError("تم إيقاف استقبال البث.")

                    send_video(
                        chat_id,
                        output_file,
                        f"YouTube Live — المقطع {segment_number} — نحو 60 ثانية"
                    )
                    sent_count += 1

                    try:
                        os.remove(segment_file)
                    except OSError:
                        pass
                    try:
                        os.remove(output_file)
                    except OSError:
                        pass

                    send_message(
                        chat_id,
                        f"✅ تم إرسال المقطع {sent_count} من البث المباشر.\n"
                        "📡 الحالة: استقبال البث مستمر وانتظار الجزء التالي."
                    )

                if capture_finished:
                    # The capture process has ended; the final segment is now closed.
                    remaining_segments = [
                        path for path in segments if path not in processed_files
                    ]
                    if not remaining_segments:
                        break

                time.sleep(LIVE_POLL_SECONDS)

            if sent_count:
                send_message(
                    chat_id,
                    f"🎉 انتهى استقبال البث وإرسال المقاطع.\n"
                    f"إجمالي المقاطع المرسلة: {sent_count}."
                )
            else:
                raise RuntimeError("انتهى البث دون العثور على مقاطع مكتملة.")

    except InterruptedError:
        send_message(
            chat_id,
            f"⏹️ تم إيقاف استقبال البث.\nالمقاطع التي أُرسلت: {sent_count}."
        )
    except Exception as exc:
        traceback.print_exc()
        print(f"LIVE ERROR: {type(exc).__name__}: {exc}", flush=True)
        if is_stopped(chat_id):
            send_message(
                chat_id,
                f"⏹️ تم إيقاف استقبال البث.\nالمقاطع التي أُرسلت: {sent_count}."
            )
        else:
            send_message(
                chat_id,
                f"❌ تعذر استقبال البث: {type(exc).__name__}: {exc}\n"
                f"المقاطع التي أُرسلت: {sent_count}."
            )
    finally:
        if capture_process is not None:
            try:
                stop_live_capture(capture_process)
            except Exception:
                traceback.print_exc()
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

    send_message(chat_id, "▶️ بدأ العمل. يمكنك إيقافه من الزر أدناه.")
    threading.Thread(
        target=process_submitted_url, args=(url, chat_id), daemon=True
    ).start()


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
        "inline_keyboard": [[
            {"text": "▶️ ابدأ العمل", "callback_data": f"start:{link_id}"},
            {"text": "⏹ إيقاف العمل", "callback_data": f"stop:{link_id}"}
        ]]
    }


@app.route("/", methods=["GET"])
def home():
    return "GTA 6 AI Clipper Bot is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    callback = data.get("callback_query")

    if callback:
        callback_id = callback.get("id")
        callback_data = callback.get("data", "")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")

        if callback_data.startswith("start:") and chat_id:
            answer_callback_query(callback_id, "بدء العمل")
            start_pending_link(chat_id, callback_data.split(":", 1)[1])
        elif callback_data.startswith("stop:") and chat_id:
            answer_callback_query(callback_id, "تم إرسال طلب الإيقاف")
            stopped, _ = stop_chat_job(chat_id)
            send_message(
                chat_id,
                "⏹️ تم إرسال طلب إيقاف العمل." if stopped
                else "لا يوجد عمل جارٍ لإيقافه."
            )
        else:
            answer_callback_query(callback_id, "طلب غير معروف")

        return "OK", 200

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return "OK", 200

    if text.startswith("/start"):
        send_message(
            chat_id,
            "🎮 أهلاً بك في GTA 6 AI Clipper Bot!\n"
            "أرسل رابط فيديو مسجل أو رابط YouTube Live، ثم اضغط زر ابدأ العمل.\n"
            "مدة المقاطع: 60 ثانية."
        )
    elif text.startswith(("http://", "https://")):
        link_id = uuid.uuid4().hex[:12]
        with pending_lock:
            pending_links[link_id] = text

        if is_youtube_live_url(text):
            description = "🔴 تم استلام رابط YouTube Live. استخدم الزرين للتحكم."
        else:
            description = "🎬 تم استلام رابط الفيديو. مدة كل مقطع 60 ثانية."

        send_message(
            chat_id,
            description,
            reply_markup=make_control_keyboard(link_id)
        )
    else:
        send_message(chat_id, "أرسل رابط فيديو أو رابط YouTube Live أولاً.")

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
                   selected = segments[0]
                    break
                if capture_process.poll() is not None:
                    if segments:
                        selected = segments[0]
                    break
                time.sleep(1)

            if not selected:
                raise TimeoutError("لم يصل جزء بث مكتمل خلال المهلة المحددة.")

            stop_live_capture(capture_process)
            with JOBS_LOCK:
                job = JOBS.get(chat_id)
                if job:
                    job["process"] = None

            output = os.path.join(folder, "live_clip_60_seconds.mp4")
            send_message(chat_id, "✅ وصل الجزء الأول. جاري تحويله وإرساله...")
            convert_live_segment(chat_id, selected, output, ffmpeg)
            if is_stopped(chat_id):
                raise InterruptedError("تم إيقاف العمل.")
            send_video(chat_id, output, "YouTube Live — مقطع نحو 60 ثانية")
            send_message(
                chat_id,
                "🎉 نجح اختبار استقبال البث وإرسال أول مقطع مدته نحو 60 ثانية.\n"
                "ملاحظة: هذه نسخة اختبار تلتقط مقطعاً واحداً فقط؛ التشغيل المستمر "
                "واستخراج أفضل اللحظات بالذكاء الاصطناعي لم يضافا بعد."
            )
    except InterruptedError:
        send_message(chat_id, "⏹️ تم إيقاف استقبال البث.")
    except Exception as exc:
        traceback.print_exc()
        print(f"LIVE ERROR: {type(exc).__name__}: {exc}", flush=True)
        if is_stopped(chat_id):
            send_message(chat_id, "⏹️ تم إيقاف استقبال البث.")
        else:
            send_message(chat_id, f"❌ تعذر استقبال البث: {type(exc).__name__}: {exc}")
    finally:
        if capture_process is not None:
            try:
                stop_live_capture(capture_process)
            except Exception:
                traceback.print_exc()
        with JOBS_LOCK:
            JOBS.pop(chat_id, None)


def process_submitted_url(url, chat_id):
    if is_youtube_live_url(url):
        process_youtube_live_test(url, chat_id)
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

    send_message(chat_id, "▶️ بدأ العمل. يمكنك إيقافه من الزر أدناه.")
    threading.Thread(
        target=process_submitted_url, args=(url, chat_id), daemon=True
    ).start()


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
        "inline_keyboard": [[
            {"text": "▶️ ابدأ العمل", "callback_data": f"start:{link_id}"},
            {"text": "⏹ إيقاف العمل", "callback_data": f"stop:{link_id}"}
        ]]
    }


@app.route("/", methods=["GET"])
def home():
    return "GTA 6 AI Clipper Bot is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    callback = data.get("callback_query")

    if callback:
        callback_id = callback.get("id")
        callback_data = callback.get("data", "")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        if callback_data.startswith("start:") and chat_id:
            answer_callback_query(callback_id, "بدء العمل")
            start_pending_link(chat_id, callback_data.split(":", 1)[1])
        elif callback_data.startswith("stop:") and chat_id:
            answer_callback_query(callback_id, "تم إرسال طلب الإيقاف")
            stopped, _ = stop_chat_job(chat_id)
            send_message(
                chat_id,
                "⏹️ تم إرسال طلب إيقاف العمل." if stopped
                else "لا يوجد عمل جارٍ لإيقافه."
            )
        else:
            answer_callback_query(callback_id, "طلب غير معروف")
        return "OK", 200

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()
    if not chat_id or not text:
        return "OK", 200

    if text.startswith("/start"):
        send_message(
            chat_id,
            "🎮 أهلاً بك في GTA 6 AI Clipper Bot!\n"
            "أرسل رابط فيديو مسجل أو رابط YouTube Live، ثم اضغط زر ابدأ العمل.\n"
            "مدة المقاطع: 60 ثانية."
        )
    elif text.startswith(("http://", "https://")):
        link_id = uuid.uuid4().hex[:12]
        with pending_lock:
            pending_links[link_id] = text
        if is_youtube_live_url(text):
            description = "🔴 تم استلام رابط YouTube Live. استخدم الزرين للتحكم."
        else:
            description = "🎬 تم استلام رابط الفيديو. مدة كل مقطع 60 ثانية."
        send_message(
            chat_id, description,
            reply_markup=make_control_keyboard(link_id)
        )
    else:
        send_message(chat_id, "أرسل رابط فيديو أو رابط YouTube Live أولاً.")

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
