
import os
import glob
import tempfile
import subprocess
import threading
import requests
import yt_dlp
import imageio_ffmpeg

from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_message(chat_id, text):
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is missing", flush=True)
        return

    try:
        requests.post(
            f"{API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=30
        )
    except Exception as e:
        print(f"Message error: {e}", flush=True)


def process_video(url, chat_id):

    try:
        send_message(chat_id, "⏳ بدأت معالجة الفيديو...")

        with tempfile.TemporaryDirectory() as folder:

            output = os.path.join(folder, "source.%(ext)s")

            options = {
                "outtmpl": output,
                "format": "best[ext=mp4]/best",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 60,
                "retries": 3,
                "merge_output_format": "mp4"
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(
                    url,
                    download=True
                )

            files = glob.glob(
                os.path.join(folder, "source.*")
            )

            files = [
                f for f in files
                if os.path.isfile(f)
                and not f.endswith(".part")
            ]

            if not files:
                raise Exception("Downloaded video file not found")

            filename = files[0]

            duration = info.get("duration") or 45

            clip_length = 45

            clip_count = min(
                10,
                max(1, int((duration + clip_length - 1) // clip_length))
            )

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

            send_message(
                chat_id,
                "🎬 تم تحميل الفيديو! جاري تجهيز المقاطع..."
            )

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

                with open(result, "rb") as video:

                    response = requests.post(
                        f"{API}/sendVideo",
                        data={"chat_id": chat_id},
                        files={"video": video},
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

        import traceback

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
                "أرسل رابط فيديو عام لبدء المعالجة."
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
