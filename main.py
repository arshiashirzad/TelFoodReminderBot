import mysql.connector
import datetime
import pytz
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext

BOT_TOKEN = "7663468482:AAHoNLFktU7OWkJsUQuFcUAy9_joi6m4-xA"

TEHRAN_TZ = pytz.timezone("Asia/Tehran")

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "A936522#a",
    "database": "food_reminder",
}


def get_db_connection():
    conn = mysql.connector.connect(**DB_CONFIG)
    return conn


def add_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = "INSERT IGNORE INTO users (user_id) VALUES (%s)"
    cursor.execute(query, (user_id,))
    conn.commit()
    conn.close()


def update_university(user_id, university):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = "UPDATE users SET university = %s, reminder_active = 1 WHERE user_id = %s"
    cursor.execute(query, (university, user_id))
    conn.commit()
    conn.close()


def get_user_data(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = "SELECT university, reminder_active FROM users WHERE user_id = %s"
    cursor.execute(query, (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result


def schedule_reminder(user_id, next_reminder):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        INSERT INTO reminders (user_id, next_reminder)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE next_reminder = %s
    """
    cursor.execute(query, (user_id, next_reminder, next_reminder))
    conn.commit()
    conn.close()


def remove_reminder(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = "DELETE FROM reminders WHERE user_id = %s"
    cursor.execute(query, (user_id,))
    conn.commit()
    conn.close()


def save_reservation(user_id):
    """ثبت رزرو کاربران در دیتابیس."""
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        INSERT INTO reservations (user_id, reservation_time)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE reservation_time = %s
    """
    now = datetime.datetime.now(TEHRAN_TZ)
    cursor.execute(query, (user_id, now, now))
    conn.commit()
    conn.close()


def get_non_reserved_users(university):
    """بازیابی کاربران یک دانشگاه که هنوز رزرو نکرده‌اند."""
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT u.user_id
        FROM users u
        LEFT JOIN reservations r ON u.user_id = r.user_id
        WHERE u.university = %s AND r.user_id IS NULL
    """
    cursor.execute(query, (university,))
    users = cursor.fetchall()
    conn.close()
    return [user[0] for user in users]  


def start_command(update: Update, context: CallbackContext):
    user_id = update.message.chat_id
    add_user(user_id)

    update.message.reply_text(
        "سلام! 👋 لطفاً دانشگاه خود را انتخاب کنید از بین موارد زیر:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏫 دانشگاه خوارزمی", callback_data="university:دانشگاه خوارزمی")],
            [InlineKeyboardButton("🏫 دانشگاه تهران", callback_data="university:دانشگاه تهران")],
            [InlineKeyboardButton("🏫 شهید بهشتی", callback_data="university:دانشگاه شهید بهشتی")],
        ])
    )


def choose_university(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    university = query.data.split(":")[1]
    user_id = query.message.chat_id

    update_university(user_id, university)

    query.edit_message_text(f"✅ دانشگاه شما تنظیم شد: {university}.\nیادآوری‌ها برای شما فعال شد.")

    schedule_weekly_reminder(context, user_id, university)


def schedule_weekly_reminder(context: CallbackContext, user_id, university):
    """Schedule weekly reminders for a user."""
    print(f"Scheduling reminder for user {user_id} with university {university}")

    days_map = {"دانشگاه خوارزمی": 3, "دانشگاه تهران": 2, "دانشگاه شهید بهشتی": 2}
    reset_hour_map = {"دانشگاه خوارزمی": (18, 0), "دانشگاه تهران": (12, 0)}

    next_day = days_map[university]
    now = datetime.datetime.now(TEHRAN_TZ)

    days_until_next = (next_day - now.weekday()) % 7
    next_reminder = now + datetime.timedelta(days=days_until_next)
    next_reminder = next_reminder.replace(
        hour=reset_hour_map[university][0], minute=reset_hour_map[university][1], second=0, microsecond=0
    )

    schedule_reminder(user_id, next_reminder)

    job = context.job_queue.run_once(
        send_reminder,
        next_reminder.timestamp(),
        context={"user_id": user_id, "university": university},
        name=f"weekly_reminder_{user_id}"
    )
    print(f"Weekly job scheduled: {job}")


def send_reminder(context: CallbackContext):
    """ارسال پیام یادآوری به کاربران."""
    try:
        user_id = context.job.context.get("user_id")
        university = context.job.context.get("university")

        print(f"Sending reminder to user {user_id} of university {university}.")

        context.bot.send_message(
            chat_id=user_id,
            text=f"⏰ وقت رزرو غذای دانشگاه {university}! لطفاً اقدام کن:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ رزرو کردم!", callback_data=f"done:{user_id}"),
                    InlineKeyboardButton("⏳ بعداً", callback_data=f"later:{user_id}")
                ]
            ])
        )

    except Exception as e:
        print(f"Error in sending reminder: {e}")


def button_click_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data.split(":")
    action, user_id = data[0], int(data[1])

    if action == "done":
        query.edit_message_text("✅ خوشحالم رزرو غذاتو انجام دادی!")
        save_reservation(user_id)
        remove_reminder(user_id)

    elif action == "later":
        query.edit_message_text("⏳ یادآوری برای ۳ ساعت آینده تنظیم شد.")


def last_hour_reminder(context: CallbackContext):
    """ارسال پیام به کاربران یک ساعت قبل از ریست، اگر رزرو نکرده باشند."""
    print("Sending last-hour reminders...")
    for university in ["دانشگاه خوارزمی", "دانشگاه تهران"]:
        users = get_non_reserved_users(university)
        for user_id in users:
            context.bot.send_message(
                chat_id=user_id,
                text="⏰ فقط ۱ ساعت وقت داری برای رزرو غذا! عجله کن!",
            )


def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start_command))
    dispatcher.add_handler(CallbackQueryHandler(choose_university, pattern="^university:"))
    dispatcher.add_handler(CallbackQueryHandler(button_click_handler, pattern="^(done|later):"))

    updater.job_queue.run_daily(
        last_hour_reminder,
        time=datetime.time(hour=17, minute=0, tzinfo=TEHRAN_TZ)
    )

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
