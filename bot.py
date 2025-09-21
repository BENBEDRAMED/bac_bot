import logging
import os
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from urllib.parse import urlparse
from flask import Flask, request
import json
import time

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Get environment variables
BOT_TOKEN = os.environ.get('BOT_TOKEN')
WEBHOOK_SECRET_TOKEN = os.environ.get('WEBHOOK_SECRET_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
try:
    ADMIN_IDS = [int(id.strip()) for id in os.environ.get('ADMIN_IDS', '').split(',') if id.strip()]
except ValueError as e:
    logger.error(f"Error parsing ADMIN_IDS: {e}")
    ADMIN_IDS = []

# Flask app for webhooks
app = Flask(__name__)

# Global application variable
application = None

# Database connection with retry
def get_db_connection(max_retries=3, retry_delay=5):
    for attempt in range(max_retries):
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
                connect_timeout=10
            )
            logger.info("Database connection established")
            return conn
        except Exception as e:
            logger.error(f"Failed to connect to database (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    return None

# Initialize database
def init_db():
    conn = get_db_connection()
    if conn is None:
        logger.error("Could not initialize database: Connection failed")
        return
        
    cursor = conn.cursor()
    
    # Create buttons table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS buttons (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        callback_data TEXT UNIQUE NOT NULL,
        parent_id INTEGER DEFAULT 0,
        content_type TEXT,
        file_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create users table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        class_type TEXT,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Insert default buttons
    default_buttons = [
        ('العلمي', 'science', 0, None, None),
        ('الأدبي', 'literary', 0, None, None),
        ('الإدارة', 'admin_panel', 0, None, None)
    ]
    
    for name, callback, parent, c_type, file_id in default_buttons:
        cursor.execute('INSERT INTO buttons (name, callback_data, parent_id, content_type, file_id) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (callback_data) DO NOTHING', 
                      (name, callback, parent, c_type, file_id))
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

# Check if user is admin
def is_admin(user_id):
    return user_id in ADMIN_IDS

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name
    
    # Save user to database
    conn = get_db_connection()
    if conn is None:
        await update.message.reply_text("عذراً، هناك مشكلة تقنية. يرجى المحاولة لاحقاً.")
        return
        
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (user_id, first_name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING', (user_id, first_name))
    conn.commit()
    conn.close()
    
    # Show main menu
    await show_main_menu(update, context)

# Show main menu
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    if conn is None:
        if update.callback_query:
            await update.callback_query.edit_message_text("عذراً، هناك مشكلة تقنية. يرجى المحاولة لاحقاً.")
        else:
            await update.message.reply_text("عذراً، هناك مشكلة تقنية. يرجى المحاولة لاحقاً.")
        return
        
    cursor = conn.cursor()
    cursor.execute('SELECT name, callback_data FROM buttons WHERE parent_id = 0')
    buttons = cursor.fetchall()
    conn.close()
    
    if not buttons:
        if update.callback_query:
            await update.callback_query.edit_message_text("لا توجد أقسام متاحة حالياً.")
        else:
            await update.message.reply_text("لا توجد أقسام متاحة حالياً.")
        return
    
    keyboard = [[InlineKeyboardButton(name, callback_data=callback_data)] for name, callback_data in buttons]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text("اختر القسم المناسب:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("مرحباً! اختر القسم المناسب:", reply_markup=reply_markup)

# Handle button clicks
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    user_id = query.from_user.id
    
    conn = get_db_connection()
    if conn is None:
        await query.edit_message_text("عذراً، هناك مشكلة تقنية. يرجى المحاولة لاحقاً.")
        return
        
    cursor = conn.cursor()
    
    if callback_data == 'admin_panel':
        if is_admin(user_id):
            await show_admin_panel(update, context)
        else:
            await query.edit_message_text("ليس لديك صلاحية للوصول إلى هذه الصفحة.")
        conn.close()
        return
    
    # Check if button has content
    cursor.execute('SELECT content_type, file_id FROM buttons WHERE callback_data = %s', (callback_data,))
    button_data = cursor.fetchone()
    
    if button_data and button_data[0] and button_data[1]:
        # Send content if button has associated file
        content_type, file_id = button_data
        try:
            if content_type == 'document':
                await context.bot.send_document(chat_id=query.message.chat_id, document=file_id)
            elif content_type == 'photo':
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=file_id)
            elif content_type == 'video':
                await context.bot.send_video(chat_id=query.message.chat_id, video=file_id)
            elif content_type == 'text':
                await query.edit_message_text(file_id)
        except Exception as e:
            logger.error(f"Error sending content: {e}")
            await query.edit_message_text("خطأ أثناء إرسال المحتوى. يرجى المحاولة لاحقاً.")
    else:
        # Show submenu if button is a parent
        cursor.execute('SELECT id FROM buttons WHERE callback_data = %s', (callback_data,))
        button_row = cursor.fetchone()
        if button_row:
            button_id = button_row[0]
            cursor.execute('SELECT name, callback_data FROM buttons WHERE parent_id = %s', (button_id,))
            sub_buttons = cursor.fetchall()
            
            if sub_buttons:
                keyboard = [[InlineKeyboardButton(name, callback_data=callback_data)] for name, callback_data in sub_buttons]
                keyboard.append([InlineKeyboardButton("العودة", callback_data="back_to_main")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("اختر من القائمة:", reply_markup=reply_markup)
            else:
                await query.edit_message_text("هذه القائمة لا تحتوي على محتوى بعد.")
        else:
            await query.edit_message_text("الزر غير موجود.")
    
    conn.close()

# Show admin panel
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("إضافة زر جديد", callback_data="admin_add_button")],
        [InlineKeyboardButton("رفع ملف لزر موجود", callback_data="admin_upload_to_button")],
        [InlineKeyboardButton("عرض جميع الأزرار", callback_data="admin_list_buttons")],
        [InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text("لوحة تحكم المشرف:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("لوحة تحكم المشرف:", reply_markup=reply_markup)

# Handle admin commands
async def handle_admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    
    if callback_data == "admin_add_button":
        await query.edit_message_text("أرسل اسم الزر الجديد ورقم الزر الأب بالصيغة: اسم الزر|رقم الأب (0 للقائمة الرئيسية)")
        context.user_data['awaiting_button_data'] = True
    
    elif callback_data == "admin_upload_to_button":
        conn = get_db_connection()
        if conn is None:
            await query.edit_message_text("عذراً، هناك مشكلة تقنية. يرجى المحاولة لاحقاً.")
            return
            
        cursor = conn.cursor()
        cursor.execute('SELECT id, name FROM buttons')
        buttons = cursor.fetchall()
        conn.close()
        
        if not buttons:
            await query.edit_message_text("لا توجد أزرار متاحة للربط.")
            return
        
        buttons_list = "\n".join([f"{id}: {name}" for id, name in buttons])
        await query.edit_message_text(f"أرسل رقم الزر ثم الملف بالصيغة: رقم الزر\nثم أرسل الملف بعد هذه الرسالة\n\nالأزرار المتاحة:\n{buttons_list}")
        context.user_data['awaiting_upload'] = True
    
    elif callback_data == "admin_list_buttons":
        conn = get_db_connection()
        if conn is None:
            await query.edit_message_text("عذراً، هناك مشكلة تقنية. يرجى المحاولة لاحقاً.")
            return
            
        cursor = conn.cursor()
        cursor.execute('SELECT id, name, callback_data, parent_id FROM buttons')
        buttons = cursor.fetchall()
        conn.close()
        
        if not buttons:
            await query.edit_message_text("لا توجد أزرار في قاعدة البيانات.")
            return
        
        buttons_list = "\n".join([f"{id}: {name} (الرمز: {callback_data}, الأب: {parent_id})" for id, name, callback_data, parent_id in buttons])
        await query.edit_message_text(f"جميع الأزرار:\n{buttons_list}")

# Handle admin text messages
async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    message_text = update.message.text
    
    if 'awaiting_button_data' in context.user_data:
        try:
            name, parent_id = message_text.split('|')
            parent_id = int(parent_id)
            
            # Generate unique callback data
            callback_data = f"btn_{name.replace(' ', '_').lower()}_{int(time.time())}"
            
            conn = get_db_connection()
            if conn is None:
                await update.message.reply_text("عذراً، هناك مشكلة تقنية. يرجى المحاولة لاحقاً.")
                return
                
            cursor = conn.cursor()
            cursor.execute('INSERT INTO buttons (name, callback_data, parent_id) VALUES (%s, %s, %s)', 
                          (name, callback_data, parent_id))
            conn.commit()
            conn.close()
            
            await update.message.reply_text(f"تم إضافة الزر '{name}' بنجاح!")
            del context.user_data['awaiting_button_data']
            
        except Exception as e:
            logger.error(f"Error adding button: {e}")
            await update.message.reply_text(f"خطأ في الصيغة. يرجى استخدام: اسم الزر|رقم الأب")
    
    elif 'awaiting_upload' in context.user_data:
        try:
            button_id = int(message_text)
            context.user_data['target_button_id'] = button_id
            await update.message.reply_text("الآن أرسل الملف الذي تريد رفعه")
        except ValueError:
            await update.message.reply_text("رقم الزر غير صحيح")

# Handle admin file uploads
async def handle_admin_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    if 'target_button_id' in context.user_data:
        button_id = context.user_data['target_button_id']
        file_id = None
        content_type = None
        
        if update.message.document:
            file_id = update.message.document.file_id
            content_type = 'document'
        elif update.message.photo:
            file_id = update.message.photo[-1].file_id
            content_type = 'photo'
        elif update.message.video:
            file_id = update.message.video.file_id
            content_type = 'video'
        elif update.message.text:
            file_id = update.message.text
            content_type = 'text'
        
        if file_id:
            conn = get_db_connection()
            if conn is None:
                await update.message.reply_text("عذراً، هناك مشكلة تقنية. يرجى المحاولة لاحقاً.")
                return
                
            cursor = conn.cursor()
            cursor.execute('UPDATE buttons SET content_type = %s, file_id = %s WHERE id = %s', 
                          (content_type, file_id, button_id))
            affected_rows = cursor.rowcount
            conn.commit()
            conn.close()
            
            if affected_rows > 0:
                await update.message.reply_text("تم ربط الملف بالزر بنجاح!")
            else:
                await update.message.reply_text("لم يتم العثور على الزر المحدد.")
            del context.user_data['target_button_id']
            del context.user_data['awaiting_upload']
        else:
            await update.message.reply_text("نوع الملف غير مدعوم")

# Back to main menu
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_main_menu(update, context)

# Flask routes
@app.route('/')
def index():
    return "البوت يعمل بشكل صحيح!"

@app.route('/webhook', methods=['POST'])
def webhook():
    global application
    if application is None:
        logger.error("Application not initialized")
        return 'Service Unavailable', 503
        
    try:
        update = Update.de_json(request.get_json(), application.bot)
        if update:
            application.process_update(update)
            return 'OK'
        else:
            logger.error("Invalid update received")
            return 'Invalid update', 400
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return 'Error', 500

def main():
    global application
    
    # Check required environment variables
    if not all([BOT_TOKEN, DATABASE_URL, WEBHOOK_SECRET_TOKEN]):
        logger.error("Missing required environment variables: BOT_TOKEN, DATABASE_URL, or WEBHOOK_SECRET_TOKEN")
        return
    
    # Initialize database
    init_db()
    
    # Set up bot
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_button, pattern="^(?!admin_).*"))
    application.add_handler(CallbackQueryHandler(handle_admin_commands, pattern="^admin_.*"))
    application.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_to_main$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_messages))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.TEXT, handle_admin_files))
    
    # Start webhook
    port = int(os.environ.get('PORT', 10000))
    webhook_url = os.environ.get('WEBHOOK_URL')
    
    if not webhook_url:
        logger.error("WEBHOOK_URL environment variable not set")
        return
    
    try:
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            secret_token=WEBHOOK_SECRET_TOKEN
        )
        logger.info(f"Webhook started at {webhook_url}")
    except Exception as e:
        logger.error(f"Failed to start webhook: {e}")

if __name__ == "__main__":
    main()