# bot.py
import logging
import os
import time
from typing import Optional
from urllib.parse import urlparse

import psycopg2
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, User

# ------------------ Logging ------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------ Env ------------------
BOT_TOKEN: Optional[str] = os.environ.get("BOT_TOKEN")
WEBHOOK_SECRET_TOKEN: Optional[str] = os.environ.get("WEBHOOK_SECRET_TOKEN")
WEBHOOK_URL: Optional[str] = os.environ.get("WEBHOOK_URL")  # e.g. "https://your-domain.com"
DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")
try:
    ADMIN_IDS = [int(id.strip()) for id in os.environ.get("ADMIN_IDS", "").split(",") if id.strip()]
except Exception:
    ADMIN_IDS = []

# ------------------ Globals ------------------
app = FastAPI()
bot: Optional[Bot] = None  # will be created on startup
BOT_ID: Optional[int] = None  # numeric id of the bot user (filled on startup)

# ------------------ DB helpers ------------------
def get_db_connection(max_retries: int = 3, retry_delay: int = 5):
    """Return a psycopg2 connection or None."""
    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set")
        return None

    for attempt in range(1, max_retries + 1):
        try:
            result = urlparse(DATABASE_URL)
            username = result.username
            password = result.password
            database = result.path[1:]
            hostname = result.hostname
            port = result.port

            conn = psycopg2.connect(
                database=database,
                user=username,
                password=password,
                host=hostname,
                port=port,
                connect_timeout=10,
            )
            logger.info("Database connection established")
            return conn
        except Exception as e:
            logger.error("Failed to connect to database (attempt %d/%d): %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(retry_delay)
    return None


def init_db():
    """Create tables and default buttons if needed."""
    conn = get_db_connection()
    if conn is None:
        logger.error("Could not initialize database: Connection failed")
        return

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS buttons (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                callback_data TEXT UNIQUE NOT NULL,
                parent_id INTEGER DEFAULT 0,
                content_type TEXT,
                file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                class_type TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        default_buttons = [
            ("العلمي", "science", 0, None, None),
            ("الأدبي", "literary", 0, None, None),
            ("الإدارة", "admin_panel", 0, None, None),
        ]

        for name, callback, parent, c_type, file_id in default_buttons:
            cursor.execute(
                """
                INSERT INTO buttons (name, callback_data, parent_id, content_type, file_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (callback_data) DO NOTHING
                """,
                (name, callback, parent, c_type, file_id),
            )

        conn.commit()
    except Exception as e:
        logger.exception("Error initializing DB: %s", e)
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()
    logger.info("Database initialized successfully")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ------------------ FastAPI endpoints ------------------
@app.get("/", response_class=PlainTextResponse)
async def index():
    return "OK"


@app.post("/webhook")
async def webhook(request: Request):
    """
    Main Telegram webhook receiver.
    Validates secret token header if configured and handles /start messages.
    """
    # Validate secret token header (if configured)
    if WEBHOOK_SECRET_TOKEN:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header != WEBHOOK_SECRET_TOKEN:
            logger.warning("Invalid secret token in incoming webhook request.")
            raise HTTPException(status_code=403, detail="Invalid secret token")

    # Parse JSON payload
    try:
        data = await request.json()
    except Exception as e:
        logger.error("Failed to parse JSON from webhook: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("Incoming webhook update id=%s type keys=%s", data.get("update_id"), list(data.keys()))

    # Protect: ignore updates that are from bots (including this bot) to avoid loops
    # For message updates:
    if "message" in data and isinstance(data["message"], dict):
        msg = data["message"]
        from_user = msg.get("from", {})

        # If there is no from_user information, just acknowledge
        if not isinstance(from_user, dict):
            return JSONResponse({"ok": True})

        # Ignore messages coming from any bot account
        if from_user.get("is_bot"):
            logger.debug("Ignoring update from a bot account (is_bot=True).")
            return JSONResponse({"ok": True})

        # Also ignore messages coming from this bot's own user id (double safety)
        if BOT_ID is not None and from_user.get("id") == BOT_ID:
            logger.debug("Ignoring update from this bot's own id.")
            return JSONResponse({"ok": True})

    # Handle /start messages only (from humans)
    if "message" in data and isinstance(data["message"], dict):
        msg = data["message"]
        text = msg.get("text", "")
        chat = msg.get("chat", {})
        from_user = msg.get("from", {})

        if isinstance(text, str) and text.strip().lower().startswith("/start"):
            # Defensive extraction
            chat_id = chat.get("id")
            user_id = from_user.get("id")
            first_name = from_user.get("first_name", "")

            if chat_id is None or user_id is None:
                logger.warning("Missing chat_id or user_id in /start update; ignoring.")
                return JSONResponse({"ok": True})

            # Save user to DB
            conn = get_db_connection()
            if conn is None:
                logger.error("DB connection failed while handling /start")
                # Return 200 to acknowledge so Telegram won't retry infinitely
                return JSONResponse({"ok": False, "error": "db"}, status_code=200)

            buttons = []
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """INSERT INTO users (user_id, first_name)
                       VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING""",
                    (user_id, first_name),
                )
                conn.commit()

                cursor.execute("SELECT name, callback_data FROM buttons WHERE parent_id = 0")
                buttons = cursor.fetchall()
            except Exception as e:
                logger.exception("DB error while handling /start: %s", e)
                buttons = []
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass
                conn.close()

            # Build reply keyboard
            reply_markup = None
            if buttons:
                keyboard = [[InlineKeyboardButton(name, callback_data=callback_data)] for name, callback_data in buttons]
                reply_markup = InlineKeyboardMarkup(keyboard)

            # Send reply message (async)
            try:
                if not bot:
                    logger.error("Bot not initialized; cannot send message.")
                else:
                    text_msg = "مرحباً! اختر القسم المناسب:"
                    if reply_markup:
                        await bot.send_message(chat_id=chat_id, text=text_msg, reply_markup=reply_markup)
                    else:
                        await bot.send_message(chat_id=chat_id, text="مرحباً! لا توجد أقسام متاحة حالياً.")
            except Exception as e:
                logger.exception("Error sending /start response: %s", e)

            return JSONResponse({"ok": True})

    # Acknowledge all other update types (no-op)
    return JSONResponse({"ok": True})


# ------------------ Startup / Shutdown ------------------
@app.on_event("startup")
async def on_startup():
    global bot, BOT_ID
    logger.info("Starting application startup...")

    # Basic env validation
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Missing required environment variables BOT_TOKEN or DATABASE_URL.")
        # Still start the server (useful to inspect logs), but bot will be None
        return

    # Initialize DB synchronously (small operation)
    try:
        init_db()
    except Exception as e:
        logger.exception("init_db() failed: %s", e)

    # Initialize Bot instance
    try:
        bot = Bot(token=BOT_TOKEN)
        # fetch bot info to get BOT_ID (async)
        me = await bot.get_me()
        if isinstance(me, User):
            BOT_ID = me.id
        else:
            BOT_ID = getattr(me, "id", None)
        logger.info("Telegram Bot instance created (id=%s, username=%s)", BOT_ID, getattr(me, "username", None))
    except Exception as e:
        logger.exception("Failed to create Bot or fetch bot info: %s", e)
        bot = None
        BOT_ID = None

    # If WEBHOOK_URL is provided, register webhook with Telegram
    if WEBHOOK_URL and bot:
        webhook_target = WEBHOOK_URL.rstrip("/") + "/webhook"
        try:
            if WEBHOOK_SECRET_TOKEN:
                res = await bot.set_webhook(url=webhook_target, secret_token=WEBHOOK_SECRET_TOKEN)
            else:
                res = await bot.set_webhook(url=webhook_target)
            logger.info("Set webhook result: %s -> %s", webhook_target, res)
        except Exception as e:
            logger.exception("Failed to set webhook to %s : %s", webhook_target, e)
    else:
        if not WEBHOOK_URL:
            logger.warning("WEBHOOK_URL not set — incoming Telegram requests will not reach this server unless webhook is set manually.")
        else:
            logger.warning("Bot not initialized; skipping webhook registration.")


@app.on_event("shutdown")
async def on_shutdown():
    global bot
    logger.info("Shutting down application...")
    if bot and BOT_TOKEN and WEBHOOK_URL:
        # Try removing webhook on shutdown (best-effort)
        try:
            await bot.delete_webhook()
            logger.info("Webhook deleted on shutdown.")
        except Exception as e:
            logger.debug("Failed to delete webhook on shutdown: %s", e)


# ------------------ Entrypoint ------------------
def main():
    port = int(os.environ.get("PORT", 10000))
    logger.info("Starting uvicorn on 0.0.0.0:%s", port)
    # uvicorn.run is blocking and will serve the FastAPI app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
