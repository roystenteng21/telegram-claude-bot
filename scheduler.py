import pytz
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import ContextTypes

import config
from config import TIMEZONE, YOUR_CHAT_ID
from profile import run_infrastructure_setup
from sheets import send_startup_message, get_sheet
from crm import (
    _get_crm_records, send_followup_reminders, send_birthday_reminders, send_birthday_followups,
)
from expenses import send_bill_reminders
from reminders import check_and_fire_reminders, reminders_sheet, get_next_recurrence
from calendar import check_icloud_daily
from stocks import check_price_alerts, send_weekly_market_summary
from state import load_fx_rates_from_sheet, refresh_fx_rates
from trips import restore_overseas_from_trips
from routing import _handle_message_inner


async def check_missed_items_on_startup(app):
    try:
        today = date.today()
        yesterday = today - timedelta(days=1)
        missed_followups = []
        missed_bills = []
        missed_reminders = []

        try:
            records = _get_crm_records()
            for r in records:
                fu_date_str = r.get("Follow Up Date", "")
                if fu_date_str:
                    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"]:
                        try:
                            fu_date = datetime.strptime(fu_date_str, fmt).date()
                            if fu_date < today:
                                missed_followups.append({
                                    "name": r.get("Name", "?"),
                                    "date": fu_date_str,
                                    "notes": r.get("Follow Up Notes", "")
                                })
                            break
                        except ValueError:
                            continue
        except Exception as e:
            print(f"Missed followups check error: {e}")

        try:
            from sheets import bills_sheet
            ws = bills_sheet()
            records = ws.get_all_records()
            for r in records:
                due_day = int(r.get("Due Date", 0) or 0)
                if due_day and yesterday.day == due_day:
                    missed_bills.append(r.get("Name", "?"))
        except Exception as e:
            print(f"Missed bills check error: {e}")

        try:
            ws = reminders_sheet()
            records = ws.get_all_records()
            now = datetime.now(TIMEZONE)
            for i, r in enumerate(records, start=2):
                if r.get("Status") != "pending":
                    continue
                scheduled_str = r.get("Scheduled Time", "")
                if not scheduled_str:
                    continue
                try:
                    scheduled = TIMEZONE.localize(datetime.strptime(scheduled_str, "%Y-%m-%d %H:%M"))
                    if scheduled < now:
                        missed_reminders.append({
                            "message": r.get("Message", ""),
                            "row": i,
                            "recurrence": r.get("Recurrence", "once")
                        })
                except Exception:
                    continue
        except Exception as e:
            print(f"Missed reminders check error: {e}")

        if missed_followups:
            lines = ["⚠️ Missed follow-ups while offline:"]
            for f in missed_followups:
                lines.append(f"• {f['name']} — was due {f['date']}")
            lines.append("\nReply 'followups' for details, 'skip missed' to dismiss all, or 'skip [name]' for one.")
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text="\n".join(lines))

        if missed_bills:
            lines = ["⚠️ Missed bill reminder(s) while offline:"]
            for b in missed_bills:
                lines.append(f"• {b}")
            lines.append("\nReply 'skip missed bills' to dismiss.")
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text="\n".join(lines))

        for rem in missed_reminders:
            try:
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🔔 Reminder (missed while offline): {rem['message']}"
                )
                ws = reminders_sheet()
                if rem["recurrence"] != "once":
                    next_time = get_next_recurrence(
                        datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                        rem["recurrence"]
                    )
                    if next_time:
                        ws.update_cell(rem["row"], 3, next_time)
                else:
                    ws.update_cell(rem["row"], 5, "sent")
            except Exception as e:
                print(f"Missed reminder fire error: {e}")

        try:
            if today.weekday() == 0:
                already_sent = False
                try:
                    sheet = get_sheet("Settings")
                    if sheet:
                        records = sheet.get_all_records()
                        for r in records:
                            if r.get("Key") == "market_summary_last_sent":
                                already_sent = r.get("Value", "") == today.isoformat()
                                break
                except Exception as e:
                    print(f"market_summary_last_sent check error: {e}")

                if not already_sent:
                    await app.bot.send_message(
                        chat_id=YOUR_CHAT_ID,
                        text="Missed the Monday market summary while offline — want me to send it now? (yes / no)"
                    )
                    config.market_summary_pending[YOUR_CHAT_ID] = True
        except Exception as e:
            print(f"Missed market summary check error: {e}")

    except Exception as e:
        print(f"check_missed_items_on_startup error: {e}")


async def post_init(app):
    health = run_infrastructure_setup()

    restore_overseas_from_trips()
    load_fx_rates_from_sheet()

    # Register restaurant functions with routing module
    try:
        from restaurants import (
            save_restaurant, format_restaurant_saved, delete_restaurant,
            is_restaurant_save, is_restaurant_search, is_restaurant_suggestion_request,
            handle_save_restaurant, handle_search_restaurants, get_similar_restaurants,
            get_restaurant_review,
        )
        from routing import register_restaurant_fns
        register_restaurant_fns(
            save_restaurant=save_restaurant,
            format_restaurant_saved=format_restaurant_saved,
            delete_restaurant=delete_restaurant,
            is_restaurant_save=is_restaurant_save,
            is_restaurant_search=is_restaurant_search,
            is_restaurant_suggestion_request=is_restaurant_suggestion_request,
            handle_save_restaurant=handle_save_restaurant,
            handle_search_restaurants=handle_search_restaurants,
            get_similar_restaurants=get_similar_restaurants,
            get_restaurant_review=get_restaurant_review,
        )
        print("✅ Restaurant functions registered")
    except Exception as e:
        print(f"⚠️ Restaurant functions registration failed: {e}")

    timezone = pytz.timezone("Asia/Kuala_Lumpur")
    scheduler = AsyncIOScheduler(timezone=timezone, misfire_grace_time=30)
    config._scheduler = scheduler
    config._app_ref = app

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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != YOUR_CHAT_ID:
        return

    try:
        await _handle_message_inner(update, context, user_id)
    except Exception as e:
        import traceback
        err_type = type(e).__name__
        err_msg = str(e)[:120]
        tb_lines = traceback.format_exc().splitlines()
        location = next((l.strip() for l in reversed(tb_lines) if ".py" in l), "")
        detail = f"{err_type}: {err_msg}"
        if location:
            detail += f"\n({location})"
        print(f"UNHANDLED handle_message error: {traceback.format_exc()}")
        try:
            await update.message.reply_text(f"❌ Something went wrong: {detail}")
        except Exception:
            pass
