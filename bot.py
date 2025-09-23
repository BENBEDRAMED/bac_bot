# bot.py
import os
import asyncio
import logging
import time
from collections import deque
from typing import Optional, List, Dict, Any, Tuple
import asyncpg
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
import gc

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
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 5))  # Conservative for stability
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 15))  # Reduced for stability
PROCESSING_SEMAPHORE_TIMEOUT = float(os.environ.get("PROCESSING_SEMAPHORE_TIMEOUT", 3.0))

# ---------------- Globals ----------------
app = FastAPI(docs_url=None, redoc_url=None)
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None
pg_pool: Optional[asyncpg.Pool] = None

# Use smaller deque to save memory
PROCESSED_UPDATES = deque(maxlen=1000)
admin_state: Dict[int, Dict[str, Any]] = {}

# Use Semaphore instead of BoundedSemaphore for better error handling
PROCESSING_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

# ---------------- Memory management ----------------
def cleanup_memory():
    """Force garbage collection"""
    gc.collect()

# ---------------- DB helpers with connection limits ----------------
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
        async with pg_pool.acquire(timeout=5) as conn:  # Add acquire timeout
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

@app.get("/health")
async def health_check():
    db_healthy = await check_db_health()
    bot_healthy = bot is not None
    
    status_code = 200 if db_healthy and bot_healthy else 503
    return JSONResponse({
        "status": "healthy" if status_code == 200 else "unhealthy",
        "database": "connected" if db_healthy else "disconnected",
        "bot": "connected" if bot_healthy else "disconnected",
        "active_requests": MAX_CONCURRENT - PROCESSING_SEMAPHORE._value,
        "memory_usage": f"{gc.mem_alloc() / 1024 / 1024:.1f}MB"
    }, status_code=status_code)

@app.get("/ping")
async def ping():
    return PlainTextResponse("pong")

# ---------------- Safe Telegram API with better error handling ----------------
async def safe_telegram_call(coro, timeout=10):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Telegram API timeout after %s seconds", timeout)
        raise
    except Exception as e:
        logger.warning("Telegram API error: %s", e)
        raise

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
        [InlineKeyboardButton("إزالة ملف من زر", callback_data="admin_remove_file")],  # NEW button
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
                    await safe_telegram_call(bot.send_message(chat_id, f"تم إضافة الزر '{name}'"))
                admin_state.pop(user_id, None)
            except Exception as e:
                logger.error("Failed to add button: %s", e)
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id, "خطأ في الإضافة"))
            return

        elif action == "awaiting_remove":
            try:
                bid = int(text.strip())
                await db_execute("DELETE FROM buttons WHERE id = $1", bid)
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id, f"تم الحذف"))
                admin_state.pop(user_id, None)
            except Exception:
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id, "خطأ في الحذف"))
            return

        # NEW: start remove-file confirmation flow when admin sent the button id
        elif action == "admin_remove_file":
            # Expect admin to send numeric button id
            try:
                bid = int(text.strip())
            except ValueError:
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id, "أرسل رقم معرف زر صحيح (رقم)."))
                return

            try:
                row = await db_fetchone("SELECT id, name, content_type, file_id FROM buttons WHERE id = $1", bid)
                if not row:
                    if bot:
                        await safe_telegram_call(bot.send_message(chat_id, f"لا يوجد زر بالمعرف {bid}"))
                    admin_state.pop(user_id, None)
                    return

                # Ask for confirmation with inline buttons (Confirm / Cancel)
                confirm_cb = f"confirm_remove_file:{bid}"
                cancel_cb = f"cancel_remove_file:{bid}"
                keyboard = [
                    [
                        InlineKeyboardButton("تأكيد الإزالة ❌", callback_data=confirm_cb),
                        InlineKeyboardButton("إلغاء", callback_data=cancel_cb),
                    ]
                ]
                markup = InlineKeyboardMarkup(keyboard)
                info_text = f"هل أنت متأكد من إزالة الملف المرتبط بالزر '{row['name']}' (id={bid})؟\n"
                info_text += f"المحتوى الحالي: {row['content_type'] or 'لا يوجد'}\n"
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id, info_text, reply_markup=markup))

                # Save pending confirmation state for this admin
                admin_state[user_id] = {"action": "waiting_confirm_remove_file", "target_id": bid}
            except Exception as e:
                logger.error("Failed to prepare remove file confirmation: %s", e)
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id, "حدث خطأ أثناء الإجراء"))
                admin_state.pop(user_id, None)
            return

        elif action in ("awaiting_remove_file",):
            # backward compatibility: respond similarly to awaiting_remove_file
            try:
                bid = int(text.strip())
            except ValueError:
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id, "أرسل رقم معرف زر صحيح (رقم)."))
                return

            try:
                row = await db_fetchone("SELECT id, name FROM buttons WHERE id = $1", bid)
                if not row:
                    if bot:
                        await safe_telegram_call(bot.send_message(chat_id, f"لا يوجد زر بالمعرف {bid}"))
                    admin_state.pop(user_id, None)
                    return

                await db_execute(
                    "UPDATE buttons SET content_type = NULL, file_id = NULL WHERE id = $1", bid
                )

                if bot:
                    await safe_telegram_call(bot.send_message(
                        chat_id,
                        f"تمت إزالة الملف من الزر '{row['name']}' (id={bid})."
                    ))
                admin_state.pop(user_id, None)

            except Exception as e:
                logger.error("Failed to remove file: %s", e)
                if bot:
                    await safe_telegram_call(bot.send_message(chat_id, "حدث خطأ أثناء محاولة إزالة الملف"))
            return

    # Start command
    if text.strip().lower().startswith("/start"):
        ok, missing, reasons = await check_user_membership(user_id)
        if not ok:
            message = "✋ يلزم الانضمام إلى:\n" + "\n".join(f"- {c}" for c in missing) + "\n\nاضغط 'لقد انضممت — تحقق'"
            if bot:
                await safe_telegram_call(bot.send_message(chat_id, message, reply_markup=missing_chats_markup()))
            return

        # Save user and show menu
        try:
            await db_execute("INSERT INTO users (user_id, first_name) VALUES ($1,$2) ON CONFLICT DO NOTHING", 
                           user_id, from_user.get("first_name", ""))
        except Exception:
            pass  # Ignore user save errors

        markup = await build_main_menu()
        if bot and markup:
            await safe_telegram_call(bot.send_message(chat_id, "مرحباً! اختر القسم:", reply_markup=markup))

# ---------------- Webhook handler ----------------
@app.post("/webhook")
async def webhook(request: Request):
    acquired = False
    
    # Validate secret token
    if WEBHOOK_SECRET_TOKEN:
        token = request.headers.get("x-telegram-bot-api-secret-token")
        if token != WEBHOOK_SECRET_TOKEN:
            raise HTTPException(403, "Invalid token")

    # Acquire semaphore with timeout
    try:
        await asyncio.wait_for(PROCESSING_SEMAPHORE.acquire(), timeout=PROCESSING_SEMAPHORE_TIMEOUT)
        acquired = True
    except asyncio.TimeoutError:
        logger.warning("Server busy, rejecting request")
        return JSONResponse({"ok": False, "error": "busy"}, status_code=429)

    try:
        # Parse update
        update = await request.json()
        update_id = update.get("update_id")
        
        if update_id and update_id in PROCESSED_UPDATES:
            return JSONResponse({"ok": True})
        PROCESSED_UPDATES.append(update_id)

        # Clean memory periodically
        if len(PROCESSED_UPDATES) % 100 == 0:
            cleanup_memory()

        # Handle callback queries
        if "callback_query" in update:
            cq = update["callback_query"]
            data = cq.get("data")
            user_id = cq.get("from", {}).get("id")
            message = cq.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")

            # Answer callback first
            if bot and cq.get("id"):
                await safe_telegram_call(bot.answer_callback_query(cq["id"]))

            # Handle different callback actions
            if data == "check_membership":
                ok, missing, _ = await check_user_membership(user_id)
                if ok:
                    markup = await build_main_menu()
                    if bot and chat_id and message_id:
                        await safe_telegram_call(bot.edit_message_text(
                            chat_id, message_id, "تم التحقق — اختر القسم:", reply_markup=markup))
                else:
                    if bot and chat_id and message_id:
                        await safe_telegram_call(bot.edit_message_text(
                            chat_id, message_id, "لا زلت تحتاج للانضمام", reply_markup=missing_chats_markup()))

            elif data == "admin_panel" and user_id in ADMIN_IDS:
                if bot and chat_id and message_id:
                    await safe_telegram_call(bot.edit_message_text(
                        chat_id, message_id, "لوحة التحكم:", reply_markup=admin_panel_markup()))

            elif data == "back_to_main":
                markup = await build_main_menu()
                if bot and chat_id and message_id:
                    await safe_telegram_call(bot.edit_message_text(
                        chat_id, message_id, "اختر القسم:", reply_markup=markup))

            elif data in ["admin_add_button", "admin_remove_button", "admin_upload_to_button", "admin_remove_file"]:
                # NOTE: admin_remove_file is included here to set the admin state and prompt for data
                if user_id in ADMIN_IDS:
                    admin_state[user_id] = {"action": data}
                    if bot and chat_id:
                        # Use a single prompt message for all these actions; process_text_message will handle specifics
                        await safe_telegram_call(bot.send_message(chat_id, "أرسل البيانات المطلوبة"))

            elif data == "admin_list_buttons" and user_id in ADMIN_IDS:
                try:
                    rows = await db_fetchall("SELECT id, name, callback_data FROM buttons ORDER BY id")
                    text = "\n".join(f"{r['id']}: {r['name']} ({r['callback_data']})" for r in rows)
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id, text or "لا توجد أزرار"))
                except Exception as e:
                    logger.error("Failed to list buttons: %s", e)

            # NEW: handle confirmation callbacks for removing file
            elif data and data.startswith("confirm_remove_file:"):
                try:
                    bid_str = data.split(":", 1)[1]
                    bid = int(bid_str)
                except Exception:
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id, "معلومات غير صحيحة."))
                    return JSONResponse({"ok": True})

                if user_id not in ADMIN_IDS:
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id, "ليس لديك صلاحية للقيام بهذه العملية."))
                    return JSONResponse({"ok": True})

                state = admin_state.get(user_id)
                if not state or state.get("action") != "waiting_confirm_remove_file" or state.get("target_id") != bid:
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id, "لا توجد عملية معلقة أو أنها انتهت."))
                    admin_state.pop(user_id, None)
                    return JSONResponse({"ok": True})

                try:
                    row = await db_fetchone("SELECT id, name FROM buttons WHERE id = $1", bid)
                    if not row:
                        if bot and chat_id:
                            await safe_telegram_call(bot.send_message(chat_id, f"لا يوجد زر بالمعرف {bid}"))
                        admin_state.pop(user_id, None)
                        return JSONResponse({"ok": True})

                    await db_execute("UPDATE buttons SET content_type = NULL, file_id = NULL WHERE id = $1", bid)
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id, f"تمت إزالة الملف من الزر '{row['name']}' (id={bid})."))
                    admin_state.pop(user_id, None)
                except Exception as e:
                    logger.error("Failed to remove file on confirm: %s", e)
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id, "حدث خطأ أثناء إزالة الملف"))
                    admin_state.pop(user_id, None)

            elif data and data.startswith("cancel_remove_file:"):
                try:
                    bid_str = data.split(":", 1)[1]
                    bid = int(bid_str)
                except Exception:
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id, "معلومات غير صحيحة."))
                    return JSONResponse({"ok": True})

                if user_id not in ADMIN_IDS:
                    if bot and chat_id:
                        await safe_telegram_call(bot.send_message(chat_id, "ليس لديك صلاحية للقيام بهذه العملية."))
                    return JSONResponse({"ok": True})

                state = admin_state.get(user_id)
                # Accept cancel even if state mismatches to make UX smoother
                admin_state.pop(user_id, None)
                if bot and chat_id:
                    await safe_telegram_call(bot.send_message(chat_id, f"تم إلغاء عملية إزالة الملف للزر id={bid}."))

            else:
                # Regular button handling
                row = await db_fetchone("SELECT content_type, file_id FROM buttons WHERE callback_data = $1", data)
                if row and row["content_type"] and row["file_id"]:
                    ctype, fid = row["content_type"], row["file_id"]
                    if bot and chat_id:
                        if ctype == "document":
                            await safe_telegram_call(bot.send_document(chat_id, fid))
                        elif ctype == "photo":
                            await safe_telegram_call(bot.send_photo(chat_id, fid))
                        elif ctype == "video":
                            await safe_telegram_call(bot.send_video(chat_id, fid))
                        else:
                            await safe_telegram_call(bot.send_message(chat_id, str(fid)))
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
                                    chat_id, message_id, "اختر:", reply_markup=markup))
                        else:
                            if bot and chat_id:
                                await safe_telegram_call(bot.send_message(chat_id, "لا محتوى"))

        # Handle messages
        elif "message" in update:
            await process_text_message(update["message"])
        elif "edited_message" in update:
            await process_text_message(update["edited_message"])

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error("Webhook error: %s", e)
        return JSONResponse({"ok": False, "error": "internal"}, status_code=500)
        
    finally:
        if acquired:
            try:
                PROCESSING_SEMAPHORE.release()
            except ValueError:
                logger.warning("Semaphore release error")

# ---------------- Startup/shutdown ----------------
@app.on_event("startup")
async def on_startup():
    global bot, BOT_ID
    logger.info("Starting up...")
    
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Missing required environment variables")
        return

    try:
        await init_pg_pool()
        await init_db_schema_and_defaults()
        
        bot = Bot(token=BOT_TOKEN)
        me = await safe_telegram_call(bot.get_me())
        BOT_ID = me.id
        logger.info("Bot initialized: %s", me.username)
        
        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            if WEBHOOK_SECRET_TOKEN:
                await safe_telegram_call(bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET_TOKEN))
            else:
                await safe_telegram_call(bot.set_webhook(webhook_url))
            logger.info("Webhook set: %s", webhook_url)
            
    except Exception as e:
        logger.error("Startup failed: %s", e)
        bot = None

@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Shutting down...")
    if bot and WEBHOOK_URL:
        try:
            await safe_telegram_call(bot.delete_webhook())
        except Exception:
            pass
    if pg_pool:
        await pg_pool.close()

# ---------------- Main ----------------
def main():
    logger.info("Starting server on port %s", PORT)
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=PORT, 
        log_level="info",
        # Limit workers for stability
        workers=1,
        # Limit request size
        max_requests=1000,
        max_requests_jitter=100
    )

if __name__ == "__main__":
    main()
