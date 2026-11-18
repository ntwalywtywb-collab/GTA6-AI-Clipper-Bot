
import os
import glob
import tempfile
import subprocess
import threading
import shutil
import traceback
import requests
import yt_dlp
import imageio_ffmpeg

from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

COOKIES_FILE = "/etc/secrets/m.youtube.com_cookies.txt"

DENO_PATH = os.path.expanduser("~/.deno/bin/deno")


def send_message(chat_id, text):
    try:
        response = requests.post(
            f"{API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=30
        )
        response.raise_for_status()

    except Exception as e:
        print(f"Telegram message error: {e}", flush=True)


def process_video(url, chat_id):

    try:
        send_message(
            chat_id,
            "⏳ بدأت معالجة الفيديو..."
        )

        with tempfile.TemporaryDirectory() as folder:

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

            output = os.path.join(
                folder,
                "source.%(ext)s"
            )

            # إعدادات yt-dlp
            options = {
                "outtmpl": output,

                "format": "bestvideo*+bestaudio/best",

                "noplaylist": True,

                "quiet": False,
                "no_warnings": False,
                "verbose": True,

                "socket_timeout": 60,
                "retries": 3,
                "fragment_retries": 3,

                "ffmpeg_location": ffmpeg,
                "merge_output_format": "mp4",

                # تفعيل Deno
                "js_runtimes": {
                    "deno": {
                        "path": DENO_PATH
                    }
                },

                # السماح بتحميل مكونات حل تحديات YouTube
                "remote_components": {
                    "ejs:github"
                }
            }

            # إعداد Cookies ليوتيوب
            if "youtube.com" in url or "youtu.be" in url:

                if os.path.isfile(COOKIES_FILE):

                    writable_cookies = os.path.join(
                        folder,
                        "cookies.txt"
                    )

                    shutil.copyfile(
                        COOKIES_FILE,
                        writable_cookies
                    )

                    options["cookiefile"] = writable_cookies

                    print(
                        "YouTube cookies copied to writable folder.",
                        flush=True
                    )

                else:

                    print(
                        "YouTube cookies file not found.",
                        flush=True
                    )

            # التحقق من Deno
            if os.path.isfile(DENO_PATH):

                print(
                    f"Deno found: {DENO_PATH}",
                    flush=True
                )

            else:

                print(
                    "WARNING: Deno executable not found at expected path.",
                    flush=True
                )

            # تحميل الفيديو
            print(
                "Starting video extraction...",
                flush=True
            )

            with yt_dlp.YoutubeDL(options) as ydl:

                info = ydl.extract_info(
                    url,
                    download=True
                )

            # البحث عن الملف المحمل
            files = glob.glob(
                os.path.join(folder, "source.*")
            )

            files = [
                f for f in files
                if os.path.isfile(f)
                and not f.endswith(".part")
                and not f.endswith(".ytdl")
            ]

            if not files:

                raise Exception(
                    "Downloaded video file not found"
                )

            filename = max(
                files,
                key=os.path.getsize
            )

            duration = info.get("duration") or 45

            clip_length = 45

            clip_count = min(
                10,
                max(
                    1,
                    int(
                        (duration + clip_length - 1)
                        // clip_length
                    )
                )
            )

            send_message(
                chat_id,
                "🎬 تم تحميل الفيديو! جاري تجهيز المقاطع..."
            )

            # إنشاء المقاطع
            for i in range(clip_count):

                start = i * clip_length

                result = os.path.join(
                    folder,
                    f"clip_{i + 1}.mp4"
                )

                command = [
                    ffmpeg,
                    "-y",
                    "-ss", str(start),
                    "-i", filename,
                    "-t", str(clip_length),

                    "-vf",
                    "scale=720:1280:force_original_aspect_ratio=increase,"
                    "crop=720:1280",

                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "28",

                    "-c:a", "aac",
                    "-movflags", "+faststart",

                    result
                ]

                subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=600
                )

                # إرسال المقطع إلى Telegram
                with open(result, "rb") as video:

                    response = requests.post(
                        f"{API}/sendVideo",

                        data={
                            "chat_id": chat_id
                        },

                        files={
                            "video": video
                        },

                        timeout=300
                    )

                    response.raise_for_status()

                    result_data = response.json()

                    if not result_data.get("ok"):

                        raise Exception(
                            f"Telegram upload failed: {result_data}"
                        )

                send_message(
                    chat_id,
                    f"✅ تم إرسال المقطع {i + 1} من {clip_count}"
                )

            send_message(
                chat_id,
                "🎉 انتهت معالجة الفيديو!"
            )

    except Exception as e:

        traceback.print_exc()

        print(
            f"PROCESSING ERROR: {type(e).__name__}: {e}",
            flush=True
        )

        send_message(
            chat_id,
            "❌ فشلت المعالجة. راجع سجلات الخادم لمعرفة السبب."
        )


@app.route("/", methods=["GET"])
def home():

    return "GTA 6 AI Clipper Bot is running!"


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json(silent=True) or {}

    message = data.get("message", {})

    chat_id = message.get("chat", {}).get("id")

    text = message.get("text", "").strip()

    if chat_id and text:

        if text.startswith("/start"):

            send_message(
                chat_id,
                "🎮 مرحباً بك في GTA 6 AI Clipper Bot!\n"
                "أرسل رابط فيديو لبدء المعالجة."
            )

        elif text.startswith(("http://", "https://")):

            send_message(
                chat_id,
                "🔗 وصل الرابط! بدأت المعالجة."
            )

            threading.Thread(
                target=process_video,
                args=(text, chat_id),
                daemon=True
            ).start()

        else:

            send_message(
                chat_id,
                "⚠️ أرسل رابط فيديو صالحاً."
            )

    return "OK", 200


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
