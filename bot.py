import os
import json
import logging
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from datetime import date, datetime
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

load_dotenv()

# --- Your credentials ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
YOUR_CHAT_ID = 281095850

# --- Google Sheets Setup ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
google_creds_env = os.getenv("GOOGLE_CREDENTIALS")
if google_creds_env:
    google_creds = json.loads(google_creds_env)
    creds = Credentials.from_service_account_info(google_creds, scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SHEET_ID).sheet1

# --- Anthropic Setup ---
client = Anthropic(api_key=ANTHROPIC_API_KEY)
conversation_histories = {}

# --- Helper: Calculate Age ---
def calculate_age(birthday_str):
    try:
        bday = datetime.strptime(birthday_str, "%d/%m/%Y").date()
        today = date.today()
        age = today.year - bday.year - ((today.month, today.day) < (bday.month, bday.day))
        return str(age)
    except:
        return ""

# --- Helper: Find Row ---
def find_row(name):
    records = sheet.get_all_records()
    for i, r in enumerate(records):
        if name.lower() in r.get("Name", "").lower():
            return i + 2, r
    return None, None

# --- CRM Functions ---
def save_contact(data):
    try:
        parts = [p.strip() for p in data.split(",")]
        while len(parts) < 8:
            parts.append("")
        name = parts[0]
        phone = parts[1]
        birthday = parts[2]
        age = calculate_age(birthday) if birthday else ""
        where_met = parts[3]
        notes = parts[4]
        followup_date = parts[5]
        followup_notes = parts[6]
        if not name:
            return "❌ Name is required"
        sheet.append_row([name, phone, birthday, age, where_met, notes, followup_date, followup_notes])
        return (
            f"✅ Contact saved!\n\n"
            f"👤 *{name}*\n"
            f"📞 {phone or 'Not provided'}\n"
            f"🎂 {birthday or 'Not provided'}" + (f" (Age: {age})" if age else "") + "\n"
            f"📍 Met at: {where_met or 'Not provided'}\n"
            f"📝 Notes: {notes or 'None'}\n"
            f"📅 Follow Up: {followup_date or 'None'}\n"
            f"🔔 Follow Up Notes: {followup_notes or 'None'}"
        )
    except Exception as e:
        return f"❌ Error saving contact: {str(e)}"

def find_contact(name):
    try:
        records = sheet.get_all_records()
        results = [r for r in records if name.lower() in r.get("Name", "").lower()]
        if not results:
            return f"❌ No contact found for '{name}'"
        response = ""
        for r in results:
            birthday = r.get("Birthday", "")
            age = calculate_age(birthday) if birthday else "N/A"
            response += (
                f"👤 *{r.get('Name', '')}*\n"
                f"📞 {r.get('Phone', '') or 'Not provided'}\n"
                f"🎂 {birthday or 'Not provided'}" + (f" (Age: {age})" if birthday else "") + "\n"
                f"📍 Met at: {r.get('Where We Met', '') or 'Not provided'}\n"
                f"📝 Notes: {r.get('Notes', '') or 'None'}\n"
                f"📅 Follow Up: {r.get('Follow Up Date', '') or 'None'}\n"
                f"🔔 Follow Up Notes: {r.get('Follow Up Notes', '') or 'None'}\n\n"
            )
        return response
    except Exception as e:
        return f"❌ Error finding contact: {str(e)}"

def add_note(data):
    try:
        parts = data.split("-", 1)
        if len(parts) < 2:
            return "❌ Format: note Name - your note here"
        name = parts[0].strip()
        note = parts[1].strip()
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        existing = record.get("Notes", "")
        new_note = f"{existing}; {note}" if existing else note
        sheet.update_cell(row_num, 6, new_note)
        return f"✅ Note added to *{record.get('Name')}*"
    except Exception as e:
        return f"❌ Error adding note: {str(e)}"

def set_followup(data):
    try:
        parts = [p.strip() for p in data.split(",", 2)]
        if len(parts) < 2:
            return "❌ Format: followup Name, DD/MM/YYYY, notes"
        name = parts[0]
        followup_date = parts[1]
        followup_notes = parts[2] if len(parts) > 2 else ""
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        sheet.update_cell(row_num, 7, followup_date)
        sheet.update_cell(row_num, 8, followup_notes)
        return f"✅ Follow up set for *{record.get('Name')}* on {followup_date}"
    except Exception as e:
        return f"❌ Error setting follow up: {str(e)}"

def update_field(data):
    try:
        parts = [p.strip() for p in data.split(",", 2)]
        if len(parts) < 3:
            return "❌ Format: update Name, field, new value"
        name, field, value = parts
        field_map = {
            "phone": 2, "birthday": 3, "where we met": 5,
            "notes": 6, "follow up date": 7, "follow up notes": 8
        }
        col = field_map.get(field.lower())
        if not col:
            return f"❌ Unknown field '{field}'. Options: phone, birthday, where we met, notes, follow up date, follow up notes"
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        sheet.update_cell(row_num, col, value)
        if field.lower() == "birthday":
            age = calculate_age(value)
            sheet.update_cell(row_num, 4, age)
            return f"✅ Birthday updated for *{record.get('Name')}* — Age: {age}"
        return f"✅ {field.title()} updated for *{record.get('Name')}*"
    except Exception as e:
        return f"❌ Error updating contact: {str(e)}"

def list_contacts():
    try:
        records = sheet.get_all_records()
        if not records:
            return "❌ No contacts found"
        response = f"📋 *{len(records)} contact(s):*\n\n"
        for r in records:
            response += f"👤 {r.get('Name', '')} — 📍 {r.get('Where We Met', '') or 'Unknown'}\n"
        return response
    except Exception as e:
        return f"❌ Error listing contacts: {str(e)}"

def upcoming_followups():
    try:
        records = sheet.get_all_records()
        today = date.today()
        upcoming = []
        for r in records:
            fu_date = r.get("Follow Up Date", "")
            if fu_date:
                try:
                    fu = datetime.strptime(fu_date, "%d/%m/%Y").date()
                    if fu >= today:
                        upcoming.append((fu, r))
                except:
                    pass
        if not upcoming:
            return "✅ No upcoming follow ups!"
        upcoming.sort(key=lambda x: x[0])
        response = "📅 *Upcoming Follow Ups:*\n\n"
        for fu, r in upcoming:
            response += f"👤 *{r.get('Name')}* — {r.get('Follow Up Date')}\n🔔 {r.get('Follow Up Notes') or 'No notes'}\n\n"
        return response
    except Exception as e:
        return f"❌ Error fetching follow ups: {str(e)}"

def upcoming_birthdays():
    try:
        records = sheet.get_all_records()
        today = date.today()
        upcoming = []
        for r in records:
            bday_str = r.get("Birthday", "")
            if bday_str:
                try:
                    bday = datetime.strptime(bday_str, "%d/%m/%Y").date()
                    this_year = bday.replace(year=today.year)
                    if this_year < today:
                        this_year = bday.replace(year=today.year + 1)
                    days_away = (this_year - today).days
                    if days_away <= 30:
                        upcoming.append((days_away, r))
                except:
                    pass
        if not upcoming:
            return "🎂 No birthdays in the next 30 days!"
        upcoming.sort(key=lambda x: x[0])
        response = "🎂 *Upcoming Birthdays (next 30 days):*\n\n"
        for days, r in upcoming:
            response += f"👤 *{r.get('Name')}* — {r.get('Birthday')} ({'today! 🎉' if days == 0 else f'in {days} days'})\n"
        return response
    except Exception as e:
        return f"❌ Error fetching birthdays: {str(e)}"

# --- Scheduled Reminder Functions ---
async def send_daily_briefing(app):
    try:
        records = sheet.get_all_records()
        today = date.today()
        message = "☀️ *Good morning! Here's your daily briefing:*\n\n"

        # Today's follow ups
        todays_followups = []
        for r in records:
            fu_date = r.get("Follow Up Date", "")
            if fu_date:
                try:
                    fu = datetime.strptime(fu_date, "%d/%m/%Y").date()
                    if fu == today:
                        todays_followups.append(r)
                except:
                    pass

        if todays_followups:
            message += "📅 *Follow ups due today:*\n"
            for r in todays_followups:
                message += f"👤 {r.get('Name')} — {r.get('Follow Up Notes') or 'No notes'}\n"
            message += "\n"
        else:
            message += "📅 No follow ups due today\n\n"

        # Upcoming birthdays in next 7 days
        upcoming_bdays = []
        for r in records:
            bday_str = r.get("Birthday", "")
            if bday_str:
                try:
                    bday = datetime.strptime(bday_str, "%d/%m/%Y").date()
                    this_year = bday.replace(year=today.year)
                    if this_year < today:
                        this_year = bday.replace(year=today.year + 1)
                    days_away = (this_year - today).days
                    if 0 <= days_away <= 7:
                        upcoming_bdays.append((days_away, r))
                except:
                    pass

        if upcoming_bdays:
            upcoming_bdays.sort(key=lambda x: x[0])
            message += "🎂 *Birthdays in the next 7 days:*\n"
            for days, r in upcoming_bdays:
                if days == 0:
                    message += f"🎉 *{r.get('Name')}* — Today!\n"
                else:
                    message += f"👤 {r.get('Name')} — in {days} days\n"
            message += "\n"
        else:
            message += "🎂 No birthdays in the next 7 days\n\n"

        message += "Have a great day! 💪"
        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending daily briefing: {e}")

async def send_followup_reminders(app):
    try:
        records = sheet.get_all_records()
        today = date.today()
        for r in records:
            fu_date = r.get("Follow Up Date", "")
            if fu_date:
                try:
                    fu = datetime.strptime(fu_date, "%d/%m/%Y").date()
                    if fu == today:
                        message = (
                            f"🔔 *Follow up reminder!*\n\n"
                            f"👤 *{r.get('Name')}*\n"
                            f"📝 {r.get('Follow Up Notes') or 'No notes'}\n\n"
                            f"Don't forget to reach out today! 💪"
                        )
                        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=message, parse_mode="Markdown")
                except:
                    pass
    except Exception as e:
        print(f"Error sending follow up reminders: {e}")

async def send_birthday_reminders(app):
    try:
        records = sheet.get_all_records()
        today = date.today()
        for r in records:
            bday_str = r.get("Birthday", "")
            if bday_str:
                try:
                    bday = datetime.strptime(bday_str, "%d/%m/%Y").date()
                    this_year = bday.replace(year=today.year)
                    if this_year < today:
                        this_year = bday.replace(year=today.year + 1)
                    days_away = (this_year - today).days
                    age = calculate_age(bday_str)
                    if days_away == 0:
                        message = f"🎉 *Happy Birthday {r.get('Name')}!* They're turning {age} today! Don't forget to wish them well! 🎂"
                        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=message, parse_mode="Markdown")
                    elif days_away == 3:
                        message = f"🎂 *Heads up!* {r.get('Name')}'s birthday is in 3 days ({r.get('Birthday')}) — they'll be turning {age}!"
                        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=message, parse_mode="Markdown")
                except:
                    pass
    except Exception as e:
        print(f"Error sending birthday reminders: {e}")

# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text.lower().startswith("save "):
        reply = save_contact(text[5:])
    elif text.lower().startswith("find "):
        reply = find_contact(text[5:])
    elif text.lower().startswith("note "):
        reply = add_note(text[5:])
    elif text.lower().startswith("followup "):
        reply = set_followup(text[9:])
    elif text.lower().startswith("update "):
        reply = update_field(text[7:])
    elif text.lower() == "list":
        reply = list_contacts()
    elif text.lower() == "followups":
        reply = upcoming_followups()
    elif text.lower() == "birthdays":
        reply = upcoming_birthdays()
    elif text.lower() == "help":
        reply = (
            "🤖 *Em's Commands:*\n\n"
            "*Save a contact:*\n`save Name, Phone, Birthday, Where We Met, Notes, Follow Up Date, Follow Up Notes`\n_(all fields except Name are optional)_\n\n"
            "*Find a contact:*\n`find Name`\n\n"
            "*Add a note:*\n`note Name - your note here`\n\n"
            "*Set follow up:*\n`followup Name, DD/MM/YYYY, notes`\n\n"
            "*Update a field:*\n`update Name, field, new value`\n\n"
            "*List all contacts:*\n`list`\n\n"
            "*Upcoming follow ups:*\n`followups`\n\n"
            "*Birthdays in next 30 days:*\n`birthdays`\n\n"
            "*Chat with Em:*\nJust type anything!"
        )
    else:
        if user_id not in conversation_histories:
            conversation_histories[user_id] = []
        conversation_histories[user_id].append({"role": "user", "content": text})
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=(
                "Your name is Em. You are a smart, focused personal assistant with a casual, cool vibe. "
                "You use light slang naturally — things like 'got it', 'sure thing', 'on it', 'no worries', 'lemme check that', 'all good' — but keep it clean and simple, never over the top. "
                "You vary how you greet and sign off each message so you never sound repetitive or robotic. "
		"Mix it up naturally — sometimes start with 'hey', 'yo', 'alright', 'aite', 'sup' or just dive straight into the answer. "                
		"Use emojis occasionally but sparingly — only when they feel natural. "
                "Stay focused on being helpful and getting things done — you're not here to small talk, just to make the user's life easier. "
                "Keep responses concise and to the point unless asked to elaborate. "
                "You also help manage the user's personal CRM — their contacts, notes, follow ups and birthdays. "
                "Never sound stiff, corporate or like a typical AI assistant."
            ),
            messages=conversation_histories[user_id]
        )
        reply = response.content[0].text
        conversation_histories[user_id].append({"role": "assistant", "content": reply})

    await update.message.reply_text(reply, parse_mode="Markdown")

# --- Main ---
async def post_init(app):
    timezone = pytz.timezone("Asia/Kuala_Lumpur")
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(send_daily_briefing, "cron", hour=9, minute=0, args=[app])
    scheduler.add_job(send_followup_reminders, "cron", hour=9, minute=0, args=[app])
    scheduler.add_job(send_birthday_reminders, "cron", hour=9, minute=0, args=[app])
    scheduler.start()
    print("Reminders scheduled!")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Em is running with reminders... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()