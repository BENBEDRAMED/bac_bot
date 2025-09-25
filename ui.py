from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database import db_fetchall
import logging

logger = logging.getLogger(__name__)

def rows_to_markup(rows, buttons_per_row=2, back_button=True):
    if not rows:
        return None
    
    # Create compact keyboard with multiple buttons per row
    keyboard = []
    row = []
    
    for i, r in enumerate(rows):
        row.append(InlineKeyboardButton(r["name"], callback_data=r["callback_data"]))
        if len(row) == buttons_per_row or i == len(rows) - 1:
            keyboard.append(row)
            row = []
    
    # Add back button at the bottom
    if back_button:
        keyboard.append([InlineKeyboardButton("العودة", callback_data="back_to_main")])
    
    return InlineKeyboardMarkup(keyboard)

async def build_main_menu():
    try:
        rows = await db_fetchall("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id")
        # For main menu, use 2 buttons per row, no back button
        if not rows:
            return None
        
        keyboard = []
        row = []
        
        for i, r in enumerate(rows):
            row.append(InlineKeyboardButton(r["name"], callback_data=r["callback_data"]))
            if len(row) == 2 or i == len(rows) - 1:  # 2 buttons per row
                keyboard.append(row)
                row = []
        
        return InlineKeyboardMarkup(keyboard)
    except Exception as e:
        logger.error("Failed to build main menu: %s", e)
        return None

def admin_panel_markup():
    # Compact admin panel with 2 buttons per row
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

# New function for compact submenu layout
async def build_compact_submenu(parent_id, buttons_per_row=2):
    try:
        subs = await db_fetchall(
            "SELECT name, callback_data FROM buttons WHERE parent_id = $1 ORDER BY id", 
            parent_id
        )
        if not subs:
            return None
            
        return rows_to_markup(subs, buttons_per_row=buttons_per_row, back_button=True)
    except Exception as e:
        logger.error("Failed to build submenu: %s", e)
        return None