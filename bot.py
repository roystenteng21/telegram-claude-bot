import os
import json
import logging
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from datetime import date, datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz
import caldav

load_dotenv()

# --- Credentials ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
ICLOUD_USERNAME = os.getenv("ICLOUD_USERNAME")
ICLOUD_PASSWORD = os.getenv("ICLOUD_PASSWORD")
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
spreadsheet = gc.open_by_key(SHEET_ID)
sheet = spreadsheet.sheet1

# --- Todo Sheet ---
try:
    todo_sheet = spreadsheet.worksheet("Todos")
except:
    todo_sheet = spreadsheet.add_worksheet(title="Todos", rows=100, cols=3)
    todo_sheet.append_row(["Task", "Status", "Added"])

# --- Anthropic Setup ---
client = Anthropic(api_key=ANTHROPIC_API_KEY)
conversation_histories = {}
edit_sessions = {}

# --- iCloud Calendar Setup ---
def get_calendar(name=None):
    try:
        caldav_client = caldav.DAVClient(
            url="https://caldav.icloud.com",
            username=ICLOUD_USERNAME,
            password=ICLOUD_PASSWORD
        )
        principal = caldav_client.principal()
        calendars = principal.calendars()
        if name:
            for cal in calendars:
                if name.lower() in cal.name.lower():
                    return cal
            return None
        return calendars[0] if calendars else None
    except Exception as e:
        return None

# --- Helpers ---
def format_date(date_str):
    try:
        d = datetime.strptime(date_str, "%d/%m/%Y")
        return d.strftime("%d %b %Y")
    except:
        return date_str

def calculate_age(birthday_str):
    try:
        bday = datetime.strptime(birthday_str, "%d/%m/%Y").date()
        today = date.today()
        age = today.year - bday.year - ((today.month, today.day) < (bday.month, bday.day))
        return str(age)
    except:
        return ""

def format_contact(r):
    name = r.get("Name", "")
    birthday = r.get("Birthday", "")
    age = calculate_age(birthday) if birthday else ""
    where_met = r.get("Where We Met", "")
    notes_raw = r.get("Notes", "")
    followup_date = r.get("Follow Up Date", "")
    followup_notes = r.get("Follow Up Notes", "")
    last_updated = r.get("Last Updated", "")

    # Format age line
    if birthday and age:
        age_line = f"- Age: {age}, {format_date(birthday)}"
    elif age:
        age_line = f"- Age: {age}, (unknown birthday)"
    else:
        age_line = f"- Age: Unknown"

    # Format notes as bullet points
    if notes_raw:
        note_items = [n.strip() for n in notes_raw.split(";") if n.strip()]
        notes_formatted = "- Notes:\n" + "\n".join(f"  - {n}" for n in note_items)
    else:
        notes_formatted = "- Notes:\n  - None"

    # Format follow up
    followup_line = ""
    if followup_date:
        followup_line = f"\n- Follow Up: {format_date(followup_date)}"
        if followup_notes:
            followup_line += f"\n  - {followup_notes}"

    # Last updated
    last_updated_line = f"\n_Last updated: {format_date(last_updated)}_" if last_updated else ""

    return (
        f"*{name}*\n"
        f"- Met: {where_met or 'Unknown'}\n"
        f"{age_line}\n"
        f"{notes_formatted}"
        f"{followup_line}"
        f"{last_updated_line}"
    )

def find_row(name):
    records = sheet.get_all_records()
    for i, r in enumerate(records):
        if name.lower() in r.get("Name", "").lower():
            return i + 2, r
    return None, None

def find_all_rows(name):
    records = sheet.get_all_records()
    results = []
    for i, r in enumerate(records):
        if name.lower() in r.get("Name", "").lower():
            results.append((i + 2, r))
    return results

# --- CRM Functions ---
def save_contact(data):
    try:
        parts = [p.strip() for p in data.split(",")]
        while len(parts) < 7:
            parts.append("")
        name = parts[0]
        birthday = parts[1]
        age = calculate_age(birthday) if birthday else ""
        where_met = parts[2]
        notes = parts[3]
        followup_date = parts[4]
        followup_notes = parts[5]
        last_updated = date.today().strftime("%d/%m/%Y")
        if not name:
            return "❌ Name is required"
        sheet.append_row([name, birthday, age, where_met, notes, followup_date, followup_notes, last_updated])
        return f"✅ Contact saved!\n\n" + format_contact({
            "Name": name, "Birthday": birthday, "Age": age,
            "Where We Met": where_met, "Notes": notes,
            "Follow Up Date": followup_date, "Follow Up Notes": followup_notes,
            "Last Updated": last_updated
        })
    except Exception as e:
        return f"❌ Error saving contact: {str(e)}"

def find_contact(name):
    try:
        results = find_all_rows(name)
        if not results:
            return f"❌ No contact found for '{name}'"
        return "\n\n".join(format_contact(r) for _, r in results)
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
        sheet.update_cell(row_num, 5, new_note)
        sheet.update_cell(row_num, 8, date.today().strftime("%d/%m/%Y"))
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
        sheet.update_cell(row_num, 6, followup_date)
        sheet.update_cell(row_num, 7, followup_notes)
        sheet.update_cell(row_num, 8, date.today().strftime("%d/%m/%Y"))
        return f"✅ Follow up set for *{record.get('Name')}* on {format_date(followup_date)}"
    except Exception as e:
        return f"❌ Error setting follow up: {str(e)}"

def update_field(data):
    try:
        parts = [p.strip() for p in data.split(",", 2)]
        if len(parts) < 3:
            return "❌ Format: update Name, field, new value"
        name, field, value = parts
        field_map = {
            "birthday": 2, "where we met": 4,
            "notes": 5, "follow up date": 6, "follow up notes": 7
        }
        col = field_map.get(field.lower())
        if not col:
            return f"❌ Unknown field '{field}'. Options: birthday, where we met, notes, follow up date, follow up notes"
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        sheet.update_cell(row_num, col, value)
        if field.lower() == "birthday":
            age = calculate_age(value)
            sheet.update_cell(row_num, 3, age)
        sheet.update_cell(row_num, 8, date.today().strftime("%d/%m/%Y"))
        return f"✅ {field.title()} updated for *{record.get('Name')}*"
    except Exception as e:
        return f"❌ Error updating contact: {str(e)}"

def delete_contact(name):
    try:
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        sheet.delete_rows(row_num)
        return f"✅ Contact *{record.get('Name')}* deleted"
    except Exception as e:
        return f"❌ Error deleting contact: {str(e)}"

def search_contacts(keyword):
    try:
        records = sheet.get_all_records()
        results = []
        for r in records:
            if any(keyword.lower() in str(v).lower() for v in r.values()):
                results.append(r)
        if not results:
            return f"❌ No results for '{keyword}'"
        return f"🔍 *{len(results)} result(s) for '{keyword}':*\n\n" + "\n\n".join(format_contact(r) for r in results)
    except Exception as e:
        return f"❌ Error searching: {str(e)}"

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

def get_stats():
    try:
        records = sheet.get_all_records()
        today = date.today()
        total = len(records)
        followups_due = 0
        birthdays_month = 0
        for r in records:
            fu = r.get("Follow Up Date", "")
            if fu:
                try:
                    fu_date = datetime.strptime(fu, "%d/%m/%Y").date()
                    if fu_date >= today:
                        followups_due += 1
                except:
                    pass
            bday = r.get("Birthday", "")
            if bday:
                try:
                    b = datetime.strptime(bday, "%d/%m/%Y").date()
                    this_year = b.replace(year=today.year)
                    if this_year < today:
                        this_year = b.replace(year=today.year + 1)
                    if (this_year - today).days <= 30:
                        birthdays_month += 1
                except:
                    pass
        return (
            f"📊 *CRM Stats*\n\n"
            f"👥 Total contacts: {total}\n"
            f"📅 Upcoming follow ups: {followups_due}\n"
            f"🎂 Birthdays in next 30 days: {birthdays_month}"
        )
    except Exception as e:
        return f"❌ Error getting stats: {str(e)}"

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
            response += f"👤 *{r.get('Name')}* — {format_date(r.get('Follow Up Date'))}\n"
            if r.get('Follow Up Notes'):
                response += f"  - {r.get('Follow Up Notes')}\n"
            response += "\n"
        return response
    except Exception as e:
        return f"❌ Error fetching follow ups: {str(e)}"

def overdue_followups():
    try:
        records = sheet.get_all_records()
        today = date.today()
        overdue = []
        for r in records:
            fu_date = r.get("Follow Up Date", "")
            if fu_date:
                try:
                    fu = datetime.strptime(fu_date, "%d/%m/%Y").date()
                    if fu < today:
                        overdue.append((fu, r))
                except:
                    pass
        if not overdue:
            return "✅ No overdue follow ups!"
        overdue.sort(key=lambda x: x[0])
        response = "⚠️ *Overdue Follow Ups:*\n\n"
        for fu, r in overdue:
            days_ago = (today - fu).days
            response += f"👤 *{r.get('Name')}* — {format_date(r.get('Follow Up Date'))} ({days_ago} days ago)\n"
            if r.get('Follow Up Notes'):
                response += f"  - {r.get('Follow Up Notes')}\n"
            response += "\n"
        return response
    except Exception as e:
        return f"❌ Error fetching overdue: {str(e)}"

def upcoming_birthdays(days=30):
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
                    if days_away <= days:
                        upcoming.append((days_away, r))
                except:
                    pass
        if not upcoming:
            return f"🎂 No birthdays in the next {days} days!"
        upcoming.sort(key=lambda x: x[0])
        label = "next 7 days" if days == 7 else "next 30 days"
        response = f"🎂 *Birthdays in the {label}:*\n\n"
        for days_away, r in upcoming:
            response += f"👤 *{r.get('Name')}* — {format_date(r.get('Birthday'))} ({'today! 🎉' if days_away == 0 else f'in {days_away} days'})\n"
        return response
    except Exception as e:
        return f"❌ Error fetching birthdays: {str(e)}"

def last_contact(name):
    try:
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        last = record.get("Last Updated", "")
        if last:
            return f"👤 *{record.get('Name')}*\n📅 Last updated: {format_date(last)}"
        return f"👤 *{record.get('Name')}*\n📅 No updates recorded yet"
    except Exception as e:
        return f"❌ Error: {str(e)}"

# --- Todo Functions ---
def add_todo(task):
    try:
        todo_sheet.append_row([task, "Pending", date.today().strftime("%d/%m/%Y")])
        return f"✅ Added to your to-do list: _{task}_"
    except Exception as e:
        return f"❌ Error adding task: {str(e)}"

def complete_todo(task):
    try:
        records = todo_sheet.get_all_records()
        for i, r in enumerate(records):
            if task.lower() in r.get("Task", "").lower() and r.get("Status") == "Pending":
                todo_sheet.update_cell(i + 2, 2, "Done")
                return f"✅ Marked as done: _{r.get('Task')}_"
        return f"❌ No pending task found matching '{task}'"
    except Exception as e:
        return f"❌ Error completing task: {str(e)}"

def list_todos():
    try:
        records = todo_sheet.get_all_records()
        pending = [r for r in records if r.get("Status") == "Pending"]
        if not pending:
            return "✅ No pending tasks!"
        response = f"📝 *{len(pending)} pending task(s):*\n\n"
        for r in pending:
            response += f"• {r.get('Task')} _(added {format_date(r.get('Added', ''))})_\n"
        return response
    except Exception as e:
        return f"❌ Error listing tasks: {str(e)}"

# --- Calendar Functions ---
def add_calendar_event(data):
    try:
        parts = [p.strip() for p in data.split(",")]
        if len(parts) < 3:
            return "❌ Format: add event Title, DD/MM/YYYY HH:MM, DD/MM/YYYY HH:MM, calendar name (optional)"
        title = parts[0]
        start = datetime.strptime(parts[1], "%d/%m/%Y %H:%M")
        end = datetime.strptime(parts[2], "%d/%m/%Y %H:%M")
        notes = parts[3] if len(parts) > 3 else ""
        cal_name = parts[4] if len(parts) > 4 else None
        calendar = get_calendar(cal_name) if cal_name else get_calendar()
        if not calendar:
            return f"❌ Could not find calendar '{cal_name}'" if cal_name else "❌ Could not connect to iCloud Calendar"
        ics = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
            f"SUMMARY:{title}\n"
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}\n"
            f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}\n"
            f"DESCRIPTION:{notes}\n"
            "END:VEVENT\nEND:VCALENDAR"
        )
        calendar.add_event(ics)
        return (
            f"✅ Event created!\n\n"
            f"📅 *{title}*\n"
            f"🕐 {start.strftime('%d %b %Y, %H:%M')} → {end.strftime('%H:%M')}\n"
            f"📁 Calendar: {calendar.name}\n"
            + (f"📝 {notes}" if notes else "")
        )
    except Exception as e:
        return f"❌ Error creating event: {str(e)}"

def get_events(days=1):
    try:
        calendar = get_calendar()
        if not calendar:
            return "❌ Could not connect to iCloud Calendar"
        start = datetime.now()
        end = start + timedelta(days=days)
        events = calendar.date_search(start=start, end=end)
        if not events:
            label = "today" if days == 1 else "this week"
            return f"📅 No events {label}"
        label = "Today's events" if days == 1 else "This week's events"
        response = f"📅 *{label}:*\n\n"
        for event in events:
            e = event.vobject_instance.vevent
            summary = str(e.summary.value)
            dtstart = e.dtstart.value
            if hasattr(dtstart, 'strftime'):
                response += f"• *{summary}* — {dtstart.strftime('%d %b, %H:%M')}\n"
            else:
                response += f"• *{summary}* — {dtstart}\n"
        return response
    except Exception as e:
        return f"❌ Error fetching events: {str(e)}"

def delete_calendar_event(title):
    try:
        calendar = get_calendar()
        if not calendar:
            return "❌ Could not connect to iCloud Calendar"
        start = datetime.now()
        end = start + timedelta(days=30)
        events = calendar.date_search(start=start, end=end)
        for event in events:
            e = event.vobject_instance.vevent
            if title.lower() in str(e.summary.value).lower():
                event.delete()
                return f"✅ Event *{str(e.summary.value)}* deleted"
        return f"❌ No event found matching '{title}' in the next 30 days"
    except Exception as e:
        return f"❌ Error deleting event: {str(e)}"

# --- Scheduled Reminders ---
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
                            f"Don't forget to reach out today!"
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
                        msg = f"🎉 *Happy Birthday {r.get('Name')}!* They're turning {age} today! Don't forget to wish them well!"
                        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode="Markdown")
                    elif days_away == 3:
                        msg = f"🎂 *Heads up!* {r.get('Name')}'s birthday is in 3 days ({format_date(bday_str)}) — they'll be turning {age}!"
                        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode="Markdown")
                except:
                    pass
    except Exception as e:
        print(f"Error sending birthday reminders: {e}")

# --- Edit Session Handler ---
async def handle_edit_session(user_id, text, update):
    session = edit_sessions[user_id]
    step = session["step"]
    fields = ["birthday", "where we met", "notes", "follow up date", "follow up notes"]

    if step == "choose_field":
        field = text.lower().strip()
        if field not in fields:
            await update.message.reply_text(
                f"Pick a field to edit:\n1. Birthday\n2. Where we met\n3. Notes\n4. Follow up date\n5. Follow up notes\n\nOr type *cancel* to exit.",
                parse_mode="Markdown"
            )
            return
        session["field"] = field
        session["step"] = "enter_value"
        await update.message.reply_text(f"Enter the new value for *{field.title()}*:", parse_mode="Markdown")

    elif step == "enter_value":
        field = session["field"]
        name = session["name"]
        result = update_field(f"{name}, {field}, {text}")
        del edit_sessions[user_id]
        await update.message.reply_text(result, parse_mode="Markdown")

# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    lower = text.lower()

    # Handle active edit session
    if user_id in edit_sessions:
        await handle_edit_session(user_id, text, update)
        return

    # CRM Commands
    if lower.startswith("save "):
        reply = save_contact(text[5:])
    elif lower.startswith("find ") or lower.startswith("pull up "):
        name = text[5:] if lower.startswith("find ") else text[8:]
        reply = find_contact(name)
    elif lower.startswith("note "):
        reply = add_note(text[5:])
    elif lower.startswith("followup "):
        reply = set_followup(text[9:])
    elif lower.startswith("update "):
        reply = update_field(text[7:])
    elif lower.startswith("delete ") and not lower.startswith("delete event"):
        reply = delete_contact(text[7:])
    elif lower.startswith("search "):
        reply = search_contacts(text[7:])
    elif lower.startswith("edit "):
        name = text[5:].strip()
        _, record = find_row(name)
        if not record:
            reply = f"❌ No contact found for '{name}'"
        else:
            edit_sessions[user_id] = {"name": record.get("Name"), "step": "choose_field"}
            reply = (
                f"Editing *{record.get('Name')}*. Which field?\n\n"
                f"1. Birthday\n2. Where we met\n3. Notes\n4. Follow up date\n5. Follow up notes\n\n"
                f"Type the field name or *cancel* to exit."
            )
    elif lower == "cancel":
        if user_id in edit_sessions:
            del edit_sessions[user_id]
        reply = "Cancelled."
    elif lower == "list":
        reply = list_contacts()
    elif lower == "stats":
        reply = get_stats()
    elif lower == "followups":
        reply = upcoming_followups()
    elif lower == "overdue":
        reply = overdue_followups()
    elif lower == "birthdays":
        reply = upcoming_birthdays(30)
    elif lower == "soon":
        reply = upcoming_birthdays(7)
    elif lower.startswith("lastcontact "):
        reply = last_contact(text[12:])

    # Todo Commands
    elif lower.startswith("todo "):
        reply = add_todo(text[5:])
    elif lower.startswith("done "):
        reply = complete_todo(text[5:])
    elif lower == "todos":
        reply = list_todos()

    # Calendar Commands
    elif lower.startswith("add event "):
        reply = add_calendar_event(text[10:])
    elif lower == "events today":
        reply = get_events(1)
    elif lower == "events week":
        reply = get_events(7)
    elif lower.startswith("delete event "):
        reply = delete_calendar_event(text[13:])

    # Help
    elif lower == "help":
        reply = (
            "🤖 *Em's Commands:*\n\n"
            "*CRM:*\n"
            "`save Name, Birthday, Where We Met, Notes`\n"
            "`find Name` / `pull up Name`\n"
            "`note Name - your note`\n"
            "`followup Name, DD/MM/YYYY, notes`\n"
            "`update Name, field, value`\n"
            "`edit Name` — guided edit\n"
            "`delete Name`\n"
            "`search keyword`\n"
            "`list` — all contacts\n"
            "`stats` — CRM summary\n"
            "`followups` — upcoming\n"
            "`overdue` — past due\n"
            "`birthdays` — next 30 days\n"
            "`soon` — next 7 days\n"
            "`lastcontact Name`\n\n"
            "*Calendar:*\n"
            "`add event Title, DD/MM/YYYY HH:MM, DD/MM/YYYY HH:MM`\n"
            "`events today`\n"
            "`events week`\n"
            "`delete event Title`\n\n"
            "*To-Do:*\n"
            "`todo Task`\n"
            "`done Task`\n"
            "`todos`\n\n"
            "*Chat with Em:* Just type anything!"
        )

    # Claude Chat
    else:
        if user_id not in conversation_histories:
            conversation_histories[user_id] = []
        conversation_histories[user_id].append({"role": "user", "content": text})
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=(
                "# Em — Your Personal Assistant\n\n"
                "## Core Identity\n"
                "You're Em — a smart, focused personal assistant with a casual, cool vibe. "
                "You keep things real and get stuff done without the corporate robot speak.\n\n"
                "## Communication Style\n"
                "- Natural and conversational — light slang like 'got it', 'sure thing', 'on it', 'no worries', 'lemme check that', 'all good'\n"
                "- Clean and simple — never over the top or trying too hard\n"
                "- Helpful and focused — you're here to make life easier, not to chat\n"
                "- Never say 'cool cool'\n"
                "- Never use the shaka emoji\n"
                "- Always capitalise the first letter of each sentence\n\n"
  		"- Never use dashes or hyphens in your conversational replies — write in natural flowing sentences instead\n"
                "- Only use dashes when displaying CRM contact info in the required format\n\n"
                "## Greetings & Sign-offs\n"
                "- Mix it up — never sound repetitive\n"
                "- Options: 'hey', 'yo', 'alright', 'aite', 'sup', or just dive straight in\n"
                "- Vary your closings naturally too\n\n"
                "## Emojis\n"
                "- Use sparingly — only when they feel natural\n"
                "- Don't overdo it\n\n"
                "## Response Length\n"
                "- Concise and to the point by default\n"
                "- Elaborate only when asked\n\n"
                "## CRM Display Format\n"
                "When displaying contact info, always use this exact format:\n\n"
                "[Full Name]\n"
                "- Met: [where you met]\n"
                "- Age: [age], [DD MMM YYYY or note if unknown]\n"
                "- Notes:\n"
                "  - [note 1]\n"
                "  - [note 2]\n\n"
                "_Last updated: DD MMM YYYY_\n\n"
                "Example:\n\n"
                "John Doe\n"
                "- Met: Coffee shop downtown\n"
                "- Age: 28, 15 Jun 1997\n"
                "- Notes:\n"
                "  - Works in marketing\n"
                "  - Interested in startups\n"
                "  - Has a dog named Max\n\n"
                "_Last updated: 22 Apr 2026_\n\n"
                "Always separate notes into individual bullet points. Never dump them in one line.\n\n"
                "## What You Don't Do\n"
                "- Sound stiff or corporate\n"
                "- Act like a typical AI assistant\n"
                "- Make small talk for the sake of it\n"
                "- Get repetitive with phrases or greetings"
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
    scheduler.add_job(send_followup_reminders, "cron", hour=9, minute=0, args=[app])
    scheduler.add_job(send_birthday_reminders, "cron", hour=9, minute=0, args=[app])
    scheduler.start()
    print("Reminders scheduled!")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Em is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()