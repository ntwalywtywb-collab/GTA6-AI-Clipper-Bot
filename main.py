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

PENDING_TTL_SECONDS = 3600
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional but recommended

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
    # Upload as a document so Telegram preserves the converted MP4 file
    # instead of processing it as a playable video message.
    with open(filename, "rb") as document:
        response = requests.post(
            f"{API}/sendDocument",
            data={"chat_id": chat_id, "caption": caption or ""},
            files={"document": (os.path.basename(filename), document, "application/octet-stream")},
            timeout=(30, 600)
        )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram document upload failed: {result}")


def is_youtube_live_url(url):
    low = url.lower()
    return "youtube.com" in low and "/live/" in low


def is_live_stream(url):
    """True for /live/ links, and for watch?v= / youtu.be links that are active live streams."""
    if is_youtube_live_url(url):
        return True
    low = url.lower()
    if "youtube.com" not in low and "youtu.be" not in low:
        return False
    try:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": 30,
        }
        if os.path.isfile(DENO_PATH) and os.access(DENO_PATH, os.X_OK):
            options["js_runtimes"] = {"deno": {"path": DENO_PATH}}
            options["remote_components"] = ["ejs:github"]
        cookies_copy = None
        if os.path.isfile(COOKIES_FILE):
            fd, cookies_copy = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            shutil.copyfile(COOKIES_FILE, cookies_copy)
            options["cookiefile"] = cookies_copy
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
        finally:
            if cookies_copy and os.path.exists(cookies_copy):
                os.remove(cookies_copy)
        return bool(info and (info.get("is_live") or info.get("live_status") == "is_live"))
    except Exception as exc:
        print(f"Live check failed, treating as recorded video: {exc}", flush=True)
        return False


def purge_pending_links():
    now = time.time()
    with pending_lock:
        expired = [k for k, (_, ts) in pending_links.items() if now - ts > PENDING_TTL_SECONDS]
        for k in expired:
            pending_links.pop(k, None)


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
                raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0639\u0645\u0644 \u0628\u0637\u0644\u0628 \u0627\u0644\u0645\u0633\u062a\u062e\u062f\u0645.")

            if time.monotonic() > deadline:
                process.kill()
                process.wait()
                error_file.seek(0)
                details = error_file.read().decode("utf-8", errors="replace")[-2500:]
                raise TimeoutError(
                    f"\u0627\u0646\u062a\u0647\u062a \u0645\u0647\u0644\u0629 \u0645\u0639\u0627\u0644\u062c\u0629 \u0627\u0644\u0641\u064a\u062f\u064a\u0648 \u0628\u0639\u062f {timeout} \u062b\u0627\u0646\u064a\u0629. "
                    f"\u062a\u0641\u0627\u0635\u064a\u0644 FFmpeg: {details}"
                )
            time.sleep(0.5)

        if process.returncode != 0:
            error_file.seek(0)
            details = error_file.read().decode("utf-8", errors="replace")[-3000:]
            raise RuntimeError(f"FFmpeg failed (code {process.returncode}): {details}")


def process_recorded_video(url, chat_id):
    try:
        send_message(chat_id, "\u23f3 \u0628\u062f\u0623\u062a \u0645\u0639\u0627\u0644\u062c\u0629 \u0627\u0644\u0641\u064a\u062f\u064a\u0648 \u0627\u0644\u0645\u0633\u062c\u0644. \u0645\u062f\u0629 \u0643\u0644 \u0645\u0642\u0637\u0639 60 \u062b\u0627\u0646\u064a\u0629.")
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
                raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0639\u0645\u0644.")

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)

            if is_stopped(chat_id):
                raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0639\u0645\u0644.")

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
                f"\U0001f3ac \u0627\u0643\u062a\u0645\u0644 \u0627\u0644\u062a\u062d\u0645\u064a\u0644. \u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u0642\u0627\u0637\u0639: {clip_count}.\n"
                f"\U0001f4cb \u0627\u0644\u062d\u0627\u0644\u0629: 0 \u0645\u0646 {clip_count} \u0645\u0643\u062a\u0645\u0644\u0629 \u2014 \u0627\u0644\u0645\u062a\u0628\u0642\u064a {clip_count}."
            )

            completed = 0
            for i in range(clip_count):
                if is_stopped(chat_id):
                    raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0639\u0645\u0644.")

                remaining_before = clip_count - completed
                send_message(
                    chat_id,
                    f"\u2699 \u062c\u0627\u0631\u064a \u062a\u062c\u0647\u064a\u0632 \u0627\u0644\u0645\u0642\u0637\u0639 {i + 1} \u0645\u0646 {clip_count}.\n"
                    f"\U0001f4cb \u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u0645\u0643\u062a\u0645\u0644\u0629: {completed} \u2014 \u0627\u0644\u0645\u062a\u0628\u0642\u064a\u0629: {remaining_before}."
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
                    raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0639\u0645\u0644.")

                send_document(chat_id, result, f"\u0627\u0644\u0645\u0642\u0637\u0639 {i + 1} \u0645\u0646 {clip_count} \u2014 60 \u062b\u0627\u0646\u064a\u0629")
                completed += 1
                send_message(
                    chat_id,
                    f"\u2705 \u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0645\u0642\u0637\u0639 {completed} \u0645\u0646 {clip_count}.\n"
                    f"\U0001f4cb \u0627\u0644\u0645\u062a\u0628\u0642\u064a: {clip_count - completed} \u0645\u0642\u0637\u0639."
                )

            send_message(chat_id, "\U0001f389 \u0627\u0646\u062a\u0647\u062a \u0645\u0639\u0627\u0644\u062c\u0629 \u0627\u0644\u0641\u064a\u062f\u064a\u0648 \u0627\u0644\u0645\u0633\u062c\u0644 \u0648\u0625\u0631\u0633\u0627\u0644 \u062c\u0645\u064a\u0639 \u0627\u0644\u0645\u0642\u0627\u0637\u0639!")

    except InterruptedError:
        send_message(chat_id, "\u23f9 \u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0645\u0639\u0627\u0644\u062c\u0629.")
    except Exception as exc:
        traceback.print_exc()
        print(f"RECORDED ERROR: {type(exc).__name__}: {exc}", flush=True)
        if is_stopped(chat_id):
            send_message(chat_id, "\u23f9 \u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0645\u0639\u0627\u0644\u062c\u0629.")
        else:
            send_message(chat_id, f"\u274c \u0641\u0634\u0644\u062a \u0645\u0639\u0627\u0644\u062c\u0629 \u0627\u0644\u0641\u064a\u062f\u064a\u0648 \u0627\u0644\u0645\u0633\u062c\u0644: {type(exc).__name__}: {exc}")
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
            "\U0001f534 \u062c\u0627\u0631\u064a \u0627\u0644\u0627\u062a\u0635\u0627\u0644 \u0628\u0628\u062b YouTube Live.\n"
            "\u0633\u064a\u062a\u0645 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0648\u062a\u062d\u0648\u064a\u0644\u0647\u0627 \u0648\u0625\u0631\u0633\u0627\u0644\u0647\u0627 \u062a\u0628\u0627\u0639\u0627\u064b\u060c \u0645\u062f\u0629 \u0643\u0644 \u0645\u0642\u0637\u0639 \u0646\u062d\u0648 60 \u062b\u0627\u0646\u064a\u0629."
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
                f"\U0001f4e1 \u062a\u0645 \u0627\u0644\u0627\u062a\u0635\u0627\u0644 \u0628\u0627\u0644\u0628\u062b: {capture.get('title', 'YouTube Live')}.\n"
                "\U0001f4cb \u0627\u0644\u062d\u0627\u0644\u0629: \u0628\u0627\u0646\u062a\u0638\u0627\u0631 \u0627\u0643\u062a\u0645\u0627\u0644 \u0623\u0648\u0644 \u0645\u0642\u0637\u0639."
            )

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            processed_files = set()
            last_status_count = -1

            while True:
                if is_stopped(chat_id):
                    raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.")

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
                        raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.")

                    processed_files.add(segment_file)
                    segment_number = sent_count + 1
                    output_file = os.path.join(
                        folder, f"live_clip_{segment_number}.mp4"
                    )

                    send_message(
                        chat_id,
                        f"\u2699 \u0627\u0643\u062a\u0645\u0644 \u062c\u0632\u0621 \u062c\u062f\u064a\u062f \u0645\u0646 \u0627\u0644\u0628\u062b. \u062c\u0627\u0631\u064a \u062a\u062d\u0648\u064a\u0644 \u0627\u0644\u0645\u0642\u0637\u0639 {segment_number}.\n"
                        f"\u2705 \u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u0645\u0631\u0633\u0644\u0629: {sent_count}."
                    )

                    convert_live_segment(
                        chat_id, segment_file, output_file, ffmpeg
                    )

                    if is_stopped(chat_id):
                        raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.")

                    send_document(
                        chat_id,
                        output_file,
                        f"YouTube Live \u2014 \u0627\u0644\u0645\u0642\u0637\u0639 {segment_number} \u2014 \u0646\u062d\u0648 60 \u062b\u0627\u0646\u064a\u0629"
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
                        f"\u2705 \u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0645\u0642\u0637\u0639 {sent_count} \u0645\u0646 \u0627\u0644\u0628\u062b \u0627\u0644\u0645\u0628\u0627\u0634\u0631.\n"
                        "\U0001f4e1 \u0627\u0644\u062d\u0627\u0644\u0629: \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b \u0645\u0633\u062a\u0645\u0631 \u0648\u0627\u0646\u062a\u0638\u0627\u0631 \u0627\u0644\u062c\u0632\u0621 \u0627\u0644\u062a\u0627\u0644\u064a."
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
                    f"\U0001f389 \u0627\u0646\u062a\u0647\u0649 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b \u0648\u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0645\u0642\u0627\u0637\u0639.\n"
                    f"\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u0645\u0631\u0633\u0644\u0629: {sent_count}."
                )
            else:
                raise RuntimeError("\u0627\u0646\u062a\u0647\u0649 \u0627\u0644\u0628\u062b \u062f\u0648\u0646 \u0627\u0644\u0639\u062b\u0648\u0631 \u0639\u0644\u0649 \u0645\u0642\u0627\u0637\u0639 \u0645\u0643\u062a\u0645\u0644\u0629.")

    except InterruptedError:
        send_message(
            chat_id,
            f"\u23f9 \u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.\n\u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u062a\u064a \u0623\u064f\u0631\u0633\u0644\u062a: {sent_count}."
        )
    except Exception as exc:
        traceback.print_exc()
        print(f"LIVE ERROR: {type(exc).__name__}: {exc}", flush=True)
        if is_stopped(chat_id):
            send_message(
                cha
