# bot.py
import os
import time
import asyncio
import logging
from collections import deque
from typing import Optional, Dict, Any, List, Tuple, Callable
from urllib.parse import urlparse

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# -------------------- Config & Logging --------------------
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
ADMIN_IDS_ENV = os.environ.get("ADMIN_IDS", "")
try:
    ADMIN_IDS: List[int] = [int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []

REQUIRED_CHATS_RAW = os.environ.get("REQUIRED_CHATS", "")
REQUIRED_CHATS: List[str] = [c.strip() for c in REQUIRED_CHATS_RAW.split(",") if c.strip()]

PORT = int(os.environ.get("PORT", 10000))
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", 1))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 10))

# -------------------- Globals --------------------
app = FastAPI()
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None

# DB pool (Threaded) to avoid blocking event loop
db_pool: Optional[ThreadedConnectionPool] = None

# In-memory small structures
PROCESSED_UPDATES = deque(maxlen=5000)
admin_state: Dict[int, Dict[str, Any]] = {}  # admin flows in-memory

# -------------------- DB helpers (threaded) --------------------
async def init_db_pool():
    global db_pool
    if db_pool:
        return
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set; cannot initialize DB pool")
        return
    def create_pool():
        result = urlparse(DATABASE_URL)
        return ThreadedConnectionPool(
            DB_POOL_MIN,
            DB_POOL_MAX,
            database=result.path[1:],
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port,
        )
    logger.info("Creating DB pool min=%s max=%s", DB_POOL_MIN, DB_POOL_MAX)
    db_pool = await asyncio.to_thread(create_pool)
    logger.info("DB pool created")

async def run_db(fn: Callable):
    """
    Run provided function with a cursor in a thread.
    The provided function must accept a single argument: cur (psycopg2 cursor).
    The function may commit via cur.connection.commit() if needed.
    """
    if not db_pool:
        await init_db_pool()
    if not db_pool:
        raise RuntimeError("DB pool is not available")
    def thread_fn():
        conn = db_pool.getconn()
        try:
            cur = conn.cursor()
            try:
                return fn(cur)
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        finally:
            db_pool.putconn(conn)
    return await asyncio.to_thread(thread_fn)

async def db_fetchall(query: str, params: tuple = ()):
    def fn(cur):
        cur.execute(query, params)
        return cur.fetchall()
    return await run_db(fn)

async def db_fetchone(query: str, params: tuple = ()):
    def fn(cur):
        cur.execute(query, params)
        return cur.fetchone()
    return await run_db(fn)

async def db_execute(query: str, params: tuple = (), commit: bool = True) -> int:
    def fn(cur):
        cur.execute(query, params)
        if commit:
            cur.connection.commit()
        return cur.rowcount
    return await run_db(fn)

# -------------------- Initialization (tables + defaults) --------------------
async def init_db_schema_and_defaults():
    # Create tables and insert default top-level buttons
    logger.info("Initializing DB schema and default rows")
    create_buttons = """
    CREATE TABLE IF NOT EXISTS buttons (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        callback_data TEXT UNIQUE NOT NULL,
        parent_id INTEGER DEFAULT 0,
        content_type TEXT,
        file_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    create_users = """
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        class_type TEXT,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    await db_execute(create_buttons, (), commit=True)
    await db_execute(create_users, (), commit=True)
    # default buttons
    defaults = [
        ("العلمي", "science", 0, None, None),
        ("الأدبي", "literary", 0, None, None),
        ("الإدارة", "admin_panel", 0, None, None),
    ]
    for name, cb, parent, ctype, fid in defaults:
        await db_execute(
            "INSERT INTO buttons (name, callback_data, parent_id, content_type, file_id) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (callback_data) DO NOTHING",
            (name, cb, parent, ctype, fid),
            commit=True,
        )
    logger.info("DB schema and defaults ensured")

# -------------------- UI builders --------------------
def make_main_markup_from_rows(rows: List[tuple]) -> Optional[InlineKeyboardMarkup]:
    if not rows:
        return None
    keyboard = [[InlineKeyboardButton(name, callback_data=cb)] for name, cb in rows]
    return InlineKeyboardMarkup(keyboard)

async def build_main_menu() -> Optional[InlineKeyboardMarkup]:
    logger.debug("Fetching main menu buttons from DB")
    try:
        rows = await db_fetchall("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id;")
        logger.debug("Main menu rows=%s", rows)
        return make_main_markup_from_rows(rows)
    except Exception as e:
        logger.exception("Failed to build main menu: %s", e)
        return None

def admin_panel_markup() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("إضافة زر جديد", callback_data="admin_add_button")],
        [InlineKeyboardButton("حذف زر", callback_data="admin_remove_button")],
        [InlineKeyboardButton("رفع ملف لزر موجود", callback_data="admin_upload_to_button")],
        [InlineKeyboardButton("عرض جميع الأزرار", callback_data="admin_list_buttons")],
        [InlineKeyboardButton("العودة", callback_data="back_to_main")],
    ]
    return InlineKeyboardMarkup(keyboard)

def missing_chats_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("لقد انضممت — تحقق", callback_data="check_membership")]])

# -------------------- Membership check --------------------
async def check_user_membership(user_id: int) -> Tuple[bool, List[str], Dict[str, str]]:
    """
    Returns (ok, missing_list, reasons)
    reasons are codes: ok, bot_must_be_admin, bot_cannot_access_members, chat_not_found, user_not_member, bot_not_initialized, unknown_error
    """
    missing: List[str] = []
    reasons: Dict[str, str] = {}
    logger.debug("Checking membership for user=%s REQUIRED_CHATS=%s", user_id, REQUIRED_CHATS)
    if not REQUIRED_CHATS:
        logger.debug("No required chats configured -> membership ok")
        return True, missing, reasons
    if not bot:
        logger.warning("Bot not initialized; cannot check membership")
        for c in REQUIRED_CHATS:
            missing.append(c)
            reasons[c] = "bot_not_initialized"
        return False, missing, reasons

    for chat_ref in REQUIRED_CHATS:
        logger.debug("Checking chat %s for user %s", chat_ref, user_id)
        try:
            member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
            status = getattr(member, "status", None)
            logger.info("get_chat_member(%s,%s) -> status=%s", chat_ref, user_id, status)
            if status in ("member", "administrator", "creator"):
                reasons[chat_ref] = "ok"
                continue
            else:
                missing.append(chat_ref)
                reasons[chat_ref] = "user_not_member"
        except Exception as e:
            txt = str(e)
            logger.warning("get_chat_member failed for %s user=%s: %s", chat_ref, user_id, txt)
            missing.append(chat_ref)
            if "Chat_admin_required" in txt:
                reasons[chat_ref] = "bot_must_be_admin"
            elif "Member list is inaccessible" in txt or "not enough rights" in txt:
                reasons[chat_ref] = "bot_cannot_access_members"
            elif "chat not found" in txt.lower():
                reasons[chat_ref] = "chat_not_found"
            else:
                reasons[chat_ref] = "unknown_error"
    ok = len(missing) == 0
    logger.debug("Membership check result user=%s ok=%s missing=%s reasons=%s", user_id, ok, missing, reasons)
    return ok, missing, reasons

# -------------------- Core message processing --------------------
async def process_text_message(msg: dict):
    """
    Handle incoming message text and admin flows.
    """
    text = msg.get("text") if isinstance(msg.get("text"), str) else None
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    from_user = msg.get("from", {}) or {}
    user_id = from_user.get("id")

    logger.info("process_text_message user=%s chat=%s text=%s", user_id, chat_id, text)

    # ignore bots
    if isinstance(from_user, dict) and from_user.get("is_bot"):
        logger.debug("Ignoring message from bot account")
        return
    if BOT_ID is not None and isinstance(from_user, dict) and from_user.get("id") == BOT_ID:
        logger.debug("Ignoring message from this bot itself")
        return

    # admin flows in-memory (add/remove/upload)
    if user_id in admin_state:
        state = admin_state[user_id]
        action = state.get("action")
        logger.info("User %s in admin flow action=%s", user_id, action)

        # add button: expects "name|parent_id"
        if action == "awaiting_add":
            logger.debug("Admin %s submitting add data: %s", user_id, text)
            if not text or "|" not in text:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="خطأ في الصيغة. استخدم: اسم الزر|رقم الأب (0 للقائمة الرئيسية)")
                return
            name_part, parent_part = text.split("|", 1)
            name = name_part.strip()
            try:
                parent_id = int(parent_part.strip())
            except ValueError:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="رقم الأب يجب أن يكون عدداً صحيحاً.")
                return
            callback_data = f"btn_{int(time.time())}_{name.replace(' ', '_')}"
            try:
                rowcount = await db_execute(
                    "INSERT INTO buttons (name, callback_data, parent_id) VALUES (%s, %s, %s)",
                    (name, callback_data, parent_id),
                    commit=True
                )
                logger.info("Admin %s added button name=%s callback=%s parent=%s rowcount=%s", user_id, name, callback_data, parent_id, rowcount)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text=f"تم إضافة الزر '{name}' بنجاح! (الرمز: {callback_data})")
            except Exception as e:
                logger.exception("Error adding button: %s", e)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء إضافة الزر.")
            admin_state.pop(user_id, None)
            return

        # remove button: expects numeric id
        if action == "awaiting_remove":
            logger.debug("Admin %s removing id_text=%s", user_id, text)
            if not text:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر المراد حذفه.")
                return
            try:
                bid = int(text.strip())
            except ValueError:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="يرجى إرسال رقم صحيح.")
                return
            try:
                affected = await db_execute("DELETE FROM buttons WHERE id = %s", (bid,), commit=True)
                logger.info("Admin %s deleted button id=%s affected=%s", user_id, bid, affected)
                if bot and chat_id:
                    if affected > 0:
                        await bot.send_message(chat_id=chat_id, text=f"تم حذف الزر بالمعرف {bid}.")
                    else:
                        await bot.send_message(chat_id=chat_id, text="لم يتم العثور على الزر المحدد.")
            except Exception as e:
                logger.exception("Error removing button: %s", e)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء حذف الزر.")
            admin_state.pop(user_id, None)
            return

        # awaiting upload target id
        if action == "awaiting_upload_button_id":
            logger.debug("Admin %s upload target id text=%s", user_id, text)
            if not text:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد رفع الملف له.")
                return
            try:
                bid = int(text.strip())
            except ValueError:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="يرجى إرسال رقم صحيح.")
                return
            admin_state[user_id] = {"action": "awaiting_upload_file", "target_button_id": bid}
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="الآن أرسل الملف (مستند/صورة/فيديو) أو نص لربطه بالزر.")
            return

        # awaiting upload file
        if action == "awaiting_upload_file":
            logger.debug("Admin %s upload file state=%s", user_id, state)
            target_bid = state.get("target_button_id")
            if not target_bid:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لم يتم تحديد الزر الهدف. أعد العملية.")
                admin_state.pop(user_id, None)
                return
            file_id = None
            content_type = None
            # note: this function receives parsed update message dict; if it's coming from webhook,
            # the media fields are present (document, photo, video)
            if msg.get("document"):
                file_id = msg["document"].get("file_id")
                content_type = "document"
            elif msg.get("photo"):
                file_id = msg["photo"][-1].get("file_id")
                content_type = "photo"
            elif msg.get("video"):
                file_id = msg["video"].get("file_id")
                content_type = "video"
            elif text:
                file_id = text.strip()
                content_type = "text"
            if not file_id:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لم يتم العثور على ملف في هذه الرسالة. أرسل ملفاً أو نصاً.")
                return
            try:
                affected = await db_execute("UPDATE buttons SET content_type=%s, file_id=%s WHERE id=%s",
                                            (content_type, file_id, target_bid), commit=True)
                logger.info("Admin %s updated button id=%s content=%s affected=%s", user_id, target_bid, content_type, affected)
                if bot and chat_id:
                    if affected > 0:
                        await bot.send_message(chat_id=chat_id, text="تم ربط الملف بالزر بنجاح!")
                    else:
                        await bot.send_message(chat_id=chat_id, text="لم يتم العثور على الزر المحدد.")
            except Exception as e:
                logger.exception("Error updating button content: %s", e)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء ربط الملف.")
            admin_state.pop(user_id, None)
            return

    # Not an admin flow -> handle /start (with membership check)
    if isinstance(text, str) and text.strip().lower().startswith("/start"):
        logger.info("/start from user=%s chat=%s", user_id, chat_id)
        ok, missing, reasons = await check_user_membership(user_id)
        if not ok:
            logger.info("User %s missing required chats=%s reasons=%s", user_id, missing, reasons)
            lines = ["✋ قبل استخدام البوت، يلزم الانضمام إلى القنوات/المجموعة التالية:"]
            for c in missing:
                r = reasons.get(c, "")
                if r == "bot_must_be_admin":
                    lines.append(f"- {c} — يجب إضافة البوت كمشرف (admin) في هذه القناة.")
                elif r == "bot_cannot_access_members":
                    lines.append(f"- {c} — البوت لا يستطيع الوصول إلى قائمة الأعضاء (تأكد أنه عضو/مشرف).")
                elif r == "chat_not_found":
                    lines.append(f"- {c} — لم يتم العثور على القناة/المجموعة. تحقق من الاسم أو id.")
                elif r == "user_not_member":
                    lines.append(f"- {c} — لم تنضم بعد إلى هذه القناة/المجموعة.")
                else:
                    lines.append(f"- {c} — خطأ: {r}")
            lines.append("\nبعد الانضمام اضغط: 'لقد انضممت — تحقق'")
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=missing_chats_markup())
                    logger.debug("Sent missing-chats instructions to user=%s", user_id)
            except Exception as e:
                logger.exception("Failed to send missing-chats message: %s", e)
            return

        # Save user to DB and show menu
        try:
            await db_execute("INSERT INTO users (user_id, first_name) VALUES (%s,%s) ON CONFLICT (user_id) DO NOTHING",
                             (user_id, from_user.get("first_name", "")), commit=True)
            logger.debug("Saved user %s to DB", user_id)
        except Exception as e:
            logger.exception("Failed to save user: %s", e)

        try:
            markup = await build_main_menu()
            if bot and chat_id:
                if markup:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=markup)
                    logger.info("Sent main menu to user=%s", user_id)
                else:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! لا توجد أقسام متاحة حالياً.")
                    logger.info("Sent no-sections message to user=%s", user_id)
        except Exception as e:
            logger.exception("Failed to send /start reply: %s", e)
        return

# -------------------- Webhook endpoint --------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"

@app.post("/webhook")
async def webhook(request: Request):
    # Accept header case-insensitively
    headers = {k.lower(): v for k, v in request.headers.items()}
    if WEBHOOK_SECRET_TOKEN:
        header_val = headers.get("x-telegram-bot-api-secret-token")
        if header_val != WEBHOOK_SECRET_TOKEN:
            logger.warning("Invalid secret token in incoming webhook request (got=%s)", header_val)
            raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        update = await request.json()
    except Exception as e:
        logger.exception("Failed to parse JSON from webhook: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.debug("Incoming update JSON: %s", update)
    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in PROCESSED_UPDATES:
            logger.debug("Duplicate update id=%s -> skipping", update_id)
            return JSONResponse({"ok": True})
        PROCESSED_UPDATES.append(update_id)

    logger.info("Incoming update id=%s keys=%s", update.get("update_id"), list(update.keys()))

    # Ignore updates from other bots or ourselves
    if "message" in update and isinstance(update["message"], dict):
        frm = update["message"].get("from", {})
        if isinstance(frm, dict) and frm.get("is_bot"):
            logger.debug("Ignoring message from a bot account")
            return JSONResponse({"ok": True})
        if BOT_ID is not None and isinstance(frm, dict) and frm.get("id") == BOT_ID:
            logger.debug("Ignoring message from our own bot id")
            return JSONResponse({"ok": True})

    if "edited_message" in update and isinstance(update["edited_message"], dict):
        frm = update["edited_message"].get("from", {})
        if isinstance(frm, dict) and frm.get("is_bot"):
            logger.debug("Ignoring edited_message from bot account")
            return JSONResponse({"ok": True})
        if BOT_ID is not None and isinstance(frm, dict) and frm.get("id") == BOT_ID:
            logger.debug("Ignoring edited_message from our own bot id")
            return JSONResponse({"ok": True})

    # ---------------- callback_query handling ----------------
    if "callback_query" in update and isinstance(update["callback_query"], dict):
        cq = update["callback_query"]
        cq_id = cq.get("id")
        data = cq.get("data")
        user = cq.get("from", {}) or {}
        user_id = user.get("id")
        message = cq.get("message") or {}
        chat = message.get("chat", {}) or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        logger.info("Callback query: user=%s data=%s chat=%s message_id=%s", user_id, data, chat_id, message_id)

        # answer callback to stop spinner ASAP
        try:
            if bot and cq_id:
                await bot.answer_callback_query(callback_query_id=cq_id)
                logger.debug("Answered callback_query id=%s", cq_id)
        except Exception as e:
            logger.exception("answerCallbackQuery failed: %s", e)

        # membership re-check button
        if data == "check_membership":
            logger.info("User %s clicked check_membership", user_id)
            ok, missing, reasons = await check_user_membership(user_id)
            logger.info("check_membership result ok=%s missing=%s reasons=%s", ok, missing, reasons)
            if ok:
                try:
                    markup = await build_main_menu()
                    if bot and chat_id and message_id:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                                    text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:",
                                                    reply_markup=markup)
                        logger.debug("Edited callback message to show main menu")
                    elif bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:", reply_markup=markup)
                        logger.debug("Sent membership success message")
                except Exception as e:
                    logger.exception("Failed to show main menu after membership ok: %s", e)
                    try:
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:", reply_markup=markup)
                    except Exception:
                        logger.exception("Fallback send_message failed")
            else:
                lines = ["✋ يلزم الانضمام أو إصلاح صلاحيات البوت في التالي:"]
                for c in missing:
                    r = reasons.get(c, "")
                    if r == "bot_must_be_admin":
                        lines.append(f"- {c} — يجب إضافة البوت كمشرف (admin) في هذه القناة.")
                    elif r == "bot_cannot_access_members":
                        lines.append(f"- {c} — البوت لا يستطيع الوصول إلى قائمة الأعضاء (تأكد أنه عضو/مشرف).")
                    elif r == "chat_not_found":
                        lines.append(f"- {c} — لم يتم العثور على القناة/المجموعة. تحقق من الاسم أو id.")
                    elif r == "user_not_member":
                        lines.append(f"- {c} — لم تنضم بعد إلى هذه القناة/المجموعة.")
                    else:
                        lines.append(f"- {c} — خطأ: {r}")
                lines.append("\nبعد التصحيح اضغط: 'لقد انضممت — تحقق' أو انتظر بضع ثوانٍ ثم حاول مرة أخرى.")
                try:
                    if bot and chat_id and message_id:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="\n".join(lines),
                                                    reply_markup=missing_chats_markup())
                        logger.debug("Edited message to show missing reasons")
                    elif bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=missing_chats_markup())
                        logger.debug("Sent missing reasons message")
                except Exception as e:
                    logger.exception("Failed to send missing reasons message: %s", e)
            return JSONResponse({"ok": True})

        # admin panel entry
        if data == "admin_panel":
            logger.info("User %s requested admin_panel", user_id)
            if not (user_id in ADMIN_IDS):
                try:
                    if bot and cq_id:
                        await bot.answer_callback_query(callback_query_id=cq_id, text="ليس لديك صلاحية للوصول إلى هذه الصفحة.")
                except Exception:
                    logger.exception("Failed to answer unauthorized admin_panel")
                return JSONResponse({"ok": True})
            try:
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                logger.debug("Displayed admin panel to admin %s", user_id)
            except Exception as e:
                logger.exception("Failed to show admin panel: %s", e)
            return JSONResponse({"ok": True})

        # admin actions
        if data == "admin_add_button":
            if user_id in ADMIN_IDS:
                admin_state[user_id] = {"action": "awaiting_add"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل اسم الزر الجديد ورقم الزر الأب بالصيغة:\nاسم الزر|رقم الأب (0 للقائمة الرئيسية)")
                except Exception:
                    logger.exception("Failed to send admin add instruction")
            return JSONResponse({"ok": True})

        if data == "admin_remove_button":
            if user_id in ADMIN_IDS:
                admin_state[user_id] = {"action": "awaiting_remove"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد حذفه (انظر 'عرض جميع الأزرار').")
                except Exception:
                    logger.exception("Failed to send admin remove instruction")
            return JSONResponse({"ok": True})

        if data == "admin_upload_to_button":
            if user_id in ADMIN_IDS:
                admin_state[user_id] = {"action": "awaiting_upload_button_id"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد رفع الملف له ثم أرسل الملف بعد ذلك.")
                except Exception:
                    logger.exception("Failed to send admin upload instruction")
            return JSONResponse({"ok": True})

        if data == "admin_list_buttons":
            if user_id in ADMIN_IDS:
                try:
                    rows = await db_fetchall("SELECT id, name, callback_data, parent_id FROM buttons ORDER BY id;")
                    lines = ["جميع الأزرار:"]
                    for r in rows:
                        lines.append(f"{r[0]}: {r[1]} (رمز: {r[2]}, أب: {r[3]})")
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="\n".join(lines))
                except Exception as e:
                    logger.exception("admin_list_buttons error: %s", e)
                    try:
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء جلب الأزرار.")
                    except Exception:
                        logger.exception("Failed to notify admin of error")
            return JSONResponse({"ok": True})

        if data == "back_to_main":
            try:
                markup = await build_main_menu()
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="مرحباً! اختر القسم المناسب:", reply_markup=markup)
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=markup)
                logger.debug("Back to main for user %s", user_id)
            except Exception as e:
                logger.exception("back_to_main error: %s", e)
            return JSONResponse({"ok": True})

        # Regular button callback handling: check DB for content or submenu
        try:
            row = await db_fetchone("SELECT content_type, file_id, id FROM buttons WHERE callback_data = %s", (data,))
            logger.debug("Button lookup for data=%s -> %s", data, row)
        except Exception as e:
            logger.exception("DB error while fetching button: %s", e)
            row = None

        if row and row[0] and row[1]:
            ctype, fid, _id = row
            try:
                if ctype == "document":
                    await bot.send_document(chat_id=chat_id, document=fid)
                elif ctype == "photo":
                    await bot.send_photo(chat_id=chat_id, photo=fid)
                elif ctype == "video":
                    await bot.send_video(chat_id=chat_id, video=fid)
                else:
                    await bot.send_message(chat_id=chat_id, text=str(fid))
            except Exception as e:
                logger.exception("Error sending button content: %s", e)
            return JSONResponse({"ok": True})

        # show submenu if any
        try:
            parent_row = await db_fetchone("SELECT id FROM buttons WHERE callback_data = %s", (data,))
            if not parent_row:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="الزر غير موجود.")
                logger.info("Callback data not found in DB: %s", data)
                return JSONResponse({"ok": True})
            parent_id = parent_row[0]
            subs = await db_fetchall("SELECT name, callback_data FROM buttons WHERE parent_id = %s", (parent_id,))
            if subs:
                keyboard = [[InlineKeyboardButton(name, callback_data=cb)] for name, cb in subs]
                keyboard.append([InlineKeyboardButton("العودة", callback_data="back_to_main")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                try:
                    if bot and chat_id and message_id:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="اختر من القائمة:", reply_markup=reply_markup)
                    elif bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="اختر من القائمة:", reply_markup=reply_markup)
                except Exception as e:
                    logger.exception("Failed to show submenu: %s", e)
            else:
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="هذه القائمة لا تحتوي على محتوى بعد.")
                except Exception:
                    logger.exception("Failed to notify empty submenu")
        except Exception as e:
            logger.exception("DB error fetching submenu for callback %s: %s", data, e)
        return JSONResponse({"ok": True})

    # ---------------- message / edited_message handling ----------------
    if "message" in update and isinstance(update["message"], dict):
        try:
            await process_text_message(update["message"])
        except Exception as e:
            logger.exception("Unhandled exception in process_text_message: %s", e)
            # notify admins
            if bot and ADMIN_IDS:
                try:
                    for admin in ADMIN_IDS:
                        await bot.send_message(chat_id=admin, text=f"Error processing message update: {e}")
                except Exception:
                    logger.exception("Failed to notify admins")
        return JSONResponse({"ok": True})

    if "edited_message" in update and isinstance(update["edited_message"], dict):
        try:
            await process_text_message(update["edited_message"])
        except Exception as e:
            logger.exception("Unhandled exception in process_text_message (edited): %s", e)
        return JSONResponse({"ok": True})

    logger.debug("No actionable update fields; acking")
    return JSONResponse({"ok": True})

# -------------------- Startup / Shutdown --------------------
@app.on_event("startup")
async def on_startup():
    global bot, BOT_ID
    logger.info("Startup: initializing DB pool and bot")
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Missing BOT_TOKEN or DATABASE_URL; bot will not be fully initialized")
        return

    # Create DB pool and schema
    try:
        await init_db_pool()
        await init_db_schema_and_defaults()
    except Exception as e:
        logger.exception("Failed to init DB pool or schema: %s", e)

    # Init bot
    try:
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        BOT_ID = getattr(me, "id", None)
        logger.info("Bot ready id=%s username=%s", BOT_ID, getattr(me, "username", None))
    except Exception as e:
        logger.exception("Failed to initialize Bot: %s", e)
        bot = None
        BOT_ID = None

    # register webhook if URL provided
    if WEBHOOK_URL and bot:
        webhook_target = WEBHOOK_URL.rstrip("/") + "/webhook"
        try:
            if WEBHOOK_SECRET_TOKEN:
                res = await bot.set_webhook(url=webhook_target, secret_token=WEBHOOK_SECRET_TOKEN)
            else:
                res = await bot.set_webhook(url=webhook_target)
            logger.info("set_webhook result: %s", res)
            try:
                info = await bot.get_webhook_info()
                logger.info("webhook_info: %s", info)
            except Exception:
                logger.exception("Failed to get webhook_info")
        except Exception:
            logger.exception("Failed to set webhook")
    else:
        if not WEBHOOK_URL:
            logger.warning("WEBHOOK_URL not set; webhook won't be auto-registered")
        else:
            logger.warning("Bot not initialized; cannot register webhook")

@app.on_event("shutdown")
async def on_shutdown():
    global bot, db_pool
    logger.info("Shutdown: cleaning up")
    if bot and WEBHOOK_URL:
        try:
            await bot.delete_webhook()
            logger.info("Webhook deleted")
        except Exception:
            logger.exception("Failed to delete webhook")
    if db_pool:
        try:
            db_pool.closeall()
            logger.info("Closed DB pool")
        except Exception:
            logger.exception("Failed to close DB pool")

# -------------------- Entrypoint --------------------
def main():
    logger.info("Starting uvicorn on 0.0.0.0:%s", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

if __name__ == "__main__":
    main()
