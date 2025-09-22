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
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", 10))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 80))
PROCESSING_SEMAPHORE_TIMEOUT = float(os.environ.get("PROCESSING_SEMAPHORE_TIMEOUT", 2.0))

# ---------------- Globals ----------------
app = FastAPI()
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None
pg_pool: Optional[asyncpg.Pool] = None

PROCESSED_UPDATES = deque(maxlen=5000)
admin_state: Dict[int, Dict[str, Any]] = {}

PROCESSING_SEMAPHORE = asyncio.BoundedSemaphore(MAX_CONCURRENT)

# ---------------- DB helpers (asyncpg) ----------------
async def init_pg_pool():
    global pg_pool
    if pg_pool:
        return
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set, cannot create pool")
        return
    logger.info("Creating asyncpg pool max_size=%s", DB_POOL_MAX)
    pg_pool = await asyncpg.create_pool(dsn=DATABASE_URL, max_size=DB_POOL_MAX)
    logger.info("Postgres pool created")

async def db_fetchall(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    async with pg_pool.acquire() as conn:
        return await conn.fetch(query, *params)

async def db_fetchone(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(query, *params)

async def db_execute(query: str, *params):
    if not pg_pool:
        raise RuntimeError("DB pool not initialized")
    async with pg_pool.acquire() as conn:
        return await conn.execute(query, *params)

# ---------------- Init schema and defaults ----------------
async def init_db_schema_and_defaults():
    logger.info("Ensuring DB schema + default buttons")
    # create tables
    await db_execute(
        """
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
    )
    await db_execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            class_type TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    defaults = [
        ("العلمي", "science", 0, None, None),
        ("الأدبي", "literary", 0, None, None),
        ("الإدارة", "admin_panel", 0, None, None),
    ]
    for name, cb, parent, ctype, fid in defaults:
        await db_execute(
            "INSERT INTO buttons (name, callback_data, parent_id, content_type, file_id) VALUES ($1,$2,$3,$4,$5) ON CONFLICT (callback_data) DO NOTHING",
            name, cb, parent, ctype, fid
        )
    logger.info("DB schema + defaults ready")

# ---------------- UI builders ----------------
def rows_to_markup(rows) -> Optional[InlineKeyboardMarkup]:
    if not rows:
        return None
    keyboard = [[InlineKeyboardButton(r["name"], callback_data=r["callback_data"])] for r in rows]
    return InlineKeyboardMarkup(keyboard)

async def build_main_menu():
    try:
        rows = await db_fetchall("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id")
        return rows_to_markup(rows)
    except Exception as e:
        logger.exception("Failed to build main menu: %s", e)
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
    missing = []
    reasons = {}
    logger.debug("Membership check user=%s REQUIRED_CHATS=%s", user_id, REQUIRED_CHATS)
    if not REQUIRED_CHATS:
        return True, missing, reasons
    if not bot:
        for ch in REQUIRED_CHATS:
            missing.append(ch)
            reasons[ch] = "bot_not_initialized"
        return False, missing, reasons
    for chat_ref in REQUIRED_CHATS:
        try:
            member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
            status = getattr(member, "status", None)
            logger.info("get_chat_member(%s,%s) -> %s", chat_ref, user_id, status)
            if status in ("member", "administrator", "creator"):
                reasons[chat_ref] = "ok"
            else:
                missing.append(chat_ref); reasons[chat_ref] = "user_not_member"
        except Exception as e:
            txt = str(e)
            logger.warning("getChatMember failed for %s user=%s: %s", chat_ref, user_id, txt)
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
    return ok, missing, reasons

# ---------------- Core: process text message (admin flows + /start) ----------------
async def process_text_message(msg: dict):
    text = msg.get("text") if isinstance(msg.get("text"), str) else None
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    from_user = msg.get("from", {}) or {}
    user_id = from_user.get("id")

    logger.info("Processing text msg user=%s chat=%s text=%s", user_id, chat_id, text)

    if isinstance(from_user, dict) and from_user.get("is_bot"):
        logger.debug("Ignore bot message")
        return
    if BOT_ID is not None and isinstance(from_user, dict) and from_user.get("id") == BOT_ID:
        logger.debug("Ignore own bot message")
        return

    # ADMIN flows stored in memory
    if user_id in admin_state:
        state = admin_state[user_id]
        action = state.get("action")
        logger.info("Admin user %s in action=%s", user_id, action)

        # add
        if action == "awaiting_add":
            if not text or "|" not in text:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="خطأ في الصيغة. استخدم: اسم الزر|رقم الأب (0 للقائمة الرئيسية)")
                return
            name_part, parent_part = text.split("|",1)
            name = name_part.strip()
            try:
                parent_id = int(parent_part.strip())
            except Exception:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="رقم الأب يجب أن يكون عدداً صحيحاً.")
                return
            callback_data = f"btn_{int(time.time())}_{name.replace(' ','_')}"
            try:
                await db_execute("INSERT INTO buttons (name, callback_data, parent_id) VALUES ($1,$2,$3)", name, callback_data, parent_id)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text=f"تم إضافة الزر '{name}' بنجاح! (الرمز: {callback_data})")
            except Exception:
                logger.exception("Failed to add button")
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء إضافة الزر.")
            admin_state.pop(user_id, None)
            return

        # remove
        if action == "awaiting_remove":
            if not text:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر المراد حذفه.")
                return
            try:
                bid = int(text.strip())
            except Exception:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="يرجى إرسال رقم صحيح.")
                return
            try:
                await db_execute("DELETE FROM buttons WHERE id = $1", bid)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text=f"تم حذف الزر بالمعرف {bid}.")
            except Exception:
                logger.exception("Failed to delete button")
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء حذف الزر.")
            admin_state.pop(user_id, None)
            return

        # awaiting upload id
        if action == "awaiting_upload_button_id":
            if not text:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد رفع الملف له.")
                return
            try:
                bid = int(text.strip())
            except Exception:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="يرجى إرسال رقم صحيح.")
                return
            admin_state[user_id] = {"action": "awaiting_upload_file", "target_button_id": bid}
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="الآن أرسل الملف (مستند/صورة/فيديو) أو نص لربطه بالزر.")
            return

        # awaiting upload file
        if action == "awaiting_upload_file":
            target_bid = state.get("target_button_id")
            if not target_bid:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="خطأ: لم يتم تحديد الزر الهدف.")
                admin_state.pop(user_id, None)
                return
            file_id = None; content_type = None
            if msg.get("document"):
                file_id = msg["document"].get("file_id"); content_type = "document"
            elif msg.get("photo"):
                file_id = msg["photo"][-1].get("file_id"); content_type = "photo"
            elif msg.get("video"):
                file_id = msg["video"].get("file_id"); content_type = "video"
            elif text:
                file_id = text.strip(); content_type = "text"
            if not file_id:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لم يتم العثور على ملف في هذه الرسالة. أرسل ملفاً أو نصاً.")
                return
            try:
                await db_execute("UPDATE buttons SET content_type=$1, file_id=$2 WHERE id=$3", content_type, file_id, target_bid)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="تم ربط الملف بالزر بنجاح!")
            except Exception:
                logger.exception("Failed to update button with file")
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء ربط الملف.")
            admin_state.pop(user_id, None)
            return

    # Not admin flow: handle /start membership and main menu
    if isinstance(text, str) and text.strip().lower().startswith("/start"):
        logger.info("Received /start from user=%s chat=%s", user_id, chat_id)
        ok, missing, reasons = await check_user_membership(user_id)
        if not ok:
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
            except Exception:
                logger.exception("Failed to send missing-chats message")
            return

        # Save user
        try:
            await db_execute("INSERT INTO users (user_id, first_name) VALUES ($1,$2) ON CONFLICT (user_id) DO NOTHING", user_id, from_user.get("first_name",""))
        except Exception:
            logger.exception("Failed to save user (non-fatal)")

        try:
            markup = await build_main_menu()
            if bot and chat_id:
                if markup:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=markup)
                else:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! لا توجد أقسام متاحة حالياً.")
        except Exception:
            logger.exception("Failed to send /start reply")
        return

# ---------------- Webhook & callback handling ----------------
@app.get("/", response_class=PlainTextResponse)
async def index():
    return "OK"

@app.post("/webhook")
async def webhook(request: Request):
    # validate secret token header if set
    headers = {k.lower(): v for k, v in request.headers.items()}
    if WEBHOOK_SECRET_TOKEN:
        header_val = headers.get("x-telegram-bot-api-secret-token")
        if header_val != WEBHOOK_SECRET_TOKEN:
            logger.warning("Invalid secret token header: got=%s", header_val)
            raise HTTPException(status_code=403, detail="Invalid secret token")

    # try to acquire processing slot quickly
    try:
        await asyncio.wait_for(PROCESSING_SEMAPHORE.acquire(), timeout=PROCESSING_SEMAPHORE_TIMEOUT)
        acquired = True
    except asyncio.TimeoutError:
        logger.warning("Server busy (concurrency max=%s). Rejecting incoming webhook briefly.", MAX_CONCURRENT)
        # Respond 200 to avoid Telegram heavy retries; the client can try again.
        return JSONResponse({"ok": False, "error": "server_busy"}, status_code=200)

    try:
        try:
            update = await request.json()
        except Exception:
            logger.exception("Invalid JSON in webhook")
            raise HTTPException(status_code=400, detail="Invalid JSON")

        logger.debug("Incoming update: keys=%s", list(update.keys()))
        update_id = update.get("update_id")
        if update_id is not None:
            if update_id in PROCESSED_UPDATES:
                logger.debug("Duplicate update %s -> ack", update_id)
                return JSONResponse({"ok": True})
            PROCESSED_UPDATES.append(update_id)

        # ignore bot-originated updates
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
                logger.debug("Ignoring edited message from bot")
                return JSONResponse({"ok": True})
            if BOT_ID is not None and isinstance(frm, dict) and frm.get("id") == BOT_ID:
                logger.debug("Ignoring edited_message from our own bot id")
                return JSONResponse({"ok": True})

        # callback_query handling
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
            logger.info("Callback query user=%s data=%s", user_id, data)

            # quick answerCallbackQuery
            try:
                if bot and cq_id:
                    await bot.answer_callback_query(callback_query_id=cq_id)
            except Exception:
                logger.exception("answer_callback_query failed")

            # handle membership re-check
            if data == "check_membership":
                ok, missing, reasons = await check_user_membership(user_id)
                if ok:
                    try:
                        mk = await build_main_menu()
                        if bot and chat_id and message_id:
                            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:", reply_markup=mk)
                        elif bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:", reply_markup=mk)
                    except Exception:
                        logger.exception("Failed to show main menu after membership check")
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
                    lines.append("\nبعد التصحيح اضغط: 'لقد انضممت — تحقق'")
                    try:
                        if bot and chat_id and message_id:
                            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="\n".join(lines), reply_markup=missing_chats_markup())
                        elif bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=missing_chats_markup())
                    except Exception:
                        logger.exception("Failed to send missing-chats message")
                return JSONResponse({"ok": True})

            # admin panel
            if data == "admin_panel":
                if user_id not in ADMIN_IDS:
                    try:
                        if bot and cq_id:
                            await bot.answer_callback_query(callback_query_id=cq_id, text="ليس لديك صلاحية للوصول إلى هذه الصفحة.")
                    except Exception:
                        logger.exception("answer_callback_query failed for unauthorized admin_panel")
                    return JSONResponse({"ok": True})
                try:
                    if bot and chat_id and message_id:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                    elif bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                except Exception:
                    logger.exception("Failed to show admin panel")
                return JSONResponse({"ok": True})

            # admin actions (add/remove/upload/list/back_to_main) - similar to process_text_message admin flows
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
                        rows = await db_fetchall("SELECT id, name, callback_data, parent_id FROM buttons ORDER BY id")
                        lines = ["جميع الأزرار:"]
                        for r in rows:
                            lines.append(f"{r['id']}: {r['name']} (رمز: {r['callback_data']}, أب: {r['parent_id']})")
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="\n".join(lines))
                    except Exception:
                        logger.exception("admin_list_buttons failed")
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء جلب الأزرار.")
                return JSONResponse({"ok": True})

            if data == "back_to_main":
                try:
                    mk = await build_main_menu()
                    if bot and chat_id and message_id:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="مرحباً! اختر القسم المناسب:", reply_markup=mk)
                    elif bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=mk)
                except Exception:
                    logger.exception("back_to_main failed")
                return JSONResponse({"ok": True})

            # regular button handling
            try:
                row = await db_fetchone("SELECT content_type, file_id, id FROM buttons WHERE callback_data = $1", data)
            except Exception:
                logger.exception("DB error fetching button")
                row = None

            if row and row["content_type"] and row["file_id"]:
                try:
                    ctype = row["content_type"]; fid = row["file_id"]
                    if ctype == "document":
                        await bot.send_document(chat_id=chat_id, document=fid)
                    elif ctype == "photo":
                        await bot.send_photo(chat_id=chat_id, photo=fid)
                    elif ctype == "video":
                        await bot.send_video(chat_id=chat_id, video=fid)
                    else:
                        await bot.send_message(chat_id=chat_id, text=str(fid))
                except Exception:
                    logger.exception("Failed to send button content")
                return JSONResponse({"ok": True})

            # show submenu
            try:
                parent_row = await db_fetchone("SELECT id FROM buttons WHERE callback_data = $1", data)
                if not parent_row:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="الزر غير موجود.")
                    logger.info("Callback data not found: %s", data)
                    return JSONResponse({"ok": True})
                parent_id = parent_row["id"]
                subs = await db_fetchall("SELECT name, callback_data FROM buttons WHERE parent_id = $1 ORDER BY id", parent_id)
                if subs:
                    keyboard = [[InlineKeyboardButton(s["name"], callback_data=s["callback_data"])] for s in subs]
                    keyboard.append([InlineKeyboardButton("العودة", callback_data="back_to_main")])
                    rm = InlineKeyboardMarkup(keyboard)
                    if bot and chat_id and message_id:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="اختر من القائمة:", reply_markup=rm)
                    elif bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="اختر من القائمة:", reply_markup=rm)
                else:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="هذه القائمة لا تحتوي على محتوى بعد.")
            except Exception:
                logger.exception("Failed to fetch/show submenu")
            return JSONResponse({"ok": True})

        # message handling
        if "message" in update and isinstance(update["message"], dict):
            try:
                await process_text_message(update["message"])
            except Exception:
                logger.exception("Error processing message")
            return JSONResponse({"ok": True})

        if "edited_message" in update and isinstance(update["edited_message"], dict):
            try:
                await process_text_message(update["edited_message"])
            except Exception:
                logger.exception("Error processing edited_message")
            return JSONResponse({"ok": True})

        return JSONResponse({"ok": True})
    finally:
        try:
            PROCESSING_SEMAPHORE.release()
        except Exception:
            logger.exception("Failed to release processing semaphore")

# ---------------- Startup / shutdown ----------------
@app.on_event("startup")
async def on_startup():
    global bot, BOT_ID, pg_pool
    logger.info("Startup: creating db pool and bot")
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("BOT_TOKEN and DATABASE_URL are required")
        return
    try:
        await init_pg_pool()
    except Exception:
        logger.exception("init_pg_pool failed")
    try:
        await init_db_schema_and_defaults()
    except Exception:
        logger.exception("init_db_schema_and_defaults failed")
    try:
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        BOT_ID = getattr(me, "id", None)
        logger.info("Bot ready id=%s username=%s", BOT_ID, getattr(me,"username",None))
    except Exception:
        logger.exception("Failed to initialize Bot")
        bot = None
        BOT_ID = None

    if WEBHOOK_URL and bot:
        webhook_target = WEBHOOK_URL.rstrip("/") + "/webhook"
        try:
            if WEBHOOK_SECRET_TOKEN:
                await bot.set_webhook(url=webhook_target, secret_token=WEBHOOK_SECRET_TOKEN)
            else:
                await bot.set_webhook(url=webhook_target)
            logger.info("Webhook set -> %s", webhook_target)
        except Exception:
            logger.exception("Failed to set webhook")

@app.on_event("shutdown")
async def on_shutdown():
    global pg_pool, bot
    logger.info("Shutdown: cleaning up")
    try:
        if bot and WEBHOOK_URL:
            await bot.delete_webhook()
    except Exception:
        logger.exception("Failed to delete webhook")
    if pg_pool:
        try:
            await pg_pool.close()
        except Exception:
            logger.exception("Failed to close pg pool")

# ---------------- Entrypoint ----------------
def main():
    logger.info("Starting uvicorn on 0.0.0.0:%s", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

if __name__ == "__main__":
    main()
