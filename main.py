
import os
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
    requests.post(
        f"{API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30
    )

def process_video(url, chat_id):
    with tempfile.TemporaryDirectory() as folder:
        try:
            send_message(chat_id, "⏳ بدأت معالجة الفيديو...")

            output = os.path.join(folder, "source.%(ext)s")

            options = {
                "outtmpl": output,
                "format": "best[ext=mp4]/best",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 30,
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

            send_message(chat_id, "🎬 تم التحميل! جاري تجهيز الكليبات...")

            duration = info.get("duration") or 0
            clip_length = 45
            clip_count = min(10, int(duration // clip_length))

            if clip_count == 0:
                clip_count = 1

            for i in range(clip_count):
                start = i * clip_length

                result = os.path.join(folder, f"clip_{i+1}.mp4")

                command = [
                    ffmpeg, "-y",
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
                    result
                ]

                subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=180
                )

                with open(result, "rb") as video:
                    response = requests.post(
                        f"{API}/sendVideo",
                        data={"chat_id": chat_id},
                        files={"video": video},
                        timeout=120
                    )
                    response.raise_for_status()

                send_message(
                    chat_id,
                    f"✅ تم إرسال الكليب {i+1} من {clip_count}"
                )

        except Exception:
            send_message(
                chat_id,
                "❌ فشلت المعالجة. قد يكون الرابط غير مدعوم "
                "أو الفيديو طويلاً أو حجمه كبيراً."
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
                "أرسل رابط فيديو أو تسجيل بث عام."
            )

        elif text.startswith(("http://", "https://")):
            send_message(chat_id, "🔗 وصل الرابط! بدأت المعالجة.")
            threading.Thread(
                target=process_video,
                args=(text, chat_id),
                daemon=True
            ).start()

        else:
            send_message(chat_id, "أرسل رابط فيديو صالحاً.")

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
