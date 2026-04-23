import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from datetime import date, datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
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

# --- Google Sheets + Drive Setup ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
google_creds_env = os.getenv("GOOGLE_CREDENTIALS")
if google_creds_env:
    google_creds = json.loads(google_creds_env)
    creds = Credentials.from_service_account_info(google_creds, scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)

gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SHEET_ID)
drive_service = build("drive", "v3", credentials=creds)

# --- em_profile (loaded at startup, updated by setup) ---
em_profile = {}

# --- Infrastructure Setup ---
def setup_sheets():
    """Rename Sheet1 to CRM if needed, create all required tabs."""
    existing = [ws.title for ws in spreadsheet.worksheets()]

    # Rename sheet1 -> CRM if CRM doesn't exist yet
    if "CRM" not in existing:
        try:
            sheet1 = spreadsheet.worksheet("Sheet1")
            sheet1.update_title("CRM")
            print("Renamed Sheet1 -> CRM")
        except Exception:
            # Sheet1 already renamed or doesn't exist, create CRM fresh
            if "CRM" not in [ws.title for ws in spreadsheet.worksheets()]:
                ws = spreadsheet.add_worksheet(title="CRM", rows=1000, cols=10)
                ws.append_row(["Name", "Birthday", "Age", "Where We Met", "Notes",
                               "Follow Up Date", "Follow Up Notes", "Last Updated"])
                print("Created CRM tab")

    # Todos tab
    if "Todos" not in existing:
        ws = spreadsheet.add_worksheet(title="Todos", rows=500, cols=3)
        ws.append_row(["Task", "Status", "Added"])
        print("Created Todos tab")

    # All other required tabs
    required_tabs = {
        "Meeting Notes": ["Event Name", "Topic", "Summary", "Action Items", "Date"],
        "Expenses": ["Date", "Merchant", "Amount", "Currency", "SGD Amount", "Category",
                     "Card", "Receipt Link", "Reconciled", "Notes"],
        "Bills": ["Name", "Bank", "Due Date", "Estimated Amount", "Notes"],
        "Cards": ["Card Name", "Bank", "Type", "Notes"],
        "Merchant Map": ["Merchant", "Category", "Card"],
        "Restaurants": ["Name", "Location", "Country", "Tags", "Notes"],
        "Portfolio": ["Stock", "Quantity", "Buy Price", "Buy Date", "Notes"],
        "Settings": ["Key", "Value"]
    }

    existing_now = [ws.title for ws in spreadsheet.worksheets()]
    for tab_name, headers in required_tabs.items():
        if tab_name not in existing_now:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=500, cols=len(headers))
            ws.append_row(headers)
            print(f"Created tab: {tab_name}")

    print("✅ Sheets setup complete")

def get_or_create_drive_folder(name, parent_id=None):
    """Get a Drive folder by name (under parent), or create it."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    # Create it
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent_id:
        meta["parents"] = [parent_id]
    folder = drive_service.files().create(body=meta, fields="id").execute()
    print(f"Created Drive folder: {name}")
    return folder["id"]

def setup_drive():
    """Create Em's Drive folder structure."""
    em_id = get_or_create_drive_folder("Em")
    receipts_id = get_or_create_drive_folder("Receipts", em_id)
    meeting_notes_id = get_or_create_drive_folder("Meeting Notes", em_id)
    backups_id = get_or_create_drive_folder("Backups", em_id)
    settings_id = get_or_create_drive_folder("Settings", em_id)
    print("✅ Drive folders setup complete")
    return {
        "em": em_id,
        "receipts": receipts_id,
        "meeting_notes": meeting_notes_id,
        "backups": backups_id,
        "settings": settings_id
    }

# Global Drive folder IDs (set during setup)
DRIVE_FOLDERS = {}

def setup_em_profile():
    """Load em_profile from Settings sheet, or create it if missing.
    Stored as a single row: key='em_profile', value=JSON string.
    Service accounts can't upload files to Drive, so we use Sheets instead.
    """
    global em_profile

    default_profile = {
        "version": "1.0",
        "created": date.today().strftime("%d/%m/%Y"),
        "tone": "warm, casual, never formal",
        "no_dashes_in_conversation": True,
        "emoji_style": "contextual and varied, never overdone",
        "greeting_options": ["hey", "yo", "alright", "aite", "sup", "oi"],
        "closing_style": "varied, natural, never repetitive",
        "no_repeat_phrases": True,
        "info_per_line": True,
        "silent_tracking": True,
        "language": "natural flowing sentences, no bullet dumps",
        "forbidden_phrases": ["cool cool", "certainly", "of course", "absolutely", "great question"],
        "forbidden_emojis": ["🤙"],
        "response_length": "concise by default, elaborate only when asked",
        "multi_language": True,
        "dnd_active": False,
        "dnd_held_messages": [],
        "overseas_mode": False,
        "overseas_destination": "",
        "overseas_currency": "SGD",
        "overseas_return_date": "",
        "preferences": {
            "birthday_greeting_style": "warm and casual, opens with Happy birthday",
            "expense_confirmation_emoji": "contextual and varied",
            "reminder_default_time": "09:00",
            "weekly_market_summary_day": "Monday",
            "weekly_market_summary_time": "08:00"
        },
        "learned_style": {}
    }

    try:
        settings_sheet = spreadsheet.worksheet("Settings")
        records = settings_sheet.get_all_records()

        # Look for existing em_profile row
        for i, r in enumerate(records):
            if r.get("Key") == "em_profile":
                em_profile = json.loads(r.get("Value", "{}"))
                print("✅ Loaded em_profile from Settings sheet")
                return

        # Not found — write default
        em_profile = default_profile
        settings_sheet.append_row(["em_profile", json.dumps(default_profile)])
        print("✅ Created em_profile in Settings sheet")

    except Exception as e:
        # Fallback to default in memory if sheet not ready yet
        em_profile = default_profile
        print(f"Warning: Could not load em_profile from sheet: {e}. Using defaults.")

def save_em_profile():
    """Save current em_profile back to Settings sheet."""
    try:
        settings_sheet = spreadsheet.worksheet("Settings")
        records = settings_sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("Key") == "em_profile":
                settings_sheet.update_cell(i + 2, 2, json.dumps(em_profile))
                return
        # Not found — append it
        settings_sheet.append_row(["em_profile", json.dumps(em_profile)])
    except Exception as e:
        print(f"Error saving em_profile: {e}")

def run_infrastructure_setup():
    """Run all setup steps on startup. Idempotent — safe to run every time."""
    global DRIVE_FOLDERS
    print("Running infrastructure setup...")
    setup_sheets()
    DRIVE_FOLDERS = setup_drive()
    setup_em_profile()
    print("✅ Infrastructure setup complete")

# --- Sheet References (after setup) ---
def get_sheet(name):
    try:
        return spreadsheet.worksheet(name)
    except Exception as e:
        print(f"Warning: Could not get sheet '{name}': {e}")
        return None

# We reference these after setup runs
def crm_sheet():
    return get_sheet("CRM")

def todo_sheet():
    return get_sheet("Todos")

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

    if birthday and age:
        age_line = f"- Age: {age}, {format_date(birthday)}"
    elif age:
        age_line = f"- Age: {age}, (unknown birthday)"
    else:
        age_line = "- Age: Unknown"

    if notes_raw:
        note_items = [n.strip() for n in notes_raw.split(";") if n.strip()]
        notes_formatted = "- Notes:\n" + "\n".join(f"  - {n}" for n in note_items)
    else:
        notes_formatted = "- Notes:\n  - None"

    followup_line = ""
    if followup_date:
        followup_line = f"\n- Follow Up: {format_date(followup_date)}"
        if followup_notes:
            followup_line += f"\n  - {followup_notes}"

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
    sheet = crm_sheet()
    records = sheet.get_all_records()
    for i, r in enumerate(records):
        if name.lower() in r.get("Name", "").lower():
            return i + 2, r
    return None, None

def find_all_rows(name):
    sheet = crm_sheet()
    records = sheet.get_all_records()
    results = []
    for i, r in enumerate(records):
        if name.lower() in r.get("Name", "").lower():
            results.append((i + 2, r))
    return results

# --- CRM Functions ---
def save_contact(data):
    try:
        sheet = crm_sheet()
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
        sheet = crm_sheet()
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
        sheet = crm_sheet()
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
        sheet = crm_sheet()
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
        sheet = crm_sheet()
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        sheet.delete_rows(row_num)
        return f"✅ Contact *{record.get('Name')}* deleted"
    except Exception as e:
        return f"❌ Error deleting contact: {str(e)}"

def search_contacts(keyword):
    try:
        sheet = crm_sheet()
        records = sheet.get_all_records()
        results = []
        for r in records:
            if any(keyword.lower() in str(v).lower() for v in r.values()):  # Fixed bug: was keyword.modify()
                results.append(r)
        if not results:
            return f"❌ No results for '{keyword}'"
        return f"🔍 *{len(results)} result(s) for '{keyword}':*\n\n" + "\n\n".join(format_contact(r) for r in results)
    except Exception as e:
        return f"❌ Error searching: {str(e)}"

def list_contacts():
    try:
        sheet = crm_sheet()
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
        sheet = crm_sheet()
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
        sheet = crm_sheet()
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
        sheet = crm_sheet()
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
        sheet = crm_sheet()
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
        sheet = todo_sheet()
        sheet.append_row([task, "Pending", date.today().strftime("%d/%m/%Y")])
        return f"✅ Added to your to-do list: _{task}_"
    except Exception as e:
        return f"❌ Error adding task: {str(e)}"

def complete_todo(task):
    try:
        sheet = todo_sheet()
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if task.lower() in r.get("Task", "").lower() and r.get("Status") == "Pending":
                sheet.update_cell(i + 2, 2, "Done")
                return f"✅ Marked as done: _{r.get('Task')}_"
        return f"❌ No pending task found matching '{task}'"
    except Exception as e:
        return f"❌ Error completing task: {str(e)}"

def list_todos():
    try:
        sheet = todo_sheet()
        records = sheet.get_all_records()
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
def get_events(days=1):
    try:
        calendar = get_calendar("Personal")
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

def is_calendar_request(text):
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=10,
            messages=[{"role": "user", "content": f"""Is this message asking to add, schedule, create, book, set up, pencil in, or block out a calendar event? Reply with only YES or NO.

Message: "{text}" """}]
        )
        return response.content[0].text.strip().upper() == "YES"
    except:
        return False

# --- Smart Calendar ---
def smart_add_event(text, user_id):
    try:
        today = date.today()
        tomorrow = today + timedelta(days=1)
        calendar_names = [
            "Estate Planning", "Work Meeting", "Personal",
            "Closing", "Urgent", "MinimaList", "Appointment"
        ]
        parse_prompt = f"""Today is {today.strftime('%d %b %Y')} ({today.strftime('%A')}).
Tomorrow is {tomorrow.strftime('%d %b %Y')}.

Available calendars: {', '.join(calendar_names)}

The user wants to add a calendar event. Extract the details from this message:
"{text}"

Respond ONLY with a JSON object in this exact format, nothing else:
{{
  "title": "event title",
  "start": "DD/MM/YYYY HH:MM",
  "end": "DD/MM/YYYY HH:MM",
  "notes": "any notes or empty string",
  "calendar": "exact calendar name from the list above or Personal if unclear"
}}

Rules:
- If no end time given, assume 1 hour after start
- If AM/PM not specified, use context to determine (6pm not 6am for dinner etc)
- Match calendar name exactly from the available list
- If no calendar specified, use Personal"""

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": parse_prompt}]
        )

        raw = response.content[0].text.strip()
        clean = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)

        title = parsed.get("title", "")
        start_str = parsed.get("start", "")
        end_str = parsed.get("end", "")
        notes = parsed.get("notes", "")
        cal_name = parsed.get("calendar", "Personal")

        if not title or not start_str or not end_str:
            return "❌ Couldn't parse the event details. Try something like: schedule dinner with James tomorrow 7pm"

        start = datetime.strptime(start_str, "%d/%m/%Y %H:%M")
        end = datetime.strptime(end_str, "%d/%m/%Y %H:%M")

        calendar = get_calendar(cal_name)
        if not calendar:
            calendar = get_calendar("Personal")

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
            f"✅ Done!\n\n"
            f"📅 *{title}*\n"
            f"🕐 {start.strftime('%d %b %Y, %H:%M')} → {end.strftime('%H:%M')}\n"
            f"📁 {calendar.name}\n"
            + (f"📝 {notes}" if notes else "")
        )

    except json.JSONDecodeError:
        return "❌ Couldn't parse the event details. Try something like: schedule dinner with James tomorrow 7pm"
    except Exception as e:
        return f"❌ Error creating event: {str(e)}"

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

# --- Scheduled Reminders ---
async def send_followup_reminders(app):
    try:
        sheet = crm_sheet()
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

# --- Birthday Greeting State ---
# Tracks who got a 12pm prompt today, pending acknowledgement
birthday_pending = {}

def ensure_birthday_greeted_column():
    """Make sure CRM has a Birthday Greeted column."""
    try:
        sheet = crm_sheet()
        headers = sheet.row_values(1)
        if "Birthday Greeted" not in headers:
            next_col = len(headers) + 1
            sheet.update_cell(1, next_col, "Birthday Greeted")
            print("Added Birthday Greeted column to CRM")
    except Exception as e:
        print(f"Error ensuring Birthday Greeted column: {e}")

def get_birthday_greeted_col():
    """Return the column index (1-based) of Birthday Greeted."""
    try:
        sheet = crm_sheet()
        headers = sheet.row_values(1)
        if "Birthday Greeted" in headers:
            return headers.index("Birthday Greeted") + 1
        return None
    except:
        return None

def generate_birthday_greeting(name, age, where_met, notes):
    """Generate a personalised birthday greeting via Claude."""
    greeting_prompt = (
        f"Write a warm, casual birthday greeting that someone could copy and paste to send to {name}, "
        f"who is turning {age} today.\n\n"
        f"Context: met at {where_met or 'unknown'}. Notes about them: {notes or 'none'}.\n\n"
        f"Rules:\n"
        f"- Must open with Happy birthday\n"
        f"- Casual and warm throughout, never stiff or corporate\n"
        f"- Add one personal touch based on their notes if relevant\n"
        f"- End with a warm closing line\n"
        f"- No dashes anywhere\n"
        f"- Do NOT use: Hope you had a great one, Hope its been a good one\n"
        f"- 2 to 4 sentences max\n"
        f"- Write it in first person, ready to copy and send"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": greeting_prompt}]
    )
    return resp.content[0].text.strip()

async def send_birthday_reminders(app):
    """12pm job: find today birthdays, generate greeting, send prompt."""
    global birthday_pending
    try:
        ensure_birthday_greeted_column()
        sheet = crm_sheet()
        records = sheet.get_all_records()
        today = date.today()

        for i, r in enumerate(records):
            bday_str = r.get("Birthday", "")
            if not bday_str:
                continue
            try:
                bday = datetime.strptime(bday_str, "%d/%m/%Y").date()
                if bday.day != today.day or bday.month != today.month:
                    continue

                name = r.get("Name", "")
                age = calculate_age(bday_str)
                notes = r.get("Notes", "")
                where_met = r.get("Where We Met", "")

                # Skip if already greeted today
                already_greeted = r.get("Birthday Greeted", "")
                if already_greeted == today.strftime("%d/%m/%Y"):
                    continue

                greeting = generate_birthday_greeting(name, age, where_met, notes)

                msg = (
                    f"It's {name}'s birthday today! Turning {age}. 🎂\n\n"
                    f"Here's a message you can send:\n\n"
                    f"{greeting}"
                )
                await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)

                birthday_pending[name] = {
                    "row": i + 2,
                    "greeted": False,
                    "greeting": greeting
                }
                print(f"Birthday prompt sent for {name}")

            except Exception as e:
                print(f"Birthday reminder error for {r.get('Name', '?')}: {e}")

    except Exception as e:
        print(f"Error in send_birthday_reminders: {e}")

async def send_birthday_followups(app):
    """2pm job: follow up on unacknowledged birthdays, then drop."""
    global birthday_pending
    try:
        still_pending = {k: v for k, v in birthday_pending.items() if not v.get("greeted")}

        for name, data in still_pending.items():
            try:
                msg = (
                    f"Did you get a chance to wish {name} happy birthday? 🎂\n\n"
                    f"Here's that message again:\n\n"
                    f"{data['greeting']}"
                )
                await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
                print(f"2pm follow-up sent for {name}")
            except Exception as e:
                print(f"Error sending 2pm follow-up for {name}: {e}")

        birthday_pending = {}

    except Exception as e:
        print(f"Error in send_birthday_followups: {e}")

def mark_birthday_greeted(name):
    """Mark contact as greeted today in CRM sheet."""
    try:
        col = get_birthday_greeted_col()
        if not col:
            return
        row_num, record = find_row(name)
        if record:
            sheet = crm_sheet()
            sheet.update_cell(row_num, col, date.today().strftime("%d/%m/%Y"))
    except Exception as e:
        print(f"Error marking birthday greeted for {name}: {e}")

def check_birthday_acknowledgement():
    """
    If birthday greetings are pending, mark them all as acknowledged.
    Called on any incoming message. Returns True if any were acknowledged.
    """
    global birthday_pending
    if not birthday_pending:
        return False
    acknowledged = []
    for name, data in birthday_pending.items():
        if not data.get("greeted"):
            data["greeted"] = True
            mark_birthday_greeted(name)
            acknowledged.append(name)
    return len(acknowledged) > 0



# --- Meeting Recap ---
# Session state: { user_id: { "event_name": str, "notes": [str], "step": "collecting"|"confirming", "pending_recap": dict } }
meeting_sessions = {}

MEETING_START_PHRASES = [
    "meeting recap", "taking notes", "log this meeting", "meeting notes",
    "recap for", "notes for", "log meeting", "start recap", "new recap",
    "networking recap", "presentation recap"
]

MEETING_DONE_PHRASES = [
    "done", "that's it", "thats it", "save that", "save it",
    "finish", "finished", "end recap", "process this", "that's all", "thats all"
]

def is_meeting_start(text):
    lower = text.lower()
    return any(p in lower for p in MEETING_START_PHRASES)

def is_meeting_done(text):
    lower = text.lower().strip()
    return any(lower == p or lower.startswith(p) for p in MEETING_DONE_PHRASES)

def extract_event_name(text):
    """Try to pull event name from the start message."""
    lower = text.lower()
    for phrase in ["recap for", "notes for", "meeting notes for", "taking notes for",
                   "meeting recap for", "log meeting for"]:
        if phrase in lower:
            idx = lower.index(phrase) + len(phrase)
            return text[idx:].strip().strip(".,!?")
    return ""

def tag_crm_contacts(text):
    """Find any CRM contacts mentioned in the text."""
    try:
        sheet = crm_sheet()
        records = sheet.get_all_records()
        tagged = []
        for r in records:
            name = r.get("Name", "")
            if name and name.lower() in text.lower():
                tagged.append(name)
        return tagged
    except:
        return []

def process_meeting_notes(event_name, notes_list):
    """Send all buffered notes to Claude and get a structured recap back as JSON."""
    combined = "\n".join(notes_list)
    prompt = (
        f"You are processing meeting notes for an event called: {event_name or 'unknown event'}\n\n"
        f"Here are the raw notes:\n{combined}\n\n"
        f"Extract and return a JSON object with these fields:\n"
        f"- event_name: string (use the provided event name, or infer if not given)\n"
        f"- topic: string (1 line summary of what the meeting/event was about)\n"
        f"- summary: string (2-4 sentences capturing key points, insights, context)\n"
        f"- action_items: list of strings (only include if there are clear action items, otherwise empty list)\n"
        f"- contacts_mentioned: list of strings (names of people mentioned)\n"
        f"- foreign_phrases: dict (any non-English phrases found, key=original, value=English translation)\n\n"
        f"Return ONLY the JSON object, no markdown, no preamble."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def format_recap_confirmation(recap):
    """Format recap for user confirmation — no emoji on header line."""
    lines = []
    lines.append(f"Meeting Recap — {recap.get('event_name', 'Untitled')}")
    lines.append("")
    lines.append(f"Topic: {recap.get('topic', '')}")
    lines.append("")
    lines.append(f"Summary:\n{recap.get('summary', '')}")

    action_items = recap.get("action_items", [])
    if action_items:
        lines.append("")
        lines.append("Action Items:")
        for item in action_items:
            lines.append(f"• {item}")

    foreign = recap.get("foreign_phrases", {})
    if foreign:
        lines.append("")
        lines.append("Phrases:")
        for orig, trans in foreign.items():
            lines.append(f"• {orig} [{trans}]")

    contacts = recap.get("contacts_mentioned", [])
    if contacts:
        lines.append("")
        lines.append(f"Contacts tagged: {', '.join(contacts)}")

    lines.append("")
    lines.append("Reply Y to save or E to edit.")
    return "\n".join(lines)

def save_meeting_recap(recap):
    """Save the confirmed recap to the Meeting Notes sheet."""
    try:
        sheet = spreadsheet.worksheet("Meeting Notes")
        today = date.today().strftime("%d/%m/%Y")
        action_items_str = "; ".join(recap.get("action_items", []))
        sheet.append_row([
            recap.get("event_name", ""),
            recap.get("topic", ""),
            recap.get("summary", ""),
            action_items_str,
            today
        ])
        return True
    except Exception as e:
        print(f"Error saving meeting recap: {e}")
        return False

def search_meeting_notes(query):
    """Search meeting notes by keyword or date range."""
    try:
        sheet = spreadsheet.worksheet("Meeting Notes")
        records = sheet.get_all_records()
        if not records:
            return "No meeting notes saved yet."

        query_lower = query.lower().strip()
        results = []
        for r in records:
            searchable = " ".join(str(v).lower() for v in r.values())
            if query_lower in searchable:
                results.append(r)

        if not results:
            return f"No meeting notes found for '{query}'."

        lines = [f"Found {len(results)} recap(s):\n"]
        for r in results:
            lines.append(f"📋 {r.get('Event Name', '')} — {r.get('Date', '')}")
            lines.append(f"   {r.get('Topic', '')}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching meeting notes: {str(e)}"

async def handle_meeting_session(user_id, text, update):
    """Handle messages when user is in an active meeting recap session."""
    session = meeting_sessions[user_id]
    step = session.get("step")

    # Confirming step — waiting for Y or E
    if step == "confirming":
        if text.strip().upper() == "Y":
            saved = save_meeting_recap(session["pending_recap"])
            # Tag CRM contacts
            contacts = session["pending_recap"].get("contacts_mentioned", [])
            for name in contacts:
                row_num, record = find_row(name)
                if record:
                    pass  # silently tagged via presence in recap
            del meeting_sessions[user_id]
            if saved:
                await update.message.reply_text("Saved! 📋")
            else:
                await update.message.reply_text("Something went wrong saving the recap. Try again.")

        elif text.strip().upper() == "E":
            session["step"] = "collecting"
            session["notes"] = []
            await update.message.reply_text(
                "No worries, let's redo it. Send your notes again and say done when you're finished."
            )

        else:
            await update.message.reply_text("Reply Y to save or E to start over.")
        return

    # Get event name step
    if step == "get_name":
        session["event_name"] = text.strip()
        session["step"] = "collecting"
        await update.message.reply_text(
            f"Got it. Send your notes for {text.strip()} and say done when you're finished."
        )
        return

    # Collecting step — buffering notes
    if is_meeting_done(text):
        if not session.get("notes"):
            await update.message.reply_text("You haven't sent any notes yet. Send your notes then say done.")
            return

        await update.message.reply_text("On it, give me a sec...")

        try:
            recap = process_meeting_notes(session.get("event_name", ""), session["notes"])
            session["pending_recap"] = recap
            session["step"] = "confirming"
            confirmation = format_recap_confirmation(recap)
            await update.message.reply_text(confirmation)
        except Exception as e:
            await update.message.reply_text(f"Couldn't process the notes: {str(e)}. Try again.")
        return

    # Still collecting — buffer the note
    session["notes"].append(text)
    # Acknowledge silently (no reply) to keep flow natural



# --- Custom Reminders ---
# Reminders sheet columns: ID, Message, Scheduled Time, Recurrence, Status, Attempts, Contact
# Status: pending | sent | cancelled
# Recurrence: once | daily | weekly | monthly | or natural string like "every Monday"

TIMEZONE = pytz.timezone("Asia/Kuala_Lumpur")

# Tracks the last reminder fired per user so they can say "remind me again in 2 hours"
last_fired_reminder = {}

def reminders_sheet():
    try:
        return spreadsheet.worksheet("Reminders")
    except:
        ws = spreadsheet.add_worksheet(title="Reminders", rows=500, cols=8)
        ws.append_row(["ID", "Message", "Scheduled Time", "Recurrence", "Status", "Attempts", "Contact"])
        return ws

def generate_reminder_id():
    """Simple unique ID based on timestamp."""
    return datetime.now().strftime("%Y%m%d%H%M%S")

def parse_reminder_request(text):
    """Use Claude to parse a natural language reminder request."""
    now = datetime.now(TIMEZONE)
    prompt = (
        f"Today is {now.strftime('%A, %d %b %Y')} and the time is {now.strftime('%H:%M')} (Asia/Kuala_Lumpur).\n\n"
        f"Parse this reminder request and return a JSON object:\n"
        f"Request: {text}\n\n"
        f"Return ONLY a JSON object with these fields:\n"
        f"- message: string (what to remind about, concise)\n"
        f"- scheduled_time: string in format YYYY-MM-DD HH:MM (exact datetime to fire)\n"
        f"- recurrence: string — one of: once, daily, weekly, monthly, or a description like 'every Monday' (use 'once' if not recurring)\n"
        f"- contact: string (name of person mentioned, or empty string)\n\n"
        f"Rules:\n"
        f"- If no time specified, default to 09:00\n"
        f"- 'tomorrow' means {(now + timedelta(days=1)).strftime('%Y-%m-%d')}\n"
        f"- 'next week' means {(now + timedelta(days=7)).strftime('%Y-%m-%d')}\n"
        f"- For recurring reminders, scheduled_time is the FIRST occurrence\n"
        f"- Return ONLY the JSON, no markdown, no explanation"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def parse_reschedule_request(text, original_message):
    """Parse a follow-up reschedule like 'remind me again in 2 hours'."""
    now = datetime.now(TIMEZONE)
    prompt = (
        f"Today is {now.strftime('%A, %d %b %Y')} and the time is {now.strftime('%H:%M')}.\n\n"
        f"The user wants to reschedule a reminder. Original reminder: '{original_message}'\n"
        f"Reschedule request: '{text}'\n\n"
        f"Return ONLY a JSON object:\n"
        f"- scheduled_time: string in format YYYY-MM-DD HH:MM\n"
        f"- recurrence: string (once, daily, weekly, monthly — use 'once' if unclear)\n\n"
        f"Return ONLY the JSON."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def add_reminder(message, scheduled_time_str, recurrence="once", contact=""):
    """Add a reminder to the Reminders sheet."""
    sheet = reminders_sheet()
    reminder_id = generate_reminder_id()
    sheet.append_row([
        reminder_id,
        message,
        scheduled_time_str,
        recurrence,
        "pending",
        "0",
        contact
    ])
    return reminder_id

def cancel_reminder_by_keyword(keyword):
    """Cancel reminders matching a keyword."""
    sheet = reminders_sheet()
    records = sheet.get_all_records()
    cancelled = []
    for i, r in enumerate(records):
        if keyword.lower() in r.get("Message", "").lower() and r.get("Status") == "pending":
            sheet.update_cell(i + 2, 5, "cancelled")
            cancelled.append(r.get("Message", ""))
    return cancelled

def list_reminders():
    """List all pending reminders."""
    sheet = reminders_sheet()
    records = sheet.get_all_records()
    pending = [r for r in records if r.get("Status") == "pending"]
    if not pending:
        return "No upcoming reminders."
    lines = [f"You have {len(pending)} upcoming reminder(s):\n"]
    for r in pending:
        t = r.get("Scheduled Time", "")
        msg = r.get("Message", "")
        rec = r.get("Recurrence", "once")
        rec_str = f" ({rec})" if rec != "once" else ""
        lines.append(f"• {msg} — {t}{rec_str}")
    return "\n".join(lines)

def get_next_recurrence(scheduled_time_str, recurrence):
    """Calculate the next fire time for a recurring reminder."""
    try:
        dt = datetime.strptime(scheduled_time_str, "%Y-%m-%d %H:%M")
        if recurrence == "daily":
            return (dt + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        elif recurrence == "weekly" or "monday" in recurrence.lower() or "every" in recurrence.lower():
            return (dt + timedelta(weeks=1)).strftime("%Y-%m-%d %H:%M")
        elif recurrence == "monthly":
            # Add roughly a month
            if dt.month == 12:
                next_dt = dt.replace(year=dt.year + 1, month=1)
            else:
                next_dt = dt.replace(month=dt.month + 1)
            return next_dt.strftime("%Y-%m-%d %H:%M")
        return None
    except:
        return None

async def check_and_fire_reminders(app):
    """Check sheet every minute and fire due reminders."""
    try:
        sheet = reminders_sheet()
        records = sheet.get_all_records()
        now = datetime.now(TIMEZONE).replace(second=0, microsecond=0)

        for i, r in enumerate(records):
            if r.get("Status") != "pending":
                continue

            scheduled_str = r.get("Scheduled Time", "")
            if not scheduled_str:
                continue

            try:
                scheduled = datetime.strptime(scheduled_str, "%Y-%m-%d %H:%M")
                scheduled = TIMEZONE.localize(scheduled)
            except:
                continue

            # Fire if within the current minute
            if scheduled <= now:
                attempts = int(r.get("Attempts", 0))
                message = r.get("Message", "")
                recurrence = r.get("Recurrence", "once")
                contact = r.get("Contact", "")
                row = i + 2

                # Build reminder message
                reminder_msg = f"🔔 Reminder: {message}"
                if contact:
                    _, record = find_row(contact)
                    if record:
                        notes = record.get("Notes", "")
                        if notes:
                            reminder_msg += f"\n\n({contact}: {notes.split(';')[0].strip()})"

                await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=reminder_msg)

                # Store as last fired for potential reschedule
                last_fired_reminder[YOUR_CHAT_ID] = {
                    "id": r.get("ID"),
                    "message": message,
                    "row": row
                }

                if recurrence != "once":
                    # Schedule next occurrence
                    next_time = get_next_recurrence(scheduled_str, recurrence)
                    if next_time:
                        sheet.update_cell(row, 3, next_time)
                        sheet.update_cell(row, 6, "0")  # reset attempts
                    else:
                        sheet.update_cell(row, 5, "sent")
                else:
                    # One-off: mark attempts, retry once after 2 hours if first attempt
                    if attempts == 0:
                        retry_time = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
                        sheet.update_cell(row, 3, retry_time)
                        sheet.update_cell(row, 6, "1")
                    else:
                        # Second attempt done — mark sent and drop
                        sheet.update_cell(row, 5, "sent")

    except Exception as e:
        print(f"Error in check_and_fire_reminders: {e}")

def is_reminder_request(text):
    """Detect if a message is asking to set a reminder."""
    lower = text.lower()
    triggers = ["remind me", "set a reminder", "reminder for", "don't let me forget",
                "alert me", "notify me", "ping me"]
    return any(t in lower for t in triggers)

def is_reschedule_request(text):
    """Detect if message is rescheduling the last reminder."""
    lower = text.lower()
    triggers = ["remind me again", "snooze", "again in", "remind again",
                "push it to", "reschedule"]
    return any(t in lower for t in triggers)

def is_cancel_reminder_request(text):
    """Detect cancel reminder intent."""
    lower = text.lower()
    return ("cancel" in lower or "delete" in lower or "remove" in lower) and "reminder" in lower

def handle_new_reminder(text):
    """Parse and save a new reminder. Returns confirmation string."""
    try:
        parsed = parse_reminder_request(text)
        message = parsed.get("message", text)
        scheduled_time = parsed.get("scheduled_time", "")
        recurrence = parsed.get("recurrence", "once")
        contact = parsed.get("contact", "")

        if not scheduled_time:
            return "Couldn't figure out when to remind you. Try something like 'remind me to call James tomorrow at 3pm'."

        # Validate contact against CRM
        if contact:
            _, record = find_row(contact)
            if not record:
                contact = ""  # contact not in CRM, ignore

        add_reminder(message, scheduled_time, recurrence, contact)

        # Format confirmation
        try:
            dt = datetime.strptime(scheduled_time, "%Y-%m-%d %H:%M")
            time_str = dt.strftime("%d %b %Y at %I:%M %p")
        except:
            time_str = scheduled_time

        rec_str = f" ({recurrence})" if recurrence != "once" else ""
        return f"Done, I'll remind you to {message} on {time_str}{rec_str}."

    except Exception as e:
        return f"Couldn't parse that reminder: {str(e)}. Try: 'remind me to call James tomorrow at 3pm'."

def handle_reschedule(text, user_id):
    """Reschedule the last fired reminder."""
    try:
        last = last_fired_reminder.get(user_id)
        if not last:
            return "Not sure which reminder you mean. Can you be more specific?"

        parsed = parse_reschedule_request(text, last["message"])
        new_time = parsed.get("scheduled_time", "")
        recurrence = parsed.get("recurrence", "once")

        if not new_time:
            return "Couldn't figure out the new time. Try 'remind me again in 2 hours'."

        sheet = reminders_sheet()
        row = last.get("row")
        if row:
            sheet.update_cell(row, 3, new_time)
            sheet.update_cell(row, 5, "pending")
            sheet.update_cell(row, 6, "0")

        try:
            dt = datetime.strptime(new_time, "%Y-%m-%d %H:%M")
            time_str = dt.strftime("%d %b at %I:%M %p")
        except:
            time_str = new_time

        return f"Got it, I'll remind you about {last['message']} again on {time_str}."

    except Exception as e:
        return f"Couldn't reschedule: {str(e)}"


# --- Em System Prompt Builder ---
def build_system_prompt():
    """Build Em's system prompt, incorporating em_profile preferences."""
    profile_notes = ""
    if em_profile:
        forbidden = ", ".join(em_profile.get("forbidden_phrases", []))
        profile_notes = f"\nForbidden phrases (never use): {forbidden}" if forbidden else ""

    return (
        "# Em — Your Personal Assistant\n\n"
        "## Core Identity\n"
        "You're Em — a smart, focused personal assistant with a casual, warm vibe. "
        "You keep things real and get stuff done without the corporate robot speak.\n\n"
        "## Communication Style\n"
        "- Natural and conversational — light slang like 'got it', 'sure thing', 'on it', 'no worries', 'lemme check that', 'all good'\n"
        "- Clean and simple — never over the top or trying too hard\n"
        "- Helpful and focused — you're here to make life easier, not to chat\n"
        "- Never say 'cool cool'\n"
        "- Never use the shaka emoji\n"
        "- Always capitalise the first letter of each sentence\n"
        "- NEVER use dashes or hyphens in conversational replies under any circumstances. Write in natural flowing sentences instead.\n"
        "- Only use dashes when displaying CRM contact info in the required format.\n"
        "- Each piece of information on its own line.\n"
        "- No unnecessary prompting or nudging.\n"
        f"{profile_notes}\n\n"
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
        "Always separate notes into individual bullet points. Never dump them in one line.\n\n"
        "## What You Don't Do\n"
        "- Sound stiff or corporate\n"
        "- Act like a typical AI assistant\n"
        "- Make small talk for the sake of it\n"
        "- Get repetitive with phrases or greetings"
    )

# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != YOUR_CHAT_ID:
        return

    text = update.message.text.strip()
    lower = text.lower()

    # Check birthday acknowledgement — any incoming message counts
    check_birthday_acknowledgement()

    # Handle active meeting recap session
    if user_id in meeting_sessions:
        await handle_meeting_session(user_id, text, update)
        return

    # Handle active CRM edit session
    if user_id in edit_sessions:
        await handle_edit_session(user_id, text, update)
        return

    reply = None

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

    # Meeting Recap Commands
    elif lower.startswith("search meetings ") or lower.startswith("find meeting "):
        query = text[16:] if lower.startswith("search meetings ") else text[13:]
        reply = search_meeting_notes(query.strip())
    elif lower == "cancel" and user_id in meeting_sessions:
        del meeting_sessions[user_id]
        reply = "Recap cancelled."
    elif is_meeting_start(text):
        event_name = extract_event_name(text)
        meeting_sessions[user_id] = {
            "step": "collecting",
            "event_name": event_name,
            "notes": []
        }
        if event_name:
            reply = f"Got it, taking notes for {event_name}. Send everything over and say done when you're finished."
        else:
            reply = "Sure, what's the event name?"
            meeting_sessions[user_id]["step"] = "get_name"

    # Reminder Commands
    elif lower == "reminders" or lower == "my reminders" or lower == "list reminders":
        reply = list_reminders()
    elif is_cancel_reminder_request(text):
        keyword = text.lower().replace("cancel", "").replace("delete", "").replace("remove", "").replace("reminder", "").strip()
        cancelled = cancel_reminder_by_keyword(keyword) if keyword else []
        if cancelled:
            reply = f"Cancelled: {', '.join(cancelled)}"
        else:
            reply = "Couldn't find a matching reminder to cancel."
    elif is_reschedule_request(text):
        reply = handle_reschedule(text, user_id)
    elif is_reminder_request(text):
        reply = handle_new_reminder(text)

    # Todo Commands
    elif lower.startswith("todo "):
        reply = add_todo(text[5:])
    elif lower.startswith("done "):
        reply = complete_todo(text[5:])
    elif lower == "todos":
        reply = list_todos()

    # Calendar Commands
    elif lower.startswith("add event") or lower.startswith("schedule ") or lower.startswith("create event"):
        reply = smart_add_event(text, user_id)
    elif lower == "events today":
        reply = get_events(1)
    elif lower == "events week":
        reply = get_events(7)
    elif lower.startswith("delete event "):
        reply = delete_calendar_event(text[13:])

    # Infrastructure / Settings
    elif lower == "em status":
        folder_status = "✅ Connected" if DRIVE_FOLDERS else "❌ Not connected"
        profile_version = em_profile.get("version", "unknown")
        reply = (
            f"⚙️ *Em Status*\n\n"
            f"Google Drive: {folder_status}\n"
            f"em_profile version: {profile_version}\n"
            f"Sheets: ✅ Connected\n"
            f"Scheduler: ✅ Running"
        )

    # Help
    elif lower == "help":
        reply = (
            "🤖 *Em — here's what I can do:*\n\n"
            "*CRM:*\n"
            "save, find, note, followup, update, edit, delete, search, list, stats, followups, overdue, birthdays, soon, lastcontact\n\n"
            "*Calendar:*\n"
            "Just tell me naturally — 'schedule dinner tomorrow 7pm' or 'add event'\n"
            "events today / events week / delete event\n\n"
            "*To-Do:*\n"
            "todo, done, todos\n\n"
            "*Other:*\n"
            "em status — check Em's health\n\n"
            "Or just chat — I'll figure it out 👍"
        )

    # Claude Chat fallback
    else:
        if is_reschedule_request(text) and user_id in last_fired_reminder:
            reply = handle_reschedule(text, user_id)
        elif is_reminder_request(text):
            reply = handle_new_reminder(text)
        elif is_calendar_request(text):
            reply = smart_add_event(text, user_id)
        else:
            if user_id not in conversation_histories:
                conversation_histories[user_id] = []
            conversation_histories[user_id].append({"role": "user", "content": text})
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=build_system_prompt(),
                messages=conversation_histories[user_id]
            )
            reply = response.content[0].text
            conversation_histories[user_id].append({"role": "assistant", "content": reply})

    if reply:
        try:
            await update.message.reply_text(reply, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(reply)

# --- Main ---
async def post_init(app):
    # Run infrastructure setup
    run_infrastructure_setup()

    timezone = pytz.timezone("Asia/Kuala_Lumpur")
    scheduler = AsyncIOScheduler(timezone=timezone)

    # Follow-up reminders at 9am
    scheduler.add_job(send_followup_reminders, "cron", hour=9, minute=0, args=[app])

    # Birthday greetings at 12pm
    scheduler.add_job(send_birthday_reminders, "cron", hour=12, minute=0, args=[app])

    # Birthday 2pm follow-up
    scheduler.add_job(send_birthday_followups, "cron", hour=14, minute=0, args=[app])

    # Custom reminders — check every minute
    scheduler.add_job(check_and_fire_reminders, "interval", minutes=1, args=[app])

    scheduler.start()
    print("✅ Scheduler started — follow-ups at 9am, birthdays at 12pm + 2pm, reminders every minute")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Em is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
