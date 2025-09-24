# bot.py
import os
import asyncio
import logging
import time
from collections import deque
from typing import Optional, List, Dict, Any, Tuple
import asyncpg
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
import uvicorn
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
import gc
from contextlib import asynccontextmanager

# ---------------- Config ----------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

BOT_TOKEN: Optional[str] = os.environ.get("BOT_TOKEN")
WEBHOOK_URL: Optional[str] = os.environ.get("WEBHOOK_URL")
WEBHOOK_SECRET_TOKEN: Optional[str] = os.environ.get("WEBHOOK_SECRET_TOKEN")
DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
try:
    ADMIN_IDS: List[int] = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []
REQUIRED_CHATS_RAW = os.environ.get("REQUIRED_CHATS", "")
REQUIRED_CHATS: List[str] = [c.strip() for c in REQUIRED_CHATS_RAW.split(",") if c.strip()]

PORT = int(os.environ.get("PORT", 10000))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 5))
MAX_CONCURRENT = 5  # Fixed value - don't use environment variable
PROCESSING_SEMAPHORE_TIMEOUT = 10.0  # Increased timeout

# ---------------- Globals ----------------
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None
pg_pool: Optional[asyncpg.Pool] = None

# Use smaller deque to save memory
PROCESSED_UPDATES = deque(maxlen=200)  # Further reduced
admin_state: Dict[int, Dict[str, Any]] = {}

# Use BoundedSemaphore to prevent over-release
PROCESSING_SEMAPHORE = asyncio.BoundedSemaphore(MAX_CONCURRENT)

# Rate limiting
LAST_REQUEST_TIME = 0
MIN_REQUEST_INTERVAL = 0.2  # Increased to 200ms between requests

# Request tracking for debugging
ACTIVE_REQUESTS = 0
REQUEST_HISTORY = deque(maxlen=20)

# ---------------- Memory management ----------------
def cleanup_memory():
    """Force garbage collection"""
    gc.collect()

# ---------------- Rate limiting ----------------
async def rate_limit():
    """Add small delay between requests to avoid rate limiting"""
    global LAST_REQUEST_TIME
    current_time = time.time()
    elapsed = current_time - LAST_REQUEST_TIME
    if elapsed < MIN_REQUEST_INTERVAL:
        await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)
    LAST_REQUEST_TIME = time.time()

# ---------------- DB helpers ----------------
async def init_pg_pool():
    global pg_pool
    if pg_pool:
        await pg_pool.close()
        
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set, cannot create pool")
        return
        
    logger.info("Creating asyncpg pool max_size=%s", DB_POOL_MAX)
    try:
        pg_pool = await asyncpg.create_pool(
            dsn=DATABASE_URL, 
            max_size=DB_POOL_MAX,
            min_size=1,
            command_timeout=30,
            timeout=10,
            max_inactive_connection_lifetime=60
        )
        logger.info("Postgres pool created successfully")
    except Exception as e:
        logger.error("Failed to create database pool: %s", e)
        pg_pool = None
        raise

async def db_fetchall(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            return await conn.fetch(query, *params)
    except asyncio.TimeoutError:
        logger.error("Timeout acquiring database connection")
        raise
    except Exception as e:
        logger.error("Database query failed: %s", e)
        raise

async def db_fetchone(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            return await conn.fetchrow(query, *params)
    except asyncio.TimeoutError:
        logger.error("Timeout acquiring database connection")
        raise
    except Exception as e:
        logger.error("Database query failed: %s", e)
        raise

async def db_execute(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            return await conn.execute(query, *params)
    except asyncio.TimeoutError:
        logger.error("Timeout acquiring database connection")
        raise
    except Exception as e:
        logger.error("Database execute failed: %s", e)
        raise

# ---------------- Health check ----------------
async def check_db_health():
    if not pg_pool:
        return False
    try:
        async with pg_pool.acquire(timeout=5) as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error("Database health check failed: %s", e)
        return False

# ---------------- Lifespan Manager ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global bot, BOT_ID, pg_pool
    
    logger.info("Starting up...")
    
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Missing required environment variables")
        yield
        return

    try:
        await init_pg_pool()
        await init_db_schema_and_defaults()
        
        bot_instance = Bot(token=BOT_TOKEN)
        me = await bot_instance.get_me()
        BOT_ID = me.id
        bot = bot_instance
        logger.info("Bot initialized: %s", me.username)
        
        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            if WEBHOOK_SECRET_TOKEN:
                await bot_instance.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET_TOKEN)
            else:
                await bot_instance.set_webhook(webhook_url)
            logger.info("Webhook set: %s", webhook_url)
            
    except Exception as e:
        logger.error("Startup failed: %s", e)
        bot = None
        yield
        return

    # Application is running
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    if pg_pool:
        await pg_pool.close()
    if bot:
        await bot.close()

# Create FastAPI app with lifespan
app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)

# ---------------- Root endpoint ----------------
@app.get("/")
async def root():
    return HTMLResponse("""
    <html>
        <head>
            <title>Telegram Bot</title>
        </head>
        <body>
            <h1>🤖 Telegram Bot is Running</h1>
            <p>Bot is active and ready to receive webhook calls.</p>
            <p><a href="/health">Check Health</a></p>
            <p><a href="/ping">Ping</a></p>
        </body>
    </html>
    """)

@app.get("/health")
async def health_check():
    global ACTIVE_REQUESTS
    db_healthy = await check_db_health()
    bot_healthy = bot is not None
    
    status_code = 200 if db_healthy and bot_healthy else 503
    return JSONResponse({
        "status": "healthy" if status_code == 200 else "unhealthy",
        "database": "connected" if db_healthy else "disconnected",
        "bot": "connected" if bot_healthy else "disconnected",
        "active_requests": ACTIVE_REQUESTS,
        "max_concurrent": MAX_CONCURRENT,
        "semaphore_value": PROCESSING_SEMAPHORE._value,
        "processed_updates": len(PROCESSED_UPDATES),
        "request_history": list(REQUEST_HISTORY)
    }, status_code=status_code)

@app.get("/ping")
async def ping():
    return PlainTextResponse("pong")

# ---------------- Safe Telegram API with better error handling ----------------
async def safe_telegram_call(coro, timeout=15, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            await rate_limit()  # Add rate limiting between Telegram API calls
            return await asyncio.wait_for(coro, timeout=timeout)
        except RetryAfter as e:
            if attempt == max_retries:
                logger.warning("Telegram rate limit exceeded, retry after %s seconds", e.retry_after)
                raise
            logger.info("Telegram rate limit, waiting %s seconds", e.retry_after)
            await asyncio.sleep(e.retry_after)
        except asyncio.TimeoutError:
            if attempt == max_retries:
                logger.warning("Telegram API timeout after %s seconds (attempt %s)", timeout, attempt + 1)
                raise
            logger.info("Telegram API timeout, retrying...")
            await asyncio.sleep(1)
        except Exception as e:
            if attempt == max_retries:
                logger.warning("Telegram API error: %s (attempt %s)", e, attempt + 1)
                raise
            logger.info("Telegram API error, retrying...")
            await asyncio.sleep(1)

# ---------------- Init schema ----------------
async def init_db_schema_and_defaults():
    try:
        await db_execute("""
            CREATE TABLE IF NOT EXISTS buttons (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                callback_data TEXT UNIQUE NOT NULL,
                parent_id INTEGER DEFAULT 0,
                content_type TEXT,
                file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await db_execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                class_type TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Insert defaults only if they don't exist
        defaults = [("العلمي", "science", 0), ("الأدبي", "literary", 0), ("الإدارة", "admin_panel", 0)]
        for name, cb, parent in defaults:
            await db_execute(
                "INSERT INTO buttons (name, callback_data, parent_id) VALUES ($1,$2,$3) ON CONFLICT (callback_data) DO NOTHING",
                name, cb, parent
            )
        logger.info("DB schema initialized")
    except Exception as e:
        logger.error("Failed to init DB schema: %s", e)
        raise

# ---------------- UI builders ----------------
def rows_to_markup(rows):
    if not rows:
        return None
    keyboard = [[InlineKeyboardButton(r["name"], callback_data=r["callback_data"])] for r in rows]
    return InlineKeyboardMarkup(keyboard)

async def build_main_menu():
    try:
        rows = await db_fetchall("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id")
        return rows_to_markup(rows)
    except Exception as e:
        logger.error("Failed to build main menu: %s", e)
        return None

def admin_panel_markup():
    keyboard = [
        [InlineKeyboardButton("إضافة زر جديد", callback_data="admin_add_button")],
        [InlineKeyboardButton("حذف زر", callback_data="admin_remove_button")],
        [InlineKeyboardButton("رفع ملف لزر موجود", callback_data="admin_upload_to_button")],
        [InlineKeyboardButton("عرض جميع الأزرار", callback_data="admin_list_buttons")],
        [InlineKeyboardButton("العودة", callback_data="back_to_main")],
    ]
    return InlineKeyboardMarkup(keyboard)

def missing_chats_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("لقد انضممت — تحقق", callback_data="check_membership")]])

# ---------------- Membership check ----------------
async def check_user_membership(user_id: int) -> Tuple[bool, List[str], Dict[str,str]]:
    if not REQUIRED_CHATS:
        return True, [], {}
        
    missing = []
    reasons = {}
    
    for chat_ref in REQUIRED_CHATS:
        try:
            member = await safe_telegram_call(bot.get_chat_member(chat_id=chat_ref, user_id=user_id))
            status = getattr(member, "status", "")
            if status in ("member", "administrator", "creator"):
                reasons[chat_ref] = "ok"
            else:
                missing.append(chat_ref)
                reasons[chat_ref] = "user_not_member"
        except Exception as e:
            missing.append(chat_ref)
            reasons[chat_ref] = str(e)
            
    return len(missing) == 0, missing, reasons

# ---------------- Process text message ----------------
async def process_text_message(msg: dict):
    text = msg.get("text")
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from", {})
    user_id = from_user.get("id")

    if not text or not chat_id or not user_id:
        return

    # Ignore bot messages
    if from_user.get("is_bot") or (BOT_ID and from_user.get("id") == BOT_ID):
        return

    # Admin flows
    if user_id in admin_state:
        state = admin_state[user_id]
        action = state.get("action")
        
        if action == "awaiting_add" and "|" in text:
            try:
                name, parent_str = text.split("|", 1)
                name = name.strip()
                parent_id = int(parent_str.strip())
                callback_data = f"btn_{int(time.time())}_{hash(name)}"
                await db_execute("INSERT INTO buttons (name, callback_data, parent_id) VALUES ($1,$2,$3)", 
                               name, callback_data, parent_id)
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text=f"تم إضافة الزر '{name}'"))
                admin_state.pop(user_id, None)
            except Exception as e:
                logger.error("Failed to add button: %s", e)
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="خطأ في الإضافة"))
            return

        elif action == "awaiting_remove":
            try:
                bid = int(text.strip())
                await db_execute("DELETE FROM buttons WHERE id = $1", bid)
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="تم الحذف"))
                admin_state.pop(user_id, None)
            except Exception:
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="خطأ في الحذف"))
            return

    # Start command
    if text.strip().lower().startswith("/start"):
        ok, missing, reasons = await check_user_membership(user_id)
        if not ok:
            message = "✋ يلزم الانضمام إلى:\n" + "\n".join(f"- {c}" for c in missing) + "\n\nاضغط 'لقد انضممت — تحقق'"
            if bot:
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id, 
                    text=message, 
                    reply_markup=missing_chats_markup()
                ))
            return

        # Save user and show menu
        try:
            await db_execute("INSERT INTO users (user_id, first_name) VALUES ($1,$2) ON CONFLICT DO NOTHING", 
                           user_id, from_user.get("first_name", ""))
        except Exception:
            pass  # Ignore user save errors

        markup = await build_main_menu()
        if bot and markup:
            await safe_telegram_call(bot.send_message(
                chat_id=chat_id, 
                text="مرحباً! اختر القسم:", 
                reply_markup=markup
            ))

# ---------------- Webhook handler with improved semaphore handling ----------------
@app.post("/webhook")
async def webhook(request: Request):
    global ACTIVE_REQUESTS
    acquired = False
    start_time = time.time()
    update_id = None
    
    # Validate secret token
    if WEBHOOK_SECRET_TOKEN:
        token = request.headers.get("x-telegram-bot-api-secret-token")
        if token != WEBHOOK_SECRET_TOKEN:
            raise HTTPException(403, "Invalid token")

    # Track request
    ACTIVE_REQUESTS += 1
    REQUEST_HISTORY.append((time.time(), "start"))
    
    # Acquire semaphore with timeout and better error handling
    try:
        logger.debug(f"Waiting for semaphore. Available: {PROCESSING_SEMAPHORE._value}")
        await asyncio.wait_for(PROCESSING_SEMAPHORE.acquire(), timeout=PROCESSING_SEMAPHORE_TIMEOUT)
        acquired = True
        ACTIVE_REQUESTS -= 1  # We're now processing, not waiting
        logger.debug(f"Semaphore acquired. Available: {PROCESSING_SEMAPHORE._value}")
        REQUEST_HISTORY.append((time.time(), "acquired"))
    except asyncio.TimeoutError:
        ACTIVE_REQUESTS -= 1
        logger.warning("Server busy, rejecting request - semaphore timeout")
        REQUEST_HISTORY.append((time.time(), "timeout"))
        return JSONResponse({"ok": False, "error": "busy"}, status_code=429)
    except Exception as e:
        ACTIVE_REQUESTS -= 1
        logger.error(f"Unexpected error acquiring semaphore: {e}")
        REQUEST_HISTORY.append((time.time(), "error"))
        return JSONResponse({"ok": False, "error": "internal"}, status_code=500)

    try:
        # Parse update
        update = await request.json()
        update_id = update.get("update_id")
        
        logger.info(f"Processing update {update_id}")
        
        if update_id and update_id in PROCESSED_UPDATES:
            logger.debug(f"Duplicate update {update_id}, skipping")
            return JSONResponse({"ok": True})
        PROCESSED_UPDATES.append(update_id)

        # Clean memory periodically
        if len(PROCESSED_UPDATES) % 50 == 0:
            cleanup_memory()

        # Handle callback queries
        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data")
            user_id = cq.get("from", {}).get("id")
            message = cq.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")

            logger.debug(f"Callback query: {data} from user {user_id}")

            # Answer callback first
            if bot and cq.get("id"):
                await safe_telegram_call(bot.answer_callback_query(callback_query_id=cq["id"]))

            # Handle different callback actions
            if data == "check_membership":
                ok, missing, _ = await check_user_membership(user_id)
                if ok:
                    markup = await build_main_menu()
                    if bot and chat_id and message_id:
                        await safe_telegram_call(bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text="تم التحقق — اختر القسم:",
                            reply_markup=markup
                        ))
                else:
                    if bot and chat_id and message_id:
                        await safe_telegram_call(bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text="لا زلت تحتاج للانضمام",
                            reply_markup=missing_chats_markup()
                        ))

            elif data == "admin_panel" and user_id in ADMIN_IDS:
                if bot and chat_id and message_id:
                    await safe_telegram_call(bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="لوحة التحكم:",
                        reply_markup=admin_panel_markup()
                    ))

            elif data == "back_to_main":
                markup = await build_main_menu()
                if bot and chat_id and message_id:
                    await safe_telegram_call(bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="اختر القسم:",
                        reply_markup=markup
                    ))

            elif data in ["admin_add_button", "admin_remove_button", "admin_upload_to_button"]:
                if user_id in ADMIN_IDS:
                    admin_state[user_id] = {"action": data}
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(
                            chat_id=chat_id,
                            text="أرسل البيانات المطلوبة"
                        ))

            elif data == "admin_list_buttons" and user_id in ADMIN_IDS:
                try:
                    rows = await db_fetchall("SELECT id, name, callback_data FROM buttons ORDER BY id")
                    text = "\n".join(f"{r['id']}: {r['name']} ({r['callback_data']})" for r in rows)
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(
                            chat_id=chat_id,
                            text=text or "لا توجد أزرار"
                        ))
                except Exception as e:
                    logger.error("Failed to list buttons: %s", e)

            else:
                # Regular button handling
                row = await db_fetchone("SELECT content_type, file_id FROM buttons WHERE callback_data = $1", data)
                if row and row["content_type"] and row["file_id"]:
                    ctype, fid = row["content_type"], row["file_id"]
                    if bot and chat_id:
                        if ctype == "document":
                            await safe_telegram_call(bot.send_document(chat_id=chat_id, document=fid))
                        elif ctype == "photo":
                            await safe_telegram_call(bot.send_photo(chat_id=chat_id, photo=fid))
                        elif ctype == "video":
                            await safe_telegram_call(bot.send_video(chat_id=chat_id, video=fid))
                        else:
                            await safe_telegram_call(bot.send_message(chat_id=chat_id, text=str(fid)))
                else:
                    # Show submenu
                    parent = await db_fetchone("SELECT id FROM buttons WHERE callback_data = $1", data)
                    if parent:
                        subs = await db_fetchall(
                            "SELECT name, callback_data FROM buttons WHERE parent_id = $1 ORDER BY id", 
                            parent["id"]
                        )
                        if subs:
                            keyboard = [[InlineKeyboardButton(s["name"], callback_data=s["callback_data"])] for s in subs]
                            keyboard.append([InlineKeyboardButton("العودة", callback_data="back_to_main")])
                            markup = InlineKeyboardMarkup(keyboard)
                            if bot and chat_id and message_id:
                                await safe_telegram_call(bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=message_id,
                                    text="اختر:",
                                    reply_markup=markup
                                ))
                        else:
                            if bot and chat_id:
                                await safe_telegram_call(bot.send_message(
                                    chat_id=chat_id,
                                    text="لا محتوى"
                                ))

        # Handle messages
        elif "message" in update:
            await process_text_message(update["message"])
        elif "edited_message" in update:
            await process_text_message(update["edited_message"])

        processing_time = time.time() - start_time
        logger.info(f"Successfully processed update {update_id} in {processing_time:.2f}s")
        REQUEST_HISTORY.append((time.time(), "success"))
        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"Webhook error for update {update_id}: {e}")
        REQUEST_HISTORY.append((time.time(), f"error: {str(e)}"))
        return JSONResponse({"ok": False, "error": "internal"}, status_code=500)
        
    finally:
        if acquired:
            try:
                PROCESSING_SEMAPHORE.release()
                logger.debug(f"Semaphore released. Available: {PROCESSING_SEMAPHORE._value}")
                REQUEST_HISTORY.append((time.time(), "released"))
            except ValueError as e:
                logger.error(f"Error releasing semaphore: {e}")
                REQUEST_HISTORY.append((time.time(), "release_error"))
                # For BoundedSemaphore, we don't manually reset

# ---------------- Main ----------------
def main():
    logger.info("Starting server on port %s with max_concurrent=%s", PORT, MAX_CONCURRENT)
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=PORT, 
        log_level="info",
        workers=1
    )

if __name__ == "__main__":
    main()