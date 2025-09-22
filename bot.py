# bot.py
"""
FastAPI webhook Telegram bot with detailed diagnostic logging.

Usage:
 - Set your env vars: BOT_TOKEN, DATABASE_URL, WEBHOOK_URL (optional), WEBHOOK_SECRET_TOKEN (optional),
   ADMIN_IDS (comma-separated), REQUIRED_CHATS (comma-separated @usernames or -100... ids)
 - Deploy and watch logs. This file is the same logic as before but with much more logging.
"""

import logging
import os
import time
from collections import deque
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse

import psycopg2
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

# -------------------- Logging --------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)

# -------------------- Environment --------------------
BOT_TOKEN: Optional[str] = os.environ.get("BOT_TOKEN")
WEBHOOK_URL: Optional[str] = os.environ.get("WEBHOOK_URL")
WEBHOOK_SECRET_TOKEN: Optional[str] = os.environ.get("WEBHOOK_SECRET_TOKEN")
DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")
try:
    ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []

REQUIRED_CHATS_RAW = os.environ.get("REQUIRED_CHATS", "")
REQUIRED_CHATS: List[str] = [c.strip() for c in REQUIRED_CHATS_RAW.split(",") if c.strip()]

PORT: int = int(os.environ.get("PORT", 10000))

# -------------------- Globals --------------------
app = FastAPI()
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None

PROCESSED_UPDATES = deque(maxlen=5000)
admin_state: Dict[int, Dict[str, Any]] = {}

# -------------------- DB helpers --------------------
def get_db_connection(max_retries: int = 3, retry_delay: int = 2):
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set")
        return None
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.debug("Attempting DB connection (attempt %d/%d)", attempt, max_retries)
            result = urlparse(DATABASE_URL)
            conn = psycopg2.connect(
                database=result.path[1:],
                user=result.username,
                password=result.password,
                host=result.hostname,
                port=result.port,
                connect_timeout=10,
            )
            logger.info("Database connection established")
            return conn
        except Exception as e:
            last_exc = e
            logger.warning("DB connect attempt %d failed: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(retry_delay)
    logger.error("All DB connection attempts failed: %s", last_exc)
    return None

def init_db():
    conn = get_db_connection()
    if conn is None:
        logger.error("init_db: cannot connect to DB")
        return
    cur = None
    try:
        logger.debug("Creating tables and default buttons if needed")
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS buttons (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                callback_data TEXT UNIQUE NOT NULL,
                parent_id INTEGER DEFAULT 0,
                content_type TEXT,
                file_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                class_type TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        default_buttons = [
            ("العلمي", "science", 0, None, None),
            ("الأدبي", "literary", 0, None, None),
            ("الإدارة", "admin_panel", 0, None, None),
        ]
        for name, cb, parent, ctype, fid in default_buttons:
            cur.execute(
                "INSERT INTO buttons (name, callback_data, parent_id, content_type, file_id) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (callback_data) DO NOTHING",
                (name, cb, parent, ctype, fid)
            )
        conn.commit()
        logger.info("DB initialized with default buttons")
    except Exception as e:
        logger.exception("init_db error: %s", e)
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# -------------------- UI builders --------------------
def build_main_menu() -> Optional[InlineKeyboardMarkup]:
    conn = get_db_connection()
    if conn is None:
        logger.error("build_main_menu: DB not available")
        return None
    cur = None
    rows = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id;")
        rows = cur.fetchall()
        logger.debug("Main menu buttons fetched: %s", rows)
    except Exception as e:
        logger.exception("Error fetching main buttons: %s", e)
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    if not rows:
        return None
    keyboard = [[InlineKeyboardButton(name, callback_data=cb)] for name, cb in rows]
    return InlineKeyboardMarkup(keyboard)

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
    keyboard = [[InlineKeyboardButton("لقد انضممت — تحقق", callback_data="check_membership")]]
    return InlineKeyboardMarkup(keyboard)

# -------------------- Membership check (with logs) --------------------
async def check_user_membership(user_id: int) -> Tuple[bool, List[str], Dict[str, str]]:
    """
    Returns (ok, missing_list, reasons)
    reasons: human codes such as 'ok','bot_must_be_admin','bot_cannot_access_members',
             'chat_not_found','user_not_member','bot_not_initialized','unknown_error'
    """
    missing = []
    reasons: Dict[str, str] = {}

    logger.debug("Checking membership for user_id=%s required_chats=%s", user_id, REQUIRED_CHATS)
    if not REQUIRED_CHATS:
        logger.debug("No REQUIRED_CHATS configured -> passing membership check")
        return True, missing, reasons

    if not bot:
        logger.warning("Bot not initialized; cannot verify membership -> block by default")
        for c in REQUIRED_CHATS:
            missing.append(c)
            reasons[c] = "bot_not_initialized"
        return False, missing, reasons

    for chat_ref in REQUIRED_CHATS:
        logger.debug("Checking chat %s for user %s", chat_ref, user_id)
        try:
            member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
            status = getattr(member, "status", None)
            logger.info("getChatMember(%s, %s) -> status=%s", chat_ref, user_id, status)
            if status in ("member", "administrator", "creator"):
                reasons[chat_ref] = "ok"
                continue
            else:
                missing.append(chat_ref)
                reasons[chat_ref] = "user_not_member"
        except Exception as e:
            txt = str(e)
            missing.append(chat_ref)
            logger.warning("getChatMember failed for %s user=%s: %s", chat_ref, user_id, txt)
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

# -------------------- Core processing --------------------
async def process_text_message(msg: dict):
    """Handle message or edited_message dictionaries (admin flows + /start)."""
    global admin_state, bot

    text = msg.get("text") if isinstance(msg.get("text"), str) else None
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    from_user = msg.get("from", {}) or {}
    user_id = from_user.get("id")

    logger.info("Processing text message from=%s text=%s chat_id=%s", user_id, text, chat_id)

    # ignore bots
    if isinstance(from_user, dict) and from_user.get("is_bot"):
        logger.debug("Ignoring text from a bot account")
        return
    if BOT_ID is not None and from_user.get("id") == BOT_ID:
        logger.debug("Ignoring text from our own bot id")
        return

    # ADMIN flows handling
    if user_id in admin_state:
        state = admin_state[user_id]
        action = state.get("action")
        logger.info("User %s in admin flow action=%s", user_id, action)

        # ADD BUTTON
        if action == "awaiting_add":
            logger.debug("Admin %s submitting add button data: text=%s", user_id, text)
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
            conn = get_db_connection()
            if conn is None:
                logger.error("DB not available to add button")
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="قاعدة البيانات غير متاحة.")
                admin_state.pop(user_id, None)
                return
            cur = None
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO buttons (name, callback_data, parent_id) VALUES (%s, %s, %s)",
                            (name, callback_data, parent_id))
                conn.commit()
                logger.info("Added button name=%s callback=%s parent=%s", name, callback_data, parent_id)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text=f"تم إضافة الزر '{name}' بنجاح! (الرمز: {callback_data})")
            except Exception as e:
                logger.exception("Error adding button: %s", e)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء إضافة الزر.")
            finally:
                try:
                    if cur:
                        cur.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            admin_state.pop(user_id, None)
            return

        # REMOVE BUTTON
        if action == "awaiting_remove":
            logger.debug("Admin %s removing button id_text=%s", user_id, text)
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
            conn = get_db_connection()
            if conn is None:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="قاعدة البيانات غير متاحة.")
                admin_state.pop(user_id, None)
                return
            cur = None
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM buttons WHERE id = %s", (bid,))
                affected = cur.rowcount
                conn.commit()
                logger.info("Admin %s attempted delete button id=%s affected=%s", user_id, bid, affected)
                if bot and chat_id:
                    if affected > 0:
                        await bot.send_message(chat_id=chat_id, text=f"تم حذف الزر بالمعرف {bid}.")
                    else:
                        await bot.send_message(chat_id=chat_id, text="لم يتم العثور على الزر المحدد.")
            except Exception as e:
                logger.exception("Error removing button: %s", e)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء حذف الزر.")
            finally:
                try:
                    if cur:
                        cur.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            admin_state.pop(user_id, None)
            return

        # AWAITING UPLOAD BUTTON ID
        if action == "awaiting_upload_button_id":
            logger.debug("Admin %s provided upload target id text=%s", user_id, text)
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

        # AWAITING UPLOAD FILE
        if action == "awaiting_upload_file":
            logger.debug("Admin %s uploading file in message; state=%s", user_id, state)
            target_bid = state.get("target_button_id")
            if not target_bid:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لم يتم تحديد الزر الهدف. أعد العملية.")
                admin_state.pop(user_id, None)
                return
            file_id = None
            content_type = None
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
            conn = get_db_connection()
            if conn is None:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="قاعدة البيانات غير متاحة.")
                admin_state.pop(user_id, None)
                return
            cur = None
            try:
                cur = conn.cursor()
                cur.execute("UPDATE buttons SET content_type = %s, file_id = %s WHERE id = %s",
                            (content_type, file_id, target_bid))
                affected = cur.rowcount
                conn.commit()
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
            finally:
                try:
                    if cur:
                        cur.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            admin_state.pop(user_id, None)
            return

    # Not admin flow: handle /start
    if isinstance(text, str) and text.strip().lower().startswith("/start"):
        logger.info("Received /start from user=%s chat=%s", user_id, chat_id)
        ok, missing, reasons = await check_user_membership(user_id)
        if not ok:
            logger.info("User %s is missing required chats %s reasons=%s", user_id, missing, reasons)
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
                    logger.debug("Sent missing-chats message to user=%s", user_id)
            except Exception as e:
                logger.exception("Failed to send missing-chats message: %s", e)
            return

        # membership ok -> save user and show menu
        conn = get_db_connection()
        if conn:
            cur = None
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO users (user_id, first_name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                            (user_id, from_user.get("first_name", "")))
                conn.commit()
                logger.debug("Saved user to DB user_id=%s", user_id)
            except Exception as e:
                logger.exception("Error saving user: %s", e)
            finally:
                try:
                    if cur:
                        cur.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass

        markup = build_main_menu()
        try:
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
    # accept secret token header case-insensitively
    headers = {k.lower(): v for k, v in request.headers.items()}
    if WEBHOOK_SECRET_TOKEN:
        header_val = headers.get("x-telegram-bot-api-secret-token")
        if header_val != WEBHOOK_SECRET_TOKEN:
            logger.warning("Invalid secret token header on incoming webhook (got=%s)", header_val)
            raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        update = await request.json()
    except Exception as e:
        logger.exception("Failed to parse JSON from webhook: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.debug("Received update JSON: %s", update)

    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in PROCESSED_UPDATES:
            logger.debug("Duplicate update_id=%s -> ignoring", update_id)
            return JSONResponse({"ok": True})
        PROCESSED_UPDATES.append(update_id)

    logger.info("Incoming update id=%s keys=%s", update.get("update_id"), list(update.keys()))

    # ignore updates from bots or our own bot id
    try:
        if "message" in update and isinstance(update["message"], dict):
            frm = update["message"].get("from", {})
            if isinstance(frm, dict) and frm.get("is_bot"):
                logger.debug("Ignoring message from bot account.")
                return JSONResponse({"ok": True})
            if BOT_ID is not None and isinstance(frm, dict) and frm.get("id") == BOT_ID:
                logger.debug("Ignoring message from our own bot id.")
                return JSONResponse({"ok": True})
    except Exception:
        # don't let a small error prevent processing
        logger.exception("Error checking message origin")

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

        logger.info("Callback query: user=%s data=%s chat_id=%s message_id=%s", user_id, data, chat_id, message_id)

        # answerCallbackQuery quickly (log result)
        try:
            if bot and cq_id:
                await bot.answer_callback_query(callback_query_id=cq_id)
                logger.debug("Answered callback_query id=%s", cq_id)
        except Exception as e:
            logger.exception("answerCallbackQuery failed: %s", e)

        # handle membership re-check
        if data == "check_membership":
            logger.info("User %s clicked check_membership", user_id)
            ok, missing, reasons = await check_user_membership(user_id)
            logger.info("check_membership: ok=%s missing=%s reasons=%s", ok, missing, reasons)
            if ok:
                try:
                    markup = build_main_menu()
                    if bot and chat_id and message_id:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:", reply_markup=markup)
                        logger.debug("Edited membership message to show main menu for user=%s", user_id)
                    elif bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:", reply_markup=markup)
                        logger.debug("Sent membership success message to user=%s", user_id)
                except Exception as e:
                    logger.exception("Failed to show main menu after membership OK: %s", e)
                    try:
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:", reply_markup=markup)
                            logger.debug("Fallback send_message after edit failure for user=%s", user_id)
                    except Exception as e2:
                        logger.exception("Fallback send_message also failed: %s", e2)
            else:
                # Build helpful reasons message
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
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="\n".join(lines), reply_markup=missing_chats_markup())
                        logger.debug("Edited message to show missing reasons for user=%s", user_id)
                    elif bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=missing_chats_markup())
                        logger.debug("Sent missing reasons message to user=%s", user_id)
                except Exception as e:
                    logger.exception("Failed to send missing reasons message: %s", e)
            return JSONResponse({"ok": True})

        # ADMIN panel
        if data == "admin_panel":
            logger.info("User %s requested admin_panel", user_id)
            if not is_admin(user_id):
                try:
                    if bot and cq_id:
                        await bot.answer_callback_query(callback_query_id=cq_id, text="ليس لديك صلاحية للوصول إلى هذه الصفحة.")
                        logger.debug("Told user %s they are not admin", user_id)
                except Exception:
                    logger.exception("Failed to answer unauthorized admin_panel")
                return JSONResponse({"ok": True})
            try:
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                    logger.debug("Displayed admin panel to admin %s", user_id)
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                    logger.debug("Sent admin panel to admin %s via send_message", user_id)
            except Exception as e:
                logger.exception("Failed to show admin panel: %s", e)
            return JSONResponse({"ok": True})

        # Admin actions / list / add / remove / upload are handled in other branches below
        # Admin: add button
        if data == "admin_add_button":
            logger.info("Admin %s clicked add_button", user_id)
            if is_admin(user_id):
                admin_state[user_id] = {"action": "awaiting_add"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل اسم الزر الجديد ورقم الزر الأب بالصيغة:\nاسم الزر|رقم الأب (0 للقائمة الرئيسية)")
                        logger.debug("Sent add-button instruction to admin %s", user_id)
                except Exception as e:
                    logger.exception("Failed to send admin add instruction: %s", e)
            return JSONResponse({"ok": True})

        if data == "admin_remove_button":
            logger.info("Admin %s clicked remove_button", user_id)
            if is_admin(user_id):
                admin_state[user_id] = {"action": "awaiting_remove"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد حذفه (انظر 'عرض جميع الأزرار').")
                        logger.debug("Sent remove-button instruction to admin %s", user_id)
                except Exception as e:
                    logger.exception("Failed to send admin remove instruction: %s", e)
            return JSONResponse({"ok": True})

        if data == "admin_upload_to_button":
            logger.info("Admin %s clicked upload_to_button", user_id)
            if is_admin(user_id):
                admin_state[user_id] = {"action": "awaiting_upload_button_id"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد رفع الملف له ثم أرسل الملف بعد ذلك.")
                        logger.debug("Sent upload-button instruction to admin %s", user_id)
                except Exception as e:
                    logger.exception("Failed to send admin upload instruction: %s", e)
            return JSONResponse({"ok": True})

        if data == "admin_list_buttons":
            logger.info("Admin %s clicked list_buttons", user_id)
            if is_admin(user_id):
                conn = get_db_connection()
                if conn is None:
                    try:
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="قاعدة البيانات غير متاحة.")
                            logger.debug("Told admin %s DB unavailable", user_id)
                    except Exception:
                        logger.exception("Failed to notify admin of DB issue")
                    return JSONResponse({"ok": True})
                cur = None
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT id, name, callback_data, parent_id FROM buttons ORDER BY id;")
                    rows = cur.fetchall()
                    lines = ["جميع الأزرار:"]
                    for r in rows:
                        lines.append(f"{r[0]}: {r[1]} (رمز: {r[2]}, أب: {r[3]})")
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="\n".join(lines))
                        logger.debug("Sent admin list to %s", user_id)
                except Exception as e:
                    logger.exception("admin_list_buttons error: %s", e)
                    try:
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء جلب الأزرار.")
                    except Exception:
                        logger.exception("Failed to send admin error message")
                finally:
                    try:
                        if cur:
                            cur.close()
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
            return JSONResponse({"ok": True})

        if data == "back_to_main":
            logger.info("User %s clicked back_to_main", user_id)
            try:
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="مرحباً! اختر القسم المناسب:", reply_markup=build_main_menu())
                    logger.debug("Back to main via edit for user %s", user_id)
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=build_main_menu())
                    logger.debug("Back to main via send for user %s", user_id)
            except Exception as e:
                logger.exception("back_to_main error: %s", e)
            return JSONResponse({"ok": True})

        # Regular button handling (DB-backed)
        logger.debug("Handling regular button callback data=%s", data)
        conn = get_db_connection()
        if conn is None:
            logger.error("DB not available while handling button callback")
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="خدمة غير متاحة الآن.")
            except Exception:
                logger.exception("Failed to notify user about DB unavailability")
            return JSONResponse({"ok": True})
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("SELECT content_type, file_id, id FROM buttons WHERE callback_data = %s", (data,))
            row = cur.fetchone()
            logger.debug("DB query for button callback returned: %s", row)
        except Exception as e:
            logger.exception("DB error getting button: %s", e)
            row = None
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

        if row and row[0] and row[1]:
            ctype, fid, _ = row
            try:
                if ctype == "document":
                    await bot.send_document(chat_id=chat_id, document=fid)
                    logger.info("Sent document fid=%s to chat=%s", fid, chat_id)
                elif ctype == "photo":
                    await bot.send_photo(chat_id=chat_id, photo=fid)
                    logger.info("Sent photo fid=%s to chat=%s", fid, chat_id)
                elif ctype == "video":
                    await bot.send_video(chat_id=chat_id, video=fid)
                    logger.info("Sent video fid=%s to chat=%s", fid, chat_id)
                else:
                    await bot.send_message(chat_id=chat_id, text=str(fid))
                    logger.info("Sent text content for button to chat=%s", chat_id)
            except Exception as e:
                logger.exception("Error sending button content: %s", e)
            return JSONResponse({"ok": True})

        # Show submenu
        conn = get_db_connection()
        if conn is None:
            return JSONResponse({"ok": True})
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM buttons WHERE callback_data = %s", (data,))
            parent_row = cur.fetchone()
            if not parent_row:
                logger.info("Button callback_data=%s not found in DB", data)
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="الزر غير موجود.")
                return JSONResponse({"ok": True})
            parent_id = parent_row[0]
            cur.execute("SELECT name, callback_data FROM buttons WHERE parent_id = %s", (parent_id,))
            subs = cur.fetchall()
            logger.debug("Submenu items for parent_id=%s: %s", parent_id, subs)
        except Exception as e:
            logger.exception("DB error fetching submenu: %s", e)
            subs = []
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

        if subs:
            keyboard = [[InlineKeyboardButton(name, callback_data=cb)] for name, cb in subs]
            keyboard.append([InlineKeyboardButton("العودة", callback_data="back_to_main")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="اختر من القائمة:", reply_markup=reply_markup)
                    logger.debug("Edited message to show submenu for user=%s", user_id)
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="اختر من القائمة:", reply_markup=reply_markup)
                    logger.debug("Sent submenu message to user=%s", user_id)
            except Exception as e:
                logger.exception("Failed to show submenu: %s", e)
        else:
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="هذه القائمة لا تحتوي على محتوى بعد.")
                    logger.debug("Told user=%s submenu empty", user_id)
            except Exception:
                logger.exception("Failed to notify user about empty submenu")
        return JSONResponse({"ok": True})

    # message / edited_message handling
    if "message" in update and isinstance(update["message"], dict):
        logger.debug("Processing update.message")
        try:
            await process_text_message(update["message"])
        except Exception as e:
            logger.exception("Unhandled exception in process_text_message: %s", e)
            # notify admins of the exception
            if bot and ADMIN_IDS:
                try:
                    for admin in ADMIN_IDS:
                        await bot.send_message(chat_id=admin, text=f"Error processing message update: {e}")
                except Exception:
                    logger.exception("Failed to notify admins about exception")
        return JSONResponse({"ok": True})

    if "edited_message" in update and isinstance(update["edited_message"], dict):
        logger.debug("Processing update.edited_message")
        try:
            await process_text_message(update["edited_message"])
        except Exception as e:
            logger.exception("Unhandled exception in process_text_message (edited): %s", e)
        return JSONResponse({"ok": True})

    logger.debug("No actionable fields in update; acking")
    return JSONResponse({"ok": True})

# -------------------- Startup / Shutdown --------------------
@app.on_event("startup")
async def on_startup():
    global bot, BOT_ID
    logger.info("Startup: initializing DB and bot")
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Missing BOT_TOKEN or DATABASE_URL; bot will not be fully initialized")
        return

    init_db()

    try:
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        BOT_ID = getattr(me, "id", None)
        logger.info("Bot initialized id=%s username=%s", BOT_ID, getattr(me, "username", None))
    except Exception as e:
        logger.exception("Failed to initialize Bot instance: %s", e)
        bot = None
        BOT_ID = None

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
                logger.info("getWebhookInfo: %s", info)
            except Exception as e:
                logger.exception("Failed to call getWebhookInfo: %s", e)
        except Exception as e:
            logger.exception("Failed to set webhook: %s", e)
    else:
        if not WEBHOOK_URL:
            logger.warning("WEBHOOK_URL not set; webhook won't be auto-registered")
        else:
            logger.warning("Bot not initialized; cannot register webhook")

@app.on_event("shutdown")
async def on_shutdown():
    global bot
    logger.info("Shutdown: cleaning up")
    if bot and WEBHOOK_URL:
        try:
            await bot.delete_webhook()
            logger.info("Webhook deleted at shutdown")
        except Exception:
            logger.exception("Failed to delete webhook at shutdown")

# -------------------- Entrypoint --------------------
def main():
    logger.info("Starting uvicorn on 0.0.0.0:%s", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

if __name__ == "__main__":
    main()
