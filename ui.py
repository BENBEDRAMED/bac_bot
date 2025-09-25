from telegram import KeyboardButton, ReplyKeyboardMarkup
from database import db_fetchall
import logging

logger = logging.getLogger(__name__)

def create_reply_markup(button_rows, resize_keyboard=True, one_time_keyboard=False):
    """Create a ReplyKeyboardMarkup with the given button rows"""
    if not button_rows:
        return None
    
    # Create keyboard with KeyboardButton instead of InlineKeyboardButton
    keyboard = []
    for row in button_rows:
        keyboard_row = []
        for button in row:
            keyboard_row.append(KeyboardButton(button["text"]))
        keyboard.append(keyboard_row)
    
    return ReplyKeyboardMarkup(
        keyboard, 
        resize_keyboard=resize_keyboard,
        one_time_keyboard=one_time_keyboard,
        selective=False
    )

async def build_main_menu():
    try:
        rows = await db_fetchall("SELECT name, callback_data FROM buttons WHERE parent_id = 0 ORDER BY id")
        if not rows:
            return None
        
        # Convert to ReplyKeyboardMarkup format
        keyboard_rows = []
        current_row = []
        
        for i, r in enumerate(rows):
            current_row.append({"text": r["name"]})
            if len(current_row) == 2 or i == len(rows) - 1:  # 2 buttons per row
                keyboard_rows.append(current_row)
                current_row = []
        
        return create_reply_markup(keyboard_rows, resize_keyboard=True)
    except Exception as e:
        logger.error("Failed to build main menu: %s", e)
        return None

def admin_panel_markup():
    # Admin panel as ReplyKeyboardMarkup
    keyboard_rows = [
        [{"text": "إضافة زر جديد"}],
        [{"text": "حذف زر"}],
        [{"text": "رفع ملف لزر موجود"}],
        [{"text": "عرض جميع الأزرار"}],
        [{"text": "العودة"}],
    ]
    return create_reply_markup(keyboard_rows, resize_keyboard=True)

def missing_chats_markup():
    # Single button for membership check
    keyboard_rows = [[{"text": "لقد انضممت — تحقق"}]]
    return create_reply_markup(keyboard_rows, resize_keyboard=True)

async def build_compact_submenu(parent_id, buttons_per_row=2):
    try:
        subs = await db_fetchall(
            "SELECT name, callback_data FROM buttons WHERE parent_id = $1 ORDER BY id", 
            parent_id
        )
        if not subs:
            return None
            
        # Convert to ReplyKeyboardMarkup format
        keyboard_rows = []
        current_row = []
        
        for i, r in enumerate(subs):
            current_row.append({"text": r["name"]})
            if len(current_row) == buttons_per_row or i == len(subs) - 1:
                keyboard_rows.append(current_row)
                current_row = []
        
        # Add back button
        keyboard_rows.append([{"text": "العودة"}])
        
        return create_reply_markup(keyboard_rows, resize_keyboard=True)
    except Exception as e:
        logger.error("Failed to build submenu: %s", e)
        return None

# Helper function to create simple keyboard
def create_simple_keyboard(button_texts, buttons_per_row=2):
    keyboard_rows = []
    current_row = []
    
    for i, text in enumerate(button_texts):
        current_row.append({"text": text})
        if len(current_row) == buttons_per_row or i == len(button_texts) - 1:
            keyboard_rows.append(current_row)
            current_row = []
    
    return create_reply_markup(keyboard_rows, resize_keyboard=True)