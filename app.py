import logging
import os
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from urllib.parse import urlparse

# تكوين السجل
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# بيانات المشرف (يمكنك تغييرها)
ADMIN_IDS = [7427206899]  # ضع هنا ID حسابك على تلغرام

# الحصول على متغيرات البيئة
DATABASE_URL = os.environ.get('DATABASE_URL')
BOT_TOKEN = os.environ.get('8481478915:AAEI0vcsF_6L7_5kg7_W2A2cFYYaEtgadQM')

# تهيئة قاعدة البيانات
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # جدول الأزرار
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
    
    # جدول المستخدمين
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        class_type TEXT,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # إضافة الأزرار الأساسية إذا لم تكن موجودة
    default_buttons = [
        ('العلوم', 'science', 0, None, None),
        ('الأدبي', 'literary', 0, None, None),
        ('الإدارة', 'admin_panel', 0, None, None)
    ]
    
    for name, callback, parent, c_type, file_id in default_buttons:
        cursor.execute('INSERT INTO buttons (name, callback_data, parent_id, content_type, file_id) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (callback_data) DO NOTHING', 
                      (name, callback, parent, c_type, file_id))
    
    conn.commit()
    conn.close()

# الاتصال بقاعدة البيانات
def get_db_connection():
    try:
        # تحليل رابط قاعدة البيانات
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
            port=port
        )
        return conn
    except Exception as e:
        logger.error(f"فشل الاتصال بقاعدة البيانات: {e}")
        raise e

# التحقق من صلاحية المشرف
def is_admin(user_id):
    return user_id in ADMIN_IDS

# أمر البدء
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name
    
    # حفظ المستخدم في قاعدة البيانات
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO users (user_id, first_name) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING', (user_id, first_name))
    conn.commit()
    conn.close()
    
    # عرض القائمة الرئيسية
    await show_main_menu(update, context)

# عرض القائمة الرئيسية
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT name, callback_data FROM buttons WHERE parent_id = 0')
    buttons = cursor.fetchall()
    conn.close()
    
    keyboard = []
    for name, callback_data in buttons:
        keyboard.append([InlineKeyboardButton(name, callback_data=callback_data)])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text("اختر القسم المناسب:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("مرحباً! اختر القسم المناسب:", reply_markup=reply_markup)

# معالجة الضغط على الأزرار
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    user_id = query.from_user.id
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if callback_data == 'admin_panel':
        if is_admin(user_id):
            await show_admin_panel(update, context)
        else:
            await query.edit_message_text("ليس لديك صلاحية للوصول إلى هذه الصفحة.")
        conn.close()
        return
    
    # التحقق إذا كان الزر يحتوي على محتوى
    cursor.execute('SELECT content_type, file_id FROM buttons WHERE callback_data = %s', (callback_data,))
    button_data = cursor.fetchone()
    
    if button_data and button_data[0] and button_data[1]:
        # إذا كان الزر يحتوي على محتوى، إرساله
        content_type, file_id = button_data
        if content_type == 'document':
            await context.bot.send_document(chat_id=query.message.chat_id, document=file_id)
        elif content_type == 'photo':
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=file_id)
        elif content_type == 'video':
            await context.bot.send_video(chat_id=query.message.chat_id, video=file_id)
        elif content_type == 'text':
            await query.edit_message_text(file_id)
    else:
        # إذا كان الزر مجرد قائمة فرعية، عرض الأزرار الفرعية
        cursor.execute('SELECT id FROM buttons WHERE callback_data = %s', (callback_data,))
        button_row = cursor.fetchone()
        if button_row:
            button_id = button_row[0]
            
            cursor.execute('SELECT name, callback_data FROM buttons WHERE parent_id = %s', (button_id,))
            sub_buttons = cursor.fetchall()
            
            if sub_buttons:
                keyboard = []
                for name, callback_data in sub_buttons:
                    keyboard.append([InlineKeyboardButton(name, callback_data=callback_data)])
                
                # إضافة زر العودة للقائمة الرئيسية
                keyboard.append([InlineKeyboardButton("العودة", callback_data="back_to_main")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text("اختر من القائمة:", reply_markup=reply_markup)
            else:
                await query.edit_message_text("هذه القائمة لا تحتوي على محتوى بعد.")
        else:
            await query.edit_message_text("الزر غير موجود.")
    
    conn.close()

# عرض لوحة التحكم للمشرف
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("إضافة زر جديد", callback_data="add_button")],
        [InlineKeyboardButton("رفع ملف لزر موجود", callback_data="upload_to_button")],
        [InlineKeyboardButton("عرض جميع الأزرار", callback_data="list_buttons")],
        [InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text("لوحة تحكم المشرف:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("لوحة تحكم المشرف:", reply_markup=reply_markup)

# معالجة أوامر المشرف
async def handle_admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    
    if callback_data == "add_button":
        await query.edit_message_text("أرسل اسم الزر الجديد ورقم الزر الأب بالصيغة: اسم الزر|رقم الأب (0 للقائمة الرئيسية)")
        context.user_data['awaiting_button_data'] = True
    
    elif callback_data == "upload_to_button":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, name FROM buttons')
        buttons = cursor.fetchall()
        conn.close()
        
        buttons_list = "\n".join([f"{id}: {name}" for id, name in buttons])
        await query.edit_message_text(f"أرسل رقم الزر ثم الملف بالصيغة: رقم الزر\nثم أرسل الملف بعد هذه الرسالة\n\nالأزرار المتاحة:\n{buttons_list}")
        context.user_data['awaiting_upload'] = True
    
    elif callback_data == "list_buttons":
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, name, callback_data, parent_id FROM buttons')
        buttons = cursor.fetchall()
        conn.close()
        
        buttons_list = "\n".join([f"{id}: {name} (الرمز: {callback_data}, الأب: {parent_id})" for id, name, callback_data, parent_id in buttons])
        await query.edit_message_text(f"جميع الأزرار:\n{buttons_list}")

# معالجة الرسائل النصية من المشرف
async def handle_admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    
    message_text = update.message.text
    
    if 'awaiting_button_data' in context.user_data:
        # معالجة إضافة زر جديد
        try:
            name, parent_id = message_text.split('|')
            parent_id = int(parent_id)
            
            # إنشاء رمز callback تلقائيًا
            callback_data = f"btn_{name.replace(' ', '_').lower()}"
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('INSERT INTO buttons (name, callback_data, parent_id) VALUES (%s, %s, %s)', 
                          (name, callback_data, parent_id))
            conn.commit()
            conn.close()
            
            await update.message.reply_text(f"تم إضافة الزر '{name}' بنجاح!")
            del context.user_data['awaiting_button_data']
            
        except Exception as e:
            await update.message.reply_text(f"خطأ في الصيغة. يرجى استخدام: اسم الزر|رقم الأب")
    
    elif 'awaiting_upload' in context.user_data:
        # حفظ رقم الزر مؤقتًا
        try:
            button_id = int(message_text)
            context.user_data['target_button_id'] = button_id
            await update.message.reply_text("الآن أرسل الملف الذي تريد رفعه")
        except:
            await update.message.reply_text("رقم الزر غير صحيح")

# معالجة الملفات المرسلة من المشرف
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
            cursor = conn.cursor()
            cursor.execute('UPDATE buttons SET content_type = %s, file_id = %s WHERE id = %s', 
                          (content_type, file_id, button_id))
            conn.commit()
            conn.close()
            
            await update.message.reply_text("تم ربط الملف بالزر بنجاح!")
            del context.user_data['target_button_id']
            del context.user_data['awaiting_upload']
        else:
            await update.message.reply_text("نوع الملف غير مدعوم")

# العودة للقائمة الرئيسية
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_main_menu(update, context)

def main():
    # التأكد من وجود متغيرات البيئة المطلوبة
    if not DATABASE_URL:
        logger.error("لم يتم تعيين متغير البيئة DATABASE_URL")
        return
    if not BOT_TOKEN:
        logger.error("لم يتم تعيين متغير البيئة BOT_TOKEN")
        return
    
    # تهيئة قاعدة البيانات
    init_db()
    
    # إعداد البوت
    application = Application.builder().token(BOT_TOKEN).build()
    
    # إضافة handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_button, pattern="^(?!admin_).*"))
    application.add_handler(CallbackQueryHandler(handle_admin_commands, pattern="^admin_.*"))
    application.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_to_main$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_messages))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_admin_files))
    
    # بدء البوت
    application.run_polling()

if __name__ == "__main__":
    main()