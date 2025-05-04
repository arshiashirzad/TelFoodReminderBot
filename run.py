import json
from bot.bot import FoodReminderBot
from bot.database import Database

with open("config/config.json") as config_file:
    config = json.load(config_file)

TOKEN = config["token"]
MYSQL = config["mysql"]

if __name__ == "__main__":
    db = Database(
        host=MYSQL["host"],
        user=MYSQL["user"],
        password=MYSQL["password"],
        database=MYSQL["database"]
    )
    bot = FoodReminderBot(TOKEN, db)
    bot.run()
