import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import ApplicationBuilder, MessageHandler, filters

import state
from config import TELEGRAM_TOKEN, YOUR_CHAT_ID, TIMEZONE
from routing import handle_message, check_missed_items_on_startup
from infrastructure import run_infrastructure_setup, send_startup_message
from sessions import load_sessions_from_sheet, load_birthday_pending_from_sheet
from trips import (
    restore_overseas_from_trips, load_trip_setup_from_sheet
)
from fx import load_fx_rates_from_sheet
from stocks import load_price_alerts_from_sheet
from expenses import get_cards_live
from crm import (
    send_followup_reminders, send_birthday_reminders, send_birthday_followups
)
from reminders import check_and_fire_reminders
from bills import send_bill_reminders
from cal import check_icloud_daily
from stocks import check_price_alerts, send_weekly_market_summary
from fx import refresh_fx_rates


async def post_init(app):
    global state
    health = run_infrastructure_setup()

    try:
        get_cards_live()
    except Exception as e:
        print(f"Startup: card cache warm failed: {e}")

    restore_overseas_from_trips()
    load_fx_rates_from_sheet()
    load_price_alerts_from_sheet()
    load_sessions_from_sheet()
    load_birthday_pending_from_sheet()
    load_trip_setup_from_sheet()

    timezone = pytz.timezone("Asia/Kuala_Lumpur")
    scheduler = AsyncIOScheduler(timezone=timezone, misfire_grace_time=30)
    state._scheduler = scheduler
    state._app_ref = app

    scheduler.add_job(send_followup_reminders, "cron", hour=9, minute=0, args=[app])
    scheduler.add_job(send_birthday_reminders, "cron", hour=12, minute=0, args=[app])
    scheduler.add_job(send_birthday_followups, "cron", hour=14, minute=0, args=[app])
    scheduler.add_job(check_and_fire_reminders, "interval", minutes=1, args=[app], misfire_grace_time=30)
    scheduler.add_job(send_bill_reminders, "cron", hour=9, minute=0, args=[app])
    scheduler.add_job(check_icloud_daily, "cron", hour=9, minute=5, args=[app])
    scheduler.add_job(check_price_alerts, "interval", minutes=15, args=[app], misfire_grace_time=30)
    scheduler.add_job(send_weekly_market_summary, "cron", day_of_week="mon", hour=8, minute=0, args=[app])
    scheduler.add_job(refresh_fx_rates, "cron", hour=8, minute=0)
    scheduler.add_job(refresh_fx_rates, "cron", hour=20, minute=0)

    scheduler.start()
    health["Scheduler"] = "✅ Running"
    print("✅ Scheduler started — follow-ups + bills at 9am, birthdays at 12pm + 2pm, reminders every minute, market Monday 8am")

    await send_startup_message(app, health)
    await check_missed_items_on_startup(app)


def main():
    import os
    missing = [v for v in ("TELEGRAM_TOKEN", "ANTHROPIC_API_KEY", "SHEET_ID") if not os.getenv(v)]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        print("Set these in Railway → Variables before deploying.")
        raise SystemExit(1)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    print("Em is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
