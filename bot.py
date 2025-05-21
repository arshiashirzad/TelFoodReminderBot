import re
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from bs4 import BeautifulSoup
from datetime import datetime
from telegram.ext import Application

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
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy import Column, Integer, String

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes, CallbackContext
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from dotenv import load_dotenv
load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MYSQL_CONFIG = {
    "host": os.getenv("MYSQLHOST"),
    "port": int(os.getenv("MYSQLPORT", "3306")),
    "user": os.getenv("MYSQLUSER"),
    "password": os.getenv("MYSQLPASSWORD"),
    "database": os.getenv("MYSQLDATABASE"),
    "pool_size": 5,
    "pool_name": "mypool",
    "connect_timeout": 30
}

SQLALCHEMY_URL = (
    f"mysql+pymysql://{MYSQL_CONFIG['user']}:{MYSQL_CONFIG['password']}"
    f"@{MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}"
)

MAX_RETRIES = 3
DB_RECONNECT_INTERVAL = 60  # seconds


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
        "hour": 12,
        "minute": 0,
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
try:
    # For Python 3.9+
    from zoneinfo import ZoneInfo
    tehran_tz = ZoneInfo("Asia/Tehran")
except ImportError:
    # For older versions
    import pytz
    tehran_tz = pytz.timezone("Asia/Tehran")

db_pool = None
bot_app = None
scheduler = AsyncIOScheduler(
    jobstores={
        'default': SQLAlchemyJobStore(url=SQLALCHEMY_URL)
    },
    job_defaults={
        'coalesce': True,  # فقط یکبار اجرا در صورت از دست دادن چندین زمان
        'misfire_grace_time': 3600  # حداکثر تاخیر مجاز: 1 ساعت
    },
    timezone=tehran_tz
)


# ─── توابع دیتابیس ────────────────────────────────────────────────
def init_db_pool():
    """ایجاد پول اتصال به دیتابیس"""
    global db_pool
    try:
        logging.info("Try to connect to database pool (create)")
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
            logging.info("connected to database succesfully")
            return True
        else:
            logging.error("cannot connect to db")
            return False
    except mysql.connector.Error as err:
        logging.error(f"fatal error in connecting to pool{err}")
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
            logging.error(f"pool error {err}")
            return None
    return None


def execute_query(query, params=None, commit=False, fetch=None):
    """اجرای کوئری با خطایابی و تلاش مجدد"""
    retries = 0
    while retries < MAX_RETRIES:
        try:
            conn = get_db_connection()
            if not conn:
                logging.error("database connection error")
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
                logging.error("maximum tries failed.")
                raise
            time.sleep(1)  # کمی صبر قبل از تلاش مجدد


def create_required_tables():
    """ایجاد جداول مورد نیاز اگر وجود نداشته باشند"""
    try:
        conn = get_db_connection()
        if not conn:
            logging.error("fatal error in creating tables (connection error)")
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

        logging.info("tables created successfully")
        return True

    except mysql.connector.Error as err:
        logging.error(f"fatal in creating tables: {err}")
        return False


def clean_food_name(food):
    return re.sub(r"(،|\(|\[)?\s*(رایگان|\d{2,3}(,\d{3})?)\s*(تومان|ریال)?\)?$", "", food).strip()

def get_today_name():
    """دریافت نام روز هفته امروز به فارسی"""
    today = datetime.now()
    weekday = today.weekday()

    days_mapping = {
        0: "دوشنبه",
        1: "سه‌شنبه",
        2: "چهارشنبه",
        3: "پنجشنبه",
        4: "جمعه",
        5: "شنبه",
        6: "یکشنبه"
    }

    return days_mapping[weekday]


def parse_food_schedule(html, university=None):
    try:
        soup = BeautifulSoup(html, "html.parser")
        schedule = {}

        day_containers = soup.find_all("div", class_="dayContainer")

        for day_container in day_containers:
            day_span = day_container.find(class_="day")
            date_span = day_container.find(class_="date")

            if day_span:
                day_name = day_span.get_text(strip=True)
                date = date_span.get_text(strip=True) if date_span else ""

                schedule[day_name] = {
                    "تاریخ": date,
                    "صبحانه": [],
                    "ناهار": [],
                    "شام": []
                }

                current_element = day_container

                while True:
                    current_element = current_element.find_next_sibling()
                    if not current_element or (
                            current_element.get('class') and 'dayContainer' in current_element.get('class')):
                        break

                    time_meal = current_element.find("span", class_="TimeMeal")
                    current_meal_type = None

                    if time_meal:
                        meal_text = time_meal.get_text(strip=True).lower()
                        if "صبحانه" in meal_text:
                            current_meal_type = "صبحانه"
                        elif "ناهار" in meal_text or "نهار" in meal_text:
                            current_meal_type = "ناهار"
                        elif "شام" in meal_text:
                            current_meal_type = "شام"

                    if current_meal_type:
                        meal_divs = current_element.find_all("div", id="MealDiv")
                        for meal_div in meal_divs:
                            food_labels = meal_div.find_all("label", class_="reserveFoodCheckBox")
                            for label in food_labels:
                                if label.get('for') and label.get_text(strip=True):
                                    food_text = clean_food_name(label.get_text(strip=True))
                                    if food_text and food_text not in schedule[day_name][current_meal_type]:
                                        schedule[day_name][current_meal_type].append(food_text)

        return schedule

    except Exception as e:
        print(f"خطا در خواندن برنامه غذایی: {e}")
        return {
            day: {"تاریخ": "", "صبحانه": [], "ناهار": [], "شام": []}
            for day in ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه"]
        }



def merge_weekly_menus(menu1, menu2):
    merged_menu = {}

    # ترتیب روزهای هفته به فارسی
    days_order = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]

    # ترکیب همه روزهای موجود در هر دو منو
    all_days = set(menu1.keys()) | set(menu2.keys())

    # ایجاد دیکشنری مرتب شده بر اساس ترتیب روزهای هفته
    for day in days_order:
        if day in all_days:
            merged_menu[day] = {
                'تاریخ': menu1.get(day, {}).get('تاریخ', '') or menu2.get(day, {}).get('تاریخ', ''),
                'صبحانه': menu1.get(day, {}).get('صبحانه', []),
                'ناهار': menu1.get(day, {}).get('ناهار', []),
                'شام': menu2.get(day, {}).get('شام', [])
            }
    return merged_menu


def format_meals(meals):
    """قالب‌بندی وعده‌های غذایی"""
    if not meals:
        return "⚠️ اطلاعات منو موجود نیست"

    message = ""
    message += "🍳 صبحانه:\n"
    message += "".join(f"    • {f}\n" for f in meals['صبحانه']) or "    • موجود نیست\n"
    message += "🍛 ناهار:\n"
    message += "".join(f"    • {f}\n" for f in meals['ناهار']) or "    • موجود نیست\n"
    message += "🍲 شام:\n"
    message += "".join(f"    • {f}\n" for f in meals['شام']) or "    • موجود نیست\n"
    return message


async def handle_food_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پردازش سوالات مربوط به منوی غذا"""
    try:
        chat_id = update.effective_chat.id
        message_text = update.message.text.lower()
        is_today = "امروز" in message_text or "today" in message_text

        # دریافت دانشگاه کاربر
        university_result = execute_query(
            "SELECT university FROM users WHERE chat_id = %s",
            (chat_id,),
            fetch="one"
        )

        if not university_result:
            await update.message.reply_text(
                "ابتدا باید دانشگاه خود را انتخاب کنید. از دستور /start استفاده کنید.",
                reply_markup=MAIN_MARKUP
            )
            return

        university = university_result[0]

        # دریافت منوی غذا
        try:
            if university == "خوارزمی":
                with open("kharazmi_menu.html", "r", encoding="utf-8") as f:
                    html = f.read()
            else:
                with open("tehran_menu_lunch.html", "r", encoding="utf-8") as f:
                    html_lunch = f.read()
                with open("tehran_menu_dinner.html", "r", encoding="utf-8") as f:
                    html_dinner = f.read()
        except FileNotFoundError:
            logging.error(f"فایل منوی {university} یافت نشد.")
            await update.message.reply_text(
                f"اطلاعات منوی دانشگاه {university} در دسترس نیست. لطفاً بعداً تلاش کنید.",
                reply_markup=MAIN_MARKUP
            )
            return

        # پردازش HTML و استخراج برنامه غذایی
        if(university=="خوارزمی"):
            schedule = parse_food_schedule(html, university)
        else:
            temp1 = parse_food_schedule(html_lunch, university)
            temp2 = parse_food_schedule(html_dinner, university)
            schedule =merge_weekly_menus(temp1, temp2)

        if is_today:
            today_name = get_today_name()
            if today_name == "جمعه":
                await update.message.reply_text("📵 امروز (جمعه) غذا سرو نمی‌شود.", reply_markup=MAIN_MARKUP)
                return

            # بررسی برای دانشگاه تهران
            if university == "تهران" and today_name not in schedule:
                await update.message.reply_text(f"📵 امروز ({today_name}) در دانشگاه تهران غذا سرو نمی‌شود.",
                                                reply_markup=MAIN_MARKUP)
                return



            meals = schedule.get(today_name, {})
            response = f"🍽 منوی امروز ({today_name}) دانشگاه {university}:\n\n"
            response += format_meals(meals)
        else:

            response = f"🗓 منوی هفته جاری دانشگاه {university}:\n\n"
            for day, meals in schedule.items():
                response += f"📅 {day}:\n{format_meals(meals)}\n\n"

        await update.message.reply_text(response, reply_markup=MAIN_MARKUP)

    except Exception as e:
        logging.error(f"خطا در پردازش سوال غذا: {e}")
        await update.message.reply_text(
            "متأسفانه در دریافت اطلاعات غذا مشکلی پیش آمد. لطفا دوباره تلاش کنید.",
            reply_markup=MAIN_MARKUP
        )


# ─── دستورات مستقیم برای غذا ────────────────────────────────────────
async def today_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور مستقیم برای نمایش غذای امروز"""
    # افزودن کلمه "امروز" به متن تا handle_food_query متوجه شود
    update.message.text = "غذای امروز"
    await handle_food_query(update, context)


async def week_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور مستقیم برای نمایش غذای هفته"""
    update.message.text = "غذای این هفته"
    await handle_food_query(update, context)


def setup_food_handlers(application):
    """ثبت تمام هندلرهای مربوط به غذا"""
    application.add_handler(CommandHandler("today", today_food))
    application.add_handler(CommandHandler("week", week_food))
    application.add_handler(MessageHandler(filters.Regex(r'^(غذای امروز|منوی امروز)$'), today_food))
    application.add_handler(MessageHandler(filters.Regex(r'^(غذای هفته|منوی هفته)$'), week_food))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food_query))


# ─── توابع لاگینگ و زمان‌بندی ───────────────────────────────────────
def setup_logging():
    """تنظیم سیستم لاگ"""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    logging.getLogger('apscheduler').setLevel(logging.WARNING)


async def job_listener(event):
    """گوش دادن به رویدادهای job scheduler"""
    if event.exception:
        logging.error(f"Job ID : {event.job_id} error occured {event.exception}")
    else:
        logging.info(f"Job ID: {event.job_id} done successfully")


async def send_reminder(chat_id, message, university):
    """ارسال یادآوری به کاربر"""
    try:
        await bot_app.bot.send_message(chat_id=chat_id, text=message)
        logging.info(f"sent reminder to : {chat_id} ( {university})")
    except Exception as e:
        logging.error(f" failed to send reminder to :{chat_id}: {e}")
        # ذخیره یادآوری ناموفق برای تلاش مجدد
        try:
            execute_query(
                "INSERT INTO failed_reminders (chat_id, university, message) VALUES (%s, %s, %s)",
                (chat_id, university, message),
                commit=True
            )
            logging.info(f"reminder succeffully saved in db for :{chat_id}")
        except Exception as db_err:
            logging.error(f"error occured in saving reminder:{db_err}")


def schedule_reminder_for_user(chat_id, university):
    """برای کاربر یادآوری تنظیم می‌کند"""
    if university not in UNIVERSITY_CONFIG:
        logging.error(f"university not true:{university}")
        return

    config = UNIVERSITY_CONFIG[university]

    # حذف یادآوری‌های قبلی برای این کاربر
    for job in scheduler.get_jobs():
        if job.id.startswith(f"reminder_{chat_id}_"):
            scheduler.remove_job(job.id)

    job_id = f"reminder_{chat_id}_{university}"

    # برای تست: اجرای هر دقیقه (در محیط واقعی باید به تنظیمات اصلی تغییر کند)

    # scheduler.add_job(
    #     send_reminder,
    #     'cron',
    #     minute='*',  # هر دقیقه اجرا شود (فقط برای تست)
    #     id=job_id,
    #     kwargs={
    #         'chat_id': chat_id,
    #         'message': f"پیام تست یادآوری برای دانشگاه {university} - {config['reminder_message']}",
    #         'university': university
    #     }
    # )
    #
    scheduler.add_job(
        send_reminder,
        'cron',
        day_of_week=config['day_of_week'],
        hour=config['hour'],
        minute=config['minute'],
        id=job_id,
        kwargs={
            'chat_id': chat_id,
            'message': config['reminder_message'],
            'university': university
        }
    )

    logging.info(f"reminder of {chat_id} added to cron")


async def retry_failed_reminders():
    """تلاش مجدد برای ارسال یادآوری‌های ناموفق"""
    try:
        failed_reminders = execute_query(
            "SELECT id, chat_id, university, message, retry_count FROM failed_reminders WHERE retry_count < %s",
            (MAX_RETRIES,),
            fetch="all"
        )

        if not failed_reminders:
            logging.info("no failed reminders exist")
            return

        logging.info(f"trying again to send reminder to :{len(failed_reminders)}  ")

        for reminder in failed_reminders:
            reminder_id, chat_id, university, message, retry_count = reminder

            try:
                await bot_app.bot.send_message(chat_id=chat_id, text=message)
                logging.info(f"trying again to send reminder to{reminder_id} user :{chat_id}")

                # حذف از لیست ناموفق‌ها
                execute_query(
                    "DELETE FROM failed_reminders WHERE id = %s",
                    (reminder_id,),
                    commit=True
                )

            except Exception as e:
                logging.warning(f"retry to remind to :{reminder_id}: {e}")

                # افزایش شمارنده تلاش
                new_retry_count = retry_count + 1
                execute_query(
                    "UPDATE failed_reminders SET retry_count = %s WHERE id = %s",
                    (new_retry_count, reminder_id),
                    commit=True
                )

                if new_retry_count >= MAX_RETRIES:
                    logging.error(f"max retries reached for :{reminder_id} ")

    except Exception as e:
        logging.error(f"fatal error in process: {e}")


async def on_startup(application):
    """تنظیمات راه‌اندازی ربات"""
    # راه‌اندازی scheduler
    scheduler.add_listener(job_listener, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
    scheduler.start()
    logging.info("schedueled successfully")

    # نمایش تمام job های تنظیم شده
    jobs = scheduler.get_jobs()
    logging.info(f"count : {len(jobs)} jobs scheduled :")
    for job in jobs:
        logging.info(f"Job ID: {job.id}, Next run: {job.next_run_time}")

    # تنظیم مجدد یادآوری‌ها برای همه کاربران
    try:
        users = execute_query("SELECT chat_id, university FROM users", fetch="all")
        if users:
            for user in users:
                chat_id, university = user
                schedule_reminder_for_user(chat_id, university)
            logging.info(f"reminders set for : {len(users)} users")
        else:
            logging.info("no one exisiting in databse")
    except Exception as e:
        logging.error(f"fatal error in rescheduling reminders: {e}")

    # بررسی و تلاش مجدد برای ارسال یادآوری‌های ناموفق
    await retry_failed_reminders()


async def shutdown(application):
    """تنظیمات خاموشی ربات"""
    logging.info("MACHINE IS OFF")

    # توقف زمان‌بند
    if scheduler.running:
        scheduler.shutdown()
        logging.info("SCHEDULING STOPPED")

    # بستن پول اتصال به دیتابیس
    global db_pool
    if db_pool:
        logging.info("CLOSED POOL OF DB")
        db_pool = None


# ─── هندلرهای تلگرام ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"دریافت دستور start از {update.effective_chat.id}")
    try:
        await update.message.reply_text(
            "👋 سلام! لطفاً دانشگاه خود را انتخاب کنید:",
            reply_markup=ReplyKeyboardMarkup(
                [["خوارزمی", "تهران"]],
                one_time_keyboard=True,
                resize_keyboard=True
            )
        )
        return CHOOSING
    except Exception as e:
        logging.error(f"error on start: {e}")
        raise


async def choose_university(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """انتخاب دانشگاه توسط کاربر"""
    uni = update.message.text
    chat_id = update.effective_chat.id

    # اطمینان از اینکه دانشگاه انتخاب شده معتبر است
    if uni not in UNIVERSITY_CONFIG:
        await update.message.reply_text("دانشگاه انتخابی معتبر نیست، لطفا دوباره انتخاب کنید:")
        return CHOOSING

    # ذخیره انتخاب کاربر در دیتابیس
    try:
        execute_query(
            "INSERT INTO users (chat_id, university) VALUES (%s, %s) ON DUPLICATE KEY UPDATE university = %s",
            (chat_id, uni, uni),
            commit=True
        )

        # تنظیم یادآوری برای کاربر
        schedule_reminder_for_user(chat_id, uni)

        await update.message.reply_text(
            f"دانشگاه {uni} انتخاب شد. من به شما یادآوری رزرو غذا را ارسال خواهم کرد.",
            reply_markup=MAIN_MARKUP
        )
        return ConversationHandler.END

    except Exception as e:
        logging.error(f"خطا در ذخیره انتخاب دانشگاه: {e}")
        await update.message.reply_text("مشکلی پیش آمد. لطفا دوباره تلاش کنید.")
        return CHOOSING




# ─── تنظیمات و راه‌اندازی ربات ───────────────────────────────────
if __name__ == "__main__":
    # تنظیم سیستم لاگ
    setup_logging()

    try:
        # راه‌اندازی اتصال دیتابیس
        if not init_db_pool():
            logging.critical("failed to connect to database")
            sys.exit(1)

        # ایجاد جداول مورد نیاز
        if not create_required_tables():
            logging.critical("failed to create requried tables")
            sys.exit(1)

        # اضافه کردن persistence برای ConversationHandler
        persistence = PicklePersistence(filepath="conversation_states")

        # تنظیم handler های مکالمه
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("start", start),
                MessageHandler(filters.Regex(r'^(تغییر دانشگاه|انتخاب دانشگاه)$'), start)
            ],
            states={
                CHOOSING: [
                    MessageHandler(filters.Regex(r'^(خوارزمی|تهران)$'), choose_university)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", lambda u, c: ConversationHandler.END)
            ],
            name="university_choice",
            persistent=True
        )
        # افزودن persistence به application
        application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
        application.add_handler(conv_handler)

        # اضافه کردن handler برای پاسخ به پرسش‌های غذا
        application.add_handler(MessageHandler(filters.Regex(".*غذای امروز.*"), handle_food_query))
        application.add_handler(MessageHandler(filters.Regex(".*غذای این هفته.*"), handle_food_query))

        # هندلر پیش‌فرض برای پیام‌های دیگر
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food_query))

        # تنظیم متغیر سراسری application
        bot_app = application

        # تنظیم callback های راه‌اندازی و خاموشی
        application.post_init = on_startup
        application.post_shutdown = shutdown

        # اجرای ربات
        logging.info("MACHINE RUNNING")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except mysql.connector.Error as db_error:
        logging.critical(f"DATABASE ERROR ON START: {db_error}")
        asyncio.run(shutdown())
        sys.exit(1)
    except Exception as e:
        logging.critical(f"FAILED TO START MACHINE: {e}")
        asyncio.run(shutdown())
        sys.exit(1)
