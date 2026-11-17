
import os
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

@app.route("/", methods=["GET"])
def home():
    return "GTA 6 AI Clipper Bot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    message = data.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "")

    if chat_id and text:
        if text.startswith("/start"):
            reply = (
                "مرحباً بك في GTA 6 AI Clipper Bot! 🎮\n"
                "أرسل رابط فيديو عام للبدء."
            )
        elif text.startswith("http://") or text.startswith("https://"):
            reply = (
                "وصلني رابط الفيديو بنجاح! 🔗\n"
                "سيتم إضافة ميزة استخراج الكليبات لاحقاً."
            )
        else:
            reply = "من فضلك أرسل رابط فيديو."

        if BOT_TOKEN:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": reply
                },
                timeout=15
            )

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
      
