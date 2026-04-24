import os
import json
import re
import io
import requests
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
AVIATIONSTACK_API_KEY = os.getenv("AVIATIONSTACK_API_KEY", "")
EXCHANGE_RATE_API_KEY = os.getenv("EXCHANGE_RATE_API_KEY", "")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
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

    # New CRM column headers
    CRM_HEADERS = [
        "Name", "Alias", "Birthday", "Relationship", "Context", "Notes",
        "Follow Up Date", "Follow Up Notes", "Last Updated", "Birthday Greeted",
        "Referred By", "Referral Date", "Email", "Address"
    ]

    # Rename sheet1 -> CRM if CRM doesn't exist yet
    if "CRM" not in existing:
        try:
            sheet1 = spreadsheet.worksheet("Sheet1")
            sheet1.update_title("CRM")
            print("Renamed Sheet1 -> CRM")
        except Exception:
            if "CRM" not in [ws.title for ws in spreadsheet.worksheets()]:
                ws = spreadsheet.add_worksheet(title="CRM", rows=1000, cols=20)
                ws.append_row(CRM_HEADERS)
                print("Created CRM tab")

    # Migrate existing CRM headers if needed
    try:
        crm_ws = spreadsheet.worksheet("CRM")
        current_headers = crm_ws.row_values(1)
        if current_headers != CRM_HEADERS:
            _migrate_crm_headers(crm_ws, current_headers, CRM_HEADERS)
    except Exception as e:
        print(f"CRM header check error: {e}")

    # Todos tab
    if "Todos" not in existing:
        ws = spreadsheet.add_worksheet(title="Todos", rows=500, cols=3)
        ws.append_row(["Task", "Status", "Added"])
        print("Created Todos tab")

    # All other required tabs
    required_tabs = {
        "Meeting Notes": ["Event Name", "Topic", "Summary", "Action Items", "Date"],
        "Expenses": ["Date", "Merchant", "Amount", "Currency", "SGD Amount", "Category",
                     "Card", "Receipt Link", "Reconciled", "Notes", "Trip ID"],
        "Bills": ["Name", "Bank", "Due Date", "Estimated Amount", "Notes"],
        "Cards": ["Card Name", "Bank", "Type", "Notes"],
        "Merchant Map": ["Merchant", "Category", "Card"],
        "Restaurants": ["Name", "Location", "Country", "Tags", "Notes"],
        "Portfolio": ["Stock", "Quantity", "Buy Price", "Buy Date", "Notes"],
        "Settings": ["Key", "Value"],
        "Trips": ["Trip ID", "Leg", "Type", "Flight No", "Airline", "From", "To",
                  "Dep Date", "Dep Time", "Arr Date", "Arr Time", "Terminal", "Gate",
                  "Booking Ref", "Timezone", "Status"],
        "Accommodation": ["Trip ID", "Property Name", "English Address", "Chinese Address",
                          "Check In", "Check Out"]
    }

    existing_now = [ws.title for ws in spreadsheet.worksheets()]
    for tab_name, headers in required_tabs.items():
        if tab_name not in existing_now:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=500, cols=len(headers))
            ws.append_row(headers)
            print(f"Created tab: {tab_name}")

    print("✅ Sheets setup complete")

def _migrate_crm_headers(ws, old_headers, new_headers):
    """Migrate CRM sheet from old column layout to new layout."""
    try:
        all_data = ws.get_all_values()
        if not all_data:
            ws.update(range_name='A1', values=[new_headers])
            print("CRM headers initialised (empty sheet)")
            return

        old_h = all_data[0]
        rows = all_data[1:]

        # Build mapping from old col name -> index
        old_idx = {h: i for i, h in enumerate(old_h)}

        def get_old(row, col_name):
            i = old_idx.get(col_name)
            return row[i] if i is not None and i < len(row) else ""

        migrated = [new_headers]
        for row in rows:
            name = get_old(row, "Name")
            if not name:
                continue
            # Map old fields to new; old "Where We Met" -> Relationship
            migrated.append([
                name,
                get_old(row, "Alias"),
                get_old(row, "Birthday"),
                get_old(row, "Where We Met"),   # becomes Relationship
                get_old(row, "Context"),
                get_old(row, "Notes"),
                get_old(row, "Follow Up Date"),
                get_old(row, "Follow Up Notes"),
                get_old(row, "Last Updated"),
                get_old(row, "Birthday Greeted"),
                get_old(row, "Referred By"),
                get_old(row, "Referral Date"),
                get_old(row, "Email"),
                get_old(row, "Address"),
            ])

        ws.clear()
        if migrated:
            ws.update(range_name='A1', values=migrated)
        print(f"✅ CRM migrated to new column layout ({len(migrated)-1} contacts)")
    except Exception as e:
        print(f"Error migrating CRM headers: {e}")

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
    auto_restore_overseas_on_startup()
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

def format_contact(r, show_private=False):
    name = r.get("Name", "")
    alias = r.get("Alias", "")
    birthday = r.get("Birthday", "")
    age = calculate_age(birthday) if birthday else ""
    relationship = r.get("Relationship", "")
    context = r.get("Context", "")
    notes_raw = r.get("Notes", "")
    followup_date = r.get("Follow Up Date", "")
    followup_notes = r.get("Follow Up Notes", "")
    last_updated = r.get("Last Updated", "")
    referred_by = r.get("Referred By", "")
    referral_date = r.get("Referral Date", "")
    email = r.get("Email", "")
    address = r.get("Address", "")

    lines = [f"*{name}*"]
    if alias:
        lines.append(f"- Also known as: {alias}")
    if relationship:
        lines.append(f"- Relationship: {relationship}")
    if context:
        lines.append(f"- Context: {context}")
    if birthday and age:
        lines.append(f"- Birthday: {format_date(birthday)} (age {age})")
    elif birthday:
        lines.append(f"- Birthday: {format_date(birthday)}")

    if notes_raw:
        note_items = [n.strip() for n in notes_raw.split(";") if n.strip()]
        lines.append("- Notes:")
        for n in note_items:
            lines.append(f"  - {n}")
    else:
        lines.append("- Notes:\n  - None")

    if followup_date:
        lines.append(f"- Follow Up: {format_date(followup_date)}")
        if followup_notes:
            lines.append(f"  - {followup_notes}")

    if referred_by:
        ref_line = f"- Referred by: {referred_by}"
        if referral_date:
            ref_line += f" on {format_date(referral_date)}"
        lines.append(ref_line)

    # Private fields — only shown when explicitly asked
    if show_private:
        if email:
            lines.append(f"- Email: {email}")
        if address:
            lines.append(f"- Address: {address}")

    if last_updated:
        lines.append(f"\n_Last updated: {format_date(last_updated)}_")

    return "\n".join(lines)

def find_row(name):
    """Search Name first, then Alias, then first-name match. Returns (row_num, record) for best single match."""
    sheet = crm_sheet()
    records = sheet.get_all_records()
    name_lower = name.strip().lower()

    # 1. Exact full name match
    for i, r in enumerate(records):
        if r.get("Name", "").lower() == name_lower:
            return i + 2, r

    # 2. Full alias match
    for i, r in enumerate(records):
        if r.get("Alias", "").lower() == name_lower:
            return i + 2, r

    # 3. Substring match on Name
    matches = []
    for i, r in enumerate(records):
        if name_lower in r.get("Name", "").lower():
            matches.append((i + 2, r))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches[0]  # Caller should use find_all_rows for disambiguation

    # 4. Substring match on Alias
    alias_matches = []
    for i, r in enumerate(records):
        if name_lower in r.get("Alias", "").lower():
            alias_matches.append((i + 2, r))
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        return alias_matches[0]

    # 5. First-name match across Name and Alias
    first_matches = []
    for i, r in enumerate(records):
        full_name = r.get("Name", "")
        alias = r.get("Alias", "")
        first_name = full_name.split()[0].lower() if full_name else ""
        alias_first = alias.split()[0].lower() if alias else ""
        if name_lower == first_name or name_lower == alias_first:
            first_matches.append((i + 2, r))
    if first_matches:
        return first_matches[0]

    return None, None

def find_all_rows(name):
    """Like find_row but returns all matches for disambiguation."""
    sheet = crm_sheet()
    records = sheet.get_all_records()
    name_lower = name.strip().lower()
    results = []

    for i, r in enumerate(records):
        full_name = r.get("Name", "").lower()
        alias = r.get("Alias", "").lower()
        first_name = full_name.split()[0] if full_name else ""
        alias_first = alias.split()[0] if alias else ""
        if (name_lower in full_name or name_lower in alias or
                name_lower == first_name or name_lower == alias_first):
            results.append((i + 2, r))
    return results

def disambiguate_contacts(matches):
    """Return a disambiguation prompt if multiple contacts match."""
    names = [r.get("Name", "?") for _, r in matches]
    options = " or ".join(f"*{n}*" for n in names)
    return f"Did you mean {options}?"

# --- CRM Functions ---
def save_contact(data):
    try:
        sheet = crm_sheet()
        parts = [p.strip() for p in data.split(",")]
        while len(parts) < 13:
            parts.append("")
        name = parts[0]
        alias = parts[1] if len(parts) > 1 else ""
        birthday = parts[2] if len(parts) > 2 else ""
        relationship = parts[3] if len(parts) > 3 else ""
        context = parts[4] if len(parts) > 4 else ""
        notes = parts[5] if len(parts) > 5 else ""
        followup_date = parts[6] if len(parts) > 6 else ""
        followup_notes = parts[7] if len(parts) > 7 else ""
        last_updated = date.today().strftime("%d/%m/%Y")
        if not name:
            return "❌ Name is required"
        sheet.append_row([
            name, alias, birthday, relationship, context, notes,
            followup_date, followup_notes, last_updated, "", "", "", "", ""
        ])
        return f"✅ Contact saved!\n\n" + format_contact({
            "Name": name, "Alias": alias, "Birthday": birthday,
            "Relationship": relationship, "Context": context,
            "Notes": notes, "Follow Up Date": followup_date,
            "Follow Up Notes": followup_notes, "Last Updated": last_updated
        })
    except Exception as e:
        return f"❌ Error saving contact: {str(e)}"

def find_contact(name, show_private=False):
    try:
        results = find_all_rows(name)
        if not results:
            return f"❌ No contact found for '{name}'"
        if len(results) > 1:
            return disambiguate_contacts(results)
        return format_contact(results[0][1], show_private=show_private)
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
        # Col 6 = Notes in new schema
        sheet.update_cell(row_num, 6, new_note)
        sheet.update_cell(row_num, 9, date.today().strftime("%d/%m/%Y"))  # Last Updated
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
        sheet.update_cell(row_num, 7, followup_date)   # Follow Up Date
        sheet.update_cell(row_num, 8, followup_notes)   # Follow Up Notes
        sheet.update_cell(row_num, 9, date.today().strftime("%d/%m/%Y"))  # Last Updated
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
        # Col indices: Name=1, Alias=2, Birthday=3, Relationship=4, Context=5,
        # Notes=6, Follow Up Date=7, Follow Up Notes=8, Last Updated=9,
        # Birthday Greeted=10, Referred By=11, Referral Date=12, Email=13, Address=14
        field_map = {
            "alias": 2, "birthday": 3, "relationship": 4, "context": 5,
            "notes": 6, "follow up date": 7, "follow up notes": 8,
            "referred by": 11, "referral date": 12, "email": 13, "address": 14
        }
        col = field_map.get(field.lower())
        if not col:
            valid = ", ".join(field_map.keys())
            return f"❌ Unknown field '{field}'. Options: {valid}"
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        sheet.update_cell(row_num, col, value)
        sheet.update_cell(row_num, 9, date.today().strftime("%d/%m/%Y"))  # Last Updated
        return f"✅ {field.title()} updated for *{record.get('Name')}*"
    except Exception as e:
        return f"❌ Error updating contact: {str(e)}"

def update_contact_field_natural(name, field, value):
    """Update a single field by natural language field name. Returns reply string."""
    return update_field(f"{name}, {field}, {value}")

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
            if any(keyword.lower() in str(v).lower() for v in r.values()):
                results.append(r)
        if not results:
            return f"No contacts found matching '{keyword}' in the CRM."
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
            rel = r.get("Relationship", "") or r.get("Context", "") or "Unknown"
            response += f"👤 {r.get('Name', '')} — {rel}\n"
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
                except Exception as e:
                    print(f"get_stats: bad follow up date in row: {e}")
            bday = r.get("Birthday", "")
            if bday:
                try:
                    b = datetime.strptime(bday, "%d/%m/%Y").date()
                    this_year = b.replace(year=today.year)
                    if this_year < today:
                        this_year = b.replace(year=today.year + 1)
                    if (this_year - today).days <= 30:
                        birthdays_month += 1
                except Exception as e:
                    print(f"get_stats: bad birthday in row: {e}")
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
                except Exception as e:
                    print(f"upcoming_followups: bad date in row: {e}")
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
                except Exception as e:
                    print(f"overdue_followups: bad date in row: {e}")
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
                except Exception as e:
                    print(f"upcoming_birthdays: bad date in row: {e}")
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

# --- Referral Tracking ---
def set_referral(referrer_name, referred_name):
    """Record that referrer_name referred referred_name."""
    try:
        sheet = crm_sheet()
        row_num, record = find_row(referred_name)
        if not record:
            return f"❌ No contact found for '{referred_name}'. Add them first."
        today = date.today().strftime("%d/%m/%Y")
        sheet.update_cell(row_num, 11, referrer_name)   # Referred By
        sheet.update_cell(row_num, 12, today)            # Referral Date
        sheet.update_cell(row_num, 9, today)             # Last Updated
        # Set relationship to Prospect if blank
        if not record.get("Relationship", ""):
            sheet.update_cell(row_num, 4, "Prospect")
        return f"✅ Got it — {referred_name} was referred by {referrer_name} (Referral Date: {format_date(today)})"
    except Exception as e:
        return f"❌ Error recording referral: {str(e)}"

def get_referrals_by(referrer_name):
    """List all contacts referred by a given person."""
    try:
        sheet = crm_sheet()
        records = sheet.get_all_records()
        results = [r for r in records if r.get("Referred By", "").lower() == referrer_name.lower()]
        if not results:
            return f"No referrals found from {referrer_name}."
        lines = [f"*Referrals from {referrer_name}:*\n"]
        for r in results:
            ref_date = format_date(r.get("Referral Date", "")) if r.get("Referral Date") else "unknown date"
            lines.append(f"👤 {r.get('Name')} — {r.get('Relationship', 'Prospect')} (referred {ref_date})")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {str(e)}"

def get_all_referrals():
    """List all contacts with a referral, grouped by referrer."""
    try:
        sheet = crm_sheet()
        records = sheet.get_all_records()
        referrals = [r for r in records if r.get("Referred By", "")]
        if not referrals:
            return "No referrals recorded yet."
        grouped = {}
        for r in referrals:
            ref_by = r.get("Referred By", "Unknown")
            grouped.setdefault(ref_by, []).append(r)
        lines = ["*All Referrals:*\n"]
        for referrer, contacts in sorted(grouped.items(), key=lambda x: -len(x[1])):
            lines.append(f"*{referrer}* ({len(contacts)} referral{'s' if len(contacts) != 1 else ''})")
            for c in contacts:
                lines.append(f"  → {c.get('Name')} ({c.get('Relationship', 'Prospect')})")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {str(e)}"

def get_top_referrers():
    """Rank contacts by number of referrals made."""
    try:
        sheet = crm_sheet()
        records = sheet.get_all_records()
        counts = {}
        for r in records:
            ref_by = r.get("Referred By", "")
            if ref_by:
                counts[ref_by] = counts.get(ref_by, 0) + 1
        if not counts:
            return "No referrals recorded yet."
        ranked = sorted(counts.items(), key=lambda x: -x[1])
        lines = ["*Top Referrers:*\n"]
        for i, (name, count) in enumerate(ranked, 1):
            lines.append(f"{i}. {name} — {count} referral{'s' if count != 1 else ''}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {str(e)}"

# --- Excel Import ---
# excel_import_sessions tracks state: { user_id: { "step": "awaiting_columns"|"awaiting_file", "column_order": [...] } }
excel_import_sessions = {}

def parse_excel_column_order(text):
    """Parse user's declared column order from a message like 'Name, Email, Date of Birth, Alias'."""
    # Strip numbering like "1. Name 2. Email" or "Name, Email, DOB"
    text = re.sub(r'\d+[\.\)]\s*', '', text)
    parts = [p.strip() for p in re.split(r'[,\n]', text) if p.strip()]
    return parts

async def handle_excel_import(file_bytes, column_order, update):
    """Import contacts from Excel bytes using declared column order."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
        ws_xl = wb.active
        rows = list(ws_xl.iter_rows(values_only=True))

        # Skip header row if first row looks like headers
        data_rows = rows
        if rows:
            first = [str(c).strip() if c else "" for c in rows[0]]
            if any(h.lower() in ["name", "email", "alias", "date of birth", "dob", "birthday",
                                  "relationship", "address", "context", "referred by"] for h in first):
                data_rows = rows[1:]

        sheet = crm_sheet()
        existing_records = sheet.get_all_records()
        existing_names_set = set()
        for r in existing_records:
            n = r.get("Name", "").strip().lower()
            if n:
                existing_names_set.add(n)

        # Build column index map from declared order
        col_map = {}
        for i, col_name in enumerate(column_order):
            col_map[col_name.strip().lower()] = i

        def get_col(row, *keys):
            for k in keys:
                idx = col_map.get(k.lower())
                if idx is not None and idx < len(row):
                    val = row[idx]
                    if val is not None:
                        return str(val).strip().replace('\xa0', '').strip()
            return ""

        def normalise_birthday(val):
            """Convert various birthday formats to DD/MM/YYYY."""
            if val is None:
                return ""
            # Already a datetime object (openpyxl reads Excel dates natively)
            if isinstance(val, (datetime,)):
                return val.strftime("%d/%m/%Y")
            s = str(val).strip()
            if not s or s.lower() == "none":
                return ""
            for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y",
                        "%d %b %Y", "%d %B %Y", "%Y-%m-%d %H:%M:%S"]:
                try:
                    return datetime.strptime(s, fmt).strftime("%d/%m/%Y")
                except:
                    pass
            # Try Excel serial
            try:
                serial = int(float(s))
                epoch = datetime(1899, 12, 30)
                return (epoch + timedelta(days=serial)).strftime("%d/%m/%Y")
            except:
                pass
            return s  # return raw if nothing parsed

        def normalise_name(raw):
            """Strip whitespace/NBSP, Title Case if ALL CAPS."""
            if not raw:
                return ""
            cleaned = str(raw).replace('\xa0', '').strip()
            if cleaned.isupper():
                cleaned = cleaned.title()
            return cleaned

        def normalise_email(raw):
            return str(raw).strip().lower() if raw else ""

        imported = 0
        skipped = 0
        today = date.today().strftime("%d/%m/%Y")

        rows_to_append = []
        for row in data_rows:
            if not any(c for c in row if c is not None):
                continue

            # Get birthday raw value directly from index (handles datetime objects)
            bday_idx = col_map.get("birthday")
            bday_raw = row[bday_idx] if bday_idx is not None and bday_idx < len(row) else None
            birthday = normalise_birthday(bday_raw)

            name = normalise_name(get_col(row, "Name", "name"))
            if not name:
                continue

            if name.lower() in existing_names_set:
                skipped += 1
                continue

            alias = get_col(row, "Alias", "alias")
            relationship = get_col(row, "Relationship", "relationship")
            context = get_col(row, "Context", "context")
            notes = get_col(row, "Notes", "notes")
            followup_date = get_col(row, "Follow Up Date", "follow up date")
            followup_notes = get_col(row, "Follow Up Notes", "follow up notes")
            referred_by = get_col(row, "Referred By", "referred by")
            referral_date = get_col(row, "Referral Date", "referral date")
            email_raw = get_col(row, "Email", "email")
            email = normalise_email(email_raw)
            address = get_col(row, "Address", "address")

            rows_to_append.append([
                name, alias, birthday, relationship, context, notes,
                followup_date, followup_notes, today, "", referred_by, referral_date, email, address
            ])
            existing_names_set.add(name.lower())
            imported += 1

        # Batch append for speed
        if rows_to_append:
            sheet.append_rows(rows_to_append)

        msg = f"✅ Import done — {imported} contact(s) added"
        if skipped:
            msg += f", {skipped} skipped (already exist)"
        await update.message.reply_text(msg)

    except ImportError:
        await update.message.reply_text("openpyxl isn't installed. Run `pip install openpyxl` and redeploy.")
    except Exception as e:
        await update.message.reply_text(f"❌ Import failed: {str(e)}")

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
            return "⚠️ Couldn't connect to iCloud Calendar — check that ICLOUD_USERNAME and ICLOUD_PASSWORD are set correctly in Railway."
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
        return f"⚠️ Calendar error: {str(e)} — if this keeps happening, check your iCloud credentials in Railway."

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
    lower = text.lower().strip()

    # Tier 1: Hard keyword triggers — zero-cost instant match
    CALENDAR_TRIGGERS = [
        "schedule ", "add event", "create event", "new event",
        "book ", "set up a meeting", "set up meeting",
        "pencil in", "block out", "block off",
        "put in my calendar", "add to calendar", "add to my calendar",
        "add a meeting", "add meeting", "meeting at ", "meeting on ",
        "dinner at ", "dinner on ", "lunch at ", "lunch on ",
        "drinks at ", "drinks on ", "breakfast at ",
        "call at ", "call on ", "appointment at ", "appointment on ",
        "catch up at ", "catch up on ", "event at ", "event on ",
        "remind me on calendar", "add reminder to calendar",
    ]
    if any(t in lower for t in CALENDAR_TRIGGERS):
        return True

    # Tier 2: Hard exclusions — zero-cost instant reject
    EXCLUSIONS = [
        "remind me", "reminder", "spent", "paid", "bought", "cost",
        "expense", "bill", "price", "how much", "what time", "what's the time",
        "stock", "portfolio", "restaurant", "save ", "note ", "find ",
        "search ", "todo", "weather",
    ]
    if any(e in lower for e in EXCLUSIONS):
        return False

    # Tier 3: Require a time anchor — calendar events always have one.
    # Without a time anchor there is nothing to schedule, so skip the API call entirely.
    TIME_WORDS = [
        "tomorrow", "tonight", "monday", "tuesday", "wednesday",
        "thursday", "friday", "saturday", "sunday", "next week",
        "this evening", "this afternoon", "this morning", "next month",
        "on the ", "this friday", "this saturday", "this sunday",
    ]
    AT_PATTERN = bool(re.search(r'\b(at|@)\s*\d{1,2}(:\d{2})?\s*(am|pm)?\b', lower))
    DATE_PATTERN = bool(re.search(r'\b\d{1,2}[\/\-]\d{1,2}\b', lower))
    has_time_anchor = AT_PATTERN or DATE_PATTERN or any(w in lower for w in TIME_WORDS)

    if not has_time_anchor:
        return False  # No time anchor → not a calendar request, no API call

    # Tier 4: Has time anchor + event word → call Claude to confirm
    # This is the only path that hits the API, and only when genuinely ambiguous
    AMBIGUOUS_SIGNALS = [
        "meet", "meeting", "catch up", "dinner", "lunch", "drinks",
        "coffee", "call", "appointment", "event", "session",
        "interview", "presentation", "visit",
    ]
    if any(s in lower for s in AMBIGUOUS_SIGNALS) or AT_PATTERN:
        try:
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=10,
                messages=[{"role": "user", "content":
                    f'Is this asking to add a calendar event? Reply YES or NO only.\n\n"{text}"'}]
            )
            return response.content[0].text.strip().upper() == "YES"
        except Exception as e:
            print(f"is_calendar_request API fallback error: {e}")
            return False

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
    fields = ["alias", "birthday", "relationship", "context", "notes",
              "follow up date", "follow up notes", "email", "address"]

    if step == "choose_field":
        field = text.lower().strip()
        if field == "cancel":
            del edit_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return
        if field not in fields:
            await update.message.reply_text(
                f"Pick a field to edit:\n1. Alias\n2. Birthday\n3. Relationship\n4. Context\n"
                f"5. Notes\n6. Follow up date\n7. Follow up notes\n8. Email\n9. Address\n\n"
                f"Or type *cancel* to exit.",
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
                except Exception as e:
                    print(f"send_followup_reminders error for {r.get('Name','?')}: {e}")
    except Exception as e:
        print(f"Error sending follow up reminders: {e}")

# --- Birthday Greeting State ---
# Tracks who got a 12pm prompt today, pending acknowledgement
birthday_pending = {}

def ensure_birthday_greeted_column():
    """Birthday Greeted is col 10 in new CRM schema — nothing to add."""
    pass  # Column already defined in new header structure

def get_birthday_greeted_col():
    """Return the column index (1-based) of Birthday Greeted — always 10 in new schema."""
    return 10

def generate_birthday_greeting(name, age, relationship, context, notes):
    """Generate a personalised birthday greeting via Claude."""
    context_str = f"Relationship: {relationship}. Context: {context}. Notes: {notes or 'none'}."
    greeting_prompt = (
        f"Write a warm, casual birthday greeting that someone could copy and paste to send to {name}, "
        f"who is turning {age} today.\n\n"
        f"Context: {context_str}\n\n"
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
                relationship = r.get("Relationship", "")
                context = r.get("Context", "")

                # Skip if already greeted today
                already_greeted = r.get("Birthday Greeted", "")
                if already_greeted == today.strftime("%d/%m/%Y"):
                    continue

                greeting = generate_birthday_greeting(name, age, relationship, context, notes)

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
    except Exception as e:
        print(f"tag_crm_contacts error: {e}")
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
    except Exception:
        print("Reminders sheet not found — creating it")
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
    except Exception as e:
        print(f"get_next_recurrence error: {e}")
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
            except Exception as e:
                print(f"check_and_fire_reminders: bad scheduled time '{scheduled_str}': {e}")
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
    triggers = [
        "remind me", "remind", "set a reminder", "set me a reminder",
        "reminder for", "reminder at", "reminder to",
        "don't let me forget", "dont let me forget",
        "alert me", "notify me", "ping me",
        "drop me a reminder", "drop a reminder", "send me a reminder"
    ]
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
        except Exception as e:
            print(f"handle_new_reminder: time format error: {e}")
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
        except Exception as e:
            print(f"handle_reschedule: time format error: {e}")
            time_str = new_time

        return f"Got it, I'll remind you about {last['message']} again on {time_str}."

    except Exception as e:
        return f"Couldn't reschedule: {str(e)}"



# =============================================================================
# EXPENSE TRACKER
# =============================================================================

EXPENSE_CATEGORIES = ["FnB", "Entertainment", "Personal", "Family", "Work", "Transport", "Shopping", "Travel"]
EXPENSE_CARDS = ["Citi", "Maybank", "Amex", "UOB"]

# Foreign transaction fee estimates per card (%)
CARD_FX_FEES = {"Citi": 3.25, "Maybank": 3.0, "Amex": 3.0, "UOB": 3.25}

# Overseas mode state
overseas_state = {
    "active": False,
    "destination": "",
    "currency": "SGD",
    "currencies": [],          # all currencies used this trip (multi-currency support)
    "return_date": "",
    "dep_job_id": None,        # scheduler job ID for departure activation
    "return_job_id": None,     # scheduler job ID for return deactivation
    "return_flight": None,     # return flight data dict
    "trip_start": None,        # date string DD/MM/YYYY when overseas mode activated
    "trip_destinations": [],   # list of destinations visited this trip
    "trip_id": None,           # active trip ID in Trips sheet
}

# Global scheduler reference (set in post_init)
_scheduler = None
_app_ref = None

# Pending new merchant — waiting for user to confirm category + card
# { user_id: { "merchant": str, "amount": float, "currency": str, "step": "category"|"card" } }
expense_sessions = {}
delete_sessions = {}
confirm_sessions = {}  # { user_id: { "action": str, "target": str, "args": [...] } }
portfolio_delete_sessions = {}

# Reconciliation sessions — { user_id: { "step": str, "unmatched": [...], "index": int } }
recon_sessions = {}

# Cached FX rates — { "JPY_SGD": { "rate": 0.0093, "fetched_at": datetime } }
cached_fx_rates = {}

def get_fx_rate(currency):
    """Return SGD rate for given currency. Uses cache if under 12hrs, else fetches fresh."""
    if currency == "SGD":
        return 1.0
    cache_key = f"{currency}_SGD"
    cached = cached_fx_rates.get(cache_key)
    if cached:
        age = datetime.now(pytz.utc) - cached["fetched_at"]
        if age.total_seconds() < 43200:  # 12 hours
            return cached["rate"]
    # Fetch fresh
    if EXCHANGE_RATE_API_KEY:
        try:
            url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/pair/{currency}/SGD"
            resp = requests.get(url, timeout=8)
            data = resp.json()
            if data.get("result") == "success":
                rate = float(data["conversion_rate"])
                cached_fx_rates[cache_key] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
                print(f"FX cache updated: 1 {currency} = {rate} SGD")
                return rate
        except Exception as e:
            print(f"FX fetch error for {currency}: {e}")
    # Fallback to Claude estimate
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=20,
            messages=[{"role": "user", "content": f"What is the approximate exchange rate from {currency} to SGD today? Return ONLY a single number. No text."}]
        )
        rate = float(resp.content[0].text.strip())
        cached_fx_rates[cache_key] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
        print(f"FX cache (Claude estimate): 1 {currency} = {rate} SGD")
        return rate
    except Exception as e:
        print(f"Claude FX estimate failed for {currency}: {e}")
        return None

async def refresh_fx_rates(app=None):
    """Refresh cached FX rates for all active overseas currencies. Called twice daily."""
    currencies = [c for c in overseas_state.get("currencies", []) if c != "SGD"]
    if not currencies:
        return
    for currency in currencies:
        cache_key = f"{currency}_SGD"
        if EXCHANGE_RATE_API_KEY:
            try:
                url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/pair/{currency}/SGD"
                resp = requests.get(url, timeout=8)
                data = resp.json()
                if data.get("result") == "success":
                    rate = float(data["conversion_rate"])
                    cached_fx_rates[cache_key] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
                    print(f"FX refresh: 1 {currency} = {rate} SGD")
            except Exception as e:
                print(f"FX refresh error for {currency}: {e}")

EXPENSE_CATEGORY_EMOJI = {
    "FnB": "🍽️",
    "Transport": "🚗",
    "Entertainment": "🎬",
    "Personal": "🪞",
    "Family": "👨‍👩‍👧",
    "Work": "💼",
    "Shopping": "🛍️",
    "Household": "🏠",
    "Travel": "✈️",
}

EXPENSE_MERCHANT_OVERRIDES = {
    "FnB": [
        (["coffee", "starbucks", "kopitiam", "ya kun", "toast box", "kopi", "cafe", "espresso", "latte"], "☕"),
        (["ramen", "ichiran", "ippudo", "japanese", "sushi", "yakitori", "donburi", "izakaya"], "🍜"),
        (["mcdonald", "burger king", "wendy", "burger", "kfc", "popeyes", "fast food"], "🍔"),
        (["bar", "beer", "wine", "drinks", "cocktail", "pub", "taproom", "brewery"], "🍺"),
    ],
    "Transport": [
        (["flight", "airline", "air asia", "scoot", "singapore airlines", "cathay", "emirates", "jetstar"], "✈️"),
        (["grab", "gojek", "taxi", "uber", "ryde", "tada"], "🚕"),
        (["mrt", "bus", "transit", "ez-link", "train"], "🚌"),
    ],
    "Entertainment": [
        (["netflix", "disney", "hbo", "prime video", "apple tv", "streaming", "hulu", "mewatch"], "📺"),
        (["spotify", "apple music", "tidal", "deezer", "music"], "🎵"),
        (["cinema", "cathay", "gv", "shaw", "golden village", "movie"], "🎥"),
        (["steam", "playstation", "xbox", "nintendo", "game"], "🎮"),
    ],
    "Shopping": [
        (["guardian", "watsons", "unity", "pharmacy", "watson"], "💊"),
        (["ntuc", "giant", "cold storage", "fairprice", "supermarket", "grocery", "market"], "🛒"),
    ],
}

_category_emoji_cache = {}

def get_merchant_emoji(category, merchant):
    """Return contextual emoji based on category + merchant name."""
    merchant_lower = merchant.lower() if merchant else ""
    overrides = EXPENSE_MERCHANT_OVERRIDES.get(category, [])
    for keywords, emoji in overrides:
        if any(kw in merchant_lower for kw in keywords):
            return emoji
    if category in EXPENSE_CATEGORY_EMOJI:
        return EXPENSE_CATEGORY_EMOJI[category]
    if category:
        if category in _category_emoji_cache:
            return _category_emoji_cache[category]
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=5,
                messages=[{"role": "user", "content": f"Return a single emoji that best represents the expense category '{category}'. Return ONLY the emoji, nothing else."}]
            )
            inferred = resp.content[0].text.strip()
            _category_emoji_cache[category] = inferred
            return inferred
        except Exception:
            pass
    return "💳"

def expenses_sheet():
    return spreadsheet.worksheet("Expenses")

def merchant_map_sheet():
    return spreadsheet.worksheet("Merchant Map")

def get_merchant_memory(merchant):
    """Look up known merchant in Merchant Map. Returns (category, card, canonical_name) or (None, None, None)."""
    try:
        sheet = merchant_map_sheet()
        records = sheet.get_all_records()
        merchant_lower = merchant.lower().strip()
        # Exact match first
        for r in records:
            if r.get("Merchant", "").lower() == merchant_lower:
                return r.get("Category", ""), r.get("Card", ""), r.get("Merchant", "")
        # Fuzzy match — check if either is a substring of the other
        for r in records:
            known = r.get("Merchant", "").lower().strip()
            if known and (known in merchant_lower or merchant_lower in known):
                return r.get("Category", ""), r.get("Card", ""), r.get("Merchant", "")
        # Word-level match — first significant word (brand name)
        merchant_words = [w for w in merchant_lower.split() if len(w) > 2]
        for r in records:
            known = r.get("Merchant", "").lower().strip()
            known_words = [w for w in known.split() if len(w) > 2]
            if merchant_words and known_words and merchant_words[0] == known_words[0]:
                return r.get("Category", ""), r.get("Card", ""), r.get("Merchant", "")
    except Exception as e:
        print(f"get_merchant_memory error for '{merchant}': {e}")
    return None, None, None

def save_merchant_memory(merchant, category, card):
    """Save new merchant to Merchant Map."""
    try:
        sheet = merchant_map_sheet()
        sheet.append_row([merchant, category, card])
    except Exception as e:
        print(f"Error saving merchant: {e}")

def parse_expense_text(text):
    """Use Claude to extract expense details from natural language."""
    overseas_currency = overseas_state["currency"] if overseas_state["active"] else "SGD"
    prompt = (
        f"Extract expense details from this message: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- merchant: string (brand name only — strip legal suffixes like Pte Ltd, Ltd, Inc, Sdn Bhd, Co., Corp, Restaurant, Cafe. "
        f"  E.g. 'Starbucks Coffee Singapore Pte Ltd' -> 'Starbucks', 'McDonald's Restaurant' -> 'McDonald's')\n"
        f"- amount: number (just the number, no currency symbol)\n"
        f"- currency: string (3-letter ISO code. If not mentioned, default to '{overseas_currency}')\n"
        f"- category: string (one of: FnB, Entertainment, Personal, Family, Work, Transport, Shopping, Travel) or empty if unclear\n"
        f"- card: string (one of: Citi, Maybank, Amex, UOB) or empty if not mentioned\n\n"
        f"Return ONLY the JSON."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link="", reconciled="No", notes="", trip_id=None):
    """Append expense row to Expenses sheet."""
    today = date.today().strftime("%d/%m/%Y")
    active_trip = trip_id or overseas_state.get("trip_id", "")
    sheet = expenses_sheet()
    sheet.append_row([
        today, merchant, amount, currency, sgd_amount,
        category, card, receipt_link, reconciled, notes, active_trip
    ])

def format_expense_confirmation(merchant, amount, currency, category, card, sgd_amount=None, receipt_saved=False):
    """Format the expense confirmation message."""
    emoji = get_merchant_emoji(category, merchant)
    lines = [f"{emoji} {merchant}"]
    if currency != "SGD" and sgd_amount is not None:
        lines.append(f"${sgd_amount:.2f} ({currency} {amount:,.0f})")
    else:
        lines.append(f"${amount:.2f}")
    lines.append(f"🗂 {category} | 💳 {card}")
    if receipt_saved:
        lines.append("🧾 Receipt saved")
    lines.append("")
    lines.append("All good?")
    return "\n".join(lines)

def get_monthly_summary(month=None, year=None):
    """Generate monthly expense summary."""
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No expenses logged yet."

        today = date.today()
        target_month = month or today.month
        target_year = year or today.year

        month_records = []
        for r in records:
            d = r.get("Date", "")
            if not d:
                continue
            try:
                dt = datetime.strptime(d, "%d/%m/%Y")
                if dt.month == target_month and dt.year == target_year:
                    month_records.append(r)
            except Exception as e:
                print(f"get_monthly_summary: bad date in row: {e}")

        if not month_records:
            return f"No expenses for {datetime(target_year, target_month, 1).strftime('%B %Y')}."

        total = sum(float(r.get("SGD Amount", 0) or r.get("Amount", 0)) for r in month_records)

        # Category breakdown
        cat_totals = {c: 0 for c in EXPENSE_CATEGORIES}
        for r in month_records:
            cat = r.get("Category", "")
            if cat in cat_totals:
                amt = float(r.get("SGD Amount", 0) or r.get("Amount", 0))
                cat_totals[cat] += amt

        # Card breakdown
        card_totals = {c: 0 for c in EXPENSE_CARDS}
        for r in month_records:
            card = r.get("Card", "")
            if card in card_totals:
                amt = float(r.get("SGD Amount", 0) or r.get("Amount", 0))
                card_totals[card] += amt

        month_label = datetime(target_year, target_month, 1).strftime("%B %Y")
        lines = [f"Monthly Summary — {month_label}\n"]

        for cat, amt in cat_totals.items():
            if amt > 0:
                lines.append(f"{cat}: ${amt:.2f}")

        lines.append(f"\nTotal: ${total:.2f}")
        lines.append("\nBy card:")
        for card, amt in card_totals.items():
            if amt > 0:
                lines.append(f"{card}: ${amt:.2f}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error generating summary: {str(e)}"

def is_log_prefix_input(text):
    """Detect 'log [merchant] [amount]' — always routes to expense flow."""
    return text.lower().startswith("log ") and len(text) > 4

def is_bare_merchant_input(text):
    """Detect bare merchant name + amount — e.g. 'Starbucks $5.60'.
    Only triggers if: first word/phrase matches a known merchant AND a dollar amount is present.
    Safeguard: avoids triggering on sentences like 'Apple announced new products'.
    """
    lower = text.lower().strip()
    # Must contain a dollar amount
    if not re.search(r"\$[\d,.]+", text):
        return False
    # First word must match a known merchant (case-insensitive)
    first_word = text.strip().split()[0]
    known_cat, _, _ = get_merchant_memory(first_word)
    return bool(known_cat)

def get_expense_categories():
    """Pull live expense categories from the Expenses sheet data, falling back to defaults."""
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        cats = sorted(set(r.get("Category", "").strip() for r in records if r.get("Category", "").strip()))
        if not cats:
            cats = EXPENSE_CATEGORIES
    except Exception:
        cats = EXPENSE_CATEGORIES
    numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(cats))
    return f"Your expense categories:\n\n{numbered}"

def is_expense_input(text):
    """Detect if message looks like an expense entry or expense question."""
    lower = text.lower()
    # Exclude command-style messages that contain expense-related words but aren't entries
    exclusions = [
        "delete expense", "remove expense", "undo expense",
        "edit expense", "edit last expense",
        "rename category", "expense report", "monthly report",
        "trip summary", "last expense", "show last expense",
        "what expense categories", "list my categories", "what categories",
        "show categories", "what are my expense", "list categories",
        "expense categories", "my categories"
    ]
    if any(lower.startswith(e) or lower == e for e in exclusions):
        return False
    triggers = ["spent", "paid", "$", "sgd", "charged", "bought", "grabbed",
                "receipt", "bill was", "cost me", "picked up",
                "recorded in", "will it be", "logged in", "track", "how much have i"]
    return any(t in lower for t in triggers)

LOCATION_CONTEXT_WORDS = {
    "japan", "korea", "thailand", "malaysia", "indonesia", "vietnam", "philippines",
    "australia", "china", "hong kong", "taiwan", "india", "uk", "england", "france",
    "germany", "italy", "spain", "usa", "america", "canada", "dubai", "uae",
    "tokyo", "osaka", "seoul", "bangkok", "kuala lumpur", "kl", "jakarta", "bali",
    "sydney", "melbourne", "beijing", "shanghai", "guangzhou", "shenzhen",
    "taipei", "mumbai", "delhi", "london", "paris", "berlin", "rome", "barcelona",
    "new york", "los angeles", "chicago", "toronto", "vancouver",
    "hkg", "nrt", "icn", "bkk", "kul", "cgk", "sin", "syd", "pek", "pvg",
    "tpe", "bom", "del", "lhr", "cdg", "txl", "fco", "jfk", "lax", "yyz",
}

def is_overseas_mode_request(text):
    """Detect overseas mode toggle. Guards against misfires like 'i'm in a meeting'."""
    lower = text.lower()
    has_flight = bool(extract_flight_number(text))
    if has_flight and any(w in lower for w in ["flying", "flight", "on tr", "on sq", "on ak", "on mh", "boarding"]):
        return True
    if any(phrase in lower for phrase in [
        "overseas", "travelling", "traveling", "flying to", "arrived in",
        "back home", "i'm back", "landed in", "just landed", "just arrived", "returned home"
    ]):
        return True
    if "i'm in" in lower or "im in" in lower:
        return any(loc in lower for loc in LOCATION_CONTEXT_WORDS)
    return False

def lookup_flight(flight_number):
    """Look up flight details via AviationStack API. Returns dict or None."""
    if not AVIATIONSTACK_API_KEY:
        print("Flight lookup: no AVIATIONSTACK_API_KEY set")
        return None
    try:
        url = "http://api.aviationstack.com/v1/flights"
        params = {
            "access_key": AVIATIONSTACK_API_KEY,
            "flight_iata": flight_number.upper()
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        print(f"AviationStack {flight_number}: {json.dumps(data)[:400]}")
        if "error" in data:
            print(f"AviationStack API error: {data['error']}")
            return None
        flights = data.get("data", [])
        if not flights:
            print(f"AviationStack: no data for {flight_number}")
            return None
        f = flights[0]
        dep = f.get("departure", {})
        arr = f.get("arrival", {})
        return {
            "flight": flight_number.upper(),
            "dep_airport": dep.get("airport", ""),
            "dep_iata": dep.get("iata", ""),
            "dep_time": dep.get("scheduled", ""),
            "dep_terminal": dep.get("terminal", ""),
            "dep_gate": dep.get("gate", ""),
            "arr_airport": arr.get("airport", ""),
            "arr_iata": arr.get("iata", ""),
            "arr_city": arr.get("city") or arr.get("airport", ""),
            "arr_time": arr.get("scheduled", ""),
            "arr_terminal": arr.get("terminal", ""),
            "arr_gate": arr.get("gate", ""),
        }
    except Exception as e:
        print(f"Flight lookup error for {flight_number}: {e}")
        return None

def extract_flight_number(text):
    """Extract first valid flight number (e.g. TR450, SQ321) from text."""
    matches = re.findall(r'\b([A-Z]{1,3}\d{2,4}[A-Z]?)\b', text.upper())
    return matches[0] if matches else None

def extract_all_flight_numbers(text):
    """Extract all flight numbers from text."""
    return re.findall(r'\b([A-Z]{1,3}\d{2,4}[A-Z]?)\b', text.upper())

def format_flight_time(iso_str):
    """Format ISO datetime string to readable local time."""
    if not iso_str:
        return "time unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d %b, %H:%M")
    except Exception as e:
        print(f"format_flight_time: bad ISO string '{iso_str}': {e}")
        return iso_str[:16]

def get_dest_info_from_iata(iata_code, airport_name):
    """Resolve IATA code + airport name to city and currency via Claude."""
    prompt = (
        f"Airport IATA code: '{iata_code}', airport name: '{airport_name}'.\n"
        f"Return ONLY JSON: {{\"destination\": \"city name\", \"currency\": \"3-letter ISO code\"}}"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=60,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"get_dest_info_from_iata JSON parse error: {e} | raw: {raw}")
        return {"destination": airport_name or iata_code, "currency": "SGD"}

def deactivate_overseas_mode():
    """Deactivate overseas mode and clear scheduled jobs."""
    global overseas_state, _scheduler
    overseas_state["active"] = False
    overseas_state["destination"] = ""
    overseas_state["currency"] = "SGD"
    overseas_state["currencies"] = []
    overseas_state["return_date"] = ""
    overseas_state["return_flight"] = None
    overseas_state["trip_start"] = None
    overseas_state["trip_destinations"] = []
    overseas_state["trip_id"] = None
    for job_key in ["dep_job_id", "return_job_id"]:
        job_id = overseas_state.get(job_key)
        if job_id and _scheduler:
            try:
                _scheduler.remove_job(job_id)
            except Exception:
                pass
        overseas_state[job_key] = None

async def activate_overseas_mode_scheduled(dest, curr, return_flight_data=None):
    """Called by scheduler at departure time to activate overseas mode."""
    global overseas_state, _app_ref
    overseas_state["active"] = True
    overseas_state["destination"] = dest
    overseas_state["currency"] = curr
    overseas_state["currencies"] = [curr] if curr != "SGD" else []
    overseas_state["trip_start"] = date.today().strftime("%d/%m/%Y")
    overseas_state["trip_destinations"] = [dest]
    msg = f"Overseas mode on ✈️\nDestination: {dest}\nCurrency: {curr}\nI'll log expenses in {curr} with SGD equivalent."
    if return_flight_data:
        ret_dep = format_flight_time(return_flight_data.get("dep_time", ""))
        msg += f"\nReturn flight: {return_flight_data.get('flight', '')} departs {ret_dep}"
        # Schedule return deactivation at return arrival time
        ret_arr_str = return_flight_data.get("arr_time", "")
        if ret_arr_str and _scheduler and _app_ref:
            try:
                ret_arr_dt = datetime.fromisoformat(ret_arr_str.replace("Z", "+00:00"))
                ret_arr_local = ret_arr_dt.astimezone(TIMEZONE)
                job = _scheduler.add_job(
                    deactivate_and_notify,
                    "date",
                    run_date=ret_arr_local,
                    args=[_app_ref]
                )
                overseas_state["return_job_id"] = job.id
            except Exception as e:
                print(f"Failed to schedule return deactivation: {e}")
    if _app_ref:
        try:
            await _app_ref.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
        except Exception as e:
            print(f"Failed to send overseas mode activation message: {e}")

async def deactivate_and_notify(app):
    """Called by scheduler at return arrival — deactivate overseas mode and notify."""
    import random
    dest = overseas_state.get("destination", "")
    deactivate_overseas_mode()
    greeting = random.choice(["Welcome back!", "Good to have you back!", "Hope the trip was great!"])
    msg = f"{greeting} Back in SG — switching to SGD. 🏠"
    try:
        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
    except Exception as e:
        print(f"Failed to send return notification: {e}")

# =============================================================================
# TRIPS & FLIGHT MEMORY
# =============================================================================

AIRLINE_NAMES = {
    "TR": "Scoot", "SQ": "Singapore Airlines", "MI": "SilkAir",
    "OD": "Batik Air", "AK": "AirAsia", "D7": "AirAsia X",
    "MH": "Malaysia Airlines", "CX": "Cathay Pacific", "KA": "Cathay Dragon",
    "EK": "Emirates", "QR": "Qatar Airways", "TG": "Thai Airways",
    "GA": "Garuda Indonesia", "JL": "Japan Airlines", "NH": "ANA",
    "KE": "Korean Air", "OZ": "Asiana Airlines", "TW": "T'way Air",
    "VN": "Vietnam Airlines", "QH": "Bamboo Airways",
    "PR": "Philippine Airlines", "5J": "Cebu Pacific",
    "CI": "China Airlines", "BR": "EVA Air", "CZ": "China Southern",
    "CA": "Air China", "MU": "China Eastern", "HX": "Hong Kong Airlines",
    "LH": "Lufthansa", "BA": "British Airways", "AF": "Air France",
    "KL": "KLM", "LX": "Swiss", "OS": "Austrian",
    "QF": "Qantas", "NZ": "Air New Zealand",
}

def get_airline_name(flight_number):
    """Return airline name from flight number prefix."""
    prefix2 = flight_number[:2].upper()
    prefix3 = flight_number[:3].upper() if len(flight_number) >= 3 else ""
    return AIRLINE_NAMES.get(prefix2) or AIRLINE_NAMES.get(prefix3) or ""

def trips_sheet():
    return spreadsheet.worksheet("Trips")

def accommodation_sheet():
    return spreadsheet.worksheet("Accommodation")

def _next_trip_id():
    """Generate next sequential trip ID like T001, T002 ..."""
    try:
        records = trips_sheet().get_all_records()
        ids = [r.get("Trip ID", "") for r in records if r.get("Trip ID", "").startswith("T")]
        if not ids:
            return "T001"
        nums = []
        for tid in ids:
            try:
                nums.append(int(tid[1:]))
            except ValueError:
                pass
        return f"T{max(nums)+1:03d}" if nums else "T001"
    except Exception:
        return "T001"

def save_trip_leg(trip_id, leg_num, leg_type, flight_data, booking_ref=""):
    """Write one flight leg to the Trips sheet."""
    try:
        sheet = trips_sheet()
        flight_num = flight_data.get("flight", "")
        airline = get_airline_name(flight_num)
        dep_dt = flight_data.get("dep_time", "")
        arr_dt = flight_data.get("arr_time", "")

        def split_dt(iso):
            if not iso:
                return "", ""
            try:
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                return dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M")
            except Exception:
                return iso[:10], iso[11:16]

        dep_date, dep_time = split_dt(dep_dt)
        arr_date, arr_time = split_dt(arr_dt)
        terminal = flight_data.get("dep_terminal", "") or flight_data.get("arr_terminal", "")
        gate = flight_data.get("dep_gate", "") or flight_data.get("arr_gate", "")

        # Infer timezone from destination
        arr_city = flight_data.get("arr_city", "") or flight_data.get("arr_airport", "")
        dest_info = get_dest_info_from_iata(flight_data.get("arr_iata", ""), arr_city)
        tz = dest_info.get("timezone", "")

        sheet.append_row([
            trip_id, str(leg_num), leg_type, flight_num, airline,
            flight_data.get("dep_iata", "") or flight_data.get("dep_airport", ""),
            flight_data.get("arr_iata", "") or flight_data.get("arr_airport", ""),
            dep_date, dep_time, arr_date, arr_time,
            terminal, gate, booking_ref, tz, "Upcoming"
        ])
        return True
    except Exception as e:
        print(f"save_trip_leg error: {e}")
        return False

def get_active_trip():
    """Return the active or most recent upcoming trip as list of leg dicts."""
    try:
        records = trips_sheet().get_all_records()
        if not records:
            return None, []
        # Find trips with Active or Upcoming legs
        trip_ids = []
        for r in records:
            tid = r.get("Trip ID", "")
            status = r.get("Status", "")
            if tid and status in ("Active", "Upcoming") and tid not in trip_ids:
                trip_ids.append(tid)
        if not trip_ids:
            # Fall back to most recent completed
            completed = [r.get("Trip ID", "") for r in records if r.get("Status") == "Completed"]
            if completed:
                trip_ids = [completed[-1]]
        if not trip_ids:
            return None, []
        # Return legs for the first matching trip
        trip_id = trip_ids[0]
        legs = [r for r in records if r.get("Trip ID") == trip_id]
        return trip_id, legs
    except Exception as e:
        print(f"get_active_trip error: {e}")
        return None, []

def mark_trip_active(trip_id):
    """Set all Upcoming legs for a trip to Active."""
    try:
        sheet = trips_sheet()
        all_values = sheet.get_all_values()
        headers = all_values[0]
        status_col = headers.index("Status") + 1
        trip_col = headers.index("Trip ID") + 1
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) >= trip_col and row[trip_col - 1] == trip_id:
                if len(row) >= status_col and row[status_col - 1] in ("Upcoming",):
                    sheet.update_cell(i, status_col, "Active")
    except Exception as e:
        print(f"mark_trip_active error: {e}")

def close_trip_in_sheet(trip_id):
    """Mark all legs of a trip as Completed."""
    try:
        sheet = trips_sheet()
        all_values = sheet.get_all_values()
        headers = all_values[0]
        status_col = headers.index("Status") + 1
        trip_col = headers.index("Trip ID") + 1
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) >= trip_col and row[trip_col - 1] == trip_id:
                sheet.update_cell(i, status_col, "Completed")
    except Exception as e:
        print(f"close_trip_in_sheet error: {e}")

def format_trip_itinerary(legs):
    """Format trip legs into a readable itinerary."""
    if not legs:
        return "No legs found."
    lines = []
    for leg in legs:
        flight = leg.get("Flight No", "")
        airline = leg.get("Airline", "")
        frm = leg.get("From", "")
        to = leg.get("To", "")
        dep_date = leg.get("Dep Date", "")
        dep_time = leg.get("Dep Time", "")
        arr_date = leg.get("Arr Date", "")
        arr_time = leg.get("Arr Time", "")
        terminal = leg.get("Terminal", "")
        gate = leg.get("Gate", "")
        status = leg.get("Status", "")
        leg_type = leg.get("Type", "")

        line = f"✈️ {flight}"
        if airline:
            line += f" ({airline})"
        line += f" | {frm} → {to}"
        line += f"\n   {dep_date} {dep_time} → {arr_date} {arr_time}"
        if terminal:
            line += f" | Terminal {terminal}"
        if gate:
            line += f" Gate {gate}"
        line += f"\n   [{leg_type} · {status}]"
        lines.append(line)
    return "\n\n".join(lines)

def auto_restore_overseas_on_startup():
    """On restart, check Trips sheet for active/upcoming trip and restore overseas_state."""
    global overseas_state
    try:
        trip_id, legs = get_active_trip()
        if not trip_id or not legs:
            return
        today = date.today()
        # Find the current leg: most recent Active/Upcoming leg where dep_date has passed
        current_leg = None
        for leg in legs:
            dep_str = leg.get("Dep Date", "")
            status = leg.get("Status", "")
            if not dep_str:
                continue
            try:
                dep_dt = datetime.strptime(dep_str, "%d/%m/%Y").date()
                arr_str = leg.get("Arr Date", "")
                arr_dt = datetime.strptime(arr_str, "%d/%m/%Y").date() if arr_str else dep_dt
                # This leg is active if departure has passed and it's not a return leg that's completed
                if dep_dt <= today and status in ("Active", "Upcoming") and leg.get("Type") != "Return":
                    current_leg = leg
            except ValueError:
                pass

        if not current_leg:
            return

        # Restore overseas_state from this leg's destination
        dest_iata = current_leg.get("To", "")
        dest_info = get_dest_info_from_iata(dest_iata, dest_iata)
        currency = dest_info.get("currency", "SGD")
        destination = dest_info.get("destination", dest_iata)
        if currency == "SGD":
            return  # Not actually overseas

        overseas_state["active"] = True
        overseas_state["destination"] = destination
        overseas_state["currency"] = currency
        overseas_state["trip_id"] = trip_id
        if currency not in overseas_state["currencies"]:
            overseas_state["currencies"].append(currency)
        trip_start_str = current_leg.get("Dep Date", "")
        if trip_start_str:
            overseas_state["trip_start"] = trip_start_str

        print(f"✅ Restored overseas mode: {destination} ({currency}) from trip {trip_id}")
    except Exception as e:
        print(f"auto_restore_overseas_on_startup error: {e}")

def get_trip_queries(query_type, trip_id=None, legs=None):
    """Answer trip-related queries from Trips sheet data."""
    if legs is None:
        trip_id, legs = get_active_trip()
    if not legs:
        return "No active trip found."

    lower_q = query_type.lower()

    if "itinerary" in lower_q or "schedule" in lower_q:
        return f"*Trip {trip_id}*\n\n" + format_trip_itinerary(legs)

    if "flight home" in lower_q or "return" in lower_q:
        return_legs = [l for l in legs if l.get("Type", "").lower() == "return"]
        if return_legs:
            r = return_legs[0]
            return f"Return flight: {r.get('Flight No')} on {r.get('Dep Date')} at {r.get('Dep Time')} from {r.get('From')}"
        return "No return flight logged for this trip."

    if "land" in lower_q or "arrive" in lower_q or "arrival" in lower_q:
        upcoming = [l for l in legs if l.get("Status") in ("Active", "Upcoming")]
        if upcoming:
            leg = upcoming[0]
            return f"You land at {leg.get('Arr Time')} on {leg.get('Arr Date')} at {leg.get('To')}."
        return "No upcoming legs found."

    if "terminal" in lower_q:
        upcoming = [l for l in legs if l.get("Status") in ("Active", "Upcoming")]
        if upcoming and upcoming[0].get("Terminal"):
            return f"Terminal: {upcoming[0].get('Terminal')} ({upcoming[0].get('Flight No')})"
        return "No terminal info for the next leg."

    if "gate" in lower_q:
        upcoming = [l for l in legs if l.get("Status") in ("Active", "Upcoming")]
        if upcoming and upcoming[0].get("Gate"):
            return f"Gate: {upcoming[0].get('Gate')} ({upcoming[0].get('Flight No')})"
        return "No gate info yet."

    if "past trip" in lower_q or "past trips" in lower_q:
        try:
            records = trips_sheet().get_all_records()
            completed = {}
            for r in records:
                tid = r.get("Trip ID", "")
                if r.get("Status") == "Completed" and tid not in completed:
                    completed[tid] = r
            if not completed:
                return "No completed trips on record."
            lines = ["*Past trips:*"]
            for tid, r in list(completed.items())[-10:]:
                lines.append(f"• {tid}: {r.get('From')} → {r.get('To')} on {r.get('Dep Date')}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching past trips: {e}"

    return get_trip_queries("itinerary", trip_id, legs)


def handle_overseas_request(text):
    """Toggle overseas mode on/off, with optional flight lookup."""
    global overseas_state, _scheduler
    lower = text.lower()

    # Returning home manually
    if any(p in lower for p in ["back home", "returned", "i'm back", "landed back", "home now"]):
        import random
        greeting = random.choice(["Welcome back!", "Good to have you back!", "Hope the trip was great!"])
        trip_id = overseas_state.get("trip_id")
        if trip_id:
            try:
                close_trip_in_sheet(trip_id)
            except Exception as e:
                print(f"close_trip_in_sheet error: {e}")
        deactivate_overseas_mode()
        return f"{greeting} Switching back to SGD. 🏠"

    # Look for flight numbers in message
    all_flights = extract_all_flight_numbers(text)
    if all_flights and AVIATIONSTACK_API_KEY:
        outbound = all_flights[0]
        return_flight_num = all_flights[1] if len(all_flights) > 1 else None

        flight_data = lookup_flight(outbound)
        if flight_data:
            dep_fmt = format_flight_time(flight_data["dep_time"])
            arr_fmt = format_flight_time(flight_data["arr_time"])
            arr_label = flight_data["arr_city"] or flight_data["arr_airport"] or flight_data["arr_iata"]

            pending = {
                "flight_number": outbound,
                "dep_time": flight_data["dep_time"],
                "dep_terminal": flight_data.get("dep_terminal", ""),
                "dep_gate": flight_data.get("dep_gate", ""),
                "arr_time": flight_data["arr_time"],
                "arr_airport": flight_data["arr_airport"],
                "arr_iata": flight_data["arr_iata"],
                "arr_city": flight_data["arr_city"],
                "arr_terminal": flight_data.get("arr_terminal", ""),
                "arr_gate": flight_data.get("arr_gate", ""),
                "return_flight_data": None,
            }

            reply = f"Found {outbound} ✈️\nDeparts: {dep_fmt}\nArrives: {arr_fmt} → {arr_label}\n"

            if return_flight_num:
                ret_data = lookup_flight(return_flight_num)
                if ret_data:
                    ret_dep = format_flight_time(ret_data["dep_time"])
                    ret_arr = format_flight_time(ret_data["arr_time"])
                    ret_data["flight"] = return_flight_num
                    pending["return_flight_data"] = ret_data
                    reply += f"Return {return_flight_num}: {ret_dep} → {ret_arr}\n"
                else:
                    reply += f"(Couldn't find {return_flight_num} — I'll skip the return)\n"

            overseas_state["_pending_flight"] = pending
            reply += "\nReply Y to confirm — overseas mode will activate at departure time."
            return reply

        else:
            return (
                f"Looked up {outbound} on AviationStack but got no data back — "
                f"the flight may not be in their system yet (free tier only covers flights within ~24hrs). "
                f"Where are you headed and when are you departing and returning?"
            )

    # No flight number — ask for details
    return "What's your departure date/time and destination? And when are you back in SG?"

def _save_confirmed_trip(outbound_data, return_data=None, booking_ref=""):
    """Write confirmed trip legs to Trips sheet and return trip_id."""
    trip_id = _next_trip_id()
    leg = 1
    leg_type = "Stay" if return_data else "Outbound"
    save_trip_leg(trip_id, leg, leg_type, outbound_data, booking_ref)
    if return_data:
        return_data_copy = dict(return_data)
        return_data_copy["flight"] = return_data_copy.get("flight", "")
        save_trip_leg(trip_id, leg + 1, "Return", return_data_copy, booking_ref)
    return trip_id


async def handle_expense_session(user_id, text, update):
    """Handle multi-step expense onboarding — category, card, fx_rate confirmation."""
    session = expense_sessions[user_id]
    step = session.get("step")

    if step == "fx_rate":
        try:
            rate = float(text.strip().replace(",", "."))
            if rate <= 0:
                raise ValueError("Rate must be positive")
        except ValueError:
            currency = session.get("currency", "")
            await update.message.reply_text(
                f"That doesn't look right. Enter a positive number — e.g. '0.0093' means 1 {currency} = 0.0093 SGD."
            )
            return
        session["sgd_amount"] = round(session["amount"] * rate, 2)
        session["step"] = "category" if not session.get("category") else "card" if not session.get("card") else "done"
        if session["step"] == "done":
            await _finalise_expense_session(user_id, update)
            return
        if session["step"] == "category":
            await update.message.reply_text(f"Got it — SGD ${session['sgd_amount']:.2f}. What category?\n{', '.join(EXPENSE_CATEGORIES)}")
        else:
            await update.message.reply_text(f"Which card?\n{', '.join(EXPENSE_CARDS)}")
        return

    if step == "confirm":
        # User confirming uncertain fields
        lower = text.strip().lower()
        missing = session.get("missing_fields", [])
        if missing:
            field = missing[0]
            if field == "category":
                matched = next((c for c in EXPENSE_CATEGORIES if c.lower() == lower), None)
                if not matched:
                    await update.message.reply_text(f"Pick a category: {', '.join(EXPENSE_CATEGORIES)}")
                    return
                session["category"] = matched
                missing.pop(0)
            elif field == "card":
                matched = next((c for c in EXPENSE_CARDS if c.lower() == lower), None)
                if not matched:
                    await update.message.reply_text(f"Pick a card: {', '.join(EXPENSE_CARDS)}")
                    return
                session["card"] = matched
                missing.pop(0)
            session["missing_fields"] = missing
            if missing:
                next_field = missing[0]
                if next_field == "card":
                    await update.message.reply_text(f"Which card?\n{', '.join(EXPENSE_CARDS)}")
                return
        await _finalise_expense_session(user_id, update)
        return

    if step == "category":
        matched = next((c for c in EXPENSE_CATEGORIES if c.lower() == text.strip().lower()), None)
        if not matched:
            await update.message.reply_text(f"Pick a category: {', '.join(EXPENSE_CATEGORIES)}")
            return
        session["category"] = matched
        session["step"] = "card" if not session.get("card") else "done"
        if session["step"] == "done":
            await _finalise_expense_session(user_id, update)
            return
        await update.message.reply_text(f"Which card?\n{', '.join(EXPENSE_CARDS)}")
        return

    if step == "card":
        matched = next((c for c in EXPENSE_CARDS if c.lower() == text.strip().lower()), None)
        if not matched:
            await update.message.reply_text(f"Pick a card: {', '.join(EXPENSE_CARDS)}")
            return
        session["card"] = matched
        await _finalise_expense_session(user_id, update)
        return

async def _finalise_expense_session(user_id, update):
    """Log the expense and send confirmation once all fields are collected."""
    session = expense_sessions[user_id]
    merchant = session["merchant"]
    amount = session["amount"]
    currency = session["currency"]
    category = session["category"]
    card = session["card"]
    sgd_amount = session.get("sgd_amount", amount)
    receipt_link = session.get("receipt_link", "")
    receipt_saved = bool(receipt_link)
    save_merchant_memory(merchant, category, card)
    log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link=receipt_link)
    del expense_sessions[user_id]
    confirmation = format_expense_confirmation(merchant, amount, currency, category, card, sgd_amount, receipt_saved)
    try:
        await update.message.reply_text(confirmation, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(confirmation)

def check_same_day_duplicate(merchant, amount, currency):
    """Return True if same merchant+amount+currency already logged today."""
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        today = date.today().strftime("%d/%m/%Y")
        for r in records:
            if (r.get("Date") == today and
                r.get("Merchant", "").lower() == merchant.lower() and
                str(r.get("Amount", "")) == str(amount) and
                r.get("Currency", "") == currency):
                return True
    except Exception as e:
        print(f"Duplicate check error: {e}")
    return False

QUESTION_WORDS = {"what", "how", "why", "when", "which", "who", "where", "list", "show", "tell", "is", "are", "do", "does", "can", "could", "would", "should"}
COMMAND_PREFIXES = ["delete", "remove", "undo", "edit", "rename", "show", "list", "what", "how"]

def _looks_like_command(text):
    """Return True if text looks like a command or question rather than an expense entry."""
    lower = text.lower().strip()
    first_word = lower.split()[0] if lower.split() else ""
    if first_word in QUESTION_WORDS:
        return True
    if any(lower.startswith(p) for p in COMMAND_PREFIXES):
        return True
    return False

def parse_receipt_image(image_bytes):
    """Pass receipt image to Claude Vision. Returns dict with merchant, amount, currency, confidence."""
    import base64
    overseas_currency = overseas_state["currency"] if overseas_state["active"] else "SGD"
    try:
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
        prompt = (
            f"You are reading a receipt image. Extract ONLY the merchant name and total amount paid.\n"
            f"Rules:\n"
            f"- merchant: brand name only, strip legal suffixes (Pte Ltd, Sdn Bhd, Restaurant, Cafe etc)\n"
            f"- amount: the final total paid (largest amount, or labelled Total/Grand Total/Amount Due)\n"
            f"- currency: 3-letter ISO code. If not visible, use '{overseas_currency}'\n"
            f"- confidence: \'high\' if both fields are clear, \'low\' if either is uncertain\n"
            f"- uncertain_fields: list any fields you are not sure about\n"
            f"Return ONLY a JSON object with keys: merchant, amount, currency, confidence, uncertain_fields.\n"
            f"No other text."
        )
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"parse_receipt_image error: {e}")
        return None

def handle_expense_text(text, user_id, receipt_link=""):
    """
    Process a text expense entry.
    Returns (reply_str, needs_session, session_data)
    """
    try:
        parsed = parse_expense_text(text)
        merchant = parsed.get("merchant", "Unknown")
        amount = float(parsed.get("amount", 0))
        currency = parsed.get("currency", "SGD")
        category = parsed.get("category", "")
        card = parsed.get("card", "")

        # Blank expense guard — reject if merchant is empty/unknown-ish or amount is 0
        if not merchant or merchant.lower() in ("unknown", "") or amount == 0:
            return (
                "Wasn't sure what to do with that — did you mean to log an expense, "
                "or were you asking something else?"
            ), False, None

        # Resolve FX rate if needed
        sgd_amount = amount
        if currency != "SGD":
            rate = get_fx_rate(currency)
            if rate is None:
                session = {
                    "merchant": merchant, "amount": amount, "currency": currency,
                    "category": category, "card": card, "step": "fx_rate",
                    "receipt_link": receipt_link
                }
                return (
                    f"Couldn't fetch the {currency}/SGD rate right now.\n"
                    f"What's the current rate? (e.g. '0.0093' means 1 {currency} = 0.0093 SGD)\n"
                    f"I'll use it for this expense and retry the live rate next time."
                ), True, session
            sgd_amount = round(amount * rate, 2)
            # Track currency for this trip
            if overseas_state["active"] and currency not in overseas_state["currencies"]:
                overseas_state["currencies"].append(currency)

        # Fuzzy merchant lookup — use canonical name if found
        known_cat, known_card, canonical = get_merchant_memory(merchant)
        if canonical:
            merchant = canonical
        if known_cat:
            category = known_cat
        if known_card:
            card = known_card

        # Same-day duplicate check
        if check_same_day_duplicate(merchant, amount, currency):
            session = {
                "merchant": merchant, "amount": amount, "currency": currency,
                "sgd_amount": sgd_amount, "category": category, "card": card,
                "step": "confirm", "missing_fields": [],
                "receipt_link": receipt_link, "duplicate_confirmed": False
            }
            return (
                f"Heads up — looks like you already logged {merchant} {currency} {amount:,.0f} today. Log it again?"
            ), True, session

        # Confirm-if-unsure — collect missing fields in one message
        missing = []
        if not category:
            missing.append("category")
        if not card:
            missing.append("card")

        if missing:
            session = {
                "merchant": merchant, "amount": amount, "currency": currency,
                "sgd_amount": sgd_amount, "category": category, "card": card,
                "step": "confirm", "missing_fields": missing,
                "receipt_link": receipt_link
            }
            _confirm_emoji = get_merchant_emoji(category, merchant)
            lines = [f"Got it —"]
            lines.append(f"{_confirm_emoji} {merchant}")
            if currency != "SGD":
                lines.append(f"${sgd_amount:.2f} ({currency} {amount:,.0f})")
            else:
                lines.append(f"${amount:.2f}")
            if "category" in missing:
                lines.append(f"\nWhat category?\n{', '.join(EXPENSE_CATEGORIES)}")
            elif "card" in missing:
                lines.append(f"\nWhich card?\n{', '.join(EXPENSE_CARDS)}")
            return "\n".join(lines), True, session

        # All info available — log directly
        receipt_saved = bool(receipt_link)
        log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link=receipt_link)
        confirmation = format_expense_confirmation(merchant, amount, currency, category, card, sgd_amount, receipt_saved)
        return confirmation, False, None

    except (json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"parse_expense_text error: {e} | input: {text}")
        # Try to reconstruct a guess from the raw text for user-facing hint
        guess = text[:60] + ("..." if len(text) > 60 else "")
        return (
            f"Couldn't read that as an expense.\n"
            f"Try: 'log [merchant] [amount]' — e.g. 'log Starbucks $5.60'"
        ), False, None
    except Exception as e:
        print(f"handle_expense_text unexpected error: {e}")
        return "Something went wrong logging that — try again or use 'log [merchant] [amount]'.", False, None

def delete_last_expense():
    """Delete the most recently added expense row."""
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return "No expenses to delete."
        last_row = len(all_values)
        last = all_values[last_row - 1]
        sheet.delete_rows(last_row)
        return f"Deleted last expense: {last[1]} ${last[2]}"
    except Exception as e:
        return f"Error deleting expense: {str(e)}"

def get_recent_expenses(n=5):
    """Return the last N expense rows as list of (sheet_row_index, dict)."""
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return []
        headers = all_values[0]
        rows = all_values[1:]
        recent = rows[-(n):]
        result = []
        for i, row in enumerate(recent):
            sheet_row = len(all_values) - len(recent) + i + 1  # 1-indexed sheet row
            d = {headers[j]: row[j] if j < len(row) else "" for j in range(len(headers))}
            result.append((sheet_row, d))
        return list(reversed(result))  # Most recent first
    except Exception as e:
        print(f"get_recent_expenses error: {e}")
        return []

def format_delete_list(expenses):
    """Format numbered list of expenses for delete selection."""
    lines = ["Which expense to delete?\n"]
    for i, (_, r) in enumerate(expenses, 1):
        merchant = r.get("Merchant", "?")
        amount = r.get("Amount", "")
        currency = r.get("Currency", "SGD")
        sgd = r.get("SGD Amount", "")
        date_str = r.get("Date", "")
        category = r.get("Category", "")
        if currency != "SGD" and sgd:
            amt_str = f"${float(sgd):.2f} ({currency} {float(amount):,.0f})"
        else:
            amt_str = f"${float(amount):.2f}" if amount else "$?"
        lines.append(f"{i}. {merchant} — {amt_str} | {category} | {date_str}")
    lines.append("\nReply with a number, or 'search [merchant]' to find another.")
    return "\n".join(lines)

def search_expenses_by_merchant(query):
    """Search expenses by merchant name, return list of (sheet_row, dict)."""
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return []
        headers = all_values[0]
        rows = all_values[1:]
        q = query.lower().strip()
        matches = []
        for i, row in enumerate(rows):
            d = {headers[j]: row[j] if j < len(row) else "" for j in range(len(headers))}
            if q in d.get("Merchant", "").lower():
                matches.append((i + 2, d))  # +2: 1-indexed + header row
        return list(reversed(matches))[:10]  # Most recent first, cap at 10
    except Exception as e:
        print(f"search_expenses_by_merchant error: {e}")
        return []

def delete_expense_by_row(sheet_row):
    """Delete an expense by its sheet row number. Returns confirmation string."""
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if sheet_row < 2 or sheet_row > len(all_values):
            return "Couldn't find that expense."
        row = all_values[sheet_row - 1]
        merchant = row[1] if len(row) > 1 else "?"
        amount = row[2] if len(row) > 2 else "?"
        sheet.delete_rows(sheet_row)
        return f"Deleted: {merchant} ${amount}"
    except Exception as e:
        return f"Error deleting expense: {str(e)}"

def get_last_expense():
    """Return the last logged expense row as a dict."""
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        if not records:
            return None
        return records[-1]
    except Exception as e:
        print(f"get_last_expense error: {e}")
        return None

def edit_last_expense(field, new_value):
    """Edit a specific field in the last expense row. Validates value and shows before/after."""
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return "No expenses to edit."
        headers = all_values[0]
        last_row_idx = len(all_values)
        field_map = {
            "merchant": "Merchant", "amount": "Amount", "currency": "Currency",
            "sgd": "SGD Amount", "category": "Category", "card": "Card", "notes": "Notes"
        }
        col_name = field_map.get(field.lower())
        if not col_name or col_name not in headers:
            valid = ", ".join(field_map.keys())
            return f"Can't edit '{field}'. Options: {valid}"
        col_idx = headers.index(col_name) + 1

        if col_name == "Amount":
            try:
                float(new_value.replace(",", "").replace("$", ""))
            except ValueError:
                return "Amount must be a number. Try: edit last expense amount to 12.50"
        elif col_name == "Category":
            try:
                all_cats = sorted(set(
                    r.get("Category", "").strip()
                    for r in expenses_sheet().get_all_records()
                    if r.get("Category", "").strip()
                )) or EXPENSE_CATEGORIES
            except Exception:
                all_cats = EXPENSE_CATEGORIES
            matched = next((c for c in all_cats if c.lower() == new_value.strip().lower()), None)
            if not matched:
                return f"Unknown category '{new_value}'. Your categories: {', '.join(all_cats)}"
            new_value = matched
        elif col_name == "Card":
            matched = next((c for c in EXPENSE_CARDS if c.lower() == new_value.strip().lower()), None)
            if not matched:
                return f"Unknown card '{new_value}'. Your cards: {', '.join(EXPENSE_CARDS)}"
            new_value = matched

        last_row = all_values[last_row_idx - 1]
        old_value = last_row[col_idx - 1] if col_idx - 1 < len(last_row) else ""
        sheet.update_cell(last_row_idx, col_idx, new_value)
        return f"Updated ✅\n{col_name}: {old_value} → {new_value}"
    except Exception as e:
        return f"Error editing expense: {str(e)}"

def show_last_expense():
    """Return a formatted view of the last logged expense."""
    r = get_last_expense()
    if not r:
        return "No expenses logged yet."
    currency = r.get("Currency", "SGD")
    amount = r.get("Amount", "")
    sgd = r.get("SGD Amount", "")
    _merchant = r.get('Merchant', '')
    _cat = r.get('Category', '')
    _emoji = get_merchant_emoji(_cat, _merchant)
    lines = [
        f"Last expense:",
        f"{_emoji} {_merchant}",
    ]
    if currency != "SGD" and sgd:
        lines.append(f"${float(sgd):.2f} ({currency} {float(amount):,.0f})")
    else:
        lines.append(f"${float(amount):.2f}" if amount else "")
    lines.append(f"🗂 {r.get('Category', '')} | 💳 {r.get('Card', '')}")
    lines.append(f"📅 {r.get('Date', '')}")
    lines.append("\nTo edit: 'edit [field] to [value]' e.g. 'edit category to Transport'")
    return "\n".join(l for l in lines if l)

def get_trip_summary():
    """Return expense summary for the current or most recent trip."""
    trip_start = overseas_state.get("trip_start")
    destinations = overseas_state.get("trip_destinations", [])
    if not trip_start:
        return "No trip data — overseas mode hasn't been activated this session."
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        start_dt = datetime.strptime(trip_start, "%d/%m/%Y").date()
        trip_records = []
        for r in records:
            d = r.get("Date", "")
            if not d:
                continue
            try:
                dt = datetime.strptime(d, "%d/%m/%Y").date()
                if dt >= start_dt:
                    trip_records.append(r)
            except Exception:
                continue
        if not trip_records:
            return "No expenses logged for this trip yet."
        total_sgd = sum(float(r.get("SGD Amount") or r.get("Amount") or 0) for r in trip_records)
        cat_totals = {}
        currency_totals = {}
        for r in trip_records:
            cat = r.get("Category", "Other")
            amt = float(r.get("SGD Amount") or r.get("Amount") or 0)
            cat_totals[cat] = cat_totals.get(cat, 0) + amt
            curr = r.get("Currency", "SGD")
            orig = float(r.get("Amount") or 0)
            if curr != "SGD":
                currency_totals[curr] = currency_totals.get(curr, 0) + orig
        dest_str = " → ".join(destinations) if destinations else "trip"
        lines = [f"Trip summary — {dest_str}", f"From {trip_start}\n"]
        for cat, amt in sorted(cat_totals.items(), key=lambda x: -x[1]):
            lines.append(f"{cat}: SGD ${amt:.2f}")
        lines.append(f"\nTotal: SGD ${total_sgd:.2f}")
        if currency_totals:
            lines.append("\nSpend by currency:")
            for curr, amt in currency_totals.items():
                lines.append(f"{curr}: {amt:,.0f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error generating trip summary: {str(e)}"

def rename_category(old_name, new_name):
    """Rename a category everywhere — category list, Merchant Map, Expenses sheet."""
    global EXPENSE_CATEGORIES
    old_title = old_name.strip().title() if old_name.lower() not in [c.lower() for c in EXPENSE_CATEGORIES] else next(c for c in EXPENSE_CATEGORIES if c.lower() == old_name.lower())
    matched = next((c for c in EXPENSE_CATEGORIES if c.lower() == old_name.lower()), None)
    if not matched:
        return f"Category '{old_name}' not found. Current categories: {', '.join(EXPENSE_CATEGORIES)}"
    new_clean = new_name.strip()
    EXPENSE_CATEGORIES = [new_clean if c == matched else c for c in EXPENSE_CATEGORIES]
    updated = 0
    # Update Expenses sheet
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if all_values:
            headers = all_values[0]
            if "Category" in headers:
                col_idx = headers.index("Category")
                for i, row in enumerate(all_values[1:], start=2):
                    if len(row) > col_idx and row[col_idx] == matched:
                        sheet.update_cell(i, col_idx + 1, new_clean)
                        updated += 1
    except Exception as e:
        print(f"rename_category expenses error: {e}")
    # Update Merchant Map
    try:
        sheet = merchant_map_sheet()
        all_values = sheet.get_all_values()
        if all_values:
            headers = all_values[0]
            if "Category" in headers:
                col_idx = headers.index("Category")
                for i, row in enumerate(all_values[1:], start=2):
                    if len(row) > col_idx and row[col_idx] == matched:
                        sheet.update_cell(i, col_idx + 1, new_clean)
    except Exception as e:
        print(f"rename_category merchant map error: {e}")
    return f"Renamed '{matched}' to '{new_clean}'. Updated {updated} expense(s)."

def get_expense_report(report_type="monthly"):
    """Generate expense report."""
    return get_monthly_summary()



# =============================================================================
# BILL REMINDERS (Step 6)
# =============================================================================

BILL_REMINDER_GREETINGS = [
    "Heads up", "Just a nudge", "Quick reminder", "Hey", "FYI"
]

def bills_sheet():
    return spreadsheet.worksheet("Bills")

def parse_bill_request(text):
    """Parse a natural language bill setup request."""
    prompt = (
        f"Extract bill details from: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- name: string (bill name, e.g. 'Citi Credit Card', 'Netflix', 'Electricity')\n"
        f"- bank: string (bank or provider name, or empty)\n"
        f"- due_day: number (day of month the bill is due, e.g. 15)\n"
        f"- estimated_amount: number (estimated amount in SGD, or 0 if not mentioned)\n"
        f"- notes: string (any extra notes, or empty)\n\n"
        f"Return ONLY the JSON."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

def add_bill(name, bank, due_day, estimated_amount, notes=""):
    """Add a bill to the Bills sheet."""
    sheet = bills_sheet()
    sheet.append_row([name, bank, str(due_day), str(estimated_amount), notes])

def list_bills():
    """List all bills."""
    try:
        sheet = bills_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No bills set up yet."
        lines = ["Your bills:\n"]
        for r in records:
            amt = r.get("Estimated Amount", "")
            amt_str = f" — ~${amt}" if amt and str(amt) != "0" else ""
            lines.append(f"• {r.get('Name', '')} (due day {r.get('Due Date', '')}){amt_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing bills: {str(e)}"

def delete_bill(name):
    """Delete a bill by name."""
    try:
        sheet = bills_sheet()
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if name.lower() in r.get("Name", "").lower():
                sheet.delete_rows(i + 2)
                return f"Deleted bill: {r.get('Name')}"
        return f"No bill found matching '{name}'."
    except Exception as e:
        return f"Error deleting bill: {str(e)}"

def get_cycle_expenses(card_name, due_day):
    """Sum expenses for a card in the current billing cycle."""
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        today = date.today()
        # Billing cycle: from last due date to today
        if today.day >= due_day:
            cycle_start = today.replace(day=due_day)
        else:
            if today.month == 1:
                cycle_start = today.replace(year=today.year - 1, month=12, day=due_day)
            else:
                cycle_start = today.replace(month=today.month - 1, day=due_day)

        total = 0
        for r in records:
            try:
                dt = datetime.strptime(r.get("Date", ""), "%d/%m/%Y").date()
                card = r.get("Card", "")
                if dt >= cycle_start and card_name.lower() in card.lower():
                    total += float(r.get("SGD Amount", 0) or r.get("Amount", 0))
            except Exception as e:
                print(f"get_cycle_expenses: bad row: {e}")
        return total
    except Exception as e:
        print(f"get_cycle_expenses error: {e}")
        return 0

async def send_bill_reminders(app):
    """Daily 9am job — check for bills due in 7 days."""
    import random
    try:
        sheet = bills_sheet()
        records = sheet.get_all_records()
        today = date.today()

        for r in records:
            try:
                due_day = int(r.get("Due Date", 0))
                if not due_day:
                    continue

                # Calculate next due date
                if today.day <= due_day:
                    next_due = today.replace(day=due_day)
                else:
                    if today.month == 12:
                        next_due = today.replace(year=today.year + 1, month=1, day=due_day)
                    else:
                        next_due = today.replace(month=today.month + 1, day=due_day)

                days_away = (next_due - today).days

                if days_away == 7:
                    name = r.get("Name", "")
                    estimated = r.get("Estimated Amount", "")

                    # Try to get actual cycle spend
                    cycle_total = get_cycle_expenses(name, due_day)
                    amount_str = f"${cycle_total:.2f} logged this cycle" if cycle_total > 0 else (f"~${estimated}" if estimated and str(estimated) != "0" else "amount unknown")

                    greeting = random.choice(BILL_REMINDER_GREETINGS)
                    msg = (
                        f"{greeting} — your {name} bill is due in 7 days ({next_due.strftime('%d %b')}).\n"
                        f"{amount_str}."
                    )
                    await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)

            except Exception as e:
                print(f"Bill reminder error for {r.get('Name', '?')}: {e}")

    except Exception as e:
        print(f"Error in send_bill_reminders: {e}")

def is_bill_request(text):
    """Detect bill setup intent."""
    lower = text.lower()
    triggers = ["bill is due", "bill due", "set up a bill", "add a bill", "my bill",
                "credit card bill", "due on the", "due every", "remind me about my"]
    return any(t in lower for t in triggers)

def handle_new_bill(text):
    """Parse and save a new bill."""
    try:
        parsed = parse_bill_request(text)
        name = parsed.get("name", "")
        bank = parsed.get("bank", "")
        due_day = parsed.get("due_day", 0)
        estimated_amount = parsed.get("estimated_amount", 0)
        notes = parsed.get("notes", "")

        if not name or not due_day:
            return "Couldn't get the bill details. Try: 'my Citi bill is due on the 15th, usually around $800'."

        add_bill(name, bank, due_day, estimated_amount, notes)
        amt_str = f", estimated ~${estimated_amount}" if estimated_amount else ""
        return f"Got it, I'll remind you about your {name} bill 7 days before it's due (day {due_day} of each month{amt_str})."
    except Exception as e:
        return f"Couldn't save that bill: {str(e)}"


# =============================================================================
# RESTAURANT TRACKER (Step 7)
# =============================================================================

def restaurants_sheet():
    return spreadsheet.worksheet("Restaurants")

def parse_restaurant_save(text):
    """Parse a restaurant save request — name, location, tags, notes."""
    prompt = (
        f"Extract restaurant details from: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- name: string (restaurant name)\n"
        f"- location: string (address or area, e.g. 'Teck Lim Road' or 'Shibuya, Tokyo')\n"
        f"- country: string (country, default 'Singapore' if not mentioned)\n"
        f"- tags: string (comma-separated tags like 'date night, japanese, omakase' — only if mentioned, else empty)\n"
        f"- notes: string (any notes like 'need reservation', 'cash only' — only if mentioned, else empty)\n\n"
        f"Return ONLY the JSON."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

def lookup_restaurant_from_maps(url):
    """Extract restaurant name and address from a Google Maps URL via Claude."""
    prompt = (
        f"Given this Google Maps URL: {url}\n\n"
        f"Extract the restaurant/place name and address from the URL itself (don't browse it).\n"
        f"Return ONLY a JSON object with:\n"
        f"- name: string (place name if visible in URL, else empty)\n"
        f"- location: string (area or address if visible, else empty)\n"
        f"- country: string (country if determinable from URL, else 'Singapore')\n\n"
        f"Return ONLY the JSON."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"lookup_restaurant_from_maps JSON parse error: {e} | raw: {raw}")
        return {"name": "", "location": "", "country": "Singapore"}

def save_restaurant(name, location, country="Singapore", tags="", notes=""):
    """Save a restaurant to the Restaurants sheet."""
    sheet = restaurants_sheet()
    sheet.append_row([name, location, country, tags, notes])

def format_restaurant_saved(name, location, tags="", notes=""):
    """Format the restaurant saved confirmation."""
    lines = ["Saved!"]
    lines.append(f"🏪 {name}")
    lines.append(f"📍 {location}")
    if tags:
        lines.append(f"🏷 {tags}")
    if notes:
        lines.append(f"📝 {notes}")
    return "\n".join(lines)

def search_restaurants(query):
    """Search restaurants by cuisine, location, or tag."""
    try:
        sheet = restaurants_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No restaurants saved yet."

        query_lower = query.lower()
        results = []
        for r in records:
            searchable = " ".join(str(v).lower() for v in r.values())
            if query_lower in searchable:
                results.append(r)

        if not results:
            return f"No restaurants found matching '{query}'."

        lines = [f"{len(results)} restaurant(s) found:\n"]
        for r in results:
            line = f"🏪 {r.get('Name', '')} — 📍 {r.get('Location', '')}"
            if r.get("Tags"):
                line += f" ({r.get('Tags')})"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching restaurants: {str(e)}"

def list_restaurants(country_filter=None):
    """List all saved restaurants, optionally filtered by country."""
    try:
        sheet = restaurants_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No restaurants saved yet."

        if country_filter:
            records = [r for r in records if country_filter.lower() in r.get("Country", "").lower()]

        if not records:
            return f"No restaurants saved for {country_filter}."

        lines = [f"{len(records)} saved restaurant(s):\n"]
        for r in records:
            line = f"🏪 {r.get('Name', '')} — 📍 {r.get('Location', '')}"
            if r.get("Tags"):
                line += f" ({r.get('Tags')})"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing restaurants: {str(e)}"

def delete_restaurant(name):
    """Delete a restaurant by name."""
    try:
        sheet = restaurants_sheet()
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if name.lower() in r.get("Name", "").lower():
                sheet.delete_rows(i + 2)
                return f"Removed {r.get('Name')} from your list."
        return f"No restaurant found matching '{name}'."
    except Exception as e:
        return f"Error: {str(e)}"

def is_restaurant_save(text):
    """Detect restaurant save intent."""
    lower = text.lower()
    triggers = ["save restaurant", "add restaurant", "save this place", "add this place",
                "want to try", "add to my list", "save to my list", "maps.google",
                "goo.gl/maps", "restaurant to try", "place to try", "log this restaurant"]
    return any(t in lower for t in triggers)

def is_restaurant_search(text):
    """Detect restaurant search intent."""
    lower = text.lower()
    triggers = ["find a restaurant", "search restaurants", "any restaurants",
                "restaurant recommendations", "where to eat", "restaurants in",
                "show my restaurants", "my restaurant list", "saved restaurants"]
    return any(t in lower for t in triggers)

def handle_save_restaurant(text):
    """Handle saving a restaurant from text or maps link."""
    try:
        # Check if it contains a Maps URL
        if "maps.google" in text or "goo.gl/maps" in text or "maps.app.goo" in text:
            # Extract URL
            words = text.split()
            url = next((w for w in words if "map" in w.lower() or "goo.gl" in w.lower()), "")
            parsed = lookup_restaurant_from_maps(url)
            name = parsed.get("name", "")
            location = parsed.get("location", "")
            country = parsed.get("country", "Singapore")
            tags = ""
            notes = ""

            if not name:
                return "I got the link but couldn't extract the name. Try: 'save Burnt Ends, Teck Lim Road, tag: date night'"
        else:
            parsed = parse_restaurant_save(text)
            name = parsed.get("name", "")
            location = parsed.get("location", "")
            country = parsed.get("country", "Singapore")
            tags = parsed.get("tags", "")
            notes = parsed.get("notes", "")

        if not name:
            return "What's the restaurant name? Try: 'save Burnt Ends, Teck Lim Road'"

        save_restaurant(name, location, country, tags, notes)
        return format_restaurant_saved(name, location, tags, notes)

    except Exception as e:
        return f"Couldn't save that restaurant: {str(e)}"

def handle_search_restaurants(text):
    """Handle a restaurant search request."""
    try:
        lower = text.lower()
        # Check if listing all
        if "my restaurant list" in lower or "show my restaurants" in lower or "saved restaurants" in lower:
            return list_restaurants()

        # Extract search term
        for trigger in ["restaurants in", "find a restaurant", "any restaurants",
                        "where to eat", "restaurant recommendations", "search restaurants"]:
            if trigger in lower:
                query = lower.replace(trigger, "").strip()
                if query:
                    return search_restaurants(query)
                else:
                    return list_restaurants()

        # Fallback — search by full text
        return search_restaurants(text)
    except Exception as e:
        return f"Error: {str(e)}"



# =============================================================================
# STOCK MARKET ACCESS (Step 8)
# =============================================================================
# Uses Yahoo Finance via yfinance (free, 15min delay for US, near-realtime for others)
# Portfolio stored in Portfolio sheet
# Price alerts stored in memory + checked every 15 minutes

import urllib.request

# Price alerts: { "AAPL": { "condition": "below", "price": 180.0, "active": True } }
price_alerts = {}

# Indices to track for weekly summary
MARKET_RSS_FEEDS = {
    "US": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    ],
    "China": [
        "https://feeds.reuters.com/reuters/asiaNews",
        "https://www.scmp.com/rss/92/feed",
    ],
    "India": [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.moneycontrol.com/rss/marketreports.xml",
    ],
}

MARKET_FLAG = {"US": "🇺🇸", "China": "🇨🇳", "India": "🇮🇳"}

def is_market_summary_request(text):
    """Detect free-form market summary queries. Returns (True, market_name_or_None)."""
    lower = text.lower()
    triggers = [
        "market summary", "market update", "market today", "how is the market",
        "how are markets", "any market news", "weekly market", "market news",
        "what's happening in", "how is the", "give me a summary on",
        "how is", "market outlook"
    ]
    if not any(t in lower for t in triggers):
        return False, None
    # Extract specific market if named
    for market in ["us", "usa", "america", "american", "wall street", "s&p", "nasdaq", "dow"]:
        if market in lower:
            return True, "US"
    for market in ["china", "chinese", "shanghai", "hang seng", "hsi"]:
        if market in lower:
            return True, "China"
    for market in ["india", "indian", "sensex", "nifty", "nse", "bse"]:
        if market in lower:
            return True, "India"
    return True, None  # All markets

def fetch_market_headlines(market):
    """Fetch top RSS headlines for a market. Returns list of headline strings."""
    feeds = MARKET_RSS_FEEDS.get(market, [])
    headlines = []
    for feed_url in feeds:
        try:
            resp = requests.get(feed_url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            # Simple XML title extraction — no external library needed
            titles = re.findall(r"<title><!\[CDATA\[(.+?)\]\]></title>|<title>(?!.*<title)(.+?)</title>", resp.text)
            for match in titles[:4]:
                title = (match[0] or match[1]).strip()
                if title and len(title) > 15 and "rss" not in title.lower() and "feed" not in title.lower():
                    headlines.append(title)
            if len(headlines) >= 4:
                break
        except Exception as e:
            print(f"RSS fetch error for {market} ({feed_url}): {e}")
    return headlines[:5]

def summarise_headlines(market, headlines):
    """Pass headlines to Claude for a 2-3 sentence market narrative."""
    if not headlines:
        return ""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=120,
            messages=[{"role": "user", "content": (
                f"Summarise these {market} market headlines into 2-3 concise sentences covering what's moving markets. "
                f"No bullet points. No preamble. Just the summary.\n\n"
                + "\n".join(f"- {h}" for h in headlines)
            )}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"summarise_headlines error: {e}")
        return ""

MARKET_INDICES = {
    "US": {"^GSPC": "S&P 500", "^DJI": "Dow Jones", "^IXIC": "Nasdaq"},
    "China": {"000001.SS": "Shanghai", "^HSI": "Hang Seng"},
    "India": {"^BSESN": "Sensex", "^NSEI": "Nifty 50"},
}

def fetch_price(ticker):
    """Fetch current price — Alpha Vantage primary, Yahoo Finance fallback."""
    # --- Alpha Vantage ---
    if ALPHA_VANTAGE_API_KEY:
        try:
            url = (
                f"https://www.alphavantage.co/query"
                f"?function=GLOBAL_QUOTE&symbol={ticker}&apikey={ALPHA_VANTAGE_API_KEY}"
            )
            resp = requests.get(url, timeout=10)
            data = resp.json()
            quote = data.get("Global Quote", {})
            if quote.get("05. price"):
                price = float(quote["05. price"])
                prev_close = float(quote["08. previous close"])
                change = float(quote["09. change"])
                change_pct = float(quote["10. change percent"].replace("%", ""))
                return {
                    "ticker": ticker,
                    "name": ticker,  # Alpha Vantage Global Quote doesn't return company name
                    "price": price,
                    "prev_close": prev_close,
                    "change": change,
                    "change_pct": change_pct,
                    "currency": "USD"
                }
        except Exception as e:
            print(f"Alpha Vantage error for {ticker}: {e}")

    # --- Yahoo Finance fallback ---
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        result = data["chart"]["result"][0]
        meta = result["meta"]
        price = meta.get("regularMarketPrice", 0)
        prev_close = meta.get("chartPreviousClose", 0)
        currency = meta.get("currency", "USD")
        name = meta.get("longName") or meta.get("shortName") or ticker
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0
        return {
            "ticker": ticker,
            "name": name,
            "price": price,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "currency": currency
        }
    except Exception as e:
        print(f"Yahoo Finance fallback error for {ticker}: {e}")
        return None

def format_price(data):
    """Format a price response naturally."""
    if not data:
        return None
    arrow = "▲" if data["change"] >= 0 else "▼"
    sign = "+" if data["change"] >= 0 else ""
    return (
        f"{data['name']} ({data['ticker']}): {data['currency']} {data['price']:.2f} "
        f"{arrow} {sign}{data['change_pct']:.2f}%"
    )

def portfolio_sheet():
    return spreadsheet.worksheet("Portfolio")

def log_portfolio_buy(ticker, quantity, price, buy_date=None):
    """Log a stock purchase to Portfolio sheet."""
    sheet = portfolio_sheet()
    today = buy_date or date.today().strftime("%d/%m/%Y")
    sheet.append_row([ticker.upper(), str(quantity), str(price), today, ""])


def get_recent_portfolio_entries(n=5):
    """Return last N portfolio rows as list of (sheet_row, dict)."""
    try:
        sheet = portfolio_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return []
        headers = all_values[0]
        rows = all_values[1:]
        recent = rows[-n:]
        result = []
        for i, row in enumerate(recent):
            sheet_row = len(all_values) - len(recent) + i + 1
            d = {headers[j]: row[j] if j < len(row) else "" for j in range(len(headers))}
            result.append((sheet_row, d))
        return list(reversed(result))
    except Exception as e:
        print(f"get_recent_portfolio_entries error: {e}")
        return []

def search_portfolio_by_ticker(ticker):
    """Return all portfolio rows matching ticker as list of (sheet_row, dict)."""
    try:
        sheet = portfolio_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return []
        headers = all_values[0]
        rows = all_values[1:]
        q = ticker.upper().strip()
        matches = []
        for i, row in enumerate(rows):
            d = {headers[j]: row[j] if j < len(row) else "" for j in range(len(headers))}
            if d.get("Stock", "").upper() == q:
                matches.append((i + 2, d))
        return list(reversed(matches))
    except Exception as e:
        print(f"search_portfolio_by_ticker error: {e}")
        return []

def format_portfolio_delete_list(entries):
    """Format numbered list of portfolio entries for delete selection."""
    lines = ["Which entry to delete?\n"]
    for i, (_, r) in enumerate(entries, 1):
        ticker = r.get("Stock", "?")
        qty = r.get("Quantity", "")
        price = r.get("Buy Price", "")
        date_str = r.get("Buy Date", "")
        lines.append(f"{i}. {ticker} — {qty} shares @ ${price} on {date_str}")
    lines.append("\nReply with a number or 'cancel'.")
    return "\n".join(lines)

def delete_portfolio_entry_by_row(sheet_row):
    """Delete a portfolio entry by sheet row. Returns confirmation string."""
    try:
        sheet = portfolio_sheet()
        all_values = sheet.get_all_values()
        if sheet_row < 2 or sheet_row > len(all_values):
            return "Couldn't find that entry."
        row = all_values[sheet_row - 1]
        ticker = row[0] if row else "?"
        qty = row[1] if len(row) > 1 else "?"
        price = row[2] if len(row) > 2 else "?"
        date_str = row[3] if len(row) > 3 else "?"
        sheet.delete_rows(sheet_row)
        return f"Deleted: {ticker} — {qty} shares @ ${price} on {date_str}"
    except Exception as e:
        return f"Error deleting entry: {str(e)}"

def get_portfolio_holdings():
    """Get current holdings with average cost."""
    try:
        sheet = portfolio_sheet()
        records = sheet.get_all_records()
        holdings = {}
        for r in records:
            ticker = r.get("Stock", "").upper()
            qty = float(r.get("Quantity", 0))
            price = float(r.get("Buy Price", 0))
            if ticker not in holdings:
                holdings[ticker] = {"total_qty": 0, "total_cost": 0}
            holdings[ticker]["total_qty"] += qty
            holdings[ticker]["total_cost"] += qty * price
        # Calculate averages
        result = {}
        for ticker, h in holdings.items():
            if h["total_qty"] > 0:
                result[ticker] = {
                    "qty": h["total_qty"],
                    "avg_cost": h["total_cost"] / h["total_qty"]
                }
        return result
    except Exception as e:
        print(f"Error getting portfolio: {e}")
        return {}

def get_portfolio_performance():
    """Get portfolio performance — current vs average cost."""
    holdings = get_portfolio_holdings()
    if not holdings:
        return "No holdings logged yet."

    lines = ["Portfolio:\n"]
    total_cost = 0
    total_value = 0

    for ticker, h in holdings.items():
        data = fetch_price(ticker)
        avg = h["avg_cost"]
        qty = h["qty"]
        cost = avg * qty

        if data:
            current = data["price"]
            value = current * qty
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost else 0
            sign = "+" if pnl >= 0 else ""
            flag = "✅" if pnl >= 0 else "⚠️"
            lines.append(
                f"{flag} {ticker}: {qty:.0f} shares @ avg {data['currency']} {avg:.2f} "
                f"| now {data['currency']} {current:.2f} | {sign}{pnl_pct:.1f}% ({sign}{data['currency']} {pnl:.2f})"
            )
            total_cost += cost
            total_value += value
        else:
            lines.append(f"• {ticker}: {qty:.0f} shares @ avg {avg:.2f} (price unavailable)")
            total_cost += cost

    if total_cost > 0:
        total_pnl = total_value - total_cost
        total_pct = (total_pnl / total_cost * 100)
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\nTotal P&L: {sign}{total_pct:.1f}% ({sign}${total_pnl:.2f})")

    return "\n".join(lines)

def set_price_alert(ticker, condition, price):
    """Set a price alert for a ticker."""
    ticker = ticker.upper()
    price_alerts[ticker] = {"condition": condition, "price": price, "active": True}

def parse_stock_request(text):
    """Use Claude to parse a stock-related request."""
    prompt = (
        f"Parse this stock market request: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- intent: string (one of: price_check, set_alert, portfolio_add, portfolio_view, "
        f"stock_suggest, market_summary, price_alert_check)\n"
        f"- ticker: string (stock ticker symbol, uppercase, or empty)\n"
        f"- quantity: number (shares, or 0)\n"
        f"- price: number (price per share, or 0)\n"
        f"- alert_condition: string (above or below, or empty)\n"
        f"- alert_price: number (alert trigger price, or 0)\n"
        f"- criteria: string (for stock suggestions — describe what user wants)\n\n"
        f"Return ONLY the JSON."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

def suggest_stocks(criteria):
    """Suggest 3 stocks based on described criteria."""
    prompt = (
        f"Suggest 3 stocks based on this criteria: '{criteria}'\n\n"
        f"For each stock provide a qualitative summary. Flag concerns with a warning, all clear with a checkmark.\n\n"
        f"Format your response exactly like this for each stock (with a divider line between each):\n\n"
        f"TICKER — Company Name\n"
        f"[checkmark or warning] [2-3 sentence qualitative summary. No numbers unless asked.]\n\n"
        f"---\n\n"
        f"Keep it concise and honest. Flag anything concerning."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text.strip()

async def check_price_alerts(app):
    """Check price alerts every 15 minutes."""
    try:
        for ticker, alert in list(price_alerts.items()):
            if not alert.get("active"):
                continue
            data = fetch_price(ticker)
            if not data:
                continue
            current = data["price"]
            condition = alert["condition"]
            trigger = alert["price"]

            triggered = (condition == "below" and current <= trigger) or                        (condition == "above" and current >= trigger)

            if triggered:
                msg = (
                    f"🔔 Price alert: {ticker} is now {data['currency']} {current:.2f} "
                    f"({condition} your target of {data['currency']} {trigger:.2f})"
                )
                await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
                price_alerts[ticker]["active"] = False  # deactivate after firing

    except Exception as e:
        print(f"Error checking price alerts: {e}")

async def send_weekly_market_summary(app):
    """Monday 8am — send US, China, India market summary with headlines."""
    try:
        msg = "*Weekly Market Summary*\n\n" + get_market_summary_now()
        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending weekly market summary: {e}")

def is_stock_request(text):
    """Detect stock market related requests using explicit trigger phrases only.
    Avoids misfires on bare capitalised words like HELP, OK, I need HELP etc.
    """
    lower = text.lower()
    explicit_triggers = [
        "pull up ", "look into ", "price of ", "check ",
        "alert me if", "alert if ", "add to portfolio",
        "bought ", "sold ", "suggest stocks", "stock ideas",
        "stock ", "ticker ", "p&l", "holdings",
        "market summary", "how is the market", "market today",
        "weekly market", "how are markets", "portfolio performance"
    ]
    if any(t in lower for t in explicit_triggers):
        return True
    ticker_match = re.search(r'\b[A-Z]{2,5}\b', text)
    if ticker_match and any(w in lower for w in ["doing", "worth", "performing", "price", "target", "outlook"]):
        return True
    return False

def handle_stock_request(text):
    """Route a stock request to the right handler."""
    try:
        parsed = parse_stock_request(text)
        intent = parsed.get("intent", "")
        ticker = parsed.get("ticker", "").upper()

        if intent == "price_check" and ticker:
            data = fetch_price(ticker)
            if data:
                return format_price(data)
            if ALPHA_VANTAGE_API_KEY:
                return f"Couldn't fetch {ticker} — Alpha Vantage and Yahoo Finance both failed. The ticker may be wrong, or try again in a moment."
            return f"Couldn't fetch {ticker}. Check the ticker and try again."

        elif intent == "set_alert" and ticker:
            condition = parsed.get("alert_condition", "below")
            alert_price = parsed.get("alert_price", 0)
            if not alert_price:
                return "What price should I alert you at? Try: 'alert me if AAPL drops below $180'"
            set_price_alert(ticker, condition, alert_price)
            return f"Alert set — I'll let you know when {ticker} goes {condition} ${alert_price:.2f}."

        elif intent == "portfolio_add" and ticker:
            qty = parsed.get("quantity", 0)
            price = parsed.get("price", 0)
            if not qty or not price:
                return "I need the quantity and price. Try: 'bought 100 AAPL at $180'"
            log_portfolio_buy(ticker, qty, price)
            return f"Logged — {qty:.0f} {ticker} @ ${price:.2f}."

        elif intent == "portfolio_view":
            return get_portfolio_performance()

        elif intent == "stock_suggest":
            criteria = parsed.get("criteria", text)
            return suggest_stocks(criteria)

        elif intent == "market_summary":
            # Trigger on-demand summary
            return "Pulling the latest market data, give me a sec..."

        else:
            # Fallback — try price check if ticker found
            if ticker:
                data = fetch_price(ticker)
                if data:
                    return format_price(data)
            return None  # Fall through to Claude chat

    except Exception as e:
        return f"Something went wrong: {str(e)}"


# --- Natural Language CRM Update Detection ---

def detect_crm_natural_update(text):
    """
    Detect natural language CRM field updates like:
    "Sarah's email is sarah@gmail.com"
    "update James's address to 123 Orchard Road"
    "James referred Sarah"
    Returns (action, name, field, value) or None.
    """
    lower = text.lower()

    # Referral: "X referred Y"
    ref_match = re.search(
        r"([a-z][a-z\s']+?)\s+referred\s+([a-z][a-z\s']+?)(?:\s+to\s+me|\s+to\s+us)?\.?$",
        lower
    )
    if ref_match:
        referrer = ref_match.group(1).strip().title()
        referred = ref_match.group(2).strip().title()
        return ("referral", referrer, referred, None)

    # Pattern: "[Name]'s [field] is [value]"
    is_match = re.search(
        r"([a-z][a-z\s']+?)'s\s+(email|address|alias|birthday|relationship|context|notes)\s+is\s+(.+)",
        lower
    )
    if is_match:
        name = is_match.group(1).strip().title()
        field = is_match.group(2).strip()
        value = is_match.group(3).strip().rstrip(".")
        return ("update", name, field, value)

    # Pattern: "update [Name]'s [field] to [value]"
    update_match = re.search(
        r"update\s+([a-z][a-z\s']+?)'s\s+(email|address|alias|birthday|relationship|context|notes)\s+to\s+(.+)",
        lower
    )
    if update_match:
        name = update_match.group(1).strip().title()
        field = update_match.group(2).strip()
        value = update_match.group(3).strip().rstrip(".")
        return ("update", name, field, value)

    # Pattern: "[Name]'s email" / "what's [Name]'s email/address"
    ask_private = re.search(
        r"(?:what'?s?\s+)?([a-z][a-z\s']+?)'s\s+(email|address)",
        lower
    )
    if ask_private and any(w in lower for w in ["what", "show", "tell", "give"]):
        name = ask_private.group(1).strip().title()
        field = ask_private.group(2).strip()
        return ("show_private", name, field, None)

    return None


def _build_market_block(market, indices):
    """Build one market's summary block — indices + sentiment + headlines."""
    flag = MARKET_FLAG.get(market, "")
    lines = [f"{flag} *{market} Market*", ""]
    market_changes = []

    for ticker, name in indices.items():
        data = fetch_price(ticker)
        if data:
            arrow = "▲" if data["change_pct"] >= 0 else "▼"
            lines.append(f"{name} {arrow} {abs(data['change_pct']):.1f}%")
            market_changes.append(data["change_pct"])

    if market_changes:
        avg = sum(market_changes) / len(market_changes)
        sentiment = "Positive" if avg > 0.3 else "Negative" if avg < -0.3 else "Mixed"
        lines.append(f"Sentiment: {sentiment}")

    # RSS headlines → Claude summary
    headlines = fetch_market_headlines(market)
    if headlines:
        summary = summarise_headlines(market, headlines)
        if summary:
            lines.append("")
            lines.append(f"📰 *What's moving*")
            lines.append(summary)

    return "\n".join(lines), market_changes

def get_market_summary_now(target_market=None):
    """Generate on-demand market summary with live indices + RSS headlines."""
    try:
        markets = {target_market: MARKET_INDICES[target_market]} if target_market and target_market in MARKET_INDICES else MARKET_INDICES
        blocks = []
        all_changes = []
        for market, indices in markets.items():
            block, changes = _build_market_block(market, indices)
            blocks.append(block)
            all_changes.extend(changes)
        result = "\n\n".join(blocks)
        if not target_market and all_changes:
            avg_all = sum(all_changes) / len(all_changes)
            overall = "broadly positive" if avg_all > 0.3 else "broadly negative" if avg_all < -0.3 else "mixed"
            result += f"\n\nOverall: {overall}"
        return result
    except Exception as e:
        return f"Couldn't pull market data right now: {str(e)}"

async def handle_statement_upload(file_bytes, fname, user_id, update):
    """Parse a bank statement CSV/XLSX and reconcile against logged expenses."""
    try:
        await update.message.reply_text("Got the statement, give me a sec to go through it...")
        # Parse statement rows
        statement_rows = []
        if fname.lower().endswith(".csv"):
            import csv
            text_data = file_bytes.decode("utf-8", errors="ignore")
            reader = csv.DictReader(io.StringIO(text_data))
            for row in reader:
                statement_rows.append(dict(row))
        else:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
            ws = wb.active
            headers = [str(c.value).strip() if c.value else "" for c in next(ws.iter_rows(max_row=1))]
            for row in ws.iter_rows(min_row=2, values_only=True):
                statement_rows.append(dict(zip(headers, row)))

        if not statement_rows:
            await update.message.reply_text("Couldn't read any rows from that file.")
            return

        # Use Claude to normalise statement columns
        sample = json.dumps(statement_rows[:3], default=str)
        norm_prompt = (
            f"Given these bank statement rows: {sample}\n\n"
            f"Return ONLY a JSON object mapping these keys to the actual column names in the data:\n"
            f"{{\"date\": \"col\", \"description\": \"col\", \"amount\": \"col\"}}\n"
            f"If a column doesn't exist, use null."
        )
        norm_resp = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=100,
            messages=[{"role": "user", "content": norm_prompt}]
        )
        col_map = json.loads(norm_resp.content[0].text.strip().replace("```json","").replace("```","").strip())
        date_col = col_map.get("date")
        desc_col = col_map.get("description")
        amt_col = col_map.get("amount")

        if not all([date_col, desc_col, amt_col]):
            await update.message.reply_text("Couldn't identify date/description/amount columns. Try a CSV with clear headers.")
            return

        # Load logged expenses
        sheet = expenses_sheet()
        logged = sheet.get_all_records()

        missing = []
        corrections = []

        for srow in statement_rows:
            raw_date = str(srow.get(date_col, "")).strip()
            raw_desc = str(srow.get(desc_col, "")).strip()
            raw_amt = str(srow.get(amt_col, "")).strip().replace(",", "")
            if not raw_date or not raw_amt:
                continue
            try:
                stmt_amount = abs(float(raw_amt))
            except ValueError:
                continue

            # Normalise date
            stmt_date = None
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y", "%d %b %y"):
                try:
                    stmt_date = datetime.strptime(raw_date, fmt).strftime("%d/%m/%Y")
                    break
                except Exception:
                    continue
            if not stmt_date:
                continue

            # Match against logged expenses — date + amount
            matched_row = None
            matched_idx = None
            for i, r in enumerate(logged):
                logged_date = r.get("Date", "")
                logged_sgd = float(r.get("SGD Amount") or r.get("Amount") or 0)
                if logged_date == stmt_date and abs(logged_sgd - stmt_amount) < 0.02:
                    matched_row = r
                    matched_idx = i
                    break

            if matched_row is None:
                # Check if ambiguous gateway — skip if can't resolve
                missing.append(f"{stmt_date} | {raw_desc[:40]} | SGD ${stmt_amount:.2f}")
            else:
                # Check for amount discrepancy
                logged_sgd = float(matched_row.get("SGD Amount") or matched_row.get("Amount") or 0)
                if abs(logged_sgd - stmt_amount) > 0.01:
                    # Correct the SGD amount in sheet — row is matched_idx + 2 (1-indexed + header)
                    all_values = sheet.get_all_values()
                    headers_row = all_values[0]
                    sgd_col_idx = headers_row.index("SGD Amount") + 1 if "SGD Amount" in headers_row else None
                    if sgd_col_idx:
                        sheet.update_cell(matched_idx + 2, sgd_col_idx, stmt_amount)
                    corrections.append(
                        f"{matched_row.get('Merchant', raw_desc[:20])} {stmt_date}: "
                        f"${logged_sgd:.2f} → ${stmt_amount:.2f}"
                    )

        lines = ["Reconciliation done ✅"]
        if corrections:
            lines.append(f"\nCorrected {len(corrections)} amount(s):")
            lines.extend(f"  {c}" for c in corrections)
        if missing:
            lines.append(f"\n{len(missing)} unmatched statement item(s) — couldn't find in your logs:")
            lines.extend(f"  {m}" for m in missing[:10])
            if len(missing) > 10:
                lines.append(f"  ...and {len(missing) - 10} more")
        if not corrections and not missing:
            lines.append("Everything matches up.")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        print(f"handle_statement_upload error: {e}")
        await update.message.reply_text(f"Something went wrong parsing the statement: {str(e)}")



    """Return a flight context block for the system prompt if terminal/gate info is available."""
    dep_terminal = overseas_state.get("dep_terminal", "")
    dep_gate = overseas_state.get("dep_gate", "")
    arr_terminal = overseas_state.get("arr_terminal", "")
    arr_gate = overseas_state.get("arr_gate", "")
    dep_flight = overseas_state.get("dep_flight", "")
    dep_time = overseas_state.get("dep_time", "")

    if not any([dep_terminal, dep_gate, arr_terminal, arr_gate]):
        return ""

    lines = ["\n\n## Upcoming Flight Info"]
    if dep_flight:
        lines.append(f"Flight: {dep_flight}")
    if dep_time:
        lines.append(f"Departure: {format_flight_time(dep_time)}")
    if dep_terminal:
        lines.append(f"Departure terminal: {dep_terminal}")
    if dep_gate:
        lines.append(f"Departure gate: {dep_gate}")
    if arr_terminal:
        lines.append(f"Arrival terminal: {arr_terminal}")
    if arr_gate:
        lines.append(f"Arrival gate: {arr_gate}")
    lines.append("Use this info to answer questions about terminals and gates directly.")
    return "\n".join(lines)


# --- Em System Prompt Builder ---
def _build_overseas_flight_context():
    """Return a flight context block for the system prompt if terminal/gate info is available."""
    dep_terminal = overseas_state.get("dep_terminal", "")
    dep_gate = overseas_state.get("dep_gate", "")
    arr_terminal = overseas_state.get("arr_terminal", "")
    arr_gate = overseas_state.get("arr_gate", "")
    dep_flight = overseas_state.get("dep_flight", "")
    dep_time = overseas_state.get("dep_time", "")

    if not any([dep_terminal, dep_gate, arr_terminal, arr_gate]):
        return ""

    lines = ["\n\n## Upcoming Flight Info"]
    if dep_flight:
        lines.append(f"Flight: {dep_flight}")
    if dep_time:
        lines.append(f"Departure: {format_flight_time(dep_time)}")
    if dep_terminal:
        lines.append(f"Departure terminal: {dep_terminal}")
    if dep_gate:
        lines.append(f"Departure gate: {dep_gate}")
    if arr_terminal:
        lines.append(f"Arrival terminal: {arr_terminal}")
    if arr_gate:
        lines.append(f"Arrival gate: {arr_gate}")
    lines.append("Use this info to answer questions about terminals and gates directly.")
    return "\n".join(lines)


def build_system_prompt():
    """Build Em's system prompt, incorporating em_profile preferences."""
    profile_notes = ""
    if em_profile:
        forbidden = ", ".join(em_profile.get("forbidden_phrases", []))
        profile_notes = f"\nForbidden phrases (never use): {forbidden}" if forbidden else ""

    # Overseas + expense context
    overseas_context = ""
    if overseas_state.get("active"):
        dest = overseas_state.get("destination", "")
        curr = overseas_state.get("currency", "SGD")
        trip_start = overseas_state.get("trip_start", "")
        currencies = overseas_state.get("currencies", [])
        curr_list = ", ".join(currencies) if currencies else curr
        overseas_context = (
            f"\n\n## Overseas Mode\n"
            f"Currently active. Destination: {dest}. Currency: {curr}.\n"
            f"Currencies used this trip: {curr_list}.\n"
            f"Trip started: {trip_start}.\n"
            f"Expenses entered without a currency are assumed to be in {curr}.\n"
            f"All expenses are converted to SGD using a cached rate refreshed twice daily (8am and 8pm).\n"
            f"You can answer questions about trip spend, currency, and expense logging directly."
        )

    expense_context = (
        "\n\n## Expense Tracking\n"
        "Em tracks expenses to Google Sheets. "
        f"Categories: {', '.join(EXPENSE_CATEGORIES)}. "
        f"Cards: {', '.join(EXPENSE_CARDS)}. "
        "Known merchants are remembered — category and card are auto-filled for repeat merchants. "
        "Foreign currency expenses show SGD amount with original currency in brackets. "
        "Receipts can be attached as photo with caption."
    )

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
        "- Relationship: [relationship]\n"
        "- Context: [how you know them]\n"
        "- Birthday: [DD MMM YYYY (age N)]\n"
        "- Notes:\n"
        "  - [note 1]\n"
        "  - [note 2]\n\n"
        "_Last updated: DD MMM YYYY_\n\n"
        "Email and Address are private fields — never show them unless the user specifically asks.\n"
        "Age is calculated on the fly from Birthday — never store or display a static age.\n"
        "Always separate notes into individual bullet points. Never dump them in one line.\n\n"
        "## ABSOLUTE RULE — CRM Data\n"
        "You NEVER invent, fabricate, guess, or generate contact information. "
        "If asked about a person and no real data was retrieved from the sheet, "
        "say you don't have them in the CRM. Never produce a formatted contact card "
        "unless the data came directly from a sheet lookup in this same message. "
        "This rule overrides everything else — no exceptions.\n\n"
        "- Sound stiff or corporate\n"
        "- Act like a typical AI assistant\n"
        "- Make small talk for the sake of it\n"
        "- Get repetitive with phrases or greetings"
    ) + _build_overseas_flight_context() + overseas_context + expense_context

# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != YOUR_CHAT_ID:
        return

    # Handle document/file uploads (Excel import or bank statement)
    if update.message.document:
        doc = update.message.document
        fname = doc.file_name or ""
        # Bank statement for reconciliation
        if fname.lower().endswith(".csv") or (fname.lower().endswith((".xlsx", ".xls")) and "statement" in fname.lower()):
            tg_file = await doc.get_file()
            file_bytes = bytes(await tg_file.download_as_bytearray())
            await handle_statement_upload(file_bytes, fname, user_id, update)
            return
        if fname.lower().endswith((".xlsx", ".xls")):
            tg_file = await doc.get_file()
            file_bytes = bytes(await tg_file.download_as_bytearray())

            # Auto-detect column order from the file's own header row
            try:
                import openpyxl
                wb_peek = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
                ws_peek = wb_peek.active
                first_row = next(ws_peek.iter_rows(max_row=1, values_only=True), None)
                auto_cols = [str(c).strip().replace('\xa0','') for c in first_row if c] if first_row else []
            except Exception as e:
                print(f"Excel header auto-detect error: {e}")
                auto_cols = []

            if auto_cols:
                # Got headers from file — import directly without asking
                col_str = ", ".join(auto_cols)
                await update.message.reply_text(f"Detected columns: {col_str}\nImporting now...")
                await handle_excel_import(file_bytes, auto_cols, update)
            elif user_id in excel_import_sessions and excel_import_sessions[user_id].get("step") == "awaiting_file":
                col_order = excel_import_sessions[user_id].get("column_order", [])
                del excel_import_sessions[user_id]
                await handle_excel_import(file_bytes, col_order, update)
            else:
                excel_import_sessions[user_id] = {
                    "step": "awaiting_columns",
                    "file_bytes": file_bytes
                }
                await update.message.reply_text(
                    "Got the file but couldn't read its headers. Tell me the column order — "
                    "e.g. 'Name, Alias, Email, Date of Birth'"
                )
        else:
            await update.message.reply_text("I can only import .xlsx or .xls files for CRM.")
        return

    # Handle photo messages — receipt with or without caption
    if update.message.photo:
        caption = (update.message.caption or "").strip()
        photo = update.message.photo[-1]  # highest resolution
        tg_file = await photo.get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())

        # Always upload to Drive first
        receipt_link = ""
        try:
            from googleapiclient.http import MediaIoBaseUpload
            today_str = date.today().strftime("%Y%m%d")
            file_name = f"receipt_{today_str}_{photo.file_id[:8]}.jpg"
            folder_id = DRIVE_FOLDERS.get("receipts", "")
            if folder_id:
                media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
                file_meta = {"name": file_name, "parents": [folder_id]}
                uploaded = drive_service.files().create(body=file_meta, media_body=media, fields="id,webViewLink").execute()
                receipt_link = uploaded.get("webViewLink", "")
        except Exception as e:
            print(f"Receipt upload error: {e}")

        # Caption takes full priority — existing flow unchanged
        if caption:
            if is_expense_input(caption):
                reply, needs_session, session_data = handle_expense_text(caption, user_id, receipt_link=receipt_link)
                if needs_session and session_data:
                    expense_sessions[user_id] = session_data
                if reply:
                    try:
                        await update.message.reply_text(reply, parse_mode="Markdown")
                    except Exception:
                        await update.message.reply_text(reply)
            else:
                await update.message.reply_text("Got the receipt but couldn't read that as an expense. Try a caption like '1400 Ichiran' or 'spent 45 at Uniqlo'.")
            return

        # No caption — use Claude Vision to parse the receipt
        await update.message.reply_text("Reading receipt...")
        parsed = parse_receipt_image(file_bytes)

        if not parsed or not parsed.get("merchant") or not parsed.get("amount"):
            await update.message.reply_text(
                "Couldn't read the receipt clearly.\n"
                "Add a caption with the details — e.g. '45.00 Ichiran' and I'll log it."
            )
            return

        merchant = parsed.get("merchant", "")
        try:
            amount = float(str(parsed.get("amount", 0)).replace(",", ""))
        except (ValueError, TypeError):
            amount = 0
        currency = parsed.get("currency", overseas_state["currency"] if overseas_state["active"] else "SGD")
        uncertain = parsed.get("uncertain_fields", [])

        if amount == 0:
            await update.message.reply_text(
                f"Got the merchant ({merchant}) but couldn't read the total.\n"
                f"Reply with the amount and I'll log it — e.g. '45.00'"
            )
            return

        # FX conversion if needed
        sgd_amount = amount
        if currency != "SGD":
            rate = get_fx_rate(currency)
            if rate:
                sgd_amount = round(amount * rate, 2)
                if overseas_state["active"] and currency not in overseas_state["currencies"]:
                    overseas_state["currencies"].append(currency)

        # Merchant Map lookup
        known_cat, known_card, canonical = get_merchant_memory(merchant)
        if canonical:
            merchant = canonical
        category = known_cat or ""
        card = known_card or ""

        # Build missing fields list
        missing = []
        if not category:
            missing.append("category")
        if not card:
            missing.append("card")

        emoji = get_merchant_emoji(category, merchant)
        amt_str = f"${sgd_amount:.2f} ({currency} {amount:,.0f})" if currency != "SGD" else f"${amount:.2f}"

        if missing:
            # Ask category + card in one message
            lines = [f"{emoji} {merchant}", amt_str, ""]
            if uncertain:
                lines.append(f"_(uncertain: {', '.join(uncertain)})_")
                lines.append("")
            if "category" in missing:
                lines.append(f"Category? ({' / '.join(EXPENSE_CATEGORIES)})")
            if "card" in missing:
                lines.append(f"Card? ({' / '.join(EXPENSE_CARDS)})")
            expense_sessions[user_id] = {
                "merchant": merchant, "amount": amount, "currency": currency,
                "sgd_amount": sgd_amount, "category": category, "card": card,
                "step": "confirm", "missing_fields": missing,
                "receipt_link": receipt_link
            }
            try:
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            except Exception:
                await update.message.reply_text("\n".join(lines))
        else:
            # All fields known — log directly and show confirmation
            log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link=receipt_link)
            save_merchant_memory(merchant, category, card)
            confirmation = format_expense_confirmation(merchant, amount, currency, category, card, sgd_amount, receipt_saved=True)
            try:
                await update.message.reply_text(confirmation, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(confirmation)
        return

    text = update.message.text.strip()
    lower = text.lower()

    # Check birthday acknowledgement — any incoming message counts
    check_birthday_acknowledgement()

    # Excel import column declaration
    if user_id in excel_import_sessions:
        session = excel_import_sessions[user_id]
        if session.get("step") == "awaiting_columns":
            cols = parse_excel_column_order(text)
            if cols:
                if session.get("file_bytes"):
                    # File already uploaded, import immediately
                    file_bytes = session["file_bytes"]
                    del excel_import_sessions[user_id]
                    await update.message.reply_text(f"Got it — columns: {', '.join(cols)}. Importing now...")
                    await handle_excel_import(file_bytes, cols, update)
                else:
                    session["column_order"] = cols
                    session["step"] = "awaiting_file"
                    await update.message.reply_text(
                        f"Got it — columns: {', '.join(cols)}. Now send the Excel file."
                    )
            else:
                await update.message.reply_text("Couldn't parse that. Try: 'Name, Email, Date of Birth, Alias'")
            return

    # Handle active meeting recap session
    if user_id in meeting_sessions:
        await handle_meeting_session(user_id, text, update)
        return

    # Handle active expense onboarding session
    if user_id in expense_sessions:
        await handle_expense_session(user_id, text, update)
        return

    # Handle active delete session
    if user_id in delete_sessions:
        session = delete_sessions[user_id]
        step = session.get("step")

        if lower in ["cancel", "nevermind", "never mind", "nvm"]:
            del delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return

        if step == "pick":
            # User replies with a number
            if text.strip().isdigit():
                idx = int(text.strip()) - 1
                expenses = session.get("expenses", [])
                if 0 <= idx < len(expenses):
                    sheet_row, _ = expenses[idx]
                    reply = delete_expense_by_row(sheet_row)
                    del delete_sessions[user_id]
                    await update.message.reply_text(reply)
                else:
                    await update.message.reply_text("Invalid number. Try again or 'search [merchant]'.")
                return
            # User wants to search instead
            elif lower.startswith("search "):
                query = text[7:].strip()
                results = search_expenses_by_merchant(query)
                if not results:
                    await update.message.reply_text(f"No expenses found matching '{query}'.")
                elif len(results) == 1:
                    sheet_row, r = results[0]
                    reply = delete_expense_by_row(sheet_row)
                    del delete_sessions[user_id]
                    await update.message.reply_text(reply)
                else:
                    delete_sessions[user_id] = {"step": "pick", "expenses": results}
                    await update.message.reply_text(format_delete_list(results))
                return
            elif lower in ["not there", "not here", "none of these", "not in the list"]:
                await update.message.reply_text("Reply 'search [merchant name]' to find it.")
                return
            else:
                await update.message.reply_text("Reply with a number, 'search [merchant]', or 'cancel'.")
                return

    # Handle active confirm session (destructive operations)
    if user_id in confirm_sessions:
        session = confirm_sessions[user_id]
        response = text.strip().lower()
        if response in ["yes", "y", "yep", "yeah", "confirm", "ok"]:
            action = session["action"]
            args = session.get("args", [])
            del confirm_sessions[user_id]
            if action == "delete_contact":
                reply = delete_contact(args[0])
            elif action == "delete_bill":
                reply = delete_bill(args[0])
            elif action == "delete_restaurant":
                reply = delete_restaurant(args[0])
            elif action == "delete_event":
                reply = delete_calendar_event(args[0])
            elif action == "rename_category":
                reply = rename_category(args[0], args[1])
            else:
                reply = "Action not recognised."
        elif response in ["no", "n", "nope", "cancel"]:
            del confirm_sessions[user_id]
            reply = "Cancelled."
        else:
            target = session.get("target", "")
            reply = f"Reply *yes* to confirm or *no* to cancel.\n({target})"
        if reply:
            try:
                await update.message.reply_text(reply, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(reply)
        return

    # Handle active portfolio delete session
    if user_id in portfolio_delete_sessions:
        session = portfolio_delete_sessions[user_id]
        step = session.get("step")
        if lower in ["cancel", "nevermind", "nvm"]:
            del portfolio_delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return
        if step == "pick":
            if text.strip().isdigit():
                idx = int(text.strip()) - 1
                entries = session.get("entries", [])
                if 0 <= idx < len(entries):
                    sheet_row, r = entries[idx]
                    ticker = r.get("Stock", "?")
                    qty = r.get("Quantity", "")
                    price = r.get("Buy Price", "")
                    date_str = r.get("Buy Date", "")
                    desc = f"{ticker} — {qty} shares @ ${price} on {date_str}"
                    portfolio_delete_sessions[user_id] = {"step": "confirm", "pending": (sheet_row, desc)}
                    await update.message.reply_text(f"Delete: {desc}? (yes / no)")
                else:
                    await update.message.reply_text("Invalid number. Try again or 'cancel'.")
            else:
                await update.message.reply_text("Reply with a number or 'cancel'.")
            return
        if step == "confirm":
            if response in ["yes", "y", "yep", "yeah"]:
                sheet_row, desc = session["pending"]
                del portfolio_delete_sessions[user_id]
                result = delete_portfolio_entry_by_row(sheet_row)
                await update.message.reply_text(result)
            elif response in ["no", "n", "nope", "cancel"]:
                del portfolio_delete_sessions[user_id]
                await update.message.reply_text("Cancelled.")
            else:
                _, desc = session["pending"]
                await update.message.reply_text(f"Delete: {desc}? (yes / no)")
            return

    # Handle active CRM edit session
    if user_id in edit_sessions:
        await handle_edit_session(user_id, text, update)
        return

    # Pending flight confirmation — intercept the next message entirely
    if overseas_state.get("_pending_flight"):
        pf = overseas_state["_pending_flight"]
        if text.strip().upper() == "Y":
            overseas_state.pop("_pending_flight")
            info = get_dest_info_from_iata(pf.get("arr_iata", ""), pf.get("arr_city", pf.get("arr_airport", "")))
            dest = info.get("destination") or pf.get("arr_city") or pf.get("arr_airport", "Unknown")
            curr = info.get("currency", "SGD")
            dep_str = pf.get("dep_time", "")
            dep_fmt = format_flight_time(dep_str)
            return_flight_data = pf.get("return_flight_data")
            # Schedule activation at departure time
            scheduled = False
            if dep_str and _scheduler:
                try:
                    dep_dt = datetime.fromisoformat(dep_str.replace("Z", "+00:00"))
                    dep_local = dep_dt.astimezone(TIMEZONE)
                    now_local = datetime.now(TIMEZONE)
                    if dep_local > now_local:
                        job = _scheduler.add_job(
                            activate_overseas_mode_scheduled,
                            "date",
                            run_date=dep_local,
                            args=[dest, curr, return_flight_data]
                        )
                        overseas_state["dep_job_id"] = job.id
                        overseas_state["destination"] = dest
                        overseas_state["currency"] = curr
                        overseas_state["dep_flight"] = pf.get("flight_number", "")
                        overseas_state["dep_time"] = pf.get("dep_time", "")
                        overseas_state["dep_terminal"] = pf.get("dep_terminal", "")
                        overseas_state["dep_gate"] = pf.get("dep_gate", "")
                        overseas_state["arr_terminal"] = pf.get("arr_terminal", "")
                        overseas_state["arr_gate"] = pf.get("arr_gate", "")
                        scheduled = True
                except Exception as e:
                    print(f"Failed to schedule departure: {e}")
            # Save trip to Trips sheet regardless of scheduled/immediate
            try:
                trip_id = _save_confirmed_trip(pf, return_flight_data)
                overseas_state["trip_id"] = trip_id
                if scheduled:
                    mark_trip_active(trip_id)  # will be marked active at departure
            except Exception as e:
                print(f"Trip save error: {e}")
                trip_id = None

            if scheduled:
                ret_str = ""
                if return_flight_data:
                    ret_dep = format_flight_time(return_flight_data.get("dep_time", ""))
                    ret_arr = format_flight_time(return_flight_data.get("arr_time", ""))
                    ret_str = f"\nReturn: {return_flight_data.get('flight', '')} {ret_dep} → {ret_arr}"
                trip_str = f" (Trip {trip_id})" if trip_id else ""
                reply = (
                    f"Got it ✈️ Overseas mode will activate at departure: {dep_fmt}\n"
                    f"Destination: {dest} ({curr}){ret_str}\n"
                    f"Saved as {trip_id}.{trip_str} I'll send a confirmation when it kicks in."
                )
            else:
                # Departure already passed or no dep time — activate now
                overseas_state["active"] = True
                overseas_state["destination"] = dest
                overseas_state["currency"] = curr
                overseas_state["dep_flight"] = pf.get("flight_number", "")
                overseas_state["dep_time"] = pf.get("dep_time", "")
                overseas_state["dep_terminal"] = pf.get("dep_terminal", "")
                overseas_state["dep_gate"] = pf.get("dep_gate", "")
                overseas_state["arr_terminal"] = pf.get("arr_terminal", "")
                overseas_state["arr_gate"] = pf.get("arr_gate", "")
                overseas_state["trip_start"] = date.today().strftime("%d/%m/%Y")
                trip_str = f" Trip {trip_id} saved." if trip_id else ""
                reply = (
                    f"Overseas mode on ✈️\n"
                    f"Destination: {dest}\nCurrency: {curr}\n"
                    f"I'll log expenses in {curr} with SGD equivalent.{trip_str}"
                )
        elif text.strip().upper() == "N":
            overseas_state.pop("_pending_flight", None)
            reply = "Got it — what's your departure date/time, destination, and when are you back in SG?"
        else:
            # Anything else = manual destination override
            overseas_state.pop("_pending_flight", None)
            reply = handle_overseas_request(text)
        if reply:
            try:
                await update.message.reply_text(reply, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(reply)
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
    elif lower.startswith("delete ") and not lower.startswith("delete event") and not lower.startswith("delete expense") and not lower.startswith("delete bill") and not lower.startswith("delete restaurant") and not lower.startswith("delete portfolio") and not lower.startswith("delete last"):
        name = text[7:].strip()
        _, record = find_row(name)
        if not record:
            reply = f"No contact found for '{name}'"
        else:
            confirm_name = record.get("Name", name)
            confirm_sessions[user_id] = {"action": "delete_contact", "args": [name], "target": f"Delete contact {confirm_name}?"}
            reply = f"Delete contact *{confirm_name}*? (yes / no)"
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
                f"1. Alias\n2. Birthday\n3. Relationship\n4. Context\n5. Notes\n"
                f"6. Follow up date\n7. Follow up notes\n8. Email\n9. Address\n\n"
                f"Type the field name or *cancel* to exit."
            )
    elif lower == "cancel":
        if user_id in edit_sessions:
            del edit_sessions[user_id]
        if user_id in excel_import_sessions:
            del excel_import_sessions[user_id]
        if user_id in confirm_sessions:
            del confirm_sessions[user_id]
        if user_id in delete_sessions:
            del delete_sessions[user_id]
        if user_id in portfolio_delete_sessions:
            del portfolio_delete_sessions[user_id]
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

    # Referral queries
    elif lower in ["referrals", "all referrals", "show referrals"]:
        reply = get_all_referrals()
    elif lower in ["top referrers", "best referrers", "who refers the most"]:
        reply = get_top_referrers()
    elif lower.startswith("referrals from ") or lower.startswith("who did "):
        if lower.startswith("referrals from "):
            name = text[15:].strip()
        else:
            # "who did James refer"
            m = re.search(r"who did (.+?) refer", lower)
            name = m.group(1).strip().title() if m else text[8:].strip()
        reply = get_referrals_by(name)

    # Import
    elif "import" in lower and ("excel" in lower or "contacts" in lower or "spreadsheet" in lower):
        excel_import_sessions[user_id] = {"step": "awaiting_columns", "column_order": []}
        reply = "Sure! Tell me the column order in your Excel first — e.g. 'Name, Email, Date of Birth, Alias'"

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

    # Expense Commands
    elif any(lower == p or lower.startswith(p) for p in [
        "what expense categories", "list my categories", "what categories",
        "show categories", "what are my expense categories", "list categories",
        "my expense categories", "expense categories"
    ]):
        reply = get_expense_categories()
    elif lower in ["expense report", "monthly report", "spending report", "expenses"]:
        reply = get_expense_report()
    elif lower in ["delete last expense", "remove last expense"]:
        reply = delete_last_expense()
    elif lower in ["delete expense", "remove expense", "undo expense", "undo last expense"]:
        # Show last 5 for selection
        recent = get_recent_expenses(5)
        if not recent:
            reply = "No expenses logged yet."
        else:
            delete_sessions[user_id] = {"step": "pick", "expenses": recent}
            reply = format_delete_list(recent)
    elif lower.startswith("delete expense ") or lower.startswith("remove expense "):
        # Direct search — "delete expense Tada"
        query = re.sub(r"^(delete|remove) expense\s+", "", lower).strip()
        results = search_expenses_by_merchant(query)
        if not results:
            reply = f"No expenses found matching '{query}'."
        elif len(results) == 1:
            sheet_row, _ = results[0]
            reply = delete_expense_by_row(sheet_row)
        else:
            delete_sessions[user_id] = {"step": "pick", "expenses": results}
            reply = format_delete_list(results)
    elif lower in ["last expense", "show last expense", "what did i log"]:
        reply = show_last_expense()
    elif lower in ["trip summary", "trip spend", "how much have i spent", "trip expenses"]:
        reply = get_trip_summary()
    elif any(lower == p or lower.startswith(p) for p in [
        "my itinerary", "show my itinerary", "what's my itinerary", "trip itinerary",
        "what's my flight home", "my flight home", "return flight",
        "when do i land", "when do i arrive", "what terminal", "what gate",
        "show past trips", "my past trips", "past trips",
        "when do i fly next", "next flight"
    ]):
        reply = get_trip_queries(lower)
    elif lower.startswith("how much did i spend in "):
        dest_q = text[24:].strip()
        try:
            trip_id, _ = get_active_trip()
            sheet = expenses_sheet()
            records = sheet.get_all_records()
            trip_records = [r for r in records if r.get("Trip ID") == trip_id] if trip_id else []
            total = sum(float(r.get("SGD Amount") or r.get("Amount") or 0) for r in trip_records)
            reply = f"Spent ${total:.2f} SGD in {dest_q} (Trip {trip_id})." if trip_id else f"No active trip to query."
        except Exception as e:
            reply = f"Error fetching trip spend: {e}"
    elif lower.startswith("edit last expense ") or lower.startswith("edit expense "):
        # "edit last expense category to FnB" or "edit expense card to Citi"
        m = re.search(r"edit (?:last )?expense (\w+) to (.+)", lower)
        if m:
            reply = edit_last_expense(m.group(1), m.group(2).strip())
        else:
            reply = show_last_expense()
    elif lower.startswith("rename category "):
        m = re.search(r"rename category (.+?) to (.+)", lower)
        if m:
            old_cat = m.group(1).strip()
            new_cat = m.group(2).strip()
            confirm_sessions[user_id] = {
                "action": "rename_category",
                "args": [old_cat, new_cat],
                "target": f"Rename {old_cat} to {new_cat}?"
            }
            reply = f"Rename category *{old_cat}* to *{new_cat}*? This will update all expense rows and Merchant Map. (yes / no)"
        else:
            reply = "Try: 'rename category FnB to Food'"
    elif lower.startswith("now in ") or lower.startswith("switched to ") or lower.startswith("arrived in "):
        # Mid-trip currency switch — "now in Korea", "arrived in Seoul"
        if overseas_state.get("active"):
            dest_text = re.sub(r"^(now in|switched to|arrived in)\s+", "", lower).strip().title()
            dest_info = get_dest_info_from_iata("", dest_text)
            new_curr = dest_info.get("currency", "")
            new_dest = dest_info.get("destination", dest_text)
            if new_curr and new_curr != "SGD":
                overseas_state["currency"] = new_curr
                overseas_state["destination"] = new_dest
                if new_curr not in overseas_state["currencies"]:
                    overseas_state["currencies"].append(new_curr)
                if new_dest not in overseas_state["trip_destinations"]:
                    overseas_state["trip_destinations"].append(new_dest)
                # Pre-cache the new currency rate
                get_fx_rate(new_curr)
                reply = f"Switched to {new_dest} — expenses will now be logged in {new_curr}."
            else:
                reply = handle_overseas_request(text)
        else:
            reply = handle_overseas_request(text)
    elif is_log_prefix_input(text):
        # "log [merchant] [amount]" — always expense flow, strip the "log " prefix
        log_text = text[4:].strip()
        reply, needs_session, session_data = handle_expense_text(log_text, user_id)
        if needs_session and session_data:
            expense_sessions[user_id] = session_data
    elif is_overseas_mode_request(text):
        if not extract_flight_number(text) and user_id in conversation_histories:
            history_text = " ".join(
                m["content"] for m in conversation_histories[user_id][-10:]
                if m["role"] == "user"
            )
            found_flights = extract_all_flight_numbers(history_text)
            if found_flights:
                reply = handle_overseas_request(" ".join(found_flights) + " " + text)
            else:
                reply = handle_overseas_request(text)
        else:
            reply = handle_overseas_request(text)
    elif is_expense_input(text):
        reply, needs_session, session_data = handle_expense_text(text, user_id)
        if needs_session and session_data:
            expense_sessions[user_id] = session_data

    # Bill Commands
    elif lower in ["bills", "my bills", "list bills"]:
        reply = list_bills()
    elif lower.startswith("delete bill "):
        bill_name = text[12:].strip()
        confirm_sessions[user_id] = {"action": "delete_bill", "args": [bill_name], "target": f"Delete bill {bill_name}?"}
        reply = f"Delete bill *{bill_name}*? (yes / no)"
    elif is_bill_request(text):
        reply = handle_new_bill(text)

    # Restaurant Commands
    elif lower.startswith("delete restaurant ") or lower.startswith("remove restaurant "):
        rest_name = text.split(" ", 2)[2].strip()
        confirm_sessions[user_id] = {"action": "delete_restaurant", "args": [rest_name], "target": f"Delete restaurant {rest_name}?"}
        reply = f"Delete *{rest_name}* from your restaurant list? (yes / no)"
    elif is_restaurant_search(text):
        reply = handle_search_restaurants(text)
    elif is_restaurant_save(text):
        reply = handle_save_restaurant(text)
    elif lower.startswith("search restaurants "):
        reply = search_restaurants(text[19:].strip())

    # Stock Commands
    elif lower in ["portfolio", "my portfolio", "holdings", "portfolio performance"]:
        reply = get_portfolio_performance()
    elif lower in ["delete portfolio entry", "remove portfolio entry", "delete portfolio"]:
        entries = get_recent_portfolio_entries(5)
        if not entries:
            reply = "No portfolio entries found."
        else:
            portfolio_delete_sessions[user_id] = {"step": "pick", "entries": entries}
            reply = format_portfolio_delete_list(entries)
    elif re.match(r"delete portfolio (.+)", lower) or re.match(r"remove portfolio (.+)", lower):
        m = re.match(r"(?:delete|remove) portfolio (.+)", lower)
        ticker_q = m.group(1).strip().upper()
        entries = search_portfolio_by_ticker(ticker_q)
        if not entries:
            reply = f"No portfolio entries found for {ticker_q}."
        elif len(entries) == 1:
            sheet_row, r = entries[0]
            desc = f"{r.get('Stock')} — {r.get('Quantity')} shares @ ${r.get('Buy Price')} on {r.get('Buy Date')}"
            portfolio_delete_sessions[user_id] = {"step": "confirm", "pending": (sheet_row, desc)}
            reply = f"Delete: {desc}? (yes / no)"
        else:
            portfolio_delete_sessions[user_id] = {"step": "pick", "entries": entries}
            reply = format_portfolio_delete_list(entries)
    elif is_market_summary_request(text)[0]:
        _, target_mkt = is_market_summary_request(text)
        reply = get_market_summary_now(target_market=target_mkt)
    elif is_stock_request(text):
        result = handle_stock_request(text)
        if result:
            reply = result

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
        event_name = text[13:].strip()
        confirm_sessions[user_id] = {"action": "delete_event", "args": [event_name], "target": f"Delete event {event_name}?"}
        reply = f"Delete event *{event_name}*? (yes / no)"

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
            "save, find, note, followup, update, edit, delete, search, list, stats, followups, overdue, birthdays, soon, lastcontact\n"
            "referrals, all referrals, top referrers, referrals from [name]\n"
            "import excel — import contacts from a spreadsheet\n\n"
            "*Calendar:*\n"
            "Just tell me naturally — 'schedule dinner tomorrow 7pm' or 'add event'\n"
            "events today / events week / delete event\n\n"
            "*To-Do:*\n"
            "todo, done, todos\n\n"
            "*Other:*\n"
            "em status — check Em's health\n\n"
            "Or just chat — I'll figure it out 👍"
        )

    # Claude Chat fallback — with natural language CRM detection
    else:
        # Check for natural language CRM updates first
        crm_action = detect_crm_natural_update(text)
        if crm_action:
            action, name, field_or_referred, value = crm_action
            if action == "referral":
                reply = set_referral(name, field_or_referred)
            elif action == "update":
                reply = update_contact_field_natural(name, field_or_referred, value)
            elif action == "show_private":
                reply = find_contact(name, show_private=True)
        elif is_reschedule_request(text) and user_id in last_fired_reminder:
            reply = handle_reschedule(text, user_id)
        elif is_reminder_request(text):
            reply = handle_new_reminder(text)
        elif is_expense_input(text):
            reply, needs_session, session_data = handle_expense_text(text, user_id)
            if needs_session and session_data:
                expense_sessions[user_id] = session_data
        elif is_bare_merchant_input(text):
            reply, needs_session, session_data = handle_expense_text(text, user_id)
            if needs_session and session_data:
                expense_sessions[user_id] = session_data
        elif is_overseas_mode_request(text):
            reply = handle_overseas_request(text)
        elif is_restaurant_save(text):
            reply = handle_save_restaurant(text)
        elif is_restaurant_search(text):
            reply = handle_search_restaurants(text)
        elif is_bill_request(text):
            reply = handle_new_bill(text)
        elif is_market_summary_request(text)[0]:
            _, target_mkt = is_market_summary_request(text)
            reply = get_market_summary_now(target_market=target_mkt)
        elif is_stock_request(text):
            reply = handle_stock_request(text)
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

    if not reply:
        try:
            fallback_response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                system=(
                    "You are Em, a personal assistant bot built in Python using python-telegram-bot and the Anthropic API. "
                    "The user just sent a message that no handler in the bot code recognised — it fell through everything. "
                    "Acknowledge you couldn't handle it in one short sentence (casual, no corporate speak). "
                    "Then suggest one concrete thing the developer could add to the bot to support this — "
                    "be specific about what kind of handler, function, or API would help. "
                    "Keep it to 2-3 sentences total. No bullet points."
                ),
                messages=[{"role": "user", "content": text}]
            )
            reply = fallback_response.content[0].text
        except Exception as e:
            print(f"Fallback Claude call failed: {e}")
            reply = "Not sure how to handle that one — might be worth adding a handler for it in the bot code."

    if reply:
        try:
            await update.message.reply_text(reply, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(reply)

# --- Main ---
async def post_init(app):
    global _scheduler, _app_ref
    # Run infrastructure setup
    run_infrastructure_setup()

    timezone = pytz.timezone("Asia/Kuala_Lumpur")
    scheduler = AsyncIOScheduler(timezone=timezone)
    _scheduler = scheduler
    _app_ref = app

    # Follow-up reminders at 9am
    scheduler.add_job(send_followup_reminders, "cron", hour=9, minute=0, args=[app])

    # Birthday greetings at 12pm
    scheduler.add_job(send_birthday_reminders, "cron", hour=12, minute=0, args=[app])

    # Birthday 2pm follow-up
    scheduler.add_job(send_birthday_followups, "cron", hour=14, minute=0, args=[app])

    # Custom reminders — check every minute
    scheduler.add_job(check_and_fire_reminders, "interval", minutes=1, args=[app])

    # Bill reminders — daily at 9am
    scheduler.add_job(send_bill_reminders, "cron", hour=9, minute=0, args=[app])

    # Price alerts — check every 15 minutes
    scheduler.add_job(check_price_alerts, "interval", minutes=15, args=[app])

    # Weekly market summary — Monday 8am
    scheduler.add_job(send_weekly_market_summary, "cron", day_of_week="mon", hour=8, minute=0, args=[app])

    # FX rate refresh — 8am and 8pm daily
    scheduler.add_job(refresh_fx_rates, "cron", hour=8, minute=0)
    scheduler.add_job(refresh_fx_rates, "cron", hour=20, minute=0)

    scheduler.start()
    print("✅ Scheduler started — follow-ups + bills at 9am, birthdays at 12pm + 2pm, reminders every minute, market Monday 8am")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    print("Em is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
