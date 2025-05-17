import os
import time
import signal
import sys
import logging
import asyncio
from datetime import datetime, timedelta
from telegram.ext import PicklePersistence

import mysql.connector
from mysql.connector import pooling

from sqlalchemy.ext.asyncio import create_async_engine

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes, CallbackContext
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

# ─── CONFIG ──────────────────────────────────────────────────────
BOT_TOKEN = "7926753254:AAEzZEwNDkqwgdzDbU3F1s9YYKMQfjyIUKo"
MYSQL_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "A936522#a",
    "database": "foodreminder",
    "pool_name": "food_reminder_pool",
    "pool_size": 5,
    "pool_reset_session": True
}
SQLALCHEMY_URL = f"mysql+pymysql://{MYSQL_CONFIG['user']}:{MYSQL_CONFIG['password']}@{MYSQL_CONFIG['host']}/{MYSQL_CONFIG['database']}"
MAX_RETRIES = 3
DB_RECONNECT_INTERVAL = 60  # ثانیه

# ─── وضعیت گفتگو ─────────────────────────────────────────────────
CHOOSING = 0

# ─── دکمه‌های اصلی ───────────────────────────────────────────────
MAIN_MARKUP = ReplyKeyboardMarkup([
    ["تغییر دانشگاه", "غذای امروز؟"],
    ["غذای این هفته؟"],
], resize_keyboard=True)

# ─── تنظیمات دانشگاه‌ها ───────────────────────────────────────────
UNIVERSITY_CONFIG = {
    "خوارزمی": {
        "day_of_week": "wed",  
        "hour": 22,
        "minute": 47,
        "reminder_message": "⏰ یادآوری: ۲۴ ساعت تا پایان مهلت رزرو غذای دانشگاه خوارزمی باقی مانده!"
    },
    "تهران": {
        "day_of_week": "tue",  
        "hour": 12,
        "minute": 0,
        "reminder_message": "⏰ یادآوری: ۲۴ ساعت تا پایان مهلت رزرو غذای دانشگاه تهران باقی مانده!"
    },
}

# ─── متغیرهای سراسری ───────────────────────────────────────────
db_pool = None
bot_app = None
scheduler = AsyncIOScheduler(
    jobstores={
        'default': SQLAlchemyJobStore(url=SQLALCHEMY_URL)
    },
    job_defaults={
        'coalesce': True,  # فقط یکبار اجرا در صورت از دست دادن چندین زمان
        'misfire_grace_time': 3600  # حداکثر تاخیر مجاز: 1 ساعت
    }
)

# ─── توابع دیتابیس ────────────────────────────────────────────────
def init_db_pool():
    """ایجاد پول اتصال به دیتابیس"""
    global db_pool
    try:
        logging.info("تلاش برای ایجاد پول اتصال به دیتابیس...")
        db_pool = mysql.connector.pooling.MySQLConnectionPool(**MYSQL_CONFIG)
        
        # بررسی اتصال با گرفتن یک اتصال از پول
        conn = get_db_connection()
        if conn:
            # تست اجرای یک کوئری ساده
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            conn.close()
            logging.info("اتصال به دیتابیس با موفقیت برقرار شد")
            return True
        else:
            logging.error("نمی‌توان اتصال معتبر از پول دریافت کرد")
            return False
    except mysql.connector.Error as err:
        logging.error(f"خطا در ایجاد پول اتصال به دیتابیس: {err}")
        return False

def get_db_connection():
    """دریافت یک اتصال از پول اتصال"""
    global db_pool
    if db_pool:
        try:
            conn = db_pool.get_connection()
            if conn.is_connected():
                return conn
            # If connection exists but isn't connected
            init_db_pool()  # Try to reinitialize the pool
            return db_pool.get_connection() if db_pool else None
        except mysql.connector.Error as err:
            logging.error(f"خطا در دریافت اتصال از پول: {err}")
            return None
    return None

def execute_query(query, params=None, commit=False, fetch=None):
    """اجرای کوئری با خطایابی و تلاش مجدد"""
    retries = 0
    while retries < MAX_RETRIES:
        try:
            conn = get_db_connection()
            if not conn:
                logging.error("عدم اتصال به دیتابیس")
                time.sleep(1)
                retries += 1
                continue

            cursor = conn.cursor()
            cursor.execute(query, params)
            
            result = None
            if fetch == "one":
                result = cursor.fetchone()
            elif fetch == "all":
                result = cursor.fetchall()
            
            if commit:
                conn.commit()
            
            cursor.close()
            conn.close()
            
            return result
        except mysql.connector.Error as err:
            retries += 1
            logging.error(f"خطای دیتابیس ({retries}/{MAX_RETRIES}): {err}")
            if retries >= MAX_RETRIES:
                logging.error("حداکثر تعداد تلاش‌ها انجام شد. عملیات ناموفق بود.")
                raise
            time.sleep(1)  # کمی صبر قبل از تلاش مجدد

def create_required_tables():
    """ایجاد جداول مورد نیاز اگر وجود نداشته باشند"""
    try:
        conn = get_db_connection()
        if not conn:
            logging.error("عدم اتصال به دیتابیس هنگام ایجاد جداول")
            return False
            
        cursor = conn.cursor()
        
        users_table = """
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            university VARCHAR(50) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
        """
        
        failed_reminders_table = """
        CREATE TABLE IF NOT EXISTS failed_reminders (
            id INT AUTO_INCREMENT PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            university VARCHAR(50) NOT NULL,
            message TEXT NOT NULL,
            retry_count INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scheduled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX (chat_id)
        )
        """
        
        cursor.execute(users_table)
        cursor.execute(failed_reminders_table)
        conn.commit()
        cursor.close()
        conn.close()
        
        logging.info("جداول مورد نیاز با موفقیت ایجاد شدند")
        return True
        
    except mysql.connector.Error as err:
        logging.error(f"خطا در ایجاد جداول: {err}")
        return False

# ─── هندلرهای تلگرام ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """شروع گفتگو و انتخاب دانشگاه"""
    await update.message.reply_text(
        "👋 سلام! لطفاً دانشگاه خود را انتخاب کنید:"
        "- خوارزمی"
        "- تهران",
        reply_markup=ReplyKeyboardMarkup([["خوارزمی", "تهران"]], one_time_keyboard=True, resize_keyboard=True)
    )
    return CHOOSING

async def choose_university(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uni, chat_id = update.message.text, update.effective_chat.id
    if uni not in UNIVERSITY_CONFIG:
        await update.message.reply_text("❗ فقط «خوارزمی» یا «تهران» ممکن است.",
                                      reply_markup=MAIN_MARKUP)
        return ConversationHandler.END

    # اضافه کردن لاگ برای بررسی بهتر
    logging.info(f"ذخیره دانشگاه {uni} برای کاربر {chat_id}")

    try:
        # ذخیره/آپدیت در users
        sql = """
          INSERT INTO users(chat_id, university)
          VALUES (%s, %s)
          ON DUPLICATE KEY UPDATE university=VALUES(university)
        """
        conn = get_db_connection()
        if not conn:
            logging.error(f"خطا: عدم اتصال به پایگاه داده برای کاربر {chat_id}")
            await update.message.reply_text("❌ خطا در اتصال به پایگاه داده. لطفا دوباره تلاش کنید.",
                                          reply_markup=MAIN_MARKUP)
            return ConversationHandler.END
        
        cursor = conn.cursor()
        cursor.execute(sql, (chat_id, uni))
        conn.commit()
        cursor.close()
        conn.close()
        
        logging.info(f"دانشگاه {uni} با موفقیت برای کاربر {chat_id} ذخیره شد")
        
        # حذف Job قبلی
        try:
            scheduler.remove_job(str(chat_id))
            logging.info(f"زمانبندی قبلی برای کاربر {chat_id} حذف شد")
        except:
            logging.info(f"هیچ زمانبندی قبلی برای کاربر {chat_id} یافت نشد")
            pass

        # کانفیگ زمان‌بندی
        cfg = UNIVERSITY_CONFIG[uni]

        # افزودن Job async
        scheduler.add_job(
            send_reminder,
            trigger="cron",
            day_of_week=cfg["day_of_week"],
            hour=cfg["hour"],
            minute=cfg["minute"],
            id=str(chat_id),
            args=[chat_id, uni],
            misfire_grace_time=3600  # اجازه می‌دهد تا 1 ساعت تأخیر در اجرا قابل قبول باشد
        )
        logging.info(f"زمانبندی جدید برای کاربر {chat_id} (دانشگاه {uni}) اضافه شد")

        await update.message.reply_text(
            f"✅ دانشجوی «{uni}» ثبت شد."
            f"⏰ یادآوری هر هفته در {cfg['day_of_week']} ساعت "
            f"{cfg['hour']:02d}:{cfg['minute']:02d} فعال شد.",
            reply_markup=MAIN_MARKUP
        )
        return ConversationHandler.END
        
    except mysql.connector.Error as err:
        logging.error(f"خطای دیتابیس هنگام ذخیره دانشگاه برای کاربر {chat_id}: {err}")
        await update.message.reply_text("❌ خطا در ذخیره اطلاعات. لطفا دوباره تلاش کنید.",
                                      reply_markup=MAIN_MARKUP)
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"خطای غیرمنتظره هنگام ذخیره دانشگاه برای کاربر {chat_id}: {e}")
        await update.message.reply_text("❌ خطا در پردازش درخواست. لطفا دوباره تلاش کنید.",
                                      reply_markup=MAIN_MARKUP)
        return ConversationHandler.END
async def send_reminder(chat_id: int, university: str):
    """ارسال پیام یادآوری"""
    global bot_app
    
    if not bot_app:
        logging.error(f"نمی‌توان یادآوری برای کاربر {chat_id} ارسال کرد: ربات آماده نیست")
        return
    
    message = UNIVERSITY_CONFIG[university]["reminder_message"]
    
    retries = 0
    while retries < MAX_RETRIES:
        try:
            await bot_app.bot.send_message(
                chat_id=chat_id,
                text=message
            )
            logging.info(f"یادآوری برای کاربر {chat_id} (دانشگاه {university}) ارسال شد")
            return
        except Exception as e:
            retries += 1
            logging.error(f"خطا در ارسال یادآوری به کاربر {chat_id} (تلاش {retries}/{MAX_RETRIES}): {e}")
            if retries >= MAX_RETRIES:
                # خطا در ارسال پیام - ذخیره برای تلاش مجدد
                sql = """
                INSERT INTO failed_reminders(chat_id, university, message, scheduled_at)
                VALUES(%s, %s, %s, %s)
                """
                next_try = datetime.now() + timedelta(minutes=30)
                try:
                    execute_query(sql, (chat_id, university, message, next_try), commit=True)
                    logging.info(f"یادآوری شکست خورده برای کاربر {chat_id} برای تلاش مجدد ذخیره شد")
                except Exception as db_err:
                    logging.error(f"خطا در ذخیره یادآوری شکست خورده: {db_err}")
                return
            # صبر قبل از تلاش مجدد
            await asyncio.sleep(2)

async def retry_failed_reminders():
    """تلاش مجدد برای ارسال پیام‌های شکست خورده"""
    # Add error handling around the query execution
    try:
        sql = """
        SELECT id, chat_id, university, message, retry_count
        FROM failed_reminders
        WHERE scheduled_at <= NOW() AND retry_count < %s
        LIMIT 10
        """
        
        failed_reminders = execute_query(sql, (MAX_RETRIES,), fetch="all")
        if not failed_reminders:
            logging.info("هیچ یادآوری شکست خورده‌ای برای تلاش مجدد وجود ندارد")
            return
        
        logging.info(f"تلاش مجدد برای {len(failed_reminders)} یادآوری شکست خورده")
        
        for reminder in failed_reminders:
            reminder_id, chat_id, university, message, retry_count = reminder
            
            try:
                await bot_app.bot.send_message(
                    chat_id=chat_id,
                    text=message
                )
                logging.info(f"یادآوری با تأخیر برای کاربر {chat_id} (دانشگاه {university}) ارسال شد")
                
                # حذف از جدول پس از ارسال موفق
                delete_sql = "DELETE FROM failed_reminders WHERE id = %s"
                execute_query(delete_sql, (reminder_id,), commit=True)
                
            except Exception as e:
                # آپدیت تعداد تلاش و زمان تلاش بعدی
                logging.warning(f"تلاش مجدد {retry_count + 1} برای {chat_id} با خطا مواجه شد: {e}")
                
                if retry_count + 1 >= MAX_RETRIES:
                    logging.error(f"تعداد تلاش‌های مجدد برای کاربر {chat_id} به حداکثر رسید. یادآوری حذف می‌شود.")
                    delete_sql = "DELETE FROM failed_reminders WHERE id = %s"
                    execute_query(delete_sql, (reminder_id,), commit=True)
                else:
                    update_sql = """
                    UPDATE failed_reminders
                    SET retry_count = %s, scheduled_at = %s
                    WHERE id = %s
                    """
                    # Exponential backoff for retries
                    backoff_minutes = 30 * (2 ** retry_count)
                    next_try = datetime.now() + timedelta(minutes=backoff_minutes)
                    execute_query(update_sql, (retry_count + 1, next_try, reminder_id), commit=True)
    except Exception as e:
        logging.error(f"خطا در تابع retry_failed_reminders: {e}")

async def handle_food_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پاسخ به سوالات در مورد غذا"""
    user_id = update.effective_chat.id
    message_text = update.message.text
    
    try:
        # بررسی دانشگاه کاربر
        sql = "SELECT university FROM users WHERE chat_id = %s"
        result = execute_query(sql, (user_id,), fetch="one")
        
        if not result:
            await update.message.reply_text(
                "❗ لطفا ابتدا دانشگاه خود را انتخاب کنید.",
                reply_markup=ReplyKeyboardMarkup([["خوارزمی", "تهران"]], one_time_keyboard=True, resize_keyboard=True)
            )
            return CHOOSING
        
        university = result[0]
        websites = {
            "خوارزمی": "https://refahi.khu.ac.ir",
            "تهران": "https://dining.ut.ac.ir"
        }
        
        if "امروز" in message_text:
            await update.message.reply_text(
                f"🍽 برای مشاهده منوی امروز به وبسایت {websites[university]} مراجعه کنید.",
                reply_markup=MAIN_MARKUP
            )
        elif "هفته" in message_text:
            await update.message.reply_text(
                f"📅 برای مشاهده منوی هفتگی به وبسایت {websites[university]} مراجعه کنید.",
                reply_markup=MAIN_MARKUP
            )
        elif "تغییر دانشگاه" in message_text:
            await update.message.reply_text(
                "👨‍🎓 لطفاً دانشگاه خود را انتخاب کنید:",
                reply_markup=ReplyKeyboardMarkup([["خوارزمی", "تهران"]], one_time_keyboard=True, resize_keyboard=True)
            )
            return CHOOSING
    except Exception as e:
        logging.error(f"خطا در پردازش سوال غذا: {e}")
        await update.message.reply_text(
            "❌ خطایی رخ داد. لطفا دوباره تلاش کنید.",
            reply_markup=MAIN_MARKUP
        )
    
    return ConversationHandler.END

# ─── تنظیمات job scheduler ───────────────────────────────────────
def job_listener(event):
    """پایش وضعیت زمانبندی‌ها"""
    job = scheduler.get_job(event.job_id)
    if not job:
        return
        
    if event.code == EVENT_JOB_EXECUTED:
        logging.info(f"Job {job.id} اجرا شد.")
    elif event.exception:
        logging.error(f"خطا در اجرای job {job.id}: {event.exception}")
        
        # اگر job مربوط به یادآوری است
        if not job.id.startswith("retry_") and not job.id.startswith("db_check"):
            try:
                chat_id = int(job.id)
                args = job.args
                if len(args) >= 2:
                    university = args[1]
                    
                    # ذخیره برای تلاش مجدد
                    message = UNIVERSITY_CONFIG[university]["reminder_message"]
                    sql = """
                    INSERT INTO failed_reminders(chat_id, university, message, scheduled_at)
                    VALUES (%s, %s, %s, %s)
                    """
                    next_try = datetime.now() + timedelta(minutes=30)
                    execute_query(sql, (chat_id, university, message, next_try), commit=True)
            except Exception as inner_error:
                logging.error(f"خطا در ذخیره job شکست خورده: {inner_error}")

async def restore_jobs():
    """بازیابی زمانبندی‌ها از دیتابیس"""
    try:
        # ایجاد اطمینان از وجود جدول‌ها در دیتابیس
        engine = create_async_engine(SQLALCHEMY_URL)
        if not database_exists(SQLALCHEMY_URL):
            create_database(SQLALCHEMY_URL)
        
        # بررسی وجود job‌های قبلی در جدول apscheduler_jobs
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SHOW TABLES LIKE 'apscheduler_jobs'")
        table_exists = cursor.fetchone()
        
        if not table_exists:
            logging.warning("جدول apscheduler_jobs وجود ندارد. ایجاد job‌های جدید براساس کاربران...")
            # اگر جدول وجود ندارد، jobs را براساس کاربران در دیتابیس ایجاد می‌کنیم
            cursor.execute("SELECT chat_id, university FROM users")
            users = cursor.fetchall()
            
            for user in users:
                chat_id, university = user
                
                if university in UNIVERSITY_CONFIG:
                    cfg = UNIVERSITY_CONFIG[university]
                    # برنامه‌ریزی برای کاربر
                    scheduler.add_job(
                        send_reminder,
                        trigger="cron",
                        day_of_week=cfg["day_of_week"],
                        hour=cfg["hour"],
                        minute=cfg["minute"],
                        id=str(chat_id),
                        args=[chat_id, university],
                        replace_existing=True
                    )
                    logging.info(f"زمانبندی یادآوری برای کاربر {chat_id} (دانشگاه {university}) بازیابی شد")
            
            cursor.close()
            conn.close()
        else:
            logging.info("جدول apscheduler_jobs موجود است. زمانبندی‌های موجود استفاده می‌شوند.")
            # برای اطمینان، یک بار بررسی کنیم که آیا همه کاربران job دارند
            cursor.execute("SELECT chat_id, university FROM users")
            users = cursor.fetchall()
            
            for user in users:
                chat_id, university = user
                job = scheduler.get_job(str(chat_id))
                
                if not job and university in UNIVERSITY_CONFIG:
                    # اگر این کاربر job نداشت، اضافه کنیم
                    cfg = UNIVERSITY_CONFIG[university]
                    scheduler.add_job(
                        send_reminder,
                        trigger="cron",
                        day_of_week=cfg["day_of_week"],
                        hour=cfg["hour"],
                        minute=cfg["minute"],
                        id=str(chat_id),
                        args=[chat_id, university],
                        replace_existing=True
                    )
                    logging.info(f"زمانبندی یادآوری گمشده برای کاربر {chat_id} (دانشگاه {university}) اضافه شد")
            
            cursor.close()
            conn.close()
    except Exception as e:
        logging.error(f"خطا در بازیابی زمانبندی‌ها: {e}")

    """بازیابی زمانبندی‌ها از دیتابیس"""
    try:
        # ایجاد اطمینان از وجود جدول‌ها در دیتابیس
        engine = create_async_engine(SQLALCHEMY_URL)
        if not database_exists(SQLALCHEMY_URL):
            create_database(SQLALCHEMY_URL)
        
        # بررسی وجود job‌های قبلی در جدول apscheduler_jobs
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SHOW TABLES LIKE 'apscheduler_jobs'")
        table_exists = cursor.fetchone()
        
        if not table_exists:
            logging.warning("جدول apscheduler_jobs وجود ندارد. ایجاد job‌های جدید براساس کاربران...")
            # اگر جدول وجود ندارد، jobs را براساس کاربران در دیتابیس ایجاد می‌کنیم
            cursor.execute("SELECT chat_id, university FROM users")
            users = cursor.fetchall()
            
            for user in users:
                chat_id, university = user
                
                if university in UNIVERSITY_CONFIG:
                    cfg = UNIVERSITY_CONFIG[university]
                    
                    # برنامه‌ریزی برای کاربر
                    scheduler.add_job(
                        send_reminder,
                        trigger="cron",
                        day_of_week=cfg["day_of_week"],
                        hour=cfg["hour"],
                        minute=cfg["minute"],
                        id=str(chat_id),
                        args=[chat_id, university],
                        replace_existing=True
                    )
                    logging.info(f"زمانبندی یادآوری برای کاربر {chat_id} (دانشگاه {university}) بازیابی شد")
            
            cursor.close()
            conn.close()
        else:
            logging.info("جدول apscheduler_jobs موجود است. زمانبندی‌های موجود استفاده می‌شوند.")
            # برای اطمینان، یک بار بررسی کنیم که آیا همه کاربران job دارند
            cursor.execute("SELECT chat_id, university FROM users")
            users = cursor.fetchall()
            
            for user in users:
                chat_id, university = user
                job = scheduler.get_job(str(chat_id))
                
                if not job and university in UNIVERSITY_CONFIG:
                    # اگر این کاربر job نداشت، اضافه کنیم
                    cfg = UNIVERSITY_CONFIG[university]
                    scheduler.add_job(
                        send_reminder,
                        trigger="cron",
                        day_of_week=cfg["day_of_week"],
                        hour=cfg["hour"],
                        minute=cfg["minute"],
                        id=str(chat_id),
                        args=[chat_id, university],
                        replace_existing=True
                    )
                    logging.info(f"زمانبندی یادآوری گمشده برای کاربر {chat_id} (دانشگاه {university}) اضافه شد")
            
            cursor.close()
            conn.close()
    except Exception as e:
        logging.error(f"خطا در بازیابی زمانبندی‌ها: {e}")

async def check_db_connection():
    """بررسی پیوسته اتصال دیتابیس"""
    global db_pool
    try:
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            conn.close()
            logging.debug("اتصال به دیتابیس فعال است")
        else:
            logging.warning("اتصال به دیتابیس قطع شده است. تلاش برای اتصال مجدد...")
            init_db_pool()
    except Exception as e:
        logging.error(f"خطا در بررسی اتصال دیتابیس: {e}")
        # تلاش برای اتصال مجدد
        logging.info("تلاش برای اتصال مجدد به دیتابیس...")
        init_db_pool()

# ─── راه‌اندازی و خاموشی ────────────────────────────────────────────
async def on_startup(app):
    """عملیات راه‌اندازی"""
    await app.bot.delete_webhook()
    
    try:
        # بررسی و ایجاد جداول مورد نیاز
        create_required_tables()
        
        # بازیابی زمانبندی‌ها
        await restore_jobs()
        
        # افزودن بررسی متناوب اتصال دیتابیس
        scheduler.add_job(
            check_db_connection,
            'interval',
            minutes=5,
            id='db_check',
            replace_existing=True
        )
        
        # افزودن تلاش مجدد برای پیام‌های شکست خورده
        scheduler.add_job(
            retry_failed_reminders,
            'interval',
            hours=1,
            id='retry_failed_messages',
            replace_existing=True
        )
        
        # افزودن listener برای نظارت بر خطاهای job
        scheduler.add_listener(job_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
        
        logging.info("ربات با موفقیت راه‌اندازی شد")
    except Exception as e:
        logging.error(f"خطا در راه‌اندازی ربات: {e}")
        raise e  # Re-raise to prevent bot from starting with incomplete setup
    
def shutdown():
    """عملیات خاموش کردن"""
    logging.info("در حال خاموش کردن ربات...")
    if scheduler.running:
        scheduler.shutdown()
        logging.info("زمان‌بندی متوقف شد")
    
    # بستن اتصالات باز دیتابیس
    global db_pool
    if db_pool:
        try:
            db_pool = None
            logging.info("اتصالات دیتابیس بسته شد")
        except Exception as e:
            logging.error(f"خطا در بستن اتصالات دیتابیس: {e}")

def setup_logging():
    """تنظیم سیستم لاگ"""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    logging.getLogger('apscheduler').setLevel(logging.WARNING)

# ─── تنظیمات و راه‌اندازی ربات ───────────────────────────────────
# ─── تنظیمات و راه‌اندازی ربات ───────────────────────────────────
if __name__ == "__main__":
    # تنظیم سیستم لاگ
    setup_logging()
    
    try:
        # راه‌اندازی اتصال دیتابیس
        init_db_pool()
        
        # ایجاد جداول مورد نیاز
        create_required_tables()
        
        # اضافه کردن persistence برای ConversationHandler
        from telegram.ext import PicklePersistence
        persistence = PicklePersistence(filepath="conversation_states")
        
        # تنظیم handler های مکالمه
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("start", start),
                MessageHandler(filters.Regex(".*تغییر دانشگاه.*"), handle_food_query)
            ],
            states={
                CHOOSING: [MessageHandler(filters.Regex("^(خوارزمی|تهران)$"), choose_university)]
            },
            fallbacks=[],
            name="main_conversation",
            persistent=True
        )
        
        # افزودن persistence به application
        application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
        application.add_handler(conv_handler)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food_query))
        
        # تنظیم متغیر سراسری application
        bot_app = application
        
        # راه‌اندازی scheduler
        scheduler.start()
        
        # تنظیم callback های راه‌اندازی و خاموشی
        application.post_init = on_startup
        application.post_shutdown = shutdown
        
        # راه‌اندازی ربات
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except mysql.connector.Error as db_error:
        logging.critical(f"خطای دیتابیس هنگام راه‌اندازی: {db_error}")
        shutdown()
        sys.exit(1)
    except Exception as e:
        logging.critical(f"خطای راه‌اندازی ربات: {e}")
        shutdown()
        sys.exit(1)
