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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, ChatMember

# ---------- Config & Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Required env vars
BOT_TOKEN: Optional[str] = os.environ.get("BOT_TOKEN")
WEBHOOK_URL: Optional[str] = os.environ.get("WEBHOOK_URL")
WEBHOOK_SECRET_TOKEN: Optional[str] = os.environ.get("WEBHOOK_SECRET_TOKEN")
DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")
try:
    ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []

# New: REQUIRED_CHATS contains comma-separated chat usernames or IDs:
# Example: @channelA,@channelB,@mygroup  OR -1001234567890,-1009876543210,-1001112131415
REQUIRED_CHATS_RAW = os.environ.get("REQUIRED_CHATS", "")  # comma-separated
REQUIRED_CHATS: List[str] = [c.strip() for c in REQUIRED_CHATS_RAW.split(",") if c.strip()]

PORT: int = int(os.environ.get("PORT", 10000))

# ---------- Globals ----------
app = FastAPI()
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None

PROCESSED_UPDATES = deque(maxlen=5000)
admin_state: Dict[int, Dict[str, Any]] = {}

# ---------- DB helpers ----------
def get_db_connection(max_retries: int = 3, retry_delay: int = 2):
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
            return conn
        except Exception as e:
            logger.error("DB connect failed (attempt %d/%d): %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(retry_delay)
    return None

def init_db():
    conn = get_db_connection()
    if not conn:
        logger.error("init_db: cannot connect to DB")
        return
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
        # default top-level buttons
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
    except Exception:
        logger.exception("init_db error")
    finally:
        try:
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
def build_main_menu():
    conn = get_db_connection()
    if conn is None:
        return None
    rows = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id;")
        rows = cur.fetchall()
        cur.close()
    except Exception:
        logger.exception("Error fetching main buttons")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not rows:
        return None
    keyboard = [[InlineKeyboardButton(name, callback_data=cb)] for name, cb in rows]
    return InlineKeyboardMarkup(keyboard)

def admin_panel_markup():
    keyboard = [
        [InlineKeyboardButton("إضافة زر جديد", callback_data="admin_add_button")],
        [InlineKeyboardButton("حذف زر", callback_data="admin_remove_button")],
        [InlineKeyboardButton("رفع ملف لزر موجود", callback_data="admin_upload_to_button")],
        [InlineKeyboardButton("عرض جميع الأزرار", callback_data="admin_list_buttons")],
        [InlineKeyboardButton("العودة", callback_data="back_to_main")],
    ]
    return InlineKeyboardMarkup(keyboard)

def missing_chats_markup(missing: List[str]):
    # Provide a "I've joined — تحقق" button and links when possible
    keyboard = []
    keyboard.append([InlineKeyboardButton("لقد انضممت — تحقق", callback_data="check_membership")])
    return InlineKeyboardMarkup(keyboard)

# ---------- Membership check ----------
async def check_user_membership(user_id: int) -> (bool, List[str]):
    """
    Returns (is_member_of_all, missing_list).
    REQUIRED_CHATS must contain usernames (like @channel) or integer IDs (-100....).
    """
    global bot
    missing = []
    if not REQUIRED_CHATS:
        # no requirements
        return True, missing

    if not bot:
        # can't check without bot
        logger.error("Bot instance not initialized; cannot verify memberships")
        return False, REQUIRED_CHATS[:]  # treat as missing so user is blocked until bot up

    for chat_ref in REQUIRED_CHATS:
        # form chat_id - allow numeric ids or usernames (with or without leading @)
        chat_id = chat_ref
        if chat_ref.startswith("@"):
            chat_id = chat_ref  # username is fine
        # if it's numeric string (including -100...), keep as-is
        # call get_chat_member
        try:
            # telegram.Bot.get_chat_member accepts chat_id as int or string username
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            # member.status: 'creator','administrator','member','restricted','left','kicked'
            status = getattr(member, "status", None)
            if status in ("member", "creator", "administrator"):
                continue
            else:
                missing.append(chat_ref)
        except Exception as e:
            # Could be because bot is not a member of a private chat or chat not found
            logger.warning("Failed to get_chat_member for %s: %s", chat_ref, e)
            missing.append(chat_ref)
    return (len(missing) == 0), missing

# ---------- Core text processing (keeps admin flows) ----------
async def process_text_message(msg: dict):
    """
    Handle messages or edited_messages.
    Enforces membership on /start and admin flows.
    """
    global admin_state, bot

    text = msg.get("text") if isinstance(msg.get("text"), str) else None
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    from_user = msg.get("from", {}) or {}
    user_id = from_user.get("id")

    logger.info("Processing text message from %s text=%s", user_id, text)

    # safety
    if isinstance(from_user, dict) and from_user.get("is_bot"):
        logger.debug("Ignoring message from bot account.")
        return
    if BOT_ID is not None and from_user.get("id") == BOT_ID:
        logger.debug("Ignoring message from our own bot id.")
        return

    # If user is in admin flow, handle admin actions (unchanged)
    if user_id in admin_state:
        state = admin_state[user_id]
        action = state.get("action")
        # ... (admin flows same as previous code: awaiting_add, awaiting_remove, awaiting_upload_button_id, awaiting_upload_file)
        # For brevity we re-use the same logic as earlier; ensure admin_state usage is present.
        # (I keep the same admin flows as previous full file. Paste the exact admin flows into your code block.)
        # --- BEGIN ADMIN FLOW (condensed here; full code below includes full admin flows) ---
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
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("INSERT INTO buttons (name, callback_data, parent_id) VALUES (%s, %s, %s)",
                                (name, callback_data, parent_id))
                    conn.commit()
                    cur.close()
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text=f"تم إضافة الزر '{name}' بنجاح! (الرمز: {callback_data})")
                except Exception:
                    logger.exception("Error adding button")
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء إضافة الزر.")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            else:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="قاعدة البيانات غير متاحة.")
            admin_state.pop(user_id, None)
            return

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
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM buttons WHERE id = %s", (bid,))
                    affected = cur.rowcount
                    conn.commit()
                    cur.close()
                    if bot and chat_id:
                        if affected > 0:
                            await bot.send_message(chat_id=chat_id, text=f"تم حذف الزر بالمعرف {bid}.")
                        else:
                            await bot.send_message(chat_id=chat_id, text="لم يتم العثور على الزر المحدد.")
