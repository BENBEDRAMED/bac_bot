import time
import logging
from typing import Dict, Any, Sequence, Optional

from database import db_execute, db_fetchone, db_fetchall
from ui import build_main_menu, missing_chats_markup, admin_panel_markup, build_compact_submenu
from telegram_client import safe_telegram_call, get_bot, get_bot_id
from settings import ADMIN_IDS, REQUIRED_CHATS
from telegram import ReplyKeyboardRemove

logger = logging.getLogger(__name__)

# State containers
admin_state: Dict[int, Dict[str, Any]] = {}
user_current_menu: Dict[int, int] = {}  # Track user's current menu level


# ---------------- Helper utilities ----------------

def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]


async def send_files_for_button(bot, chat_id: int, files: Sequence[dict]):
    """
    Send a list of files (dicts with keys: file_id, content_type, caption) to chat_id.

    Rules:
      - Batch media groups for contiguous photos/videos/animations (max 10 per media_group).
      - Send non-groupable types one by one (document, audio, etc.).
      - If send_media_group fails, fallback to single sends.
    """
    if not files:
        return

    media_group_types = {"photo", "video", "animation"}
    i = 0
    n = len(files)
    while i < n:
        f = files[i]
        ctype = (f.get("content_type") or "").lower()

        if ctype in media_group_types:
            group = []
            j = i
            while j < n and len(group) < 10 and (files[j].get("content_type") or "").lower() in media_group_types:
                group.append(files[j])
                j += 1

            if len(group) == 1:
                item = group[0]
                try:
                    if item["content_type"] == "photo":
                        await safe_telegram_call(bot.send_photo(chat_id=chat_id, photo=item["file_id"], caption=item.get("caption") or ""))
                    else:
                        await safe_telegram_call(bot.send_video(chat_id=chat_id, video=item["file_id"], caption=item.get("caption") or ""))
                except Exception:
                    logger.exception("Failed to send single media item, falling back to send_document")
                    await safe_telegram_call(bot.send_document(chat_id=chat_id, document=item["file_id"], caption=item.get("caption") or ""))
            else:
                media = []
                first = True
                for it in group:
                    media_item = {
                        "type": "photo" if it["content_type"] == "photo" else "video",
                        "media": it["file_id"],
                    }
                    if first and it.get("caption"):
                        media_item["caption"] = it["caption"]
                        first = False
                    media.append(media_item)

                try:
                    await safe_telegram_call(bot.send_media_group(chat_id=chat_id, media=media))
                except Exception as e:
                    logger.exception("send_media_group failed, falling back to single sends: %s", e)
                    for it in group:
                        try:
                            if it["content_type"] == "photo":
                                await safe_telegram_call(bot.send_photo(chat_id=chat_id, photo=it["file_id"], caption=it.get("caption") or ""))
                            else:
                                await safe_telegram_call(bot.send_video(chat_id=chat_id, video=it["file_id"], caption=it.get("caption") or ""))
                        except Exception:
                            logger.exception("Fallback single send failed for media item")

            i = j
            continue

        # Non-groupable types
        try:
            if ctype == "document":
                await safe_telegram_call(bot.send_document(chat_id=chat_id, document=f["file_id"], caption=f.get("caption") or ""))
            elif ctype == "audio":
                await safe_telegram_call(bot.send_audio(chat_id=chat_id, audio=f["file_id"], caption=f.get("caption") or ""))
            elif ctype == "voice":
                await safe_telegram_call(bot.send_voice(chat_id=chat_id, voice=f["file_id"], caption=f.get("caption") or ""))
            else:
                # Generic fallback
                await safe_telegram_call(bot.send_document(chat_id=chat_id, document=f["file_id"], caption=f.get("caption") or ""))
        except Exception:
            logger.exception("Failed to send non-groupable file, skipping")

        i += 1


# ---------------- Media extraction helper ----------------
def extract_file_from_message(msg: dict) -> Optional[dict]:
    """Return dict with keys (file_id, content_type, caption) or None if no file.
    Handles: document, photo, video, audio, animation, voice.
    For photo, chooses the largest size available.
    """
    if not msg:
        return None

    caption = msg.get("caption") or None

    if "document" in msg:
        d = msg["document"]
        return {"file_id": d["file_id"], "content_type": "document", "caption": caption}
    if "photo" in msg:
        sizes = msg["photo"]
        if sizes:
            return {"file_id": sizes[-1]["file_id"], "content_type": "photo", "caption": caption}
    if "video" in msg:
        v = msg["video"]
        return {"file_id": v["file_id"], "content_type": "video", "caption": caption}
    if "audio" in msg:
        a = msg["audio"]
        return {"file_id": a["file_id"], "content_type": "audio", "caption": caption}
    if "animation" in msg:
        a = msg["animation"]
        return {"file_id": a["file_id"], "content_type": "animation", "caption": caption}
    if "voice" in msg:
        v = msg["voice"]
        return {"file_id": v["file_id"], "content_type": "voice", "caption": caption}

    return None


# ---------------- Main handler ----------------

async def process_update(msg: dict):
    """
    Handle incoming Telegram message update. Accepts both text and media.

    New features:
      - Admins can upload a file and then give it a name (title).
      - Admins can delete a content item by sending: زر_ID|اسم_المحتوى after choosing 'حذف محتوى'.
    """
    bot = get_bot()
    BOT_ID = get_bot_id()

    text = (msg.get("text") or "").strip() if msg.get("text") else ""
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    from_user = msg.get("from", {}) or {}
    user_id = from_user.get("id")

    # Basic validation
    if not chat_id or not user_id or not bot:
        logger.debug("Ignoring message: missing chat/user/bot")
        return

    logger.debug("Received message from user_id=%s chat_id=%s chat_type=%s text=%s", user_id, chat_id, chat_type, text)

    # Only private chats
    if chat_type != "private":
        logger.info("Ignoring non-private chat (%s) update", chat_type)
        return

    # Ignore bot messages
    if from_user.get("is_bot") or (BOT_ID and user_id == BOT_ID):
        logger.debug("Ignoring message from a bot or from the bot itself")
        return

    # -------- Admin: handle file upload -> then ask for name --------
    file_info = extract_file_from_message(msg)
    if file_info and user_id in admin_state and admin_state[user_id].get("action") == "awaiting_upload":
        state = admin_state[user_id]
        target_button = state.get("target_button")
        if not target_button:
            await safe_telegram_call(bot.send_message(chat_id=chat_id, text="لم يتم تحديد زر الهدف. أرسل ID الزر أولاً."))
            return

        try:
            # Insert and return id so we can ask name
            row = await db_fetchone(
                "INSERT INTO media_files (button_id, file_id, content_type, caption, sort_order, name) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
                target_button, file_info["file_id"], file_info["content_type"], file_info.get("caption"), 0, None
            )
            new_id = row["id"] if row else None
            # move to naming step for this uploaded file
            admin_state[user_id] = {
                "action": "awaiting_name",
                "target_button": target_button,
                "last_media_id": new_id
            }
            await safe_telegram_call(bot.send_message(chat_id=chat_id, text="تم رفع الملف. أرسل اسم المحتوى لهذا الملف الآن (أو اكتب 'تخطى' لترك الاسم فارغاً).", reply_markup=admin_panel_markup()))
        except Exception as e:
            logger.exception("Failed to insert media file: %s", e)
            await safe_telegram_call(bot.send_message(chat_id=chat_id, text="فشل رفع الملف."))
        return

    # -------- Admin: handle naming the last uploaded file --------
    if user_id in admin_state and admin_state[user_id].get("action") == "awaiting_name":
        # Expecting a text name for the last uploaded media
        if text:
            st = admin_state[user_id]
            last_media_id = st.get("last_media_id")
            if text.strip().lower() == "تخطى":
                # leave name null/empty and return to upload mode
                await db_execute("UPDATE media_files SET name = NULL WHERE id = $1", last_media_id)
                admin_state[user_id] = {"action": "awaiting_upload", "target_button": st.get("target_button")}
                await safe_telegram_call(bot.send_message(chat_id=chat_id, text="تم حفظ الملف بدون اسم. أرسل ملف آخر أو اكتب 'انتهيت' لإنهاء.", reply_markup=admin_panel_markup()))
                return

            try:
                # Save the provided name
                await db_execute("UPDATE media_files SET name = $1 WHERE id = $2", text.strip(), last_media_id)
                # go back to upload mode so admin can add more files
                admin_state[user_id] = {"action": "awaiting_upload", "target_button": st.get("target_button")}
                await safe_telegram_call(bot.send_message(chat_id=chat_id, text=f\"تم حفظ الاسم: {text.strip()}. أرسل ملف آخر أو اكتب 'انتهيت' لإنهاء.\", reply_markup=admin_panel_markup()))
            except Exception as e:
                logger.exception(\"Failed to update media_files.name: %s\", e)
                await safe_telegram_call(bot.send_message(chat_id=chat_id, text=\"فشل حفظ الاسم.\"))

        else:
            await safe_telegram_call(bot.send_message(chat_id=chat_id, text=\"أرسل اسم المحتوى كنص أو اكتب 'تخطى' لتركه فارغاً.\"))
        return

    # If admin typed 'انتهيت' while in upload mode -> finish
    if text == "انتهيت" and user_id in admin_state and admin_state[user_id].get("action") in ("awaiting_upload", "awaiting_name"):
        admin_state.pop(user_id, None)
        await safe_telegram_call(bot.send_message(chat_id=chat_id, text="انتهى الرفع.", reply_markup=admin_panel_markup()))
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
                admin_state[user_id] = {"action": "awaiting_upload_select"}
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="أرسل ID الزر أو اسم الزر الذي تريد رفع ملفات له:",
                    reply_markup=ReplyKeyboardRemove()
                ))
                return

            if text == "حذف محتوى":
                admin_state[user_id] = {"action": "awaiting_delete"}
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text="أرسل حذف المحتوى بالشكل: زر_ID|اسم_المحتوى   (مثال: 42|شرح_الفصل_الأول)",
                    reply_markup=ReplyKeyboardRemove()
                ))
                return
    except Exception as e:
        logger.exception("Error in admin quick commands: %s", e)

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
                    callback_data = f"btn_{int(time.time())}_{abs(hash(name))}"
                    await db_execute("INSERT INTO buttons (name, callback_data, parent_id) VALUES ($1,$2,$3)",
                                     name, callback_data, parent_id)
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text=f"تم إضافة الزر '{name}'"))
                    admin_state.pop(user_id, None)
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
                    await safe_telegram_call(bot.send_message(
                        chat_id=chat_id,
                        text="لوحة التحكم:",
                        reply_markup=admin_panel_markup()
                    ))
                except Exception:
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="خطأ في الحذف"))
                return

            if action == "awaiting_upload_select" and text:
                # admin provided an ID or name for the target button
                try:
                    target_button = None
                    try:
                        bid = int(text.strip())
                        row = await db_fetchone("SELECT id, name FROM buttons WHERE id = $1", bid)
                        if row:
                            target_button = row['id']
                    except Exception:
                        # not an int; try name
                        row = await db_fetchone("SELECT id, name FROM buttons WHERE name = $1", text.strip())
                        if row:
                            target_button = row['id']

                    if not target_button:
                        await safe_telegram_call(bot.send_message(chat_id=chat_id, text="لم أجد زر مطابق. أعد المحاولة أو أرسل 'الغاء'"))
                        return

                    admin_state[user_id] = {"action": "awaiting_upload", "target_button": target_button}
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text=f"أرسل الملفات الآن. ستنضاف إلى الزر id={target_button}. أرسل 'انتهيت' عند الانتهاء.", reply_markup=admin_panel_markup()))
                except Exception as e:
                    logger.exception("Error selecting target button for upload: %s", e)
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="خطأ عند البحث عن الزر."))
                return

            if action == "awaiting_delete" and text and "|" in text:
                # Expecting "button_id|content_name"
                try:
                    bid_str, content_name = text.split("|", 1)
                    bid = int(bid_str.strip())
                    cname = content_name.strip()
                    # Delete the specified named content for the given button
                    res = await db_execute("DELETE FROM media_files WHERE button_id = $1 AND name = $2", bid, cname)
                    # db_execute returns status like 'DELETE 1' or similar; we can confirm existence by checking rows.
                    # To be explicit, try to fetch after delete to verify 0 rows left with that name:
                    remaining = await db_fetchall("SELECT id FROM media_files WHERE button_id = $1 AND name = $2", bid, cname)
                    if remaining:
                        # something unusual: still present
                        await safe_telegram_call(bot.send_message(chat_id=chat_id, text="حدث خطأ — لم يتم حذف المحتوى بالكامل. تفقد القاعدة."))
                    else:
                        await safe_telegram_call(bot.send_message(chat_id=chat_id, text=f"تم حذف المحتوى '{cname}' من الزر id={bid}.", reply_markup=admin_panel_markup()))
                    admin_state.pop(user_id, None)
                except ValueError:
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="معطل: أول جزء يجب أن يكون رقم الـ ID. مثال: 42|شرح_الفصل_الأول"))
                except Exception as e:
                    logger.exception("Failed to delete media by name: %s", e)
                    await safe_telegram_call(bot.send_message(chat_id=chat_id, text="فشل حذف المحتوى. تأكد من أن الاسم مطابق تماماً."))
                return

    except Exception as e:
        logger.exception("Error handling admin interactive state: %s", e)

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

    # ------- Database-driven menu/button handling -------
    try:
        button = await db_fetchone(
            "SELECT id, parent_id FROM buttons WHERE name = $1",
            text
        )
    except Exception as e:
        logger.exception("DB lookup failed for button '%s': %s", text, e)
        button = None

    try:
        if button:
            # fetch media files for this button
            rows = await db_fetchall(
                "SELECT file_id, content_type, caption FROM media_files WHERE button_id = $1 ORDER BY sort_order, id",
                button["id"]
            )
            files = [
                {"file_id": r["file_id"], "content_type": (r["content_type"] or "document"), "caption": (r["caption"] or "")}
                for r in rows
            ]

            if files:
                await send_files_for_button(bot, chat_id, files)

                # show menu after content
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

            # No media files: treat as menu button (show submenu)
            user_current_menu[user_id] = button["id"]
            markup = await build_compact_submenu(button["id"])
            if markup:
                # original button text is in `text` variable
                await safe_telegram_call(bot.send_message(
                    chat_id=chat_id,
                    text=f"اختر من {text}:",
                    reply_markup=markup
                ))
                return
            else:
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


# Keep backward-compatible alias for existing code that imports process_text_message
process_text_message = process_update
