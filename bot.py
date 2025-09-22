# bot.py
import logging
import os
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, User

# ------------------ Logging ------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------ Env ------------------
BOT_TOKEN: Optional[str] = os.environ.get("BOT_TOKEN")
WEBHOOK_URL: Optional[str] = os.environ.get("WEBHOOK_URL")  # e.g. https://your-domain.com
WEBHOOK_SECRET_TOKEN: Optional[str] = os.environ.get("WEBHOOK_SECRET_TOKEN")
PORT: int = int(os.environ.get("PORT", 10000))

# ------------------ App & Globals ------------------
app = FastAPI()
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None

# ------------------ Helper: build main keyboard ------------------
def main_menu_markup() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("الأدبي", callback_data="literature")],
        [InlineKeyboardButton("العلمي", callback_data="science")],
        [InlineKeyboardButton("الإدارة", callback_data="management")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ------------------ HTTP endpoints ------------------
@app.get("/", response_class=PlainTextResponse)
async def index():
    return "OK"

@app.post("/webhook")
async def webhook(request: Request):
    """
    Receives Telegram updates via webhook.
    Validates secret token header (if configured).
    Handles /start (shows 3 buttons) and callback_query clicks.
    """
    # Validate secret token header if configured
    if WEBHOOK_SECRET_TOKEN:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header != WEBHOOK_SECRET_TOKEN:
            logger.warning("Invalid secret token header on incoming webhook")
            raise HTTPException(status_code=403, detail="Invalid secret token")

    # Parse JSON
    try:
        update = await request.json()
    except Exception as e:
        logger.exception("Failed to parse webhook JSON: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("Incoming update id=%s keys=%s", update.get("update_id"), list(update.keys()))

    # Ignore updates from bots (including this bot) to avoid loops
    # Message case
    if "message" in update and isinstance(update["message"], dict):
        msg = update["message"]
        from_user = msg.get("from", {})
        if isinstance(from_user, dict):
            # if Telegram indicates the sender is a bot -> ignore
            if from_user.get("is_bot"):
                logger.debug("Ignoring update from another bot account.")
                return JSONResponse({"ok": True})
            # if sender id equals our bot id -> ignore (extra safety)
            if BOT_ID is not None and from_user.get("id") == BOT_ID:
                logger.debug("Ignoring update from our own bot id.")
                return JSONResponse({"ok": True})

    # Callback_query case - ignore if from bot
    if "callback_query" in update and isinstance(update["callback_query"], dict):
        cq_from = update["callback_query"].get("from", {})
        if isinstance(cq_from, dict):
            if cq_from.get("is_bot"):
                logger.debug("Ignoring callback_query from a bot account.")
                return JSONResponse({"ok": True})
            if BOT_ID is not None and cq_from.get("id") == BOT_ID:
                logger.debug("Ignoring callback_query from our own bot id.")
                return JSONResponse({"ok": True})

    # Handle callback_query (button clicks)
    if "callback_query" in update and isinstance(update["callback_query"], dict):
        cq = update["callback_query"]
        cq_id = cq.get("id")
        data = cq.get("data")
        message = cq.get("message") or {}
        chat = message.get("chat", {})
        chat_id = chat.get("id")

        logger.info("callback_query received: id=%s data=%s chat_id=%s", cq_id, data, chat_id)

        # Acknowledge the callback to remove spinner
        try:
            if bot and cq_id:
                await bot.answer_callback_query(callback_query_id=cq_id)
        except Exception as e:
            logger.exception("Failed to answer callback_query: %s", e)

        # Simple responses for each main button
        if data == "literature":
            # user clicked الأدبي
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="اخترت القسم: الأدبي ✅")
            except Exception as e:
                logger.exception("Failed sending literature message: %s", e)
            return JSONResponse({"ok": True})

        if data == "science":
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="اخترت القسم: العلمي ✅")
            except Exception as e:
                logger.exception("Failed sending science message: %s", e)
            return JSONResponse({"ok": True})

        if data == "management":
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="اخترت القسم: الإدارة ✅")
            except Exception as e:
                logger.exception("Failed sending management message: %s", e)
            return JSONResponse({"ok": True})

        # Unknown callback_data
        try:
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="❌ لا يوجد زر بهذا الاسم.")
        except Exception as e:
            logger.exception("Failed sending unknown callback response: %s", e)
        return JSONResponse({"ok": True})

    # Handle plain messages (e.g., /start)
    if "message" in update and isinstance(update["message"], dict):
        msg = update["message"]
        text = msg.get("text", "")
        chat = msg.get("chat", {})
        chat_id = chat.get("id")

        if isinstance(text, str) and text.strip().lower().startswith("/start"):
            logger.info("/start received from chat_id=%s", chat_id)
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=main_menu_markup())
            except Exception as e:
                logger.exception("Failed sending /start reply: %s", e)
            return JSONResponse({"ok": True})

    # Acknowledge all other updates
    return JSONResponse({"ok": True})

# ------------------ Startup / Shutdown ------------------
@app.on_event("startup")
async def startup():
    global bot, BOT_ID
    logger.info("Starting up...")

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Exiting startup without initializing bot.")
        return

    # Create bot and fetch bot id
    bot = Bot(token=BOT_TOKEN)
    try:
        me = await bot.get_me()
        BOT_ID = getattr(me, "id", None)
        logger.info("Bot ready: id=%s username=%s", BOT_ID, getattr(me, "username", None))
    except Exception as e:
        logger.exception("Failed to get bot info: %s", e)
        BOT_ID = None

    # Register webhook if URL provided
    if WEBHOOK_URL:
        webhook_target = WEBHOOK_URL.rstrip("/") + "/webhook"
        try:
            if WEBHOOK_SECRET_TOKEN:
                ok = await bot.set_webhook(url=webhook_target, secret_token=WEBHOOK_SECRET_TOKEN)
            else:
                ok = await bot.set_webhook(url=webhook_target)
            logger.info("set_webhook -> %s : %s", webhook_target, ok)
        except Exception as e:
            logger.exception("Failed to set webhook: %s", e)

@app.on_event("shutdown")
async def shutdown():
    global bot
    logger.info("Shutting down...")
    if bot and WEBHOOK_URL:
        try:
            await bot.delete_webhook()
            logger.info("Webhook deleted")
        except Exception as e:
            logger.debug("Failed to delete webhook on shutdown: %s", e)

# ------------------ Entrypoint ------------------
def main():
    logger.info("Starting uvicorn on 0.0.0.0:%s", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

if __name__ == "__main__":
    main()
