# bot.py
import logging
import os
import time
from collections import deque
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

import psycopg2
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

# ---------- Config & Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN: Optional[str] = os.environ.get("BOT_TOKEN")
WEBHOOK_URL: Optional[str] = os.environ.get("WEBHOOK_URL")
WEBHOOK_SECRET_TOKEN: Optional[str] = os.environ.get("WEBHOOK_SECRET_TOKEN")
DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")
try:
    ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []

REQUIRED_CHATS_RAW = os.environ.get("REQUIRED_CHATS", "")  # comma-separated usernames or ids
REQUIRED_CHATS: List[str] = [c.strip() for c in REQUIRED_CHATS_RAW.split(",") if c.strip()]

PORT: int = int(os.environ.get("PORT", 10000))

# ---------- Globals ----------
app = FastAPI()
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None

PROCESSED_UPDATES = deque(maxlen=5000)
admin_state: Dict[int, Dict[str, Any]] = {}  # in-memory admin flows: user_id -> state

# ---------- DB helpers ----------
def get_db_connection(max_retries: int = 3, retry_delay: int = 2):
    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set")
        return None
    last_exc = None
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
            return conn
        except Exception as e:
            last_exc = e
            logger.error("DB connect failed (attempt %d/%d): %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(retry_delay)
    logger.error("All DB connection attempts failed: %s", last_exc)
    return None

def init_db():
    conn = get_db_connection()
    if not conn:
        logger.error("init_db: cannot connect to DB")
        return
    cur = None
    try:
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
        logger.info("DB initialized and default buttons ensured")
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

# ---------- UI builders ----------
def build_main_menu() -> Optional[InlineKeyboardMarkup]:
    conn = get_db_connection()
    if conn is None:
        return None
    cur = None
    rows = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id;")
        rows = cur.fetchall()
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
    # only one button needed to re-check
    keyboard = [[InlineKeyboardButton("لقد انضممت — تحقق", callback_data="check_membership")]]
    return InlineKeyboardMarkup(keyboard)

# ---------- Membership check ----------
async def check_user_membership(user_id: int) -> (bool, List[str]):
    """
    Returns (ok, missing_list).
    REQUIRED_CHATS may contain usernames (like @channel) or numeric ids (-100...)
    """
    missing = []
    if not REQUIRED_CHATS:
        return True, missing
    if not bot:
        logger.error("Bot instance not initialized; cannot verify membership")
        return False, REQUIRED_CHATS[:]

    for chat_ref in REQUIRED_CHATS:
        chat_id = chat_ref
        # allow @username or numeric string; pass as-is
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            status = getattr(member, "status", None)
            if status in ("member", "administrator", "creator"):
                continue
            else:
                missing.append(chat_ref)
        except Exception as e:
            logger.warning("get_chat_member failed for %s: %s", chat_ref, e)
            missing.append(chat_ref)
    return len(missing) == 0, missing

# ---------- Core message processing ----------
async def process_text_message(msg: dict):
    """
    Handle message or edited_message dictionaries.
    Supports admin flows and /start with membership enforcement.
    """
    global admin_state, bot

    text = msg.get("text") if isinstance(msg.get("text"), str) else None
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    from_user = msg.get("from", {}) or {}
    user_id = from_user.get("id")

    logger.info("Processing text message from %s text=%s", user_id, text)

    if isinstance(from_user, dict) and from_user.get("is_bot"):
        logger.debug("Ignoring message from bot account.")
        return
    if BOT_ID is not None and from_user.get("id") == BOT_ID:
        logger.debug("Ignoring message from our own bot id.")
        return

    # Admin flows
    if user_id in admin_state:
        state = admin_state[user_id]
        action = state.get("action")
        logger.info("Admin state for %s action=%s", user_id, action)

        # Add button flow
        if action == "awaiting_add":
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

        # Remove button flow
        if action == "awaiting_remove":
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

        # Awaiting upload button id
        if action == "awaiting_upload_button_id":
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

        # Awaiting upload file
        if action == "awaiting_upload_file":
            target_bid = state.get("target_button_id")
            if not target_bid:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لم يتم تحديد الزر الهدف. أعد العملية.")
                admin_state.pop(user_id, None)
                return
            file_id = None
            content_type = None
            # Check attachments in the message dict (document/photo/video)
            if msg.get("document"):
                file_id = msg["document"].get("file_id")
                content_type = "document"
            elif msg.get("photo"):
                # take last (highest res)
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

    # Not in admin flow -> /start handling with membership enforcement
    if isinstance(text, str) and text.strip().lower().startswith("/start"):
        ok, missing = await check_user_membership(user_id)
        if not ok:
            lines = ["✋ قبل استخدام البوت، يلزم الانضمام إلى القنوات/المجموعة التالية:"]
            for c in missing:
                if c.startswith("@"):
                    lines.append(f"- https://t.me/{c.lstrip('@')}")
                else:
                    lines.append(f"- {c}")
            lines.append("\nبعد الانضمام اضغط: 'لقد انضممت — تحقق'")
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=missing_chats_markup())
            return

        # Save user
        conn = get_db_connection()
        if conn:
            cur = None
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO users (user_id, first_name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                            (user_id, from_user.get("first_name", "")))
                conn.commit()
            except Exception:
                logger.exception("Error saving user")
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

        # send main menu
        markup = build_main_menu()
        try:
            if bot and chat_id:
                if markup:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=markup)
                else:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! لا توجد أقسام متاحة حالياً.")
        except Exception as e:
            logger.exception("Failed to send /start reply: %s", e)
        return

# ---------- Webhook endpoint ----------
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
            logger.warning("Invalid secret token header on incoming webhook (got=%s)", header_val)
            raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        update = await request.json()
    except Exception:
        logger.exception("Failed to parse JSON from webhook")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in PROCESSED_UPDATES:
            logger.debug("Ignoring duplicate update_id=%s", update_id)
            return JSONResponse({"ok": True})
        PROCESSED_UPDATES.append(update_id)

    logger.info("Incoming update id=%s keys=%s", update.get("update_id"), list(update.keys()))

    # ignore bot-originated updates
    if "message" in update and isinstance(update["message"], dict):
        frm = update["message"].get("from", {})
        if isinstance(frm, dict) and frm.get("is_bot"):
            logger.debug("Ignoring message from bot account.")
            return JSONResponse({"ok": True})
        if BOT_ID is not None and isinstance(frm, dict) and frm.get("id") == BOT_ID:
            logger.debug("Ignoring message from our own bot id.")
            return JSONResponse({"ok": True})

    if "edited_message" in update and isinstance(update["edited_message"], dict):
        frm = update["edited_message"].get("from", {})
        if isinstance(frm, dict) and frm.get("is_bot"):
            logger.debug("Ignoring edited_message from bot account.")
            return JSONResponse({"ok": True})
        if BOT_ID is not None and isinstance(frm, dict) and frm.get("id") == BOT_ID:
            logger.debug("Ignoring edited_message from our own bot id.")
            return JSONResponse({"ok": True})

    if "callback_query" in update and isinstance(update["callback_query"], dict):
        cq_from = update["callback_query"].get("from", {})
        if isinstance(cq_from, dict) and cq_from.get("is_bot"):
            logger.debug("Ignoring callback_query from bot account.")
            return JSONResponse({"ok": True})
        if BOT_ID is not None and isinstance(cq_from, dict) and cq_from.get("id") == BOT_ID:
            logger.debug("Ignoring callback_query from our own bot id.")
            return JSONResponse({"ok": True})

    # ---------- callback_query handling ----------
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

        logger.info("Callback query: user=%s data=%s", user_id, data)

        # quick ack so Telegram UI stops spinner
        try:
            if bot and cq_id:
                await bot.answer_callback_query(callback_query_id=cq_id)
        except Exception:
            pass

        # membership re-check button
        if data == "check_membership":
            ok, missing = await check_user_membership(user_id)
            if ok:
                # show main menu
                try:
                    if bot and chat_id:
                        markup = build_main_menu()
                        if markup:
                            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="شكرًا! تم التحقق — يمكنك الآن استخدام البوت:", reply_markup=markup)
                        else:
                            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="تم التحقق! لكن لا توجد أقسام متاحة الآن.")
                except Exception:
                    logger.exception("Failed to show main menu after membership ok")
            else:
                lines = ["لا تزال هناك قنوات / مجموعة مفقودة:"]
                for c in missing:
                    if c.startswith("@"):
                        lines.append(f"- https://t.me/{c.lstrip('@')}")
                    else:
                        lines.append(f"- {c}")
                try:
                    if bot and chat_id:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="\n".join(lines), reply_markup=missing_chats_markup())
                except Exception:
                    logger.exception("Failed to edit message with missing list")
            return JSONResponse({"ok": True})

        # Admin panel entry
        if data == "admin_panel":
            if not is_admin(user_id):
                try:
                    if bot and cq_id:
                        await bot.answer_callback_query(callback_query_id=cq_id, text="ليس لديك صلاحية للوصول إلى هذه الصفحة.")
                except Exception:
                    pass
                return JSONResponse({"ok": True})
            try:
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
            except Exception:
                logger.exception("Failed to show admin panel")
            return JSONResponse({"ok": True})

        # Admin actions
        if data == "admin_add_button":
            if is_admin(user_id):
                admin_state[user_id] = {"action": "awaiting_add"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل اسم الزر الجديد ورقم الزر الأب بالصيغة:\nاسم الزر|رقم الأب (0 للقائمة الرئيسية)")
                except Exception:
                    logger.exception("Failed to send admin instruction")
            return JSONResponse({"ok": True})

        if data == "admin_remove_button":
            if is_admin(user_id):
                admin_state[user_id] = {"action": "awaiting_remove"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد حذفه (انظر 'عرض جميع الأزرار').")
                except Exception:
                    logger.exception("Failed to send admin instruction")
            return JSONResponse({"ok": True})

        if data == "admin_upload_to_button":
            if is_admin(user_id):
                admin_state[user_id] = {"action": "awaiting_upload_button_id"}
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد رفع الملف له ثم أرسل الملف بعد ذلك.")
                except Exception:
                    logger.exception("Failed to send admin instruction")
            return JSONResponse({"ok": True})

        if data == "admin_list_buttons":
            if is_admin(user_id):
                conn = get_db_connection()
                if conn is None:
                    try:
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="قاعدة البيانات غير متاحة.")
                    except Exception:
                        pass
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
                except Exception:
                    logger.exception("admin_list_buttons error")
                    try:
                        if bot and chat_id:
                            await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء جلب الأزرار.")
                    except Exception:
                        pass
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
            try:
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="مرحباً! اختر القسم المناسب:", reply_markup=build_main_menu())
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=build_main_menu())
            except Exception:
                logger.exception("back_to_main error")
            return JSONResponse({"ok": True})

        # Regular button handling (DB-backed)
        conn = get_db_connection()
        if conn is None:
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="خدمة غير متاحة الآن.")
            except Exception:
                pass
            return JSONResponse({"ok": True})
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("SELECT content_type, file_id, id FROM buttons WHERE callback_data = %s", (data,))
            row = cur.fetchone()
        except Exception:
            logger.exception("DB error getting button")
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

        # If button has content -> send it
        if row and row[0] and row[1]:
            ctype, fid, _ = row
            try:
                if ctype == "document":
                    await bot.send_document(chat_id=chat_id, document=fid)
                elif ctype == "photo":
                    await bot.send_photo(chat_id=chat_id, photo=fid)
                elif ctype == "video":
                    await bot.send_video(chat_id=chat_id, video=fid)
                else:
                    await bot.send_message(chat_id=chat_id, text=str(fid))
            except Exception:
                logger.exception("Error sending button content")
            return JSONResponse({"ok": True})

        # Otherwise show submenu if exists
        conn = get_db_connection()
        if conn is None:
            return JSONResponse({"ok": True})
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM buttons WHERE callback_data = %s", (data,))
            parent_row = cur.fetchone()
            if not parent_row:
                try:
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="الزر غير موجود.")
                except Exception:
                    pass
                return JSONResponse({"ok": True})
            parent_id = parent_row[0]
            cur.execute("SELECT name, callback_data FROM buttons WHERE parent_id = %s", (parent_id,))
            subs = cur.fetchall()
        except Exception:
            logger.exception("DB error fetching submenu")
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
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="اختر من القائمة:", reply_markup=reply_markup)
            except Exception:
                logger.exception("Failed to show submenu")
        else:
            try:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="هذه القائمة لا تحتوي على محتوى بعد.")
            except Exception:
                pass
        return JSONResponse({"ok": True})

    # ---------- message / edited_message handling ----------
    if "message" in update and isinstance(update["message"], dict):
        await process_text_message(update["message"])
        return JSONResponse({"ok": True})

    if "edited_message" in update and isinstance(update["edited_message"], dict):
        logger.info("Processing edited_message as message")
        await process_text_message(update["edited_message"])
        return JSONResponse({"ok": True})

    return JSONResponse({"ok": True})

# ---------- Startup / Shutdown ----------
@app.on_event("startup")
async def on_startup():
    global bot, BOT_ID
    logger.info("Startup: init DB and bot")
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Missing BOT_TOKEN or DATABASE_URL — cannot fully initialize bot.")
        return

    init_db()

    try:
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        BOT_ID = getattr(me, "id", None)
        logger.info("Bot ready id=%s username=%s", BOT_ID, getattr(me, "username", None))
    except Exception as e:
        logger.exception("Failed to create Telegram Bot instance: %s", e)
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
            except Exception:
                logger.exception("Failed to getWebhookInfo")
        except Exception:
            logger.exception("Failed to set webhook")
    else:
        logger.warning("WEBHOOK_URL not provided; webhook won't be registered automatically.")

@app.on_event("shutdown")
async def on_shutdown():
    global bot
    logger.info("Shutdown: cleaning up")
    if bot and WEBHOOK_URL:
        try:
            await bot.delete_webhook()
            logger.info("Webhook deleted at shutdown.")
        except Exception:
            pass

# ---------- Entrypoint ----------
def main():
    logger.info("Starting uvicorn on 0.0.0.0:%s", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

if __name__ == "__main__":
    main()
