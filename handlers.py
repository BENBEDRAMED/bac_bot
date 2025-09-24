import time
import logging
from typing import Tuple, List, Dict, Any
from database import db_execute, db_fetchall, db_fetchone
from telegram_client import bot, BOT_ID, safe_telegram_call
from ui import build_main_menu, missing_chats_markup
from settings import ADMIN_IDS, REQUIRED_CHATS


logger = logging.getLogger(__name__)


# state and admin state are stored at server module level; handlers expect admin_state dict to be passed


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




async def process_text_message(msg: dict, admin_state: Dict[int, Dict[str, Any]]):
text = msg.get("text")
chat = msg.get("chat", {})
chat_id = chat.get("id")
from_user = msg.get("from", {})
user_id = from_user.get("id")


if not text or not chat_id or not user_id:
return


if from_user.get("is_bot") or (BOT_ID and from_user.get("id") == BOT_ID):
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
await db_execute("INSERT INTO buttons (name, callback_data, parent_id) VALUES ($1,$2,$3)", name, callback_data, parent_id)
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
