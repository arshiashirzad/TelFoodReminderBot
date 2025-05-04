from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler

def register_handlers(dispatcher, db):
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CallbackQueryHandler(callback_query_handler))

def start(update, context):
    """Handle the /start command."""
    user = update.message.from_user
    keyboard = [[InlineKeyboardButton("Reserve", callback_data="reserve")],
                [InlineKeyboardButton("Help", callback_data="help")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(f"👋 Welcome {user.first_name}! I'm your food reminder bot 🍽️.", reply_markup=reply_markup)

def callback_query_handler(update, context):
    """Handle inline button clicks."""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "reserve":
        # Save reservation (database logic handled in database.py)
        context.bot.db.save_reservation(user_id)
        query.answer("Reservation saved! ✅")
        query.edit_message_text("Your reservation has been recorded. Thank you! 🙌")
    elif data == "help":
        query.answer("Help Section ⛑️")
        query.edit_message_text("Use the /start command to see options.")
