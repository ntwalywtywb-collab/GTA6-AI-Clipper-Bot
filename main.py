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
import math
import requests
import yt_dlp
import imageio_ffmpeg
import cv2
import numpy as np

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
LIVE_SEGMENT_SECONDS = 180
LIVE_CONVERT_TIMEOUT = 900
RECORDED_CONVERT_TIMEOUT = 1200
LIVE_POLL_SECONDS = 1
ANALYSIS_SAMPLE_SECONDS = 3

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


def send_document(chat_id, filename, caption=None):
    with open(filename, "rb") as document:
        response = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": chat_id, "caption": caption or ""},
            files={"document": (os.path.basename(filename), document, "application/octet-stream")},
            timeout=(30, 600),
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
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=error_file)
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
                raise TimeoutError(f"انتهت مهلة معالجة الفيديو بعد {timeout} ثانية. تفاصيل FFmpeg: {details}")
            time.sleep(0.5)
        if process.returncode != 0:
            error_file.seek(0)
            details = error_file.read().decode("utf-8", errors="replace")[-3000:]
            raise RuntimeError(f"FFmpeg failed (code {process.returncode}): {details}")


def probe_duration(filename, ffmpeg):
    # ffprobe is shipped beside imageio-ffmpeg's binary on supported builds.
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    if not os.path.isfile(ffprobe):
        ffprobe = "ffprobe"
    command = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filename]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            value = float(result.stdout.strip())
            if value > 0:
                return value
    except Exception:
        pass
    # OpenCV fallback: Render may not provide a separate ffprobe executable.
    cap = cv2.VideoCapture(filename)
    if cap.isOpened():
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        if frames > 0 and fps > 0:
            return float(frames / fps)
    return 0.0


def _sample_video_metrics(filename, start_seconds, ffmpeg, chat_id):
    cap = cv2.VideoCapture(filename)
    if not cap.isOpened():
        raise RuntimeError("تعذر فتح الفيديو لتحليل الحركة.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    previous = None
    motion_values = []
    face_positions = []
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if face_cascade.empty():
        cap.release()
        raise RuntimeError("تعذر تحميل نموذج اكتشاف الوجه من OpenCV.")
    for offset in range(0, CLIP_LENGTH_SECONDS, ANALYSIS_SAMPLE_SECONDS):
        if is_stopped(chat_id):
            cap.release()
            raise InterruptedError("تم إيقاف التحليل.")
        timestamp = max(0.0, start_seconds + offset)
        cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if previous is not None:
            motion_values.append(float(np.mean(cv2.absdiff(gray, previous))))
        previous = gray
        gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray_full, scaleFactor=1.1, minNeighbors=5, minSize=(max(24, width // 35), max(24, height // 35)))
        for (x, y, w, h) in faces:
            # Discard implausibly tiny detections; retain coordinates in source pixels.
            if w >= max(24, width * 0.025) and h >= max(24, height * 0.025):
                face_positions.append((int(x), int(y), int(w), int(h)))
    cap.release()
    motion_score = float(np.mean(motion_values)) if motion_values else 0.0
    return motion_score, face_positions, width, height




def _audio_score(filename, start_seconds, ffmpeg):
    command = [ffmpeg, "-hide_banner", "-ss", str(start_seconds), "-t", str(CLIP_LENGTH_SECONDS), "-i", filename, "-vn", "-af", "volumedetect", "-f", "null", "-"]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=90)
        match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", result.stderr or "")
        if match:
            return max(-60.0, min(0.0, float(match.group(1)))) + 60.0
    except Exception as exc:
        print(f"[analysis] audio score unavailable: {exc}", flush=True)
    return 0.0


def _facecam_crop(face_positions, frame_width, frame_height):
    """Infer a stable face-camera region from repeated face detections."""
    if len(face_positions) < 5 or frame_width <= 0 or frame_height <= 0:
        return None
    centers = np.array([[x + w / 2.0, y + h / 2.0] for x, y, w, h in face_positions], dtype=np.float32)
    median_center = np.median(centers, axis=0)
    distances = np.sqrt(np.sum((centers - median_center) ** 2, axis=1))
    stable = [face_positions[i] for i, distance in enumerate(distances) if distance < max(frame_width, frame_height) * 0.16]
    if len(stable) < 5 or len(stable) / len(face_positions) < 0.45:
        return None
    widths = [f[2] for f in stable]
    heights = [f[3] for f in stable]
    fw = float(np.median(widths))
    fh = float(np.median(heights))
    cx, cy = np.median([[x + w / 2.0, y + h / 2.0] for x, y, w, h in stable], axis=0)
    # Approximate a webcam tile around the face, with extra room for shoulders.
    crop_w = int(max(fw * 3.4, fh * 2.8))
    crop_h = int(crop_w * 9 / 16)
    crop_h = max(crop_h, int(fh * 3.2))
    crop_w = int(crop_h * 16 / 9)
    crop_w = min(crop_w, frame_width)
    crop_h = min(crop_h, frame_height)
    x = int(cx - crop_w / 2)
    y = int(cy - crop_h * 0.43)
    x = max(0, min(frame_width - crop_w, x))
    y = max(0, min(frame_height - crop_h, y))
    if crop_w < 80 or crop_h < 60:
        return None
    return x, y, crop_w, crop_h


def analyze_best_window(filename, chat_id, ffmpeg, window_seconds=180, source_offset=0):
    """Score 60-second windows within a source chunk and return best offset and face crop."""
    duration = probe_duration(filename, ffmpeg)
    if duration <= 0:
        duration = float(window_seconds)
    available_duration = max(0.0, duration - source_offset)
    offsets = [float(v) for v in range(0, max(1, int(available_duration - CLIP_LENGTH_SECONDS) + 1), CLIP_LENGTH_SECONDS)]
    if not offsets:
        raise RuntimeError("المقطع المستلم أقصر من 60 ثانية، لا يمكن استخراج مقطع كامل.")
    candidates = []
    for offset in offsets:
        if is_stopped(chat_id):
            raise InterruptedError("تم إيقاف تحليل أفضل اللحظات.")
        motion, faces, width, height = _sample_video_metrics(filename, source_offset + offset, ffmpeg, chat_id)
        audio = _audio_score(filename, offset, ffmpeg)
        candidates.append({"offset": offset, "motion": motion, "audio": audio, "faces": faces, "width": width, "height": height})
        print(f"[analysis] offset={offset:.0f}s motion={motion:.2f} audio={audio:.2f}", flush=True)
    motion_values = np.array([c["motion"] for c in candidates], dtype=np.float32)
    audio_values = np.array([c["audio"] for c in candidates], dtype=np.float32)
    motion_range = float(np.ptp(motion_values))
    audio_range = float(np.ptp(audio_values))
    for c in candidates:
        motion_norm = (c["motion"] - float(np.min(motion_values))) / motion_range if motion_range > 1e-6 else 0.5
        audio_norm = (c["audio"] - float(np.min(audio_values))) / audio_range if audio_range > 1e-6 else 0.5
        c["score"] = 0.55 * motion_norm + 0.45 * audio_norm
    best = max(candidates, key=lambda c: c["score"])
    face_crop = _facecam_crop(best["faces"], best["width"], best["height"])
    return best["offset"], face_crop, best["score"]


def export_highlight(chat_id, source_file, output_file, ffmpeg, source_offset=0):
    offset, face_crop, score = analyze_best_window(source_file, chat_id, ffmpeg, LIVE_SEGMENT_SECONDS, source_offset)
    seek = source_offset + offset
    if face_crop:
        x, y, w, h = face_crop
        filter_complex = (
            f"[0:v]split=2[game][face];"
            f"[game]scale=720:875:force_original_aspect_ratio=increase,crop=720:875[gb];"
            f"[face]crop={w}:{h}:{x}:{y},scale=720:405[fc];"
            f"[fc][gb]vstack=inputs=2,fps=30[outv]"
        )
        command = [ffmpeg, "-y", "-ss", str(seek), "-i", source_file, "-t", str(CLIP_LENGTH_SECONDS), "-filter_complex", filter_complex, "-map", "[outv]", "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "29", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", output_file]
    else:
        command = [ffmpeg, "-y", "-ss", str(seek), "-i", source_file, "-t", str(CLIP_LENGTH_SECONDS), "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=30", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "29", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", output_file]
    run_ffmpeg(chat_id, command, timeout=LIVE_CONVERT_TIMEOUT)
    return bool(face_crop), score


def process_recorded_video(url, chat_id):
    try:
        send_message(chat_id, "⏳ بدأت معالجة الفيديو المسجل. سيتم اختيار أفضل مقطع من كل فترة تحليل، ومدة المقطع 60 ثانية.")
        with tempfile.TemporaryDirectory() as folder:
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            output = os.path.join(folder, "source.%(ext)s")
            if not (os.path.isfile(DENO_PATH) and os.access(DENO_PATH, os.X_OK)):
                raise RuntimeError(f"Deno missing or not executable: {DENO_PATH}")
            options = {
                "outtmpl": output, "format": "bestvideo*+bestaudio/best", "noplaylist": True,
                "quiet": False, "no_warnings": False, "socket_timeout": 60, "retries": 3,
                "fragment_retries": 3, "ffmpeg_location": ffmpeg, "merge_output_format": "mp4",
                "js_runtimes": {"deno": {"path": DENO_PATH}}, "remote_components": ["ejs:github"],
                "progress_hooks": [lambda data: (_ for _ in ()).throw(yt_dlp.utils.DownloadError("STOP_REQUESTED")) if is_stopped(chat_id) else None],
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
            downloaded = [f for f in glob.glob(os.path.join(folder, "source.*")) if os.path.isfile(f) and not f.endswith((".part", ".ytdl"))]
            if not downloaded:
                raise RuntimeError("Downloaded video file not found")
            filename = max(downloaded, key=os.path.getsize)
            duration = float(info.get("duration") or probe_duration(filename, ffmpeg) or 0)
            groups = min(10, int(duration // LIVE_SEGMENT_SECONDS))
            if groups < 1:
                raise RuntimeError("الفيديو أقصر من 3 دقائق؛ نحتاج 3 دقائق لاختيار أفضل 60 ثانية.")
            send_message(chat_id, f"🎬 اكتمل التحميل. فترات التحليل: {groups}. سيتم اختيار أفضل 60 ثانية من كل فترة.")
            completed = 0
            for i in range(groups):
                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف العمل.")
                result = os.path.join(folder, f"highlight_{i + 1}.mp4")
                send_message(chat_id, f"🔎 تحليل الفترة {i + 1} من {groups} لاختيار أفضل لحظة...")
                has_face, score = export_highlight(chat_id, filename, result, ffmpeg, source_offset=i * LIVE_SEGMENT_SECONDS)
                if is_stopped(chat_id):
                    raise InterruptedError("تم إيقاف العمل.")
                caption = f"أفضل لحظة {i + 1} من {groups} — 60 ثانية — " + ("كاميرا الوجه مدمجة" if has_face else "اللعب فقط")
                send_document(chat_id, result, caption)
                completed += 1
                send_message(chat_id, f"✅ تم إرسال أفضل مقطع {completed} من {groups}. المتبقي: {groups - completed}.")
                try:
                    os.remove(result)
                except OSError:
                    pass
            send_message(chat_id, "🎉 انتهت معالجة الفيديو واختيار أفضل اللحظات.")
    except InterruptedError:
        send_message(chat_id, "⏹️ تم إيقاف المعالجة.")
    except Exception as exc:
        traceback.print_exc()
        print(f"RECORDED ERROR: {type(exc).__name__}: {exc}", flush=True)
        send_message(chat_id, f"❌ فشلت معالجة الفيديو المسجل: {type(exc).__name__}: {exc}")
    finally:
        with JOBS_LOCK:
            JOBS.pop(chat_id, None)


def process_youtube_live(url, chat_id):
    capture_process = None
    sent_count = 0
    try:
        send_message(chat_id, "🔴 جاري الاتصال ببث YouTube Live. سيتم تحليل كل 3 دقائق واختيار أفضل 60 ثانية ثم إرسالها.")
        with tempfile.TemporaryDirectory() as folder:
            capture_dir = os.path.join(folder, "live_segments")
            os.makedirs(capture_dir, exist_ok=True)
            capture = start_live_capture(url, capture_dir, segment_seconds=LIVE_SEGMENT_SECONDS)
            capture_process = capture["process"]
            register_process(chat_id, capture_process)
            send_message(chat_id, f"📡 تم الاتصال بالبث: {capture.get('title', 'YouTube Live')}.\n⏳ أول اختيار بعد اكتمال فترة تحليل تقارب 3 دقائق.")
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
                    processed_files.add(segment_file)
                    segment_number = sent_count + 1
                    output_file = os.path.join(folder, f"live_highlight_{segment_number}.mp4")
                    segment_duration = probe_duration(segment_file, ffmpeg)
                    if segment_duration and segment_duration < CLIP_LENGTH_SECONDS:
                        print(f"[live] Skipping short final segment: {segment_duration:.1f}s", flush=True)
                        try:
                            os.remove(segment_file)
                        except OSError:
                            pass
                        continue
                    send_message(chat_id, f"🔎 اكتمل جزء من البث. جاري تحليل أفضل 60 ثانية للمقطع {segment_number}...")
                    has_face, score = export_highlight(chat_id, segment_file, output_file, ffmpeg)
                    if is_stopped(chat_id):
                        raise InterruptedError("تم إيقاف استقبال البث.")
                    caption = f"أفضل لحظة من البث — 60 ثانية — " + ("كاميرا الوجه مدمجة" if has_face else "اللعب فقط")
                    send_document(chat_id, output_file, caption)
                    sent_count += 1
                    for path in (segment_file, output_file):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    send_message(chat_id, f"✅ تم إرسال أفضل مقطع {sent_count}.\n📡 استقبال البث مستمر.")
                if capture_finished:
                    remaining = [p for p in segments if p not in processed_files]
                    if not remaining:
                        break
                time.sleep(LIVE_POLL_SECONDS)
            if sent_count:
                send_message(chat_id, f"🎉 انتهى استقبال البث. إجمالي المقاطع المرسلة: {sent_count}.")
            else:
                raise RuntimeError("انتهى البث دون العثور على مقاطع مكتملة.")
    except InterruptedError:
        send_message(chat_id, f"⏹️ تم إيقاف استقبال البث. المقاطع التي أُرسلت: {sent_count}.")
    except Exception as exc:
        traceback.print_exc()
        print(f"LIVE ERROR: {type(exc).__name__}: {exc}", flush=True)
        send_message(chat_id, f"❌ تعذر استقبال البث: {type(exc).__name__}: {exc}\nالمقاطع التي أُرسلت: {sent_count}.")
    finally:
     
