# bot.py
import logging
import os
import time
from typing import Optional, Dict, Any
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
WEBHOOK_URL: Optional[str] = os.environ.get("WEBHOOK_URL")  # e.g. https://your-domain.com
DATABASE_URL: Optional[str] = os.environ.get("DATABASE_URL")
try:
    ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []

# ------------------ Globals ------------------
app = FastAPI()
bot: Optional[Bot] = None
BOT_ID: Optional[int] = None

# admin state: user_id -> state dict (action, target_button_id, etc.)
admin_state: Dict[int, Dict[str, Any]] = {}

# ------------------ DB helpers ------------------
def get_db_connection(max_retries: int = 3, retry_delay: int = 5):
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set")
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
    if conn is None:
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
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                class_type TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
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
    except Exception as e:
        logger.exception("init_db error: %s", e)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ------------------ Markup builders ------------------
def main_menu_markup():
    keyboard = [
        [InlineKeyboardButton("الأدبي", callback_data="literary")],
        [InlineKeyboardButton("العلمي", callback_data="science")],
        [InlineKeyboardButton("الإدارة", callback_data="admin_panel")],
    ]
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

# ------------------ FastAPI endpoints ------------------
@app.get("/", response_class=PlainTextResponse)
async def index():
    return "OK"

@app.post("/webhook")
async def webhook(request: Request):
    # validate secret header
    if WEBHOOK_SECRET_TOKEN:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header != WEBHOOK_SECRET_TOKEN:
            logger.warning("invalid webhook secret header")
            raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        update = await request.json()
    except Exception as e:
        logger.exception("invalid json: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("Incoming update id=%s keys=%s", update.get("update_id"), list(update.keys()))

    # ignore updates from bots to avoid loops
    if "message" in update and isinstance(update["message"], dict):
        frm = update["message"].get("from", {})
        if isinstance(frm, dict) and frm.get("is_bot"):
            return JSONResponse({"ok": True})
        if BOT_ID is not None and isinstance(frm, dict) and frm.get("id") == BOT_ID:
            return JSONResponse({"ok": True})

    if "callback_query" in update and isinstance(update["callback_query"], dict):
        cqfrom = update["callback_query"].get("from", {})
        if isinstance(cqfrom, dict) and cqfrom.get("is_bot"):
            return JSONResponse({"ok": True})
        if BOT_ID is not None and isinstance(cqfrom, dict) and cqfrom.get("id") == BOT_ID:
            return JSONResponse({"ok": True})

    # ---- callback_query handling (buttons) ----
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

        # acknowledge early
        try:
            if bot and cq_id:
                await bot.answer_callback_query(callback_query_id=cq_id)
        except Exception:
            pass

        # ADMIN PANEL entry
        if data == "admin_panel":
            if not is_admin(user_id):
                if bot and cq_id:
                    try:
                        await bot.answer_callback_query(callback_query_id=cq_id, text="ليس لديك صلاحية للوصول إلى هذه الصفحة.")
                    except Exception:
                        pass
                return JSONResponse({"ok": True})
            # show admin menu by editing or sending
            try:
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                    return JSONResponse({"ok": True})
            except Exception:
                # fallback: send message
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="لوحة تحكم المشرف:", reply_markup=admin_panel_markup())
                return JSONResponse({"ok": True})

        # ADMIN: add button
        if data == "admin_add_button":
            if not is_admin(user_id):
                return JSONResponse({"ok": True})
            admin_state[user_id] = {"action": "awaiting_add"}
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="أرسل اسم الزر الجديد ورقم الزر الأب بالصيغة:\nاسم الزر|رقم الأب (0 للقائمة الرئيسية)")
            return JSONResponse({"ok": True})

        # ADMIN: remove button (ask for id)
        if data == "admin_remove_button":
            if not is_admin(user_id):
                return JSONResponse({"ok": True})
            admin_state[user_id] = {"action": "awaiting_remove"}
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد حذفه (انظر 'عرض جميع الأزرار' للحصول على الأرقام).")
            return JSONResponse({"ok": True})

        # ADMIN: upload to button (ask for id first)
        if data == "admin_upload_to_button":
            if not is_admin(user_id):
                return JSONResponse({"ok": True})
            admin_state[user_id] = {"action": "awaiting_upload_button_id"}
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد رفع الملف له ثم أرسل الملف بعد ذلك.")
            return JSONResponse({"ok": True})

        # ADMIN: list buttons
        if data == "admin_list_buttons":
            if not is_admin(user_id):
                return JSONResponse({"ok": True})
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT id, name, callback_data, parent_id FROM buttons ORDER BY id")
                    rows = cur.fetchall()
                    cur.close()
                    text_lines = ["جميع الأزرار:"]
                    for r in rows:
                        text_lines.append(f"{r[0]}: {r[1]} (رمز: {r[2]}, أب: {r[3]})")
                    if bot and chat_id:
                        await bot.send_message(chat_id=chat_id, text="\n".join(text_lines))
                except Exception as e:
                    logger.exception("admin_list_buttons error: %s", e)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            else:
                if bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="خطأ: قاعدة البيانات غير متاحة.")
            return JSONResponse({"ok": True})

        # back to main
        if data == "back_to_main":
            # show main menu
            try:
                if bot and chat_id and message_id:
                    await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="مرحباً! اختر القسم المناسب:", reply_markup=main_menu_markup())
                elif bot and chat_id:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=main_menu_markup())
            except Exception as e:
                logger.exception("back_to_main error: %s", e)
            return JSONResponse({"ok": True})

        # regular buttons: see if there's content attached or submenu
        # Look up button in DB
        conn = get_db_connection()
        if conn is None:
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="خدمة غير متاحة الآن.")
            return JSONResponse({"ok": True})
        try:
            cur = conn.cursor()
            cur.execute("SELECT content_type, file_id, id FROM buttons WHERE callback_data = %s", (data,))
            row = cur.fetchone()
            cur.close()
        except Exception as e:
            logger.exception("DB error getting button: %s", e)
            row = None
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if row and row[0] and row[1]:
            # has content -> send it
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
            except Exception as e:
                logger.exception("Error sending content: %s", e)
            return JSONResponse({"ok": True})

        # otherwise treat as parent -> show submenu
        conn = get_db_connection()
        if conn is None:
            return JSONResponse({"ok": True})
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM buttons WHERE callback_data = %s", (data,))
            parent_row = cur.fetchone()
            if not parent_row:
                # not found
                await bot.send_message(chat_id=chat_id, text="الزر غير موجود.")
                cur.close()
                conn.close()
                return JSONResponse({"ok": True})
            parent_id = parent_row[0]
            cur.execute("SELECT name, callback_data FROM buttons WHERE parent_id = %s", (parent_id,))
            subs = cur.fetchall()
            cur.close()
        except Exception as e:
            logger.exception("DB error fetching submenu: %s", e)
            subs = []
        finally:
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
            except Exception as e:
                logger.exception("Failed to show submenu: %s", e)
        else:
            if bot and chat_id:
                await bot.send_message(chat_id=chat_id, text="هذه القائمة لا تحتوي على محتوى بعد.")
        return JSONResponse({"ok": True})

    # ---- message handling (text/file) including admin flows ----
    if "message" in update and isinstance(update["message"], dict):
        msg = update["message"]
        text = msg.get("text")
        chat = msg.get("chat", {}) or {}
        chat_id = chat.get("id")
        from_user = msg.get("from", {}) or {}
        user_id = from_user.get("id")

        # Ignore bot-sent messages (safety)
        if isinstance(from_user, dict) and from_user.get("is_bot"):
            return JSONResponse({"ok": True})
        if BOT_ID is not None and from_user.get("id") == BOT_ID:
            return JSONResponse({"ok": True})

        # Admin workflow: check state
        if user_id in admin_state:
            state = admin_state[user_id]
            action = state.get("action")
            # Awaiting add: text "Name|parent_id"
            if action == "awaiting_add":
                if not text or "|" not in text:
                    await bot.send_message(chat_id=chat_id, text="خطأ في الصيغة. استخدم: اسم الزر|رقم الأب (0 للقائمة الرئيسية)")
                    return JSONResponse({"ok": True})
                try:
                    name, parent_id_str = text.split("|", 1)
                    parent_id = int(parent_id_str.strip())
                    callback_data = f"btn_{name.strip().replace(' ', '_')}_{int(time.time())}"
                    conn = get_db_connection()
                    if conn:
                        cur = conn.cursor()
                        cur.execute("INSERT INTO buttons (name, callback_data, parent_id) VALUES (%s, %s, %s)", (name.strip(), callback_data, parent_id))
                        conn.commit()
                        cur.close()
                        conn.close()
                        await bot.send_message(chat_id=chat_id, text=f"تم إضافة الزر '{name.strip()}' بنجاح! (الرمز: {callback_data})")
                    else:
                        await bot.send_message(chat_id=chat_id, text="خطأ: قاعدة البيانات غير متاحة.")
                except Exception as e:
                    logger.exception("Error adding button: %s", e)
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء إضافة الزر.")
                finally:
                    admin_state.pop(user_id, None)
                return JSONResponse({"ok": True})

            # Awaiting remove: expecting button id (int)
            if action == "awaiting_remove":
                if not text:
                    await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر المراد حذفه.")
                    return JSONResponse({"ok": True})
                try:
                    bid = int(text.strip())
                    conn = get_db_connection()
                    if conn:
                        cur = conn.cursor()
                        cur.execute("DELETE FROM buttons WHERE id = %s", (bid,))
                        affected = cur.rowcount
                        conn.commit()
                        cur.close()
                        conn.close()
                        if affected > 0:
                            await bot.send_message(chat_id=chat_id, text=f"تم حذف الزر بالمعرف {bid}.")
                        else:
                            await bot.send_message(chat_id=chat_id, text="لم يتم العثور على الزر المحدد.")
                    else:
                        await bot.send_message(chat_id=chat_id, text="قاعدة البيانات غير متاحة.")
                except ValueError:
                    await bot.send_message(chat_id=chat_id, text="يرجى إرسال رقم صحيح.")
                except Exception as e:
                    logger.exception("Error removing button: %s", e)
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء حذف الزر.")
                finally:
                    admin_state.pop(user_id, None)
                return JSONResponse({"ok": True})

            # Awaiting upload -> first we asked for button id (action 'awaiting_upload_button_id')
            if action == "awaiting_upload_button_id":
                if not text:
                    await bot.send_message(chat_id=chat_id, text="أرسل رقم الزر الذي تريد رفع الملف له.")
                    return JSONResponse({"ok": True})
                try:
                    bid = int(text.strip())
                    # store target and set next state to awaiting_upload_file
                    admin_state[user_id] = {"action": "awaiting_upload_file", "target_button_id": bid}
                    await bot.send_message(chat_id=chat_id, text="الآن أرسل الملف (مستند/صورة/فيديو) أو نص لربطه بالزر.")
                except ValueError:
                    await bot.send_message(chat_id=chat_id, text="يرجى إرسال رقم صحيح.")
                return JSONResponse({"ok": True})

            # Awaiting upload file: check message for file types or text
            if action == "awaiting_upload_file":
                target_bid = state.get("target_button_id")
                if not target_bid:
                    await bot.send_message(chat_id=chat_id, text="لم يتم تحديد الزر الهدف. أعد العملية.")
                    admin_state.pop(user_id, None)
                    return JSONResponse({"ok": True})
                # Document
                file_id = None
                content_type = None
                if "document" in msg and msg.get("document"):
                    file_id = msg["document"].get("file_id")
                    content_type = "document"
                elif "photo" in msg and msg.get("photo"):
                    # get highest-res photo
                    file_id = msg["photo"][-1].get("file_id")
                    content_type = "photo"
                elif "video" in msg and msg.get("video"):
                    file_id = msg["video"].get("file_id")
                    content_type = "video"
                elif isinstance(text, str) and text.strip():
                    file_id = text.strip()
                    content_type = "text"

                if not file_id:
                    await bot.send_message(chat_id=chat_id, text="لم يتم العثور على ملف في هذه الرسالة. أرسل ملفاً (مستند/صورة/فيديو) أو نصاً.")
                    return JSONResponse({"ok": True})

                # Update DB
                try:
                    conn = get_db_connection()
                    if conn:
                        cur = conn.cursor()
                        cur.execute("UPDATE buttons SET content_type = %s, file_id = %s WHERE id = %s", (content_type, file_id, target_bid))
                        affected = cur.rowcount
                        conn.commit()
                        cur.close()
                        conn.close()
                        if affected > 0:
                            await bot.send_message(chat_id=chat_id, text="تم ربط الملف بالزر بنجاح!")
                        else:
                            await bot.send_message(chat_id=chat_id, text="لم يتم العثور على الزر المحدد.")
                    else:
                        await bot.send_message(chat_id=chat_id, text="قاعدة البيانات غير متاحة.")
                except Exception as e:
                    logger.exception("Error updating button content: %s", e)
                    await bot.send_message(chat_id=chat_id, text="حصل خطأ أثناء ربط الملف.")
                finally:
                    admin_state.pop(user_id, None)
                return JSONResponse({"ok": True})

        # ---- not in an admin workflow, handle /start or other messages ----
        # handle /start: show main 3 buttons
        if isinstance(text, str) and text.strip().lower().startswith("/start"):
            # Save user in users table (non-admin flow)
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    cur.execute("INSERT INTO users (user_id, first_name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (user_id, from_user.get("first_name", "")))
                    conn.commit()
                    cur.close()
                except Exception as e:
                    logger.exception("Error inserting user: %s", e)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            # send main menu
            if bot and chat_id:
                try:
                    await bot.send_message(chat_id=chat_id, text="مرحباً! اختر القسم المناسب:", reply_markup=main_menu_markup())
                except Exception as e:
                    logger.exception("Failed sending main menu: %s", e)
            return JSONResponse({"ok": True})

    return JSONResponse({"ok": True})

# ------------------ Startup / Shutdown ------------------
@app.on_event("startup")
async def on_startup():
    global bot, BOT_ID
    logger.info("Startup: initializing DB and bot")
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("Missing BOT_TOKEN or DATABASE_URL")
        return
    init_db()
    try:
        bot = Bot(token=BOT_TOKEN)
        me = await bot.get_me()
        BOT_ID = getattr(me, "id", None)
        logger.info("Bot ready id=%s username=%s", BOT_ID, getattr(me, "username", None))
    except Exception as e:
        logger.exception("Failed to create bot: %s", e)
        bot = None
        BOT_ID = None

    # register webhook if provided
    if WEBHOOK_URL and bot:
        webhook_target = WEBHOOK_URL.rstrip("/") + "/webhook"
        try:
            if WEBHOOK_SECRET_TOKEN:
                res = await bot.set_webhook(url=webhook_target, secret_token=WEBHOOK_SECRET_TOKEN)
            else:
                res = await bot.set_webhook(url=webhook_target)
            logger.info("Webhook set: %s -> %s", webhook_target, res)
        except Exception as e:
            logger.exception("Failed to set webhook: %s", e)

@app.on_event("shutdown")
async def on_shutdown():
    global bot
    logger.info("Shutdown: cleaning up")
    if bot and WEBHOOK_URL:
        try:
            await bot.delete_webhook()
        except Exception:
            pass

# ------------------ Entrypoint ------------------
def main():
    port = int(os.environ.get("PORT", 10000))
    logger.info("Starting uvicorn on 0.0.0.0:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

if __name__ == "__main__":
    main()
