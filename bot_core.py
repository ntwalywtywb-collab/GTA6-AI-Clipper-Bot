import os
import subprocess
import tempfile
import threading
import time

import requests

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
