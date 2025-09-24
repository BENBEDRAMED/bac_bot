from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database import db_fetchall
import logging

logger = logging.getLogger(__name__)

def rows_to_markup(rows):
    if not rows:
        return None
    keyboard = [[InlineKeyboardButton(r["name"], callback_data=r["callback_data"])] for r in rows]
    return InlineKeyboardMarkup(keyboard)

async def build_main_menu():
    try:
        rows = await db_fetchall("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id")
        return rows_to_markup(rows)
    except Exception as e:
        logger.error("Failed to build main menu: %s", e)
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