import glob
import os
import shutil
import tempfile
import traceback

import imageio_ffmpeg
import yt_dlp

from bot_core import *


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
