import os
import threading
import time
import traceback
import uuid

from flask import Flask, request

from bot_core import *
from bot_router import is_youtube_live_url, process_submitted_url

app = Flask(__name__)

PENDING_TTL_SECONDS = 3600
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional but recommended

pending_links = {}
pending_lock = threading.Lock()

SEEN_UPDATES = []
SEEN_UPDATES_SET = set()
SEEN_LOCK = threading.Lock()


def purge_pending_links():
    now = time.time()
    with pending_lock:
        expired = [k for k, (_, ts) in pending_links.items() if now - ts > PENDING_TTL_SECONDS]
        for k in expired:
            pending_links.pop(k, None)


def start_pending_link(chat_id, link_id):
    purge_pending_links()
    with pending_lock:
        entry = pending_links.pop(link_id, None)
    url = entry[0] if entry else None
    if not url:
        send_message(chat_id, "\u0627\u0646\u062a\u0647\u062a \u0635\u0644\u0627\u062d\u064a\u0629 \u0627\u0644\u0631\u0627\u0628\u0637 \u0623\u0648 \u062a\u0645 \u062a\u0634\u063a\u064a\u0644\u0647 \u0645\u0633\u0628\u0642\u0627\u064b.")
        return

    with JOBS_LOCK:
        if chat_id in JOBS:
            send_message(chat_id, "\u26a0 \u064a\u0648\u062c\u062f \u0639\u0645\u0644 \u062c\u0627\u0631\u064d \u0628\u0627\u0644\u0641\u0639\u0644. \u0623\u0648\u0642\u0641\u0647 \u0623\u0648\u0644\u0627\u064b.")
            return
        JOBS[chat_id] = {
            "stop_event": threading.Event(),
            "process": None,
            "link_id": link_id
        }

    send_message(chat_id, "\u25b6 \u0628\u062f\u0623 \u0627\u0644\u0639\u0645\u0644. \u064a\u0645\u0643\u0646\u0643 \u0625\u064a\u0642\u0627\u0641\u0647 \u0645\u0646 \u0627\u0644\u0632\u0631 \u0623\u062f\u0646\u0627\u0647.")
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
            {"text": "\u25b6 \u0627\u0628\u062f\u0623 \u0627\u0644\u0639\u0645\u0644", "callback_data": f"start:{link_id}"},
            {"text": "\u23f9 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0639\u0645\u0644", "callback_data": f"stop:{link_id}"}
        ]]
    }


@app.route("/", methods=["GET"])
def home():
    return "GTA 6 AI Clipper Bot is running!"


def is_duplicate_update(update_id):
    """Telegram re-sends an update if it did not get a fast 200; ignore repeats."""
    with SEEN_LOCK:
        if update_id in SEEN_UPDATES_SET:
            return True
        SEEN_UPDATES.append(update_id)
        SEEN_UPDATES_SET.add(update_id)
        if len(SEEN_UPDATES) > 500:
            SEEN_UPDATES_SET.discard(SEEN_UPDATES.pop(0))
        return False


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if received != WEBHOOK_SECRET:
            return "Forbidden", 403

    data = request.get_json(silent=True) or {}
    update_id = data.get("update_id")
    if update_id is not None and is_duplicate_update(update_id):
        return "OK", 200

    # Always answer 200 so Telegram never retries a failing update in a loop.
    try:
        handle_update(data)
    except Exception:
        traceback.print_exc()
    return "OK", 200


def handle_update(data):
    callback = data.get("callback_query")

    if callback:
        callback_id = callback.get("id")
        callback_data = callback.get("data") or ""
        chat_id = callback.get("message", {}).get("chat", {}).get("id")

        if callback_data.startswith("start:") and chat_id:
            answer_callback_query(callback_id, "\u0628\u062f\u0621 \u0627\u0644\u0639\u0645\u0644")
            start_pending_link(chat_id, callback_data.split(":", 1)[1])
        elif callback_data.startswith("stop:") and chat_id:
            answer_callback_query(callback_id, "\u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0637\u0644\u0628 \u0627\u0644\u0625\u064a\u0642\u0627\u0641")
            stopped, _ = stop_chat_job(chat_id)
            send_message(
                chat_id,
                "\u23f9 \u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0637\u0644\u0628 \u0625\u064a\u0642\u0627\u0641 \u0627\u0644\u0639\u0645\u0644." if stopped
                else "\u0644\u0627 \u064a\u0648\u062c\u062f \u0639\u0645\u0644 \u062c\u0627\u0631\u064d \u0644\u0625\u064a\u0642\u0627\u0641\u0647."
            )
        else:
            answer_callback_query(callback_id, "\u0637\u0644\u0628 \u063a\u064a\u0631 \u0645\u0639\u0631\u0648\u0641")

        return "OK", 200

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return "OK", 200

    if text.startswith("/start"):
        send_message(
            chat_id,
            "\U0001f3ae \u0623\u0647\u0644\u0627\u064b \u0628\u0643 \u0641\u064a GTA 6 AI Clipper Bot!\n"
            "\u0623\u0631\u0633\u0644 \u0631\u0627\u0628\u0637 \u0641\u064a\u062f\u064a\u0648 \u0645\u0633\u062c\u0644 \u0623\u0648 \u0631\u0627\u0628\u0637 YouTube Live\u060c \u062b\u0645 \u0627\u0636\u063a\u0637 \u0632\u0631 \u0627\u0628\u062f\u0623 \u0627\u0644\u0639\u0645\u0644.\n"
            "\u0645\u062f\u0629 \u0627\u0644\u0645\u0642\u0627\u0637\u0639: 60 \u062b\u0627\u0646\u064a\u0629."
        )
    elif text.startswith(("http://", "https://")):
        link_id = uuid.uuid4().hex[:12]
        purge_pending_links()
        with pending_lock:
            pending_links[link_id] = (text, time.time())

        if is_youtube_live_url(text):
            description = "\U0001f534 \u062a\u0645 \u0627\u0633\u062a\u0644\u0627\u0645 \u0631\u0627\u0628\u0637 YouTube Live. \u0627\u0633\u062a\u062e\u062f\u0645 \u0627\u0644\u0632\u0631\u064a\u0646 \u0644\u0644\u062a\u062d\u0643\u0645."
        else:
            description = "\U0001f3ac \u062a\u0645 \u0627\u0633\u062a\u0644\u0627\u0645 \u0631\u0627\u0628\u0637 \u0627\u0644\u0641\u064a\u062f\u064a\u0648. \u0645\u062f\u0629 \u0643\u0644 \u0645\u0642\u0637\u0639 60 \u062b\u0627\u0646\u064a\u0629."

        send_message(
            chat_id,
            description,
            reply_markup=make_control_keyboard(link_id)
        )
    else:
        send_message(chat_id, "\u0623\u0631\u0633\u0644 \u0631\u0627\u0628\u0637 \u0641\u064a\u062f\u064a\u0648 \u0623\u0648 \u0631\u0627\u0628\u0637 YouTube Live \u0623\u0648\u0644\u0627\u064b.")

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
