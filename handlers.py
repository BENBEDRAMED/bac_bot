import time
from typing import Dict, Any, Tuple, List
from database import db_execute, db_fetchone, db_fetchall
from ui import build_main_menu, missing_chats_markup, admin_panel_markup
from telegram_client import bot, safe_telegram_call
from settings import ADMIN_IDS, REQUIRED_CHATS
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import logging

logger = logging.getLogger(__name__)

admin_state: Dict[int, Dict[str, Any]] = {}

async def check_user_membership(user_id: int) -> Tuple[bool, List[str], Dict[str, str]]:
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

async def process_text_message(msg: dict):
    text = msg.get("text")
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from", {})
    user_id = from_user.get("id")

    if not text or not chat_id or not user_id:
        return

    if from_user.get("is_bot"):
        return

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

        try:
            await db_execute("INSERT INTO users (user_id, first_name) VALUES ($1,$2) ON CONFLICT DO NOTHING", 
                           user_id, from_user.get("first_name", ""))
        except Exception:
            pass

        markup = await build_main_menu()
        if bot and markup:
            await safe_telegram_call(bot.send_message(
                chat_id=chat_id, 
                text="مرحباً! اختر القسم:", 
                reply_markup=markup
            ))

async def handle_callback_query(cq: dict):
    data = cq.get("data")
    user_id = cq.get("from", {}).get("id")
    message = cq.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    logger.debug(f"Callback query: {data} from user {user_id}")

    if bot and cq.get("id"):
        await safe_telegram_call(bot.answer_callback_query(callback_query_id=cq["id"]))

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