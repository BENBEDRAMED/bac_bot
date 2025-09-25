import time
from typing import Dict, Any, Tuple, List
from database import db_execute, db_fetchone, db_fetchall
from ui import build_main_menu, missing_chats_markup, admin_panel_markup, build_compact_submenu
from telegram_client import safe_telegram_call, get_bot, get_bot_id
from settings import ADMIN_IDS, REQUIRED_CHATS
from telegram import ReplyKeyboardRemove
import logging

logger = logging.getLogger(__name__)

admin_state: Dict[int, Dict[str, Any]] = {}
user_current_menu: Dict[int, int] = {}  # Track user's current menu level

async def process_text_message(msg: dict):
    bot = get_bot()
    BOT_ID = get_bot_id()

    # Basic extraction
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    from_user = msg.get("from", {}) or {}
    user_id = from_user.get("id")

    # Basic validation
    if not text or not chat_id or not user_id or not bot:
        logger.debug("Ignoring message: missing text/chat/user/bot")
        return

    logger.debug("Received message from user_id=%s chat_id=%s chat_type=%s text=%s", user_id, chat_id, chat_type, text)

    # Only allow private chats (ignore group / supergroup / channel)
    if chat_type != "private":
        logger.info("Ignoring non-private chat (%s) update from chat_id=%s user_id=%s", chat_type, chat_id, user_id)
        return

    # Ignore bot messages including the bot itself
    if from_user.get("is_bot") or (BOT_ID and user_id == BOT_ID):
        logger.debug("Ignoring message from a bot or from the bot itself")
        return

    # ------- Immediate reply-keyboard buttons -------
    try:
        if text == "لقد انضممت — تحقق":
            ok, missing, _ = await check_user_membership(user_id)
            if ok:
                user_current_menu[user_id] = 0
                markup = await build_main_menu()
                if markup:
                    await safe_telegram_call(bot.send_message(
                        chat_id=chat_id,
                        text="تم التحقق — اختر القسم:",
                        reply_markup=markup
                    ))
            else:
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="لا زلت تحتاج للانضمام",
                    reply_markup=missing_chats_markup()
                ))
            return

        if text == "العودة":
            user_current_menu[user_id] = 0
            markup = await build_main_menu()
            if markup:
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="اختر القسم:",
                    reply_markup=markup
                ))
            return
    except Exception as e:
        logger.exception("Error handling immediate reply-keyboard buttons: %s", e)
        # continue gracefully

    # ------- Admin quick commands (only for admins) -------
    try:
        if user_id in ADMIN_IDS:
            if text == "إضافة زر جديد":
                admin_state[user_id] = {"action": "awaiting_add"}
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="أرسل البيانات المطلوبة بالشكل: اسم الزر|الأب_ID",
                    reply_markup=ReplyKeyboardRemove()
                ))
                return

            if text == "حذف زر":
                admin_state[user_id] = {"action": "awaiting_remove"}
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="أرسل الـ ID للزر الذي تريد حذفه",
                    reply_markup=ReplyKeyboardRemove()
                ))
                return

            if text == "عرض جميع الأزرار":
                try:
                    rows = await db_fetchall("SELECT id, name, callback_data FROM buttons ORDER BY id")
                    text_msg = "\n".join(f"{r['id']}: {r['name']} ({r['callback_data']})" for r in rows)
                    await safe_telegram_call(bot.send_message(
                        chat_id=chat_id,
                        text=text_msg or "لا توجد أزرار",
                        reply_markup=ReplyKeyboardRemove()
                    ))
                except Exception as e:
                    logger.exception("Failed to list buttons: %s", e)
                return

            if text == "رفع ملف لزر موجود":
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="هذه الميزة غير متاحة بعد",
                    reply_markup=admin_panel_markup()
                ))
                return
    except Exception as e:
        logger.exception("Error in admin quick commands: %s", e)
        # continue gracefully

    # ------- Admin interactive state (awaiting text inputs) -------
    try:
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
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text=f"تم إضافة الزر '{name}'"))
                    admin_state.pop(user_id, None)
                    # Show admin menu again
                    await safe_telegram_call(bot.send_message(
                        chat_id=chat_id,
                        text="لوحة التحكم:",
                        reply_markup=admin_panel_markup()
                    ))
                except Exception as e:
                    logger.exception("Failed to add button: %s", e)
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="خطأ في الإضافة"))
                return

            if action == "awaiting_remove":
                try:
                    bid = int(text.strip())
                    await db_execute("DELETE FROM buttons WHERE id = $1", bid)
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="تم الحذف"))
                    admin_state.pop(user_id, None)
                    # Show admin menu again
                    await safe_telegram_call(bot.send_message(
                        chat_id=chat_id,
                        text="لوحة التحكم:",
                        reply_markup=admin_panel_markup()
                    ))
                except Exception:
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="خطأ في الحذف"))
                return
    except Exception as e:
        logger.exception("Error handling admin interactive state: %s", e)
        # continue gracefully

    # ------- /start command -------
    try:
        if text.lower().startswith("/start"):
            ok, missing, reasons = await check_user_membership(user_id)
            if not ok:
                message = "✋ يلزم الانضمام إلى:\n" + "\n".join(f"- {c}" for c in missing) + "\n\nاضغط 'لقد انضممت — تحقق'"
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
                logger.exception("Failed to insert user (non-fatal)")

            user_current_menu[user_id] = 0
            markup = await build_main_menu()
            if markup:
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="مرحباً! اختر القسم:",
                    reply_markup=markup
                ))
            return
    except Exception as e:
        logger.exception("Error handling /start: %s", e)
        # continue gracefully

    # ------- Admin panel access must be checked BEFORE DB lookup -------
    try:
        if text == "الإدارة" and user_id in ADMIN_IDS:
            logger.debug("Admin panel requested by user_id=%s", user_id)
            await safe_telegram_call(bot.send_message(
                chat_id=chat_id,
                text="لوحة التحكم:",
                reply_markup=admin_panel_markup()
            ))
            return
    except Exception as e:
        logger.exception("Error showing admin panel: %s", e)
        # continue gracefully

    # ------- Database-driven menu/button handling -------
    try:
        button = await db_fetchone(
            "SELECT id, content_type, file_id, parent_id FROM buttons WHERE name = $1",
            text
        )
    except Exception as e:
        logger.exception("DB lookup failed for button '%s': %s", text, e)
        button = None

    try:
        if button:
            # If button has content, send it
            if button.get("content_type") and button.get("file_id"):
                ctype, fid = button["content_type"], button["file_id"]
                if ctype == "document":
                    await safe_telegram_call(bot.send_document(chat_id=chat_id, document=fid))
                elif ctype == "photo":
                    await safe_telegram_call(bot.send_photo(chat_id=chat_id, photo=fid))
                elif ctype == "video":
                    await safe_telegram_call(bot.send_video(chat_id=chat_id, video=fid))
                else:
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text=str(fid)))

                # Show appropriate menu after content
                if button.get("parent_id") == 0:
                    markup = await build_main_menu()
                    if markup:
                        await safe_telegram_call(bot.send_message(
                            chat_id=chat_id,
                            text="اختر القسم التالي:",
                            reply_markup=markup
                        ))
                else:
                    markup = await build_compact_submenu(button["parent_id"])
                    if markup:
                        parent_button = await db_fetchone("SELECT name FROM buttons WHERE id = $1", button["parent_id"])
                        parent_name = parent_button["name"] if parent_button else "القسم"
                        await safe_telegram_call(bot.send_message(
                            chat_id=chat_id,
                            text=f"اختر من {parent_name}:",
                            reply_markup=markup
                        ))
                return

            # This is a menu button - show its submenu
            user_current_menu[user_id] = button["id"]
            markup = await build_compact_submenu(button["id"])
            if markup:
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text=f"اختر من {text}:",
                    reply_markup=markup
                ))
                return
            else:
                # No submenu and no content
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="لا محتوى متاح حالياً",
                    reply_markup=await build_main_menu()
                ))
                return
    except Exception as e:
        logger.exception("Error handling DB-driven button: %s", e)

    # ------- Fallback (unrecognized text) -------
    try:
        logger.debug("Unrecognized text; sending main menu if available")
        main_markup = await build_main_menu()
        if main_markup:
            await safe_telegram_call(bot.send_message(
                chat_id=chat_id,
                text="اختر القسم:",
                reply_markup=main_markup
            ))
        else:
            await safe_telegram_call(bot.send_message(
                chat_id=chat_id,
                text="عذراً، لم أفهم الرسالة."
            ))
    except Exception as e:
        logger.exception("Error sending fallback/main menu: %s", e)
