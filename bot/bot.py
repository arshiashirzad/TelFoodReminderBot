from telegram.ext import Updater
from bot.handlers import register_handlers
from bot.notifications import schedule_notifications
from bot.database import Database

class FoodReminderBot:
    def __init__(self, token, db_path):
        self.token = token
        self.db = Database(db_path)
        self.updater = Updater(token=self.token, use_context=True)

        # Add handlers
        register_handlers(self.updater.dispatcher, self.db)

        # Schedule notifications
        schedule_notifications(self.updater.job_queue, self.db)

    def run(self):
        print("Bot is starting...")
        self.updater.start_polling()
        self.updater.idle()
