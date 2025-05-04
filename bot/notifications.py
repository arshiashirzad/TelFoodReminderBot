from datetime import datetime, timedelta

def schedule_notifications(job_queue, db):
    """Schedule periodic tasks."""
    job_queue.run_repeating(notify_users, interval=3600, first=0, context=db)

def notify_users(context):
    """Notify users who haven't reserved their meals."""
    db = context.job.context

    users = db.get_users_without_reservation()
    bot = context.bot

    for user in users:
        user_id = user["user_id"]
        try:
            bot.send_message(chat_id=user_id, text="🚨 Reminder: You haven't reserved your meal yet!")
        except Exception as e:
            print(f"Failed to send message to {user_id}: {e}")
