import os
import logging
from fastapi import FastAPI, Request
import requests

# Load environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "mysecret")

# Telegram API URL
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI app
app = FastAPI()

# Set webhook (optional, you can also set it manually once)
@app.on_event("startup")
async def set_webhook():
    webhook_url = os.getenv("RENDER_EXTERNAL_URL") + "/webhook"
    requests.get(f"{TELEGRAM_API_URL}/setWebhook", params={
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET_TOKEN
    })
    logger.info("Webhook set to %s", webhook_url)

# Function to send messages
def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    # Verify secret token
    if request.headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET_TOKEN:
        return {"status": "forbidden"}

    update = await request.json()
    logger.info("Update: %s", update)

    # Handle commands
    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"]

        if text == "/start":
            keyboard = {
                "inline_keyboard": [
                    [{"text": "العلوم", "callback_data": "sciences"}],
                    [{"text": "الأدبي", "callback_data": "literature"}],
                    [{"text": "الإدارة", "callback_data": "management"}],
                ]
            }
            send_message(chat_id, "مرحباً! اختر القسم المناسب:", reply_markup=keyboard)

    # Handle button clicks
    if "callback_query" in update:
        callback = update["callback_query"]
        chat_id = callback["message"]["chat"]["id"]
        data = callback["data"]

        if data == "management":
            keyboard = {
                "inline_keyboard": [
                    [{"text": "محاسبة", "callback_data": "accounting"}],
                    [{"text": "تمويل", "callback_data": "finance"}],
                    [{"text": "تسويق", "callback_data": "marketing"}],
                    [{"text": "إدارة أعمال", "callback_data": "business"}],
                ]
            }
            send_message(chat_id, "اختر تخصص الإدارة:", reply_markup=keyboard)

        elif data == "accounting":
            send_message(chat_id, "اخترت تخصص: محاسبة ✅")

        elif data == "finance":
            send_message(chat_id, "اخترت تخصص: تمويل ✅")

        elif data == "marketing":
            send_message(chat_id, "اخترت تخصص: تسويق ✅")

        elif data == "business":
            send_message(chat_id, "اخترت تخصص: إدارة أعمال ✅")

        elif data == "sciences":
            send_message(chat_id, "اخترت قسم: العلوم ✅")

        elif data == "literature":
            send_message(chat_id, "اخترت قسم: الأدبي ✅")

        else:
            send_message(chat_id, "❌ لا يوجد زر بهذا الاسم.")

    return {"status": "ok"}
