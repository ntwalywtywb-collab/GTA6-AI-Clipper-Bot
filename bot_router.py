import os
import shutil
import tempfile

import yt_dlp

from bot_core import *
from bot_live import process_youtube_live
from bot_recorded import process_recorded_video


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


def process_submitted_url(url, chat_id):
    if is_live_stream(url):
        process_youtube_live(url, chat_id)
    else:
        process_recorded_video(url, chat_id)
