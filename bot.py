import logging
import os
import psycopg2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot
from urllib.parse import urlparse
import time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get environment variables
BOT_TOKEN = os.environ.get('BOT_TOKEN')
WEBHOOK_SECRET_TOKEN = os.environ.get('WEBHOOK_SECRET_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
try:
    ADMIN_IDS = [int(id.strip()) for id in os.environ.get('ADMIN_IDS', '').split(',') if id.strip()]
except ValueError as e:
    logger.error(f"Error parsing ADMIN_IDS: {e}")
    ADMIN_IDS = []

# DB helpers (unchanged logic)
def get_db_connection(max_retries=3, retry_delay=5):
    for attempt in range(max_retries):
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
                connect_timeout=10
            )
            logger.info("Database connection established")
            return conn
        except Exception as e:
            logger.error(f"Failed to connect to database (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    return None

def init_db():
    conn = get_db_connection()
    if conn is None:
        logger.error("Could not initialize database: Connection failed")
        return

    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS buttons (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        callback_data TEXT UNIQUE NOT NULL,
        parent_id INTEGER DEFAULT 0,
        content_type TEXT,
        file_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        class_type TEXT,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    default_buttons = [
        ('العلمي', 'science', 0, None, None),
        ('الأدبي', 'literary', 0, None, None),
        ('الإدارة', 'admin_panel', 0, None, None)
    ]

    for name, callback, parent, c_type, file_id in default_buttons:
        cursor.execute(
            'INSERT INTO buttons (name, callback_data, parent_id, content_type, file_id) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (callback_data) DO NOTHING',
            (name, callback, parent, c_type, file_id)
        )

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

def is_admin(user_id):
    return user_id in ADMIN_IDS

# --- Minimal FastAPI webhook app that handles /start messages ---

app = FastAPI()
bot = None  # will be set in main()

@app.post("/webhook")
async def webhook(request: Request):
    """
    Minimal webhook handler. Validates secret token header (if provided),
    then looks for message updates with text '/start' and responds by saving
    the user and sending the main menu. Returns 200 JSON for Telegram.
    """
    # Validate secret token header if configured
    if WEBHOOK_SECRET_TOKEN:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header != WEBHOOK_SECRET_TOKEN:
            logger.warning("Invalid secret token in incoming webhook request.")
            # Telegram will consider the request failed; return 403 to be explicit
            raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        data = await request.json()
    except Exception as e:
        logger.error("Failed to parse JSON from webhook request: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("Incoming webhook update: %s", data)

    # Simple handling of message /start
    if "message" in data:
        msg = data["message"]
        text = msg.get("text", "")
        chat = msg.get("chat", {})
        from_user = msg.get("from", {})

        # Only handle /start here (case-insensitive)
        if isinstance(text, str) and text.strip().lower().startswith("/start"):
            chat_id = chat.get("id")
            user_id = from_user.get("id")
            first_name = from_user.get("first_name", "")

            # Save user to DB (mirrors your original start handler)
            conn = get_db_connection()
            if conn is None:
                # Return 200 to Telegram (so it won't keep retrying) but log the issue
                logger.error("DB connection failed while handling /start")
                # You may choose to return 500 so Telegram retries; for now we return 200 to avoid too many retries
                return JSONResponse({"ok": False, "error": "db"}, status_code=200)

            try:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT INTO users (user_id, first_name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING',
                    (user_id, first_name)
                )
                conn.commit()

                # Build main menu from buttons table (parent_id = 0)
                cursor.execute('SELECT name, callback_data FROM buttons WHERE parent_id = 0')
                buttons = cursor.fetchall()
            except Exception as e:
                logger.error("DB error while handling /start: %s", e)
                buttons = []
            finally:
                conn.close()

            # Build keyboard
            if buttons:
                keyboard = [[InlineKeyboardButton(name, callback_data=callback_data)] for name, callback_data in buttons]
                reply_markup = InlineKeyboardMarkup(keyboard)
            else:
                reply_markup = None

            try:
                # Send welcome message + menu
                text_msg = "مرحباً! اختر القسم المناسب:"
                if reply_markup:
                    await bot.send_message(chat_id=chat_id, text=text_msg, reply_markup=reply_markup)
                else:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! لا توجد أقسام متاحة حالياً.")
            except Exception as e:
                logger.error("Error sending /start response: %s", e)

            # Return OK to Telegram
            return JSONResponse({"ok": True})

    # For any other update type we just acknowledge with 200 (no-op)
    return JSONResponse({"ok": True})


# -------------------------
# Synchronous entrypoint
# -------------------------
def main():
    global bot

    # required env checks
    if not all([BOT_TOKEN, DATABASE_URL]):
        logger.error("Missing required environment variables: BOT_TOKEN or DATABASE_URL")
        return

    # Initialize DB (creates tables etc.)
    init_db()

    # Initialize Bot instance (used in webhook)
    bot = Bot(token=BOT_TOKEN)

    # Port for the ASGI server
    port = int(os.environ.get("PORT", 10000))

    # Log and start uvicorn (FastAPI) — Render will run this Python process and serve /webhook
    logger.info("Starting FastAPI webhook listener on 0.0.0.0:%s", port)
    # Note: host must be 0.0.0.0 for Render
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
