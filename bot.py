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
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "281095850"))
RAILWAY_DEPLOYMENT_ID = os.getenv("RAILWAY_DEPLOYMENT_ID", "")

# Anthropic API health tracking
_anthropic_failure_count = 0
_anthropic_down_notified = False
ANTHROPIC_FAILURE_THRESHOLD = 3

# iCloud health tracking
_icloud_down = False
_icloud_last_notified = None

# Manual FX rate cache: { currency: { "rate": float, "date": str, "sgd_per_unit": bool } }
manual_fx_rates = {}

# Session timestamps for timeout: { user_id: datetime }
session_timestamps = {}

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
                     "Card", "Receipt Link", "Reconciled", "Notes"],
        "Bills": ["Name", "Bank", "Due Date", "Estimated Amount", "Notes"],
        "Merchant Map": ["Merchant", "Category", "Card"],
        "Restaurants": ["Name", "Location", "Country", "Tags", "Notes"],
        "Portfolio": ["Stock", "Quantity", "Buy Price", "Buy Date", "Notes"],
        "Trips": ["Trip ID", "Destination", "Currency", "Dep Flight", "Dep Time", "Return Flight", "Return Time", "Status", "Started", "Ended"],
        "Reminders": ["ID", "Message", "Scheduled Time", "Recurrence", "Status", "Attempts", "Contact"],
        "Settings": ["Key", "Value"]
    }

    existing_now = [ws.title for ws in spreadsheet.worksheets()]
    for tab_name, headers in required_tabs.items():
        if tab_name not in existing_now:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=500, cols=len(headers))
            ws.append_row(headers)
            print(f"Created tab: {tab_name}")

    # Cards sheet — always overwrite with new schema and initial cards
    try:
        existing_tabs = [ws.title for ws in spreadsheet.worksheets()]
        if "Cards" not in existing_tabs:
            cards_ws = spreadsheet.add_worksheet(title="Cards", rows=100, cols=4)
        else:
            cards_ws = spreadsheet.worksheet("Cards")
        cards_ws.clear()
        cards_ws.append_row(CARDS_SCHEMA)
        for card_row in INITIAL_CARDS:
            cards_ws.append_row(card_row)
        print("✅ Cards sheet initialised with new schema")
    except Exception as e:
        print(f"Cards sheet setup error: {e}")

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

RECEIPTS_FOLDER_ID = os.getenv("RECEIPTS_FOLDER_ID", "14pG1lNPANRwehiW_xSFjoHzt-AUk5-Xb")

def setup_drive():
    """Create Em's Drive folder structure."""
    em_id = get_or_create_drive_folder("Em")
    meeting_notes_id = get_or_create_drive_folder("Meeting Notes", em_id)
    backups_id = get_or_create_drive_folder("Backups", em_id)
    settings_id = get_or_create_drive_folder("Settings", em_id)
    print("✅ Drive folders setup complete")
    return {
        "em": em_id,
        "receipts": RECEIPTS_FOLDER_ID,  # shared folder — no service account quota issues
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

    health = {
        "Sheets": "✅ Connected",
        "Drive": "✅ Connected",
        "iCloud": "✅ Connected",
        "Scheduler": "✅ Running",
        "Profile": "✅ Loaded",
    }

    # Sheets
    try:
        setup_sheets()
    except Exception as e:
        health["Sheets"] = f"❌ Failed — {str(e)[:40]}"
        print(f"setup_sheets error: {e}")

    # Drive
    try:
        DRIVE_FOLDERS = setup_drive()
    except Exception as e:
        health["Drive"] = "❌ Failed — receipt uploads won't work"
        DRIVE_FOLDERS = {}
        print(f"setup_drive error: {e}")

    # Profile — attempt with retry
    try:
        setup_em_profile()
        if not em_profile:
            raise Exception("Profile empty after load")
    except Exception:
        import time as _time
        _time.sleep(5)
        try:
            setup_em_profile()
            if not em_profile:
                raise Exception("Profile empty after retry")
        except Exception as e2:
            health["Profile"] = "❌ Failed — running on defaults"
            print(f"em_profile load failed after retry: {e2}")

    # iCloud — tested lazily on first use, mark unknown here
    health["iCloud"] = "⚠️ Not tested yet"

    print("✅ Infrastructure setup complete")
    return health

async def send_startup_message(app, health):
    """Send startup health check if this is a new Railway deployment."""
    try:
        settings_ws = spreadsheet.worksheet("Settings")
        records = settings_ws.get_all_records()
        last_deploy_id = ""
        last_deploy_row = None
        for i, r in enumerate(records, start=2):
            if r.get("Key") == "last_deployment_id":
                last_deploy_id = r.get("Value", "")
                last_deploy_row = i
                break

        # Only send if deployment ID changed
        if RAILWAY_DEPLOYMENT_ID and RAILWAY_DEPLOYMENT_ID == last_deploy_id:
            return  # Same deployment restart — silent

        # New deploy — update stored ID
        if RAILWAY_DEPLOYMENT_ID:
            if last_deploy_row:
                settings_ws.update_cell(last_deploy_row, 2, RAILWAY_DEPLOYMENT_ID)
            else:
                settings_ws.append_row(["last_deployment_id", RAILWAY_DEPLOYMENT_ID])

        # Build status message — match em status format exactly
        issues = []
        for k, v in health.items():
            if k == "iCloud":
                continue  # iCloud tested lazily, skip on startup
            if "❌" in v:
                issues.append(f"• {k}: {v.replace('❌ ', '')}")
            elif "⚠️" in v:
                issues.append(f"• {k}: {v.replace('⚠️ ', '')}")

        if "❌" in health.get("Profile", ""):
            issues.append("• Reply 'reload profile' to retry loading preferences.")

        if not issues:
            msg = "✅ Systems all green"
        else:
            msg = "⚠️ Issues detected on startup:\n\n" + "\n".join(issues)

        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)

    except Exception as e:
        print(f"send_startup_message error: {e}")

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

def track_anthropic_call(success):
    """Track Anthropic API call success/failure for downtime detection."""
    global _anthropic_failure_count, _anthropic_down_notified
    if success:
        if _anthropic_down_notified:
            _anthropic_down_notified = False
            # Recovery will be notified by the next scheduled check
        _anthropic_failure_count = 0
    else:
        _anthropic_failure_count += 1

async def notify_anthropic_down(app):
    """Send Anthropic API down notification if threshold reached."""
    global _anthropic_down_notified
    if _anthropic_failure_count >= ANTHROPIC_FAILURE_THRESHOLD and not _anthropic_down_notified:
        _anthropic_down_notified = True
        try:
            await app.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=(
                    "⚠️ Anthropic API appears to be down — some features unavailable:\n"
                    "• Receipt vision parsing\n"
                    "• Expense text parsing\n"
                    "• Birthday greetings\n"
                    "• Reminder parsing\n"
                    "• Market narrative\n\n"
                    "I'll notify you when it recovers."
                )
            )
        except Exception as e:
            print(f"notify_anthropic_down error: {e}")

def sheets_call_with_retry(fn, *args, max_retries=2, **kwargs):
    """Wrap a Google Sheets API call with quota retry logic."""
    import time as _time
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if "quota" in err_str or "rate" in err_str or "429" in err_str:
                print(f"Sheets quota hit — retrying in 60s (attempt {attempt+1})")
                _time.sleep(60)
            else:
                raise
    raise Exception("Google Sheets rate limit — retrying automatically in 60s.")


def get_calendar(name=None):
    global _icloud_down
    import time as _time

    def _attempt():
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

    # Attempt 1
    try:
        result = _attempt()
        _icloud_down = False
        return result
    except Exception:
        pass

    # Retry after 3 seconds
    _time.sleep(3)
    try:
        result = _attempt()
        _icloud_down = False
        return result
    except Exception as e:
        _icloud_down = True
        print(f"iCloud Calendar unavailable after retry: {e}")
        return None

async def check_icloud_daily(app):
    """Daily check — notify if iCloud Calendar still down."""
    global _icloud_down, _icloud_last_notified
    today = date.today()
    if _icloud_down:
        if _icloud_last_notified != today:
            _icloud_last_notified = today
            await app.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text="⚠️ iCloud Calendar still unavailable — calendar features won't work.\nCheck your credentials in Railway."
            )
    else:
        # Test connection
        cal = get_calendar()
        if cal is None and _icloud_last_notified != today:
            _icloud_last_notified = today
            await app.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text="⚠️ iCloud Calendar unavailable — calendar features won't work.\nCheck your credentials in Railway."
            )



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
    """Search Name first, then Alias, then first-name match.
    Returns (row_num, record) for single match, or ('disambig', message) for multiple."""
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
        return "disambig", disambiguate_contacts(matches)

    # 4. Substring match on Alias
    alias_matches = []
    for i, r in enumerate(records):
        if name_lower in r.get("Alias", "").lower():
            alias_matches.append((i + 2, r))
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        return "disambig", disambiguate_contacts(alias_matches)

    # 5. First-name match across Name and Alias
    first_matches = []
    for i, r in enumerate(records):
        full_name = r.get("Name", "")
        alias = r.get("Alias", "")
        first_name = full_name.split()[0].lower() if full_name else ""
        alias_first = alias.split()[0].lower() if alias else ""
        if name_lower == first_name or name_lower == alias_first:
            first_matches.append((i + 2, r))
    if len(first_matches) == 1:
        return first_matches[0]
    if len(first_matches) > 1:
        return "disambig", disambiguate_contacts(first_matches)

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
# Pending duplicate contact saves: { user_id: { "data": str, "existing_name": str } }
pending_contact_saves = {}
pending_restaurant_saves = {}  # { user_id: { "name": str, "location": str, ... } }

def save_contact(data, force_new=False):
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

        # Duplicate check unless force_new
        if not force_new:
            existing_row, existing_record = find_row(name)
            if existing_record and existing_row != "disambig":
                return f"_DUPLICATE_:{name}"  # Signal to caller to handle

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
        matches = [
            (i + 2, r) for i, r in enumerate(records)
            if task.lower() in r.get("Task", "").lower() and r.get("Status") == "Pending"
        ]
        if not matches:
            return f"❌ No pending task found matching '{task}'"
        if len(matches) > 1:
            lines = [f"Found {len(matches)} matching tasks — which one?"]
            for i, (_, r) in enumerate(matches, 1):
                lines.append(f"{i}. {r.get('Task')}")
            lines.append("\nReply with the number.")
            return "_DISAMBIG_TODO_COMPLETE_:" + "|".join(r.get("Task", "") for _, r in matches)
        row_idx, r = matches[0]
        sheet.update_cell(row_idx, 2, "Done")
        return f"✅ Marked as done: _{r.get('Task')}_"
    except Exception as e:
        return f"❌ Error completing task: {str(e)}"

def delete_todo(task):
    """Delete a todo by task name, with disambiguation if multiple match."""
    try:
        sheet = todo_sheet()
        records = sheet.get_all_records()
        matches = [
            (i + 2, r) for i, r in enumerate(records)
            if task.lower() in r.get("Task", "").lower()
        ]
        if not matches:
            return f"❌ No task found matching '{task}'"
        if len(matches) > 1:
            return "_DISAMBIG_TODO_DELETE_:" + "|".join(r.get("Task", "") for _, r in matches)
        row_idx, r = matches[0]
        sheet.delete_rows(row_idx)
        return f"Deleted — {r.get('Task')} ✅"
    except Exception as e:
        return f"❌ Error deleting task: {str(e)}"



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
        return "⚠️ Couldn't parse that — what are the event details?\n(e.g. Team Offsite, 30 Apr 2026, 2pm)"
    except Exception as e:
        if "iCloud" in str(e) or "caldav" in str(e).lower() or "calendar" in str(e).lower():
            return "⚠️ Couldn't connect to iCloud Calendar — check credentials in Railway."
        return "⚠️ Couldn't parse that — what are the event details?\n(e.g. Team Offsite, 30 Apr 2026, 2pm)"

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
DATE_FORMATS = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"]

def parse_date_flexible(date_str):
    """Try multiple date formats. Returns date object or None."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None

async def send_followup_reminders(app):
    try:
        sheet = crm_sheet()
        records = sheet.get_all_records()
        today = date.today()
        for i, r in enumerate(records, start=2):
            fu_date_str = r.get("Follow Up Date", "")
            if not fu_date_str:
                continue
            fu_date = parse_date_flexible(fu_date_str)
            if fu_date is None:
                # Unrecognised format — ask for correct date
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=(
                        f"⚠️ Couldn't read the follow-up date for *{r.get('Name', '?')}*.\n"
                        f"What's the correct date? (e.g. 25 Apr 2026)\n"
                        f"Reply: followup date {r.get('Name', '')} [date]"
                    ),
                    parse_mode="Markdown"
                )
                continue
            if fu_date == today:
                message = (
                    f"🔔 *Follow up reminder!*\n\n"
                    f"👤 *{r.get('Name')}*\n"
                    f"📝 {r.get('Follow Up Notes') or 'No notes'}\n\n"
                    f"Don't forget to reach out today!"
                )
                await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=message, parse_mode="Markdown")
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
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": greeting_prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"generate_birthday_greeting Claude error for {name}: {e}")
        return f"Happy birthday {name}! Hope you have a wonderful day ahead."

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
                    f"{greeting}\n\n"
                    f"Reply 'sent' when you've wished them, or 'skip' to dismiss."
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
        if record and row_num != "disambig":
            sheet = crm_sheet()
            sheet.update_cell(row_num, col, date.today().strftime("%d/%m/%Y"))
    except Exception as e:
        print(f"Error marking birthday greeted for {name}: {e}")

def check_birthday_acknowledgement(text):
    """
    Check if user explicitly said 'sent' or 'skip' for a pending birthday.
    Must be called with the user's message text.
    Returns (True, reply_message) if handled, (False, None) otherwise.
    """
    global birthday_pending
    if not birthday_pending:
        return False, None

    lower = text.strip().lower()
    if lower not in ["sent", "done", "skip", "skipped", "sent it", "sent!"]:
        return False, None

    acknowledged = []
    for name, data in list(birthday_pending.items()):
        if not data.get("greeted"):
            if lower in ["skip", "skipped"]:
                data["greeted"] = True
                acknowledged.append(f"Skipped {name}")
            else:
                data["greeted"] = True
                mark_birthday_greeted(name)
                acknowledged.append(f"Marked {name} as greeted ✅")

    if acknowledged:
        return True, "\n".join(acknowledged)
    return False, None





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

def process_meeting_notes(event_name, notes_list):
    """Send all buffered notes to Claude and get a structured recap back as JSON.
    Falls back to saving raw notes if Claude call fails."""
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
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw), False  # (recap, is_raw)
    except Exception as e:
        print(f"process_meeting_notes Claude error: {e}")
        # Return raw notes as fallback
        return {
            "event_name": event_name or "Untitled",
            "topic": "Raw notes — processing failed",
            "summary": combined,
            "action_items": [],
            "contacts_mentioned": [],
            "foreign_phrases": {}
        }, True  # (recap, is_raw)

def save_meeting_recap(recap):
    """Save the confirmed recap to the Meeting Notes sheet. Returns (success, error_msg)."""
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
        return True, None
    except Exception as e:
        print(f"Error saving meeting recap: {e}")
        return False, str(e)

def search_meeting_notes(query):
    """Search meeting notes by keyword or date range."""
    try:
        sheet = spreadsheet.worksheet("Meeting Notes")
        try:
            records = sheet.get_all_records()
        except Exception as e:
            return f"⚠️ Couldn't search meeting notes right now — try again in a moment."

        if not records:
            return "No meeting notes saved yet."

        query_lower = query.lower().strip()
        results = []
        for r in records:
            searchable = " ".join(str(v).lower() for v in r.values())
            if query_lower in searchable:
                results.append(r)

        if not results:
            return f"No meeting notes found matching '{query}'."

        lines = [f"Found {len(results)} recap(s):\n"]
        for r in results:
            lines.append(f"📋 {r.get('Event Name', '')} — {r.get('Date', '')}")
            lines.append(f"   {r.get('Topic', '')}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return "⚠️ Couldn't search meeting notes right now — try again in a moment."

async def handle_meeting_session(user_id, text, update):
    """Handle messages when user is in an active meeting recap session."""
    session = meeting_sessions[user_id]
    step = session.get("step")

    # Confirming step — waiting for Y or E
    if step == "confirming":
        if text.strip().upper() == "Y":
            saved, err = save_meeting_recap(session["pending_recap"])
            contacts = session["pending_recap"].get("contacts_mentioned", [])
            for name in contacts:
                row_num, record = find_row(name)
                if record and row_num != "disambig":
                    pass  # silently tagged via presence in recap
            del meeting_sessions[user_id]
            session_timestamps.pop(user_id, None)
            if saved:
                await update.message.reply_text("Saved! 📋")
            else:
                await update.message.reply_text(
                    f"⚠️ Couldn't save recap to sheet — keeping it in memory.\n"
                    f"Reply 'retry save' to try again or 'send recap' to have me send it as a message."
                )
                # Keep session alive with saved recap for retry
                meeting_sessions[user_id] = {**session, "step": "save_failed"}
                touch_session(user_id)

        elif text.strip().upper() == "E":
            session["step"] = "collecting"
            session["notes"] = []
            await update.message.reply_text(
                "No worries, let's redo it. Send your notes again and say done when you're finished."
            )
        else:
            await update.message.reply_text("Reply Y to save or E to start over.")
        return

    if step == "save_failed":
        lower_t = text.strip().lower()
        if lower_t == "retry save":
            saved, err = save_meeting_recap(session["pending_recap"])
            if saved:
                del meeting_sessions[user_id]
                session_timestamps.pop(user_id, None)
                await update.message.reply_text("Saved! 📋")
            else:
                await update.message.reply_text("Still couldn't save — try again in a moment.")
        elif lower_t == "send recap":
            recap = session["pending_recap"]
            msg = format_recap_confirmation(recap).replace("\nReply Y to save or E to edit.", "")
            del meeting_sessions[user_id]
            session_timestamps.pop(user_id, None)
            await update.message.reply_text(msg)
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
            recap, is_raw = process_meeting_notes(session.get("event_name", ""), session["notes"])
            session["pending_recap"] = recap
            session["step"] = "confirming"
            touch_session(user_id)
            if is_raw:
                await update.message.reply_text(
                    "⚠️ Took too long to process — saved your raw notes.\n"
                    "Reply 'retry recap' to process them when ready, or Y to save as-is."
                )
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
    """Use Claude to parse a natural language reminder request. Falls back to manual input prompt."""
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
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"parse_reminder_request error: {e}")
        return None  # Caller handles None by prompting manual input

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
    """Cancel reminders matching a keyword. Returns list of cancelled, or '_DISAMBIG_:' prefix if multiple."""
    sheet = reminders_sheet()
    records = sheet.get_all_records()
    matches = [
        (i + 2, r) for i, r in enumerate(records)
        if keyword.lower() in r.get("Message", "").lower() and r.get("Status") == "pending"
    ]
    if not matches:
        return []
    if len(matches) > 1:
        # Return signal for disambiguation
        return ["_DISAMBIG_:" + "|".join(f"{row}:{r.get('Message','')} — {r.get('Scheduled Time','')}"
                                          for row, r in matches)]
    row_idx, r = matches[0]
    sheet.update_cell(row_idx, 5, "cancelled")
    return [r.get("Message", "")]

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
    """Check sheet every minute and fire due reminders. Only reads pending rows."""
    try:
        sheet = reminders_sheet()
        records = sheet.get_all_records()
        now = datetime.now(TIMEZONE).replace(second=0, microsecond=0)
        pending_records = [(i, r) for i, r in enumerate(records) if r.get("Status") == "pending"]

        for i, r in pending_records:
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
        if parsed is None:
            return "⚠️ Couldn't parse that — what are the reminder details?\n(e.g. Call John, 30 Apr 2026, 9am)"

        message = parsed.get("message", text)
        scheduled_time = parsed.get("scheduled_time", "")
        recurrence = parsed.get("recurrence", "once")
        contact = parsed.get("contact", "")

        if not scheduled_time:
            return "Couldn't figure out when to remind you. Try: 'remind me to call James tomorrow at 3pm'."

        # Validate contact against CRM
        if contact:
            row, record = find_row(contact)
            if not record or row == "disambig":
                contact = ""

        add_reminder(message, scheduled_time, recurrence, contact)

        try:
            dt = datetime.strptime(scheduled_time, "%Y-%m-%d %H:%M")
            time_str = dt.strftime("%d %b %Y at %I:%M %p")
        except Exception as e:
            print(f"handle_new_reminder: time format error: {e}")
            time_str = scheduled_time

        rec_str = f" ({recurrence})" if recurrence != "once" else ""
        return f"Done, I'll remind you to {message} on {time_str}{rec_str}."

    except Exception as e:
        return f"⚠️ Couldn't parse that — what are the reminder details?\n(e.g. Call John, 30 Apr 2026, 9am)"

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
EXPENSE_CARDS = ["Citi", "Maybank", "Amex", "UOB"]  # fallback only — live list read from Cards sheet

# Cards sheet new schema: Card Name | Last 4 | Default Category | Notes
CARDS_SCHEMA = ["Card Name", "Last 4", "Default Category", "Notes"]
INITIAL_CARDS = [
    ["Maybank", "4002", "FnB", ""],
    ["Citi", "1176", "General", ""],
    ["UOB", "5372", "", ""],
    ["Amex", "1008", "", ""],
]

def cards_sheet():
    return spreadsheet.worksheet("Cards")

def get_cards_live():
    """Read card list live from Cards sheet. Returns list of dicts."""
    try:
        ws = cards_sheet()
        records = ws.get_all_records()
        return records
    except Exception as e:
        print(f"get_cards_live error: {e}")
        return []

def get_card_names_live():
    """Return list of card names from Cards sheet."""
    cards = get_cards_live()
    names = [c.get("Card Name", "") for c in cards if c.get("Card Name")]
    return names if names else EXPENSE_CARDS

def get_card_default_for_category(category):
    """Return default card name for a given category from Cards sheet."""
    try:
        cards = get_cards_live()
        for c in cards:
            default_cat = c.get("Default Category", "").strip().lower()
            if default_cat and default_cat == category.strip().lower():
                return c.get("Card Name", "")
        # Fallback defaults
        cat_lower = category.strip().lower()
        if cat_lower in ["fnb", "food", "dining", "f&b"]:
            return "Maybank"
        if cat_lower in ["grab", "transport"]:
            return "Amex"
        return "Citi"
    except Exception as e:
        print(f"get_card_default_for_category error: {e}")
        return "Citi"

def get_card_by_last4(last4):
    """Match a card by last 4 digits. Returns card name or None."""
    try:
        cards = get_cards_live()
        for c in cards:
            if str(c.get("Last 4", "")).strip() == str(last4).strip():
                return c.get("Card Name", "")
    except Exception as e:
        print(f"get_card_by_last4 error: {e}")
    return None

def set_card_default_category(card_name, category):
    """Update default category for a card in Cards sheet."""
    try:
        ws = cards_sheet()
        records = ws.get_all_records()
        for i, r in enumerate(records, start=2):
            if r.get("Card Name", "").lower() == card_name.lower():
                # Default Category is col 3
                ws.update_cell(i, 3, category)
                return f"Updated — {card_name} will now default to {category} ✅"
        return f"Card '{card_name}' not found. Your cards: {', '.join(get_card_names_live())}"
    except Exception as e:
        return f"Couldn't update card default: {str(e)}"

def fuzzy_match_card(text):
    """Fuzzy match text against card names. Returns (matched_name, exact) or (None, False)."""
    cards = get_card_names_live()
    text_lower = text.strip().lower()
    # Exact match
    for c in cards:
        if c.lower() == text_lower:
            return c, True
    # Substring match
    for c in cards:
        if text_lower in c.lower() or c.lower() in text_lower:
            return c, True
    # First 3 chars match
    for c in cards:
        if len(text_lower) >= 3 and c.lower().startswith(text_lower[:3]):
            return c, False
    return None, False

def fuzzy_match_category(text):
    """Fuzzy match text against expense categories. Returns (matched, exact) or (None, False)."""
    text_lower = text.strip().lower()
    # Exact match
    for c in EXPENSE_CATEGORIES:
        if c.lower() == text_lower:
            return c, True
    # Synonym map
    synonyms = {
        "food": "FnB", "dining": "FnB", "restaurant": "FnB", "eat": "FnB", "f&b": "FnB",
        "fun": "Entertainment", "movie": "Entertainment", "movies": "Entertainment",
        "gym": "Personal", "health": "Personal",
        "taxi": "Transport", "grab": "Transport", "uber": "Transport", "mrt": "Transport", "bus": "Transport",
        "clothes": "Shopping", "shop": "Shopping",
        "trip": "Travel", "holiday": "Travel", "flight": "Travel",
        "office": "Work", "business": "Work",
    }
    if text_lower in synonyms:
        return synonyms[text_lower], True
    # Substring match
    for c in EXPENSE_CATEGORIES:
        if text_lower in c.lower() or c.lower() in text_lower:
            return c, False
    return None, False

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
}

# Global scheduler reference (set in post_init)
_scheduler = None
_app_ref = None

# Pending new merchant — waiting for user to confirm category + card
# { user_id: { "merchant": str, "amount": float, "currency": str, "step": "category"|"card" } }
expense_sessions = {}
delete_sessions = {}
portfolio_delete_sessions = {}  # { user_id: { "step": "pick", "rows": [...] } }
confirm_sessions = {}  # { user_id: { "action": str, "args": list, "target": str } }
receipt_confirm_sessions = {}  # { user_id: { "merchant": str, "amount": float, ... } }
todo_disambig_sessions = {}  # { user_id: { "tasks": list, "action": str } }
market_summary_pending = {}  # { user_id: True }

# Reconciliation sessions — { user_id: { "step": str, "unmatched": [...], "index": int } }
recon_sessions = {}

SESSION_TIMEOUT_MINUTES = 5
SESSION_TIMEOUT_MESSAGES = {
    "expense": "Session timed out — nothing was logged. Start again when ready.",
    "delete": "Session timed out — nothing was deleted.",
    "portfolio_delete": "Session timed out — nothing was removed.",
    "confirm": "Session timed out — nothing was changed.",
    "receipt_confirm": "Session timed out — nothing was logged. Send the receipt again when ready.",
    "edit": "Session timed out — nothing was changed.",
    "meeting": "Meeting recap session timed out.",
}

def touch_session(user_id):
    """Record current time for session timeout tracking."""
    session_timestamps[user_id] = datetime.now(TIMEZONE)

def is_session_expired(user_id):
    """Return True if the session for user_id has exceeded SESSION_TIMEOUT_MINUTES."""
    ts = session_timestamps.get(user_id)
    if not ts:
        return True
    elapsed = (datetime.now(TIMEZONE) - ts).total_seconds() / 60
    return elapsed > SESSION_TIMEOUT_MINUTES

def clear_all_sessions(user_id):
    """Clear all active sessions for a user."""
    for d in [expense_sessions, delete_sessions, portfolio_delete_sessions,
              confirm_sessions, receipt_confirm_sessions, recon_sessions,
              edit_sessions, meeting_sessions, session_timestamps]:
        d.pop(user_id, None)

async def check_session_timeouts(user_id, update):
    """Check all active sessions for timeout. Returns True if any session was expired and cleared."""
    if not is_session_expired(user_id):
        return False

    expired = False
    if user_id in expense_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["expense"])
        del expense_sessions[user_id]
        expired = True
    if user_id in delete_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["delete"])
        del delete_sessions[user_id]
        expired = True
    if user_id in portfolio_delete_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["portfolio_delete"])
        del portfolio_delete_sessions[user_id]
        expired = True
    if user_id in confirm_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["confirm"])
        del confirm_sessions[user_id]
        expired = True
    if user_id in receipt_confirm_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["receipt_confirm"])
        del receipt_confirm_sessions[user_id]
        expired = True
    if user_id in edit_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["edit"])
        del edit_sessions[user_id]
        expired = True
    if user_id in meeting_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["meeting"])
        del meeting_sessions[user_id]
        expired = True

    session_timestamps.pop(user_id, None)
    return expired



# Cached FX rates — { "JPY_SGD": { "rate": 0.0093, "fetched_at": datetime } }
cached_fx_rates = {}

def get_fx_rate(currency):
    """Return SGD rate for given currency. Uses cache if under 12hrs, else fetches fresh.
    Falls back to manual rate if API unavailable. No Claude estimate."""
    if currency == "SGD":
        return 1.0
    cache_key = f"{currency}_SGD"

    # Check live cache
    cached = cached_fx_rates.get(cache_key)
    if cached:
        age = datetime.now(pytz.utc) - cached["fetched_at"]
        if age.total_seconds() < 43200:  # 12 hours
            return cached["rate"]

    # Fetch fresh from API
    if EXCHANGE_RATE_API_KEY:
        try:
            url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/pair/{currency}/SGD"
            resp = requests.get(url, timeout=8)
            data = resp.json()
            if data.get("result") == "success":
                rate = float(data["conversion_rate"])
                cached_fx_rates[cache_key] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
                print(f"FX cache updated: 1 {currency} = {rate} SGD")
                # Clear manual rate if API is back
                if currency in manual_fx_rates:
                    del manual_fx_rates[currency]
                return rate
            else:
                print(f"FX API non-success for {currency}: {data.get('error-type', data)}")
        except Exception as e:
            print(f"FX fetch error for {currency}: {e}")

    # Check manual rate cache
    manual = manual_fx_rates.get(currency)
    if manual:
        today_str = date.today().strftime("%Y-%m-%d")
        if manual.get("date") == today_str:
            return manual["rate"]
        # Stale manual rate from previous day — reuse but notify
        print(f"Using previous manual rate for {currency}: {manual['rate']}")
        return manual["rate"]

    # No rate available — return None to trigger manual input prompt
    return None

def save_manual_fx_rate(currency, rate, sgd_per_unit=True):
    """Save a manually entered FX rate. sgd_per_unit=True means rate is how much SGD per 1 unit of currency."""
    if not sgd_per_unit:
        # User entered SGD to currency rate, e.g. $1 SGD = 110 JPY → 1 JPY = 1/110 SGD
        rate = 1.0 / rate if rate else 0
    manual_fx_rates[currency] = {
        "rate": rate,
        "date": date.today().strftime("%Y-%m-%d"),
        "sgd_per_unit": sgd_per_unit
    }
    # Also update live cache
    cached_fx_rates[f"{currency}_SGD"] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
    print(f"Manual FX rate saved: 1 {currency} = {rate} SGD")

def parse_manual_fx_input(text, currency):
    """Parse manual FX rate input. Supports multiple formats:
    - '110' or '1 SGD = 110 JPY' → SGD to foreign (sgd_per_unit=False)
    - '0.0093' or '1 JPY = 0.0093 SGD' → foreign to SGD (sgd_per_unit=True)
    - '1sgd to 3.11 myr' or '1sgd to 3.11 my' → SGD to foreign (partial currency codes accepted)
    Returns (rate_as_sgd_per_unit, display_str) or (None, None)
    """
    try:
        text = text.strip().replace(",", "")
        text_lower = text.lower()

        # Normalise partial currency codes to 3-letter (e.g. "my" → "myr", "rp" → "idr")
        partial_currency_map = {
            "my": "myr", "rm": "myr",
            "rp": "idr",
            "bt": "thb", "baht": "thb",
            "vnd": "vnd", "dong": "vnd",
            "php": "php", "peso": "php",
        }
        for partial, full in partial_currency_map.items():
            text_lower = re.sub(rf"\b{partial}\b", full, text_lower)

        # Extract all numbers from text
        nums = re.findall(r"[\d]+\.[\d]+|[\d]+", text_lower)
        if not nums:
            return None, None

        # Try to detect "1 SGD to X CURRENCY" pattern
        to_pattern = re.search(
            r"1\s*sgd\s+(?:to|=|:)\s*([\d.]+)\s*\w*", text_lower
        )
        if to_pattern:
            num = float(to_pattern.group(1))
            if num <= 0:
                return None, None
            sgd_per_unit = False
            display = f"$1 SGD = {num:.4f} {currency}"
            return (num, sgd_per_unit), display

        # Try to detect "1 CURRENCY to X SGD" pattern
        curr_to_sgd = re.search(
            rf"1\s*{re.escape(currency.lower())}\s+(?:to|=|:)\s*([\d.]+)\s*(?:sgd)?", text_lower
        )
        if curr_to_sgd:
            num = float(curr_to_sgd.group(1))
            if num <= 0:
                return None, None
            sgd_per_unit = True
            display = f"1 {currency} = ${num:.4f} SGD"
            return (num, sgd_per_unit), display

        # Fall back to extracting first number and inferring direction
        num_match = re.search(r"[\d.]+", text)
        if not num_match:
            return None, None
        num = float(num_match.group())
        if num <= 0:
            return None, None

        # Determine direction from keyword context
        if "sgd" in text_lower and currency.lower() in text_lower:
            sgd_pos = text_lower.index("sgd")
            curr_pos = text_lower.index(currency.lower()) if currency.lower() in text_lower else -1
            if curr_pos > sgd_pos:
                sgd_per_unit = False
                display = f"$1 SGD = {num:.4f} {currency}"
            else:
                sgd_per_unit = True
                display = f"1 {currency} = ${num:.4f} SGD"
        elif num > 10:
            # Large number → likely SGD to foreign (e.g. 110 JPY per SGD)
            sgd_per_unit = False
            display = f"$1 SGD = {num:.2f} {currency}"
        else:
            # Small number → likely foreign to SGD (e.g. 0.0093 SGD per JPY)
            sgd_per_unit = True
            display = f"1 {currency} = ${num:.4f} SGD"

        return (num, sgd_per_unit), display
    except Exception as e:
        print(f"parse_manual_fx_input error: {e}")
        return None, None



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
    """Redirect to v2 which uses live category/card lists."""
    return parse_expense_text_v2(text)
def parse_expense_text_v2(text):
    """Updated parse_expense_text using live category/card lists."""
    overseas_currency = overseas_state["currency"] if overseas_state["active"] else "SGD"
    live_cats = ", ".join(EXPENSE_CATEGORIES)
    live_cards = ", ".join(get_card_names_live())
    prompt = (
        f"Extract expense details from this message: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- merchant: string (brand name only, strip legal suffixes)\n"
        f"- amount: number (just the number, no currency symbol)\n"
        f"- currency: string (3-letter ISO code. If not mentioned, default to '{overseas_currency}')\n"
        f"- category: string (one of: {live_cats}) or empty if unclear\n"
        f"- card: string (one of: {live_cards}) or empty if not mentioned\n\n"
        f"Return ONLY the JSON."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"parse_expense_text Claude error: {e}")
        raise

def log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link="", reconciled="No", notes=""):
    """Append expense row to Expenses sheet."""
    today = date.today().strftime("%d/%m/%Y")
    sheet = expenses_sheet()
    sheet.append_row([
        today, merchant, amount, currency, sgd_amount,
        category, card, receipt_link, reconciled, notes
    ])

def format_expense_confirmation(merchant, amount, currency, category, card, sgd_amount=None,
                                receipt_saved=False, last4=None, high_amount=False,
                                use_manual_rate=False, missing_fields=None):
    """Format expense confirmation in hybrid format with new spec."""
    emoji = get_merchant_emoji(category, merchant)
    lines = [f"{emoji} {merchant}"]

    # Amount line
    if currency != "SGD" and sgd_amount is not None:
        amount_str = f"${sgd_amount:.2f} ({amount:,.0f} {currency})"
    else:
        amount_str = f"${amount:.2f}" if amount else "⚠️ Amount?"

    # Category
    cat_str = f"⚠️ Category?" if not category else category

    # Card
    if not card:
        card_str = "⚠️ Card?"
    elif last4:
        card_str = f"{card} (*{last4})"
    else:
        card_str = card

    detail_line = f"{amount_str} | {cat_str} | {card_str}"
    lines.append(detail_line)

    # Flags
    if high_amount:
        lines.append("⚠️ Amount looks high — is this correct?")
    if use_manual_rate:
        lines.append("⚠️ Using manual rate — verify if needed")
    if receipt_saved:
        lines.append("🧾 Receipt saved to Drive")

    # Missing fields prompt
    if missing_fields:
        if len(missing_fields) >= 3:
            prompt = "yes / enter missing values / skip"
        elif len(missing_fields) == 2:
            prompt = f"yes / enter {' & '.join(f.title() for f in missing_fields)} / skip"
        else:
            prompt = f"yes / enter {missing_fields[0].title()} / skip"
        lines.append(f"\nLog this? ({prompt})")
    else:
        lines.append("\nLog this? (yes / skip)")

    return "\n".join(lines)

def format_expense_logged(merchant, amount, currency, category, card, sgd_amount=None, last4=None):
    """Format the logged confirmation (after yes or after edit)."""
    emoji = get_merchant_emoji(category, merchant)
    if currency != "SGD" and sgd_amount is not None:
        amount_str = f"${sgd_amount:.2f} ({amount:,.0f} {currency})"
    else:
        amount_str = f"${amount:.2f}"
    card_str = f"{card} (*{last4})" if last4 else card
    return f"{emoji} {merchant}\n{amount_str} | {category} | {card_str}\n\nLogged ✅"



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


def get_merchant_list():
    """Pull all known merchants from Merchant Map, grouped by category."""
    try:
        sheet = merchant_map_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No merchants saved yet. They are added automatically when you log a new expense."
        by_cat = {}
        for r in records:
            merchant = r.get("Merchant", "").strip()
            cat = r.get("Category", "").strip() or "Uncategorised"
            card = r.get("Card", "").strip()
            if not merchant:
                continue
            if cat not in by_cat:
                by_cat[cat] = []
            entry = merchant + (f" ({card})" if card else "")
            by_cat[cat].append(entry)
        count = sum(len(v) for v in by_cat.values())
        lines = [f"Merchant Map ({count} merchants):\n"]
        for cat in sorted(by_cat):
            lines.append(f"*{cat}*")
            for m in sorted(by_cat[cat]):
                lines.append(f"  {m}")
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        return f"Error fetching merchants: {e}"

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
        "expense categories", "my categories",
        "what merchants", "my merchants", "merchant map", "list merchants",
        "show merchants", "known merchants"
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
    flights = extract_flight_number(text) if callable(extract_flight_number) else []
    has_flight = bool(flights)
    has_multiple_flights = len(re.findall(r'\b[A-Z]{1,3}\d{2,4}[A-Z]?\b', text.upper())) >= 2

    # Two flight numbers in one message = always a travel message
    if has_multiple_flights:
        return True

    # Single flight + travel intent words
    travel_words = [
        "flying", "flight", "boarding", "departure", "departing",
        "returning", "flying back", "headed to", "heading to",
        "on tr", "on sq", "on ak", "on mh", "on od", "on ek", "on cx",
    ]
    if has_flight and any(w in lower for w in travel_words):
        return True

    # Explicit travel phrases (no flight number needed)
    if any(phrase in lower for phrase in [
        "overseas", "travelling", "traveling", "flying to", "arrived in",
        "back home", "i'm back", "landed in", "just landed", "just arrived",
        "returned home"
    ]):
        return True

    # "i'm in [location]" — only if followed by a known place name
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
    # Close active trip in Trips sheet
    try:
        close_trip()
    except Exception:
        pass
    overseas_state["active"] = False
    overseas_state["destination"] = ""
    overseas_state["currency"] = "SGD"
    overseas_state["currencies"] = []
    overseas_state["return_date"] = ""
    overseas_state["return_flight"] = None
    overseas_state["trip_start"] = None
    overseas_state["trip_destinations"] = []
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
    # Persist trip to Trips sheet
    ret_flight_num = return_flight_data.get("flight", "") if return_flight_data else ""
    ret_time_str = return_flight_data.get("dep_time", "") if return_flight_data else ""
    dep_flight = overseas_state.get("dep_flight", "")
    dep_time = overseas_state.get("dep_time", "")
    save_trip(dest, curr, dep_flight, dep_time, ret_flight_num, ret_time_str)
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

def handle_overseas_request(text):
    """Toggle overseas mode on/off, with optional flight lookup."""
    global overseas_state, _scheduler
    lower = text.lower()

    # Returning home manually
    if any(p in lower for p in ["back home", "returned", "i'm back", "landed back", "home now"]):
        import random
        greeting = random.choice(["Welcome back!", "Good to have you back!", "Hope the trip was great!"])
        deactivate_overseas_mode()
        return f"{greeting} Switching back to SGD. 🏠"

    SG_IATA_CODES = {"SIN", "SLM"}
    SG_CITY_KEYWORDS = {"singapore", "changi"}
    # Words indicating return intent before a flight number
    RETURN_INTENT = ["returning on", "returning on the", "flying back on", "back on",
                     "return on", "coming back on", "heading back on"]

    def _has_return_intent(msg, flight_num):
        """Return True if user's message expresses return intent for this flight."""
        lower_msg = msg.lower()
        for phrase in RETURN_INTENT:
            if phrase in lower_msg and flight_num.lower() in lower_msg:
                # Check phrase appears before the flight number
                if lower_msg.index(phrase) < lower_msg.index(flight_num.lower()):
                    return True
        return False

    def _is_sg_arrival(flight_info, force_return=False):
        """Return True if this flight lands back in Singapore."""
        if force_return:
            return True
        if not flight_info:
            return False
        iata = (flight_info.get("arr_iata") or "").upper()
        city = (flight_info.get("arr_city") or "").lower()
        airport = (flight_info.get("arr_airport") or "").lower()
        return (iata in SG_IATA_CODES or
                any(k in city for k in SG_CITY_KEYWORDS) or
                any(k in airport for k in SG_CITY_KEYWORDS))

    def _extract_leg2_dest(msg, flight2):
        """Extract explicit destination for second flight from user message.
        Avoids capturing prepositions like 'on', 'the', 'a'.
        """
        f2 = re.escape(flight2)
        # Patterns: 'TG416 KL to BKK', 'TG416 to Bangkok', 'then TG416 to BKK'
        patterns = [
            rf"{f2}\s+(?:[A-Z]{{3}}\s+)?to\s+([A-Za-z][A-Za-z\s]{{2,25}}?)(?:\s+on\s+|\s+Mon|\s+Tue|\s+Wed|\s+Thu|\s+Fri|\s+Sat|\s+Sun|$)",
            rf"then\s+{f2}\s+(?:[A-Z]{{3}}\s+)?to\s+([A-Za-z][A-Za-z\s]{{2,25}}?)(?:\s+on\s+|$)",
        ]
        STOPWORDS = {"on", "the", "a", "an", "my", "this", "next"}
        for pat in patterns:
            m = re.search(pat, msg, re.IGNORECASE)
            if m:
                dest = m.group(1).strip()
                # Reject if it's just a stopword
                if dest.lower() not in STOPWORDS and len(dest) > 2:
                    return dest
        return None

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

                    # 1. Check if user expressed return intent (e.g. "returning on OD805")
                    force_return = _has_return_intent(text, return_flight_num)
                    # 2. Check if user named an explicit non-SG destination
                    user_dest_hint = _extract_leg2_dest(text, return_flight_num)
                    # 3. Determine if it's a return
                    is_return = _is_sg_arrival(ret_data, force_return=force_return)
                    # 4. If user explicitly named a non-SG destination, override to multi-city
                    if user_dest_hint and not any(k in user_dest_hint.lower() for k in SG_CITY_KEYWORDS | {"sg", "sin", "home", "singapore"}):
                        is_return = False

                    if is_return:
                        pending["return_flight_data"] = ret_data
                        reply += f"\nReturn {return_flight_num}: {ret_dep} → {ret_arr} (SIN)\n"
                    else:
                        ret_arr_label = user_dest_hint or ret_data.get("arr_city") or ret_data.get("arr_airport") or ret_data.get("arr_iata", "")
                        pending["return_flight_data"] = ret_data
                        reply += f"\nLeg 2 {return_flight_num}: {ret_dep} → {ret_arr} ({ret_arr_label})\n"
                        reply += f"_(Multi-city trip — {return_flight_num} logged as next leg)_\n"
                        reply += f"\nGot a return flight to SG? Reply with the flight number or \'no\' to log as-is."
                else:
                    reply += f"(Couldn't find {return_flight_num} — I'll skip it)\n"

                overseas_state["_pending_flight"] = pending
                reply += "\n\nReply Y to confirm — overseas mode will activate at departure time."
            else:
                # Single flight — ask for return
                overseas_state["_pending_flight"] = pending
                overseas_state["_awaiting_return_flight"] = True
                reply += "\n\nGot a return flight yet? Reply with the flight number or 'no' to log just the departure."

            return reply

        else:
            return (
                f"Looked up {outbound} on AviationStack but got no data back — "
                f"the flight may not be in their system yet (free tier only covers flights within ~24hrs). "
                f"Where are you headed and when are you departing and returning?"
            )

    # No flight number — ask for details
    return "What\'s your departure date/time and destination? And when are you back in SG?"

def parse_multi_field_edit(text, field_keywords=None):
    """Parse a multi-field edit message like 'merchant Tsukemen Fuunji category Dining'.
    Returns dict of {field: value}. Supports synonyms and quoted merchant names."""
    if field_keywords is None:
        field_keywords = {
            "merchant": "merchant", "shop": "merchant", "store": "merchant", "place": "merchant",
            "amount": "amount", "price": "amount", "total": "amount", "cost": "amount",
            "currency": "currency",
            "category": "category", "cat": "category",
            "card": "card", "payment": "card", "pay": "card",
        }
    result = {}
    # Handle quoted merchant names first
    text = re.sub(r'"([^"]+)"', lambda m: m.group(0).replace(" ", "_SPACE_"), text)
    tokens = text.strip().split()
    i = 0
    current_field = None
    current_value_parts = []

    def flush():
        if current_field and current_value_parts:
            val = " ".join(current_value_parts).replace("_SPACE_", " ").strip('"')
            result[current_field] = val

    while i < len(tokens):
        token_lower = tokens[i].lower()
        if token_lower in field_keywords:
            flush()
            current_field = field_keywords[token_lower]
            current_value_parts = []
        elif current_field:
            current_value_parts.append(tokens[i])
        i += 1
    flush()
    return result

async def handle_receipt_confirm_session(user_id, text, update):
    """Handle receipt/expense confirm session: yes / skip / field edits / card name override."""
    session = receipt_confirm_sessions.get(user_id)
    if not session:
        return False

    touch_session(user_id)
    lower = text.strip().lower()

    # Cancel / skip
    if lower in ["skip", "cancel", "no"]:
        del receipt_confirm_sessions[user_id]
        session_timestamps.pop(user_id, None)
        receipt_link = session.get("receipt_link", "")
        if receipt_link:
            await update.message.reply_text(f"Skipped — receipt saved to Drive if you need it later.")
        else:
            await update.message.reply_text("Skipped.")
        return True

    # FX rate input
    if session.get("step") == "fx_rate":
        currency = session.get("currency", "")
        parsed_rate, display = parse_manual_fx_input(text, currency)
        if parsed_rate is None:
            await update.message.reply_text(
                f"Couldn't read that rate — try one of these formats:\n"
                f"• `3.11` (1 {currency} = 3.11 SGD)\n"
                f"• `1 SGD to 3.11 {currency}`\n"
                f"• `1 {currency} = 3.11 SGD`"
            )
            return True  # Stay in session, don't drop
        num, sgd_per_unit = parsed_rate
        save_manual_fx_rate(currency, num, sgd_per_unit)
        rate = get_fx_rate(currency)
        session["sgd_amount"] = round(session["amount"] * rate, 2)
        session["step"] = "receipt_confirm"
        session["use_manual_rate"] = True
        receipt_confirm_sessions[user_id] = session
        await update.message.reply_text(
            f"Got it — using {display} for today.\n\n" +
            format_expense_confirmation(
                session["merchant"], session["amount"], session["currency"],
                session.get("category", ""), session.get("card", ""),
                session.get("sgd_amount"), receipt_saved=bool(session.get("receipt_link")),
                last4=session.get("last4"), high_amount=session.get("high_amount", False),
                use_manual_rate=True, missing_fields=session.get("missing_fields", [])
            )
        )
        return True

    # Duplicate confirm
    if session.get("step") == "duplicate_confirm":
        if lower in ["yes", "y"]:
            session["step"] = "receipt_confirm"
            receipt_confirm_sessions[user_id] = session
            await update.message.reply_text(
                format_expense_confirmation(
                    session["merchant"], session["amount"], session["currency"],
                    session.get("category", ""), session.get("card", ""),
                    session.get("sgd_amount"), receipt_saved=bool(session.get("receipt_link")),
                    last4=session.get("last4"), high_amount=session.get("high_amount", False),
                    use_manual_rate=session.get("use_manual_rate", False),
                    missing_fields=session.get("missing_fields", [])
                )
            )
        else:
            del receipt_confirm_sessions[user_id]
            await update.message.reply_text("Skipped duplicate.")
        return True

    # Yes — log it
    if lower in ["yes", "y"]:
        await _finalise_receipt_confirm(user_id, session, update)
        return True

    # Check if it's a single card name override
    card_match, _ = fuzzy_match_card(text.strip())
    if card_match and len(text.strip().split()) == 1:
        session["card"] = card_match
        # Log immediately after card override
        await _finalise_receipt_confirm(user_id, session, update)
        return True

    # Try multi-field edit
    edits = parse_multi_field_edit(text)
    if edits:
        # Check for keyword-in-merchant-name ambiguity
        merchant_val = edits.get("merchant", "")
        field_keys = {"merchant", "amount", "currency", "category", "card", "price", "total", "cost", "shop", "store", "place", "payment", "pay", "cat"}
        if merchant_val:
            words = merchant_val.lower().split()
            collision = [w for w in words if w in field_keys]
            if collision and '"' not in text:
                # Ambiguity detected — confirm with user using Option A
                # Build what we parsed
                lines = ["Just to confirm:"]
                for f, v in edits.items():
                    lines.append(f"{f.title()}: {v}")
                # Add unchanged fields
                for f in ["amount", "category", "card"]:
                    if f not in edits:
                        val = session.get(f, "")
                        if val:
                            lines.append(f"{f.title()}: {val}")
                lines.append("\nyes / no / edit")
                session["_pending_edits"] = edits
                receipt_confirm_sessions[user_id] = session
                await update.message.reply_text("\n".join(lines))
                return True

        # Apply edits
        for field, value in edits.items():
            if field == "merchant":
                session["merchant"] = value
                # Re-run merchant memory
                known_cat, known_card, canonical = get_merchant_memory(value)
                if canonical:
                    session["merchant"] = canonical
                if known_cat and not edits.get("category"):
                    session["category"] = known_cat
                # Always re-derive card from category, not merchant memory
                if session.get("category") and not edits.get("card"):
                    session["card"] = get_card_default_for_category(session["category"])
            elif field == "amount":
                try:
                    session["amount"] = float(re.sub(r"[^\d.]", "", value))
                    if session["currency"] == "SGD":
                        session["sgd_amount"] = session["amount"]
                except ValueError:
                    pass
            elif field == "currency":
                session["currency"] = value.upper()
            elif field == "category":
                matched_cat, _ = fuzzy_match_category(value)
                if matched_cat:
                    session["category"] = matched_cat
                    # Update card default if not explicitly set
                    if not edits.get("card"):
                        session["card"] = get_card_default_for_category(matched_cat)
            elif field == "card":
                matched_card, _ = fuzzy_match_card(value)
                if matched_card:
                    session["card"] = matched_card

        # Clear missing fields that are now filled
        session["missing_fields"] = [f for f in session.get("missing_fields", [])
                                      if not session.get(f)]
        session["high_amount"] = session.get("sgd_amount", 0) > SGD_HIGH_AMOUNT_THRESHOLD

        # Log immediately after edit — no re-confirm
        await _finalise_receipt_confirm(user_id, session, update)
        return True

    # Handle pending ambiguity confirm
    if session.get("_pending_edits"):
        if lower in ["yes", "y"]:
            edits = session.pop("_pending_edits")
            for field, value in edits.items():
                session[field] = value
            await _finalise_receipt_confirm(user_id, session, update)
        elif lower in ["no", "n"]:
            session.pop("_pending_edits", None)
            receipt_confirm_sessions[user_id] = session
            await update.message.reply_text(
                'Re-enter with quotes for merchant names containing field keywords.\n'
                'e.g. merchant "Gift Card Store" category Shopping'
            )
        return True

    # Unrecognised input
    await update.message.reply_text("Didn't catch that — reply yes to log, skip to cancel, or edit fields (e.g. category FnB card Citi)")
    return True

async def _finalise_receipt_confirm(user_id, session, update):
    """Log expense from receipt confirm session and send logged confirmation."""
    merchant = session["merchant"]
    amount = session["amount"]
    currency = session["currency"]
    category = session.get("category", "")
    card = session.get("card", "Citi")
    sgd_amount = session.get("sgd_amount", amount)
    receipt_link = session.get("receipt_link", "")
    last4 = session.get("last4")
    is_new_merchant = session.get("is_new_merchant", False)

    log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link=receipt_link)
    if is_new_merchant and category and card:
        save_merchant_memory(merchant, category, card)

    del receipt_confirm_sessions[user_id]
    session_timestamps.pop(user_id, None)

    reply = format_expense_logged(merchant, amount, currency, category, card, sgd_amount, last4)
    try:
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(reply)

async def handle_expense_session(user_id, text, update):
    """Handle multi-step expense onboarding — fx_rate only. Confirm flow now handled by receipt_confirm_sessions."""
    session = expense_sessions.get(user_id)
    if not session:
        return

    touch_session(user_id)
    step = session.get("step")

    if step == "fx_rate":
        currency = session.get("currency", "")
        parsed_rate, display = parse_manual_fx_input(text, currency)
        if parsed_rate is None:
            await update.message.reply_text(
                f"Couldn't read that rate — try one of these formats:\n"
                f"• `3.11` (1 {currency} = 3.11 SGD)\n"
                f"• `1 SGD to 3.11 {currency}`\n"
                f"• `1 {currency} = 3.11 SGD`"
            )
            return  # Stay in session, don't drop
        num, sgd_per_unit = parsed_rate
        save_manual_fx_rate(currency, num, sgd_per_unit)
        rate = get_fx_rate(currency)
        session["sgd_amount"] = round(session["amount"] * rate, 2)
        session["use_manual_rate"] = True

        # Move to receipt confirm flow
        del expense_sessions[user_id]
        receipt_confirm_sessions[user_id] = {**session, "step": "receipt_confirm",
                                              "missing_fields": [], "is_new_merchant": True}
        touch_session(user_id)
        await update.message.reply_text(
            f"Got it — using {display} for today.\n\n" +
            format_expense_confirmation(
                session["merchant"], session["amount"], currency,
                session.get("category", ""), session.get("card", ""),
                session["sgd_amount"], use_manual_rate=True,
                missing_fields=[]
            )
        )
        return

    if step == "duplicate_confirm":
        lower = text.strip().lower()
        if lower in ["yes", "y"]:
            # Move to confirm screen
            del expense_sessions[user_id]
            receipt_confirm_sessions[user_id] = {**session, "step": "receipt_confirm"}
            touch_session(user_id)
            confirmation = format_expense_confirmation(
                session["merchant"], session["amount"], session["currency"],
                session.get("category", ""), session.get("card", ""),
                session.get("sgd_amount"), missing_fields=[]
            )
            await update.message.reply_text(confirmation)
        else:
            del expense_sessions[user_id]
            await update.message.reply_text("Skipped.")
        return

async def _finalise_expense_session(user_id, update):
    """Legacy finalise — redirects to receipt confirm flow."""
    session = expense_sessions.get(user_id)
    if not session:
        return
    del expense_sessions[user_id]
    await _finalise_receipt_confirm(user_id, session, update)



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

SGD_HIGH_AMOUNT_THRESHOLD = 5000.0

def handle_expense_text(text, user_id, receipt_link="", last4=None):
    """
    Process a text expense entry. Returns (reply_str, needs_session, session_data).
    Option B: auto-log if fully known, confirm screen if anything missing or uncertain.
    """
    try:
        parsed = parse_expense_text(text)
        merchant = parsed.get("merchant", "Unknown")
        amount = float(parsed.get("amount", 0))
        currency = parsed.get("currency", "SGD")
        category = parsed.get("category", "")
        card = parsed.get("card", "")

        # Blank expense guard
        if not merchant or merchant.lower() in ("unknown", "") or amount == 0:
            return (
                "Wasn't sure what to do with that — did you mean to log an expense, "
                "or were you asking something else?"
            ), False, None

        # Sanity check: zero or negative
        if amount <= 0:
            return "Amount can't be zero or negative — what's the correct amount?", False, None

        # Fuzzy merchant lookup — use canonical name if found
        known_cat, known_card, canonical = get_merchant_memory(merchant)
        if canonical:
            merchant = canonical
        if known_cat and not category:
            category = known_cat
        # Card priority: (1) explicit user input, (2) category default, (3) global fallback
        # Never let merchant memory or category default override an explicit card from the user
        explicit_card = card  # preserve what the parser extracted from user text

        if not explicit_card:
            # No explicit card — derive from category
            if category:
                card = get_card_default_for_category(category)
            else:
                card = "Citi"  # global fallback
        # else: keep explicit_card as-is

        # Card override from last4 (receipt scan) — only if no explicit card
        if last4 and not explicit_card:
            matched_card = get_card_by_last4(last4)
            if matched_card:
                card = matched_card

        # Resolve FX rate if needed
        sgd_amount = amount
        use_manual_rate = False
        if currency != "SGD":
            rate = get_fx_rate(currency)
            if rate is None:
                # Check if we have a stale manual rate to use
                manual = manual_fx_rates.get(currency)
                if manual:
                    rate = manual["rate"]
                    use_manual_rate = True
                    await_msg = (
                        f"Live rate unavailable — using manual rate: 1 {currency} = ${rate:.4f} SGD\n"
                        f"I'll keep checking for the live rate."
                    )
                else:
                    # No rate at all — ask for manual input
                    session = {
                        "merchant": merchant, "amount": amount, "currency": currency,
                        "category": category, "card": card, "step": "fx_rate",
                        "receipt_link": receipt_link, "last4": last4
                    }
                    return (
                        f"Couldn't get the {currency}/SGD rate right now.\n"
                        f"Enter the exchange rate:\n"
                        f"• SGD to {currency}: e.g. 110 (meaning $1 SGD = {currency} 110)\n"
                        f"• {currency} to SGD: e.g. 0.0093 (meaning 1 {currency} = $0.0093 SGD)"
                    ), True, session
            sgd_amount = round(amount * rate, 2)
            if overseas_state["active"] and currency not in overseas_state["currencies"]:
                overseas_state["currencies"].append(currency)

        # High amount sanity check
        high_amount = sgd_amount > SGD_HIGH_AMOUNT_THRESHOLD

        # Same-day duplicate check
        if check_same_day_duplicate(merchant, amount, currency):
            session = {
                "merchant": merchant, "amount": amount, "currency": currency,
                "sgd_amount": sgd_amount, "category": category, "card": card,
                "step": "duplicate_confirm", "receipt_link": receipt_link, "last4": last4
            }
            return (
                f"Heads up — looks like you already logged {merchant} {currency} {amount:,.0f} today. Log it again? (yes / no)"
            ), True, session

        # Determine missing fields
        missing = []
        if not category:
            missing.append("category")
        if not amount:
            missing.append("amount")

        is_new_merchant = not bool(known_cat)

        # Option B: auto-log only if everything known, no flags, AND it's a known merchant
        # New merchants always go through confirm screen
        if not missing and not high_amount and not use_manual_rate and not is_new_merchant:
            log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link=receipt_link)
            save_merchant_memory(merchant, category, card)
            return format_expense_logged(merchant, amount, currency, category, card, sgd_amount, last4), False, None

        # Otherwise show confirm screen
        session = {
            "merchant": merchant, "amount": amount, "currency": currency,
            "sgd_amount": sgd_amount, "category": category, "card": card,
            "step": "receipt_confirm", "missing_fields": missing,
            "receipt_link": receipt_link, "last4": last4,
            "high_amount": high_amount, "use_manual_rate": use_manual_rate,
            "is_new_merchant": is_new_merchant
        }
        receipt_sessions_key = user_id
        receipt_confirm_sessions[receipt_sessions_key] = session
        confirmation = format_expense_confirmation(
            merchant, amount, currency, category, card, sgd_amount,
            receipt_saved=bool(receipt_link), last4=last4,
            high_amount=high_amount, use_manual_rate=use_manual_rate,
            missing_fields=missing
        )
        return confirmation, False, None  # Session stored separately in receipt_confirm_sessions

    except (json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"parse_expense_text error: {e} | input: {text}")
        return (
            f"❌ Couldn't parse that as an expense ({type(e).__name__}: {str(e)[:60]}).\n"
            "Try: 'log [merchant] [amount]' — e.g. 'log Starbucks $5.60'"
        ), False, None
    except Exception as e:
        print(f"handle_expense_text unexpected error: {e}")
        return f"❌ Something went wrong logging that ({type(e).__name__}: {str(e)[:80]}). Try again or use 'log [merchant] [amount]'.", False, None



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

EDIT_FIELD_SYNONYMS = {
    "merchant": "Merchant", "shop": "Merchant", "store": "Merchant", "place": "Merchant",
    "amount": "Amount", "price": "Amount", "total": "Amount", "cost": "Amount",
    "currency": "Currency",
    "category": "Category", "cat": "Category",
    "card": "Card", "payment": "Card",
    "notes": "Notes", "note": "Notes",
    "sgd": "SGD Amount",
}

def edit_last_expense(edit_text):
    """Edit fields in the last expense row. Supports multi-field edits in one message.
    Returns formatted logged message after editing."""
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return "No expenses to edit."
        headers = all_values[0]
        last_row_idx = len(all_values)
        last_row = all_values[last_row_idx - 1]

        # Parse multi-field edits
        edits = parse_multi_field_edit(edit_text, field_keywords=EDIT_FIELD_SYNONYMS)
        if not edits:
            return "Couldn't parse that edit. Try: 'edit merchant Starbucks category FnB'"

        applied = []
        merchant_changed = False

        for field_key, new_value in edits.items():
            col_name = EDIT_FIELD_SYNONYMS.get(field_key.lower())
            if not col_name or col_name not in headers:
                continue

            col_idx = headers.index(col_name) + 1

            # Validate and normalise per field
            if col_name == "Amount":
                try:
                    new_value = str(float(re.sub(r"[^\d.]", "", new_value)))
                except ValueError:
                    continue
            elif col_name == "Category":
                matched_cat, _ = fuzzy_match_category(new_value)
                if not matched_cat:
                    continue
                new_value = matched_cat
            elif col_name == "Card":
                matched_card, _ = fuzzy_match_card(new_value)
                if not matched_card:
                    continue
                new_value = matched_card
            elif col_name == "Merchant":
                merchant_changed = True

            sheet.update_cell(last_row_idx, col_idx, new_value)
            applied.append((col_name, new_value))

        if not applied:
            return "Couldn't match any valid fields to edit."

        # If merchant changed, re-run merchant memory for category/card
        if merchant_changed:
            new_merchant = next((v for k, v in edits.items() if EDIT_FIELD_SYNONYMS.get(k) == "Merchant"), "")
            if new_merchant:
                known_cat, known_card, canonical = get_merchant_memory(new_merchant)
                if canonical:
                    m_col = headers.index("Merchant") + 1
                    sheet.update_cell(last_row_idx, m_col, canonical)
                if known_cat and "Category" not in [c for c, _ in applied]:
                    c_col = headers.index("Category") + 1
                    sheet.update_cell(last_row_idx, c_col, known_cat)
                    applied.append(("Category", known_cat))
                if known_card and "Card" not in [c for c, _ in applied]:
                    cd_col = headers.index("Card") + 1
                    sheet.update_cell(last_row_idx, cd_col, known_card)
                    applied.append(("Card", known_card))

        # Re-read updated row to show logged confirmation
        updated_values = sheet.get_all_values()
        updated_row_data = updated_values[last_row_idx - 1] if len(updated_values) >= last_row_idx else last_row
        updated = {headers[i]: updated_row_data[i] if i < len(updated_row_data) else "" for i in range(len(headers))}

        merchant = updated.get("Merchant", "")
        amount = updated.get("Amount", "")
        currency = updated.get("Currency", "SGD")
        sgd_amount = updated.get("SGD Amount", amount)
        category = updated.get("Category", "")
        card = updated.get("Card", "")

        return format_expense_logged(merchant, float(amount) if amount else 0,
                                     currency, category, card,
                                     float(sgd_amount) if sgd_amount else None)

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
    # Update Cards sheet Default Category column
    try:
        ws = cards_sheet()
        records = ws.get_all_records()
        for i, r in enumerate(records, start=2):
            if r.get("Default Category", "").strip().lower() == matched.lower():
                ws.update_cell(i, 3, new_clean)  # Default Category is col 3
    except Exception as e:
        print(f"rename_category cards sheet error: {e}")

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
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
        result = json.loads(raw)
        # Flag if location is blank — needs manual input
        if not result.get("location"):
            result["_needs_location"] = True
        return result
    except Exception as e:
        print(f"lookup_restaurant_from_maps error: {e}")
        return {"name": "", "location": "", "country": "Singapore", "_needs_location": True}

def save_restaurant(name, location, country="Singapore", tags="", notes="", force_new=False):
    """Save a restaurant to the Restaurants sheet. Checks for duplicates unless force_new."""
    if not force_new:
        try:
            sheet = restaurants_sheet()
            records = sheet.get_all_records()
            for r in records:
                if r.get("Name", "").lower() == name.lower():
                    return "_DUPLICATE_:" + name
        except Exception:
            pass
    sheet = restaurants_sheet()
    sheet.append_row([name, location, country, tags, notes])
    return None  # Success

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

def handle_save_restaurant(text, force_new=False):
    """Handle saving a restaurant from text or maps link."""
    try:
        # Check if it contains a Maps URL
        if "maps.google" in text or "goo.gl/maps" in text or "maps.app.goo" in text:
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

            if parsed.get("_needs_location") or not location:
                return f"_NEEDS_LOCATION_:{name}:{country}"

        else:
            parsed = parse_restaurant_save(text)
            name = parsed.get("name", "")
            location = parsed.get("location", "")
            country = parsed.get("country", "Singapore")
            tags = parsed.get("tags", "")
            notes = parsed.get("notes", "")

        if not name:
            return "What's the restaurant name? Try: 'save Burnt Ends, Teck Lim Road'"

        result = save_restaurant(name, location, country, tags, notes, force_new=force_new)
        if result and result.startswith("_DUPLICATE_:"):
            existing = result.split(":", 1)[1]
            return f"_DUPLICATE_RESTAURANT_:{existing}:{name}:{location}:{country}:{tags}:{notes}"

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

# --- Trips Sheet ---

def trips_sheet():
    return spreadsheet.worksheet("Trips")

def generate_trip_id():
    return date.today().strftime("TRIP-%Y%m%d")

def save_trip(destination, currency, dep_flight="", dep_time="", return_flight="", return_time=""):
    """Write a new active trip row to Trips sheet."""
    try:
        ws = trips_sheet()
        trip_id = generate_trip_id()
        ws.append_row([
            trip_id, destination, currency,
            dep_flight, dep_time,
            return_flight, return_time,
            "active",
            datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M"),
            ""
        ])
        return trip_id
    except Exception as e:
        print(f"save_trip error: {e}")
        return None

def close_trip(trip_id=None):
    """Mark the active trip as closed."""
    try:
        ws = trips_sheet()
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if row.get("Status") == "active" and (trip_id is None or row.get("Trip ID") == trip_id):
                ws.update_cell(i, 8, "closed")  # Status col
                ws.update_cell(i, 10, datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M"))  # Ended
                return True
    except Exception as e:
        print(f"close_trip error: {e}")
    return False

def get_active_trip():
    """Return the most recent active trip row, or None."""
    try:
        ws = trips_sheet()
        records = ws.get_all_records()
        for row in reversed(records):
            if row.get("Status") == "active":
                return row
    except Exception as e:
        print(f"get_active_trip error: {e}")
    return None

def restore_overseas_from_trips():
    """On startup — if there's an active trip in the sheet, restore overseas_state."""
    try:
        trip = get_active_trip()
        if not trip:
            return False
        dest = trip.get("Destination", "")
        curr = trip.get("Currency", "SGD")
        dep_flight = trip.get("Dep Flight", "")
        return_flight = trip.get("Return Flight", "")
        if dest and curr and curr != "SGD":
            overseas_state["active"] = True
            overseas_state["destination"] = dest
            overseas_state["currency"] = curr
            overseas_state["currencies"] = [curr]
            overseas_state["trip_destinations"] = [dest]
            if dep_flight:
                overseas_state["dep_flight"] = dep_flight
            if return_flight:
                overseas_state["return_flight"] = {"flight": return_flight}
            print(f"✅ Restored overseas mode: {dest} ({curr})")
            return True
    except Exception as e:
        print(f"restore_overseas_from_trips error: {e}")
    return False

def get_trip_history(n=5):
    """Return last n trips from Trips sheet."""
    try:
        ws = trips_sheet()
        records = ws.get_all_records()
        return list(reversed(records[-n:])) if records else []
    except Exception as e:
        print(f"get_trip_history error: {e}")
        return []

def format_trip_history():
    trips = get_trip_history(10)
    if not trips:
        return "No trips logged yet."
    lines = ["*Trip History*\n"]
    for t in trips:
        status = "✈️ Active" if t.get("Status") == "active" else "✅ Done"
        dest = t.get("Destination", "—")
        curr = t.get("Currency", "")
        dep = t.get("Dep Flight", "")
        ret = t.get("Return Flight", "")
        started = t.get("Started", "")
        ended = t.get("Ended", "")
        line = f"{status} {dest} ({curr})"
        if dep:
            line += f" | Out: {dep}"
        if ret:
            line += f" | Back: {ret}"
        if started:
            line += f"\n{started}"
            if ended:
                line += f" → {ended}"
        lines.append(line)
    return "\n\n".join(lines)


    """Log a stock purchase to Portfolio sheet."""
    sheet = portfolio_sheet()
    today = buy_date or date.today().strftime("%d/%m/%Y")
    sheet.append_row([ticker.upper(), str(quantity), str(price), today, ""])

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

def get_portfolio_rows():
    """Return list of (row_index, record) for all portfolio entries."""
    try:
        ws = portfolio_sheet()
        records = ws.get_all_records()
        return [(i + 2, r) for i, r in enumerate(records)]  # row 1 = header
    except Exception as e:
        print(f"get_portfolio_rows error: {e}")
        return []

def format_portfolio_delete_list(rows):
    """Format numbered list of portfolio holdings for delete selection."""
    if not rows:
        return "No holdings in portfolio."
    lines = ["Which holding do you want to remove?\n"]
    for i, (_, r) in enumerate(rows, 1):
        ticker = r.get("Stock", "?")
        qty = r.get("Quantity", "?")
        price = r.get("Buy Price", "?")
        buy_date = r.get("Buy Date", "")
        lines.append(f"{i}. {ticker} — {qty} shares @ ${price} ({buy_date})")
    lines.append("\nReply with a number, 'search [ticker]', or 'cancel'.")
    return "\n".join(lines)

def delete_portfolio_row(sheet_row):
    """Delete a portfolio row by sheet row index."""
    try:
        ws = portfolio_sheet()
        all_vals = ws.get_all_values()
        if sheet_row - 1 < len(all_vals):
            row_data = all_vals[sheet_row - 1]
            ticker = row_data[0] if row_data else "?"
        ws.delete_rows(sheet_row)
        return f"Removed {ticker} from portfolio."
    except Exception as e:
        return f"Couldn't remove that holding: {str(e)}"

def search_portfolio_by_ticker(query):
    """Search portfolio holdings by ticker substring."""
    rows = get_portfolio_rows()
    q = query.upper()
    return [(row_idx, r) for row_idx, r in rows if q in r.get("Stock", "").upper()]


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
    """Monday 8am — send US, China, India market summary."""
    try:
        lines = []
        sentiments = []

        for market, indices in MARKET_INDICES.items():
            market_lines = [f"{market}"]
            market_changes = []

            for ticker, name in indices.items():
                data = fetch_price(ticker)
                if data:
                    arrow = "▲" if data["change_pct"] >= 0 else "▼"
                    market_lines.append(f"• {name}: {arrow} {abs(data['change_pct']):.1f}%")
                    market_changes.append(data["change_pct"])

            if market_changes:
                avg = sum(market_changes) / len(market_changes)
                sentiment = "positive" if avg > 0.3 else "negative" if avg < -0.3 else "mixed"
                sentiments.append(sentiment)
                market_lines.append(f"Sentiment: {sentiment.title()}")

            lines.append("\n".join(market_lines))

        # Overall flag
        if sentiments:
            overall = "broadly positive" if sentiments.count("positive") >= 2 else                       "broadly negative" if sentiments.count("negative") >= 2 else "mixed"
            lines.append(f"Overall: {overall}")

        msg = "Weekly Market Summary\n\n" + "\n\n---\n\n".join(lines)
        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)

    except Exception as e:
        print(f"Error sending weekly market summary: {e}")

def is_stock_request(text):
    """Detect stock market related requests using explicit trigger phrases only."""
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


MARKET_FLAGS = {
    "🇺🇸 US": "🇺🇸",
    "🇨🇳 China": "🇨🇳",
    "🇮🇳 India": "🇮🇳",
}

def fetch_market_rss_headlines(market_name):
    """Fetch top 3 market headlines from Google News RSS for a given market.
    Tries multiple query variants for better coverage."""
    try:
        queries = {
            "US": ["US stock market", "Wall Street stocks", "S&P 500 Nasdaq Dow"],
            "China": ["China stock market", "Shanghai Shenzhen stocks", "China economy markets"],
            "India": ["India stock market Nifty", "Sensex Nifty BSE", "India economy stocks"],
        }
        key = None
        for k in queries:
            if k in market_name:
                key = k
                break
        if not key:
            return []

        import xml.etree.ElementTree as ET
        headlines = []
        for q_text in queries[key]:
            if len(headlines) >= 3:
                break
            q = q_text.replace(" ", "+")
            url = f"https://news.google.com/rss/search?q={q}&hl=en-SG&gl=SG&ceid=SG:en"
            try:
                resp = requests.get(url, timeout=5)
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    if len(headlines) >= 3:
                        break
                    title = item.findtext("title", "").split(" - ")[0].strip()
                    if title and title not in headlines:
                        headlines.append(title)
            except Exception as e:
                print(f"RSS fetch error for {market_name} query '{q_text}': {e}")
                continue
        return headlines[:3]
    except Exception as e:
        print(f"fetch_market_rss_headlines error for {market_name}: {e}")
        return []

def get_market_summary_now():
    """Generate on-demand market summary with flag emoji, index data, RSS headlines, and Claude narrative."""
    try:
        market_data_blocks = []
        sentiments = []

        for market, indices in MARKET_INDICES.items():
            # Determine flag
            flag = ""
            for k, f in MARKET_FLAGS.items():
                if any(word in market for word in k.replace("🇺🇸 ", "").replace("🇨🇳 ", "").replace("🇮🇳 ", "").split()):
                    flag = f
                    break

            index_lines = []
            market_changes = []
            for ticker, name in indices.items():
                data = fetch_price(ticker)
                if data:
                    arrow = "▲" if data["change_pct"] >= 0 else "▼"
                    index_lines.append(f"  {name}: {arrow} {abs(data['change_pct']):.1f}%")
                    market_changes.append(data["change_pct"])

            avg = sum(market_changes) / len(market_changes) if market_changes else 0
            sentiment = "positive" if avg > 0.3 else "negative" if avg < -0.3 else "mixed"
            sentiments.append(sentiment)

            headlines = fetch_market_rss_headlines(market)

            block = {
                "market": market,
                "flag": flag,
                "index_lines": index_lines,
                "sentiment": sentiment,
                "avg_change": avg,
                "headlines": headlines,
            }
            market_data_blocks.append(block)

        # Build plain data summary for Claude
        data_summary = ""
        for b in market_data_blocks:
            data_summary += f"\n{b['flag']} {b['market']} — sentiment: {b['sentiment']}\n"
            data_summary += "\n".join(b["index_lines"]) + "\n"
            if b["headlines"]:
                data_summary += "Headlines: " + "; ".join(b["headlines"]) + "\n"

        overall = "broadly positive" if sentiments.count("positive") >= 2 else \
                  "broadly negative" if sentiments.count("negative") >= 2 else "mixed"

        # Claude narrative — 2-3 sentences, casual, punchy
        try:
            narrative_resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=150,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Here's today's market data:\n{data_summary}\n\n"
                        "Write a 2-3 sentence casual market narrative for a personal assistant bot. "
                        "Highlight the most notable move or story. No bullet points, no headers, just plain punchy sentences. "
                        "Don't start with 'Markets' — vary the opener."
                    )
                }]
            )
            narrative = narrative_resp.content[0].text.strip()
        except Exception as e:
            print(f"Market narrative Claude error: {e}")
            narrative = ""

        # Format output
        lines = [f"*Market Summary* — {date.today().strftime('%d %b %Y')}\n"]
        for b in market_data_blocks:
            section = f"{b['flag']} *{b['market']}*\n" + "\n".join(b["index_lines"])
            if b["headlines"]:
                # Show up to 3 headlines
                for hl in b["headlines"][:3]:
                    section += f"\n_{hl}_"
            lines.append(section)
        lines.append(f"Overall: {overall.title()}")
        if narrative:
            lines.append(f"\n{narrative}")

        return "\n\n".join(lines)

    except Exception as e:
        return f"❌ Couldn't pull market data right now ({type(e).__name__}: {str(e)[:80]})"


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

    try:
        await _handle_message_inner(update, context, user_id)
    except Exception as e:
        import traceback
        err_type = type(e).__name__
        err_msg = str(e)[:120]
        tb_lines = traceback.format_exc().splitlines()
        # Find the most relevant line (last line with our code)
        location = next((l.strip() for l in reversed(tb_lines) if "bot.py" in l), "")
        detail = f"{err_type}: {err_msg}"
        if location:
            detail += f"\n({location})"
        print(f"UNHANDLED handle_message error: {traceback.format_exc()}")
        try:
            await update.message.reply_text(f"❌ Something went wrong: {detail}")
        except Exception:
            pass

async def _handle_message_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
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

    # Handle photo messages — receipt with caption as expense
    if update.message.photo:
        caption = (update.message.caption or "").strip()
        photo = update.message.photo[-1]  # highest resolution
        tg_file = await photo.get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())

        # --- Drive upload: month subfolder + temp name, renamed after merchant known ---
        receipt_link = ""
        drive_file_id = ""
        today_obj = date.today()
        month_folder_name = today_obj.strftime("%Y-%m")
        today_str = today_obj.strftime("%Y-%m")

        try:
            from googleapiclient.http import MediaIoBaseUpload
            receipts_root = DRIVE_FOLDERS.get("receipts", "")
            if receipts_root:
                # Get or create month subfolder under Receipts
                month_folder_id = get_or_create_drive_folder(month_folder_name, receipts_root)
                # Upload with temp name — will rename once merchant is known
                temp_name = f"{today_str}-receipt-{photo.file_id[:8]}.jpg"
                media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
                file_meta = {"name": temp_name, "parents": [month_folder_id]}
                uploaded = drive_service.files().create(
                    body=file_meta, media_body=media, fields="id,webViewLink"
                ).execute()
                drive_file_id = uploaded.get("id", "")
                receipt_link = uploaded.get("webViewLink", "")
        except Exception as e:
            print(f"Receipt upload error: {e}")

        def rename_receipt_in_drive(merchant_name):
            """Rename the uploaded receipt file to YYYY-MM-merchant.jpg"""
            if not drive_file_id or not merchant_name:
                return
            try:
                safe_merchant = re.sub(r"[^a-zA-Z0-9\-_]", "", merchant_name.replace(" ", "-").lower())
                new_name = f"{today_str}-{safe_merchant}.jpg"
                drive_service.files().update(
                    fileId=drive_file_id,
                    body={"name": new_name}
                ).execute()
            except Exception as e:
                print(f"Receipt rename error: {e}")

        if not caption:
            # No caption — use Claude vision to read the receipt
            await update.message.reply_text("Got the receipt 🧾 Reading it now...")
            try:
                import base64
                img_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
                curr = overseas_state.get("currency", "SGD") if overseas_state.get("active") else "SGD"
                vision_resp = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=300,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
                            },
                            {
                                "type": "text",
                                "text": (
                                    f"This is a receipt. Extract: merchant name, total amount, and currency (default {curr} if not shown). "
                                    "Reply ONLY in this format with no other text: MERCHANT | AMOUNT | CURRENCY\n"
                                    "Example: Starbucks | 8.50 | SGD"
                                )
                            }
                        ]
                    }]
                )
                vision_text = vision_resp.content[0].text.strip()
                parts = [p.strip() for p in vision_text.split("|")]
                if len(parts) == 3:
                    merchant_v, amount_v, currency_v = parts
                    rename_receipt_in_drive(merchant_v)
                    synthesized = f"{merchant_v} {amount_v} {currency_v}"
                    reply, needs_session, session_data = handle_expense_text(synthesized, user_id, receipt_link=receipt_link)
                    if needs_session and session_data:
                        expense_sessions[user_id] = session_data
                    if reply:
                        try:
                            await update.message.reply_text(reply, parse_mode="Markdown")
                        except Exception:
                            await update.message.reply_text(reply)
                else:
                    await update.message.reply_text(f"Read the receipt but got an unexpected format from vision — got: '{vision_text}'\nTry adding a caption like '45.50 Ichiran'.")
            except Exception as e:
                print(f"Vision parse error: {e}")
                await update.message.reply_text(f"❌ Couldn't read the receipt automatically ({type(e).__name__}: {str(e)[:80]}). Add a caption like '45.50 Ichiran' and resend.")
            return

        if is_expense_input(caption):
            # Caption provided — extract merchant from caption to rename file
            parsed = parse_expense_text(caption)
            if parsed and parsed.get("merchant"):
                rename_receipt_in_drive(parsed["merchant"])
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

    text = update.message.text.strip()
    lower = text.lower()

    # Check birthday acknowledgement — explicit sent/skip only
    bday_handled, bday_reply = check_birthday_acknowledgement(text)
    if bday_handled:
        if bday_reply:
            await update.message.reply_text(bday_reply)
        return

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

    # Check session timeouts before processing any session
    if any(user_id in s for s in [expense_sessions, delete_sessions, portfolio_delete_sessions,
                                    confirm_sessions, receipt_confirm_sessions, edit_sessions, meeting_sessions]):
        if is_session_expired(user_id):
            await check_session_timeouts(user_id, update)
            return

    # Handle pending restaurant saves (location needed or duplicate)
    if user_id in pending_restaurant_saves:
        prs = pending_restaurant_saves[user_id]
        step = prs.get("step")
        if step == "awaiting_location":
            location = text.strip()
            del pending_restaurant_saves[user_id]
            result = save_restaurant(prs["name"], location, prs.get("country", "Singapore"))
            if result and result.startswith("_DUPLICATE_:"):
                pending_restaurant_saves[user_id] = {
                    "step": "duplicate", "existing": prs["name"],
                    "name": prs["name"], "location": location,
                    "country": prs.get("country", "Singapore"), "tags": "", "notes": ""
                }
                await update.message.reply_text(f"*{prs['name']}* is already in your list. Update or save as new? (update / new)")
            else:
                await update.message.reply_text(format_restaurant_saved(prs["name"], location))
            return
        elif step == "duplicate":
            if lower in ["new", "save new"]:
                del pending_restaurant_saves[user_id]
                save_restaurant(prs["name"], prs["location"], prs.get("country", "Singapore"),
                                prs.get("tags", ""), prs.get("notes", ""), force_new=True)
                await update.message.reply_text(format_restaurant_saved(prs["name"], prs["location"]))
            elif lower in ["update", "update existing"]:
                del pending_restaurant_saves[user_id]
                await update.message.reply_text(f"Use 'edit restaurant {prs['existing']}' to update it.")
            else:
                await update.message.reply_text("Reply 'update' or 'new'.")
            return

    # Handle pending duplicate contact save
    if user_id in pending_contact_saves:
        pcs = pending_contact_saves[user_id]
        if lower in ["new", "save new", "save as new"]:
            del pending_contact_saves[user_id]
            reply = save_contact(pcs["data"], force_new=True)
            await update.message.reply_text(reply)
        elif lower in ["update", "update existing"]:
            del pending_contact_saves[user_id]
            reply = f"Opening {pcs['existing_name']} for editing — use 'edit {pcs['existing_name']}' to update fields."
            await update.message.reply_text(reply)
        else:
            await update.message.reply_text(f"*{pcs['existing_name']}* already exists. Reply 'update' or 'new'.")
        return

    # Handle todo disambiguation
    if user_id in todo_disambig_sessions:
        td = todo_disambig_sessions[user_id]
        tasks = td.get("tasks", [])
        action = td.get("action", "complete")
        if text.strip().isdigit():
            idx = int(text.strip()) - 1
            if 0 <= idx < len(tasks):
                task_name = tasks[idx]
                del todo_disambig_sessions[user_id]
                if action == "complete":
                    sheet = todo_sheet()
                    records = sheet.get_all_records()
                    for i, r in enumerate(records):
                        if r.get("Task", "") == task_name and r.get("Status") == "Pending":
                            sheet.update_cell(i + 2, 2, "Done")
                            await update.message.reply_text(f"✅ Marked as done: _{task_name}_")
                            return
                elif action == "delete":
                    sheet = todo_sheet()
                    records = sheet.get_all_records()
                    for i, r in enumerate(records):
                        if r.get("Task", "") == task_name:
                            sheet.delete_rows(i + 2)
                            await update.message.reply_text(f"Deleted — {task_name} ✅")
                            return
            else:
                await update.message.reply_text("Invalid number. Try again or 'cancel'.")
        elif lower == "cancel":
            del todo_disambig_sessions[user_id]
            await update.message.reply_text("Cancelled.")
        else:
            await update.message.reply_text("Reply with a number or 'cancel'.")
        return

    # Handle active receipt confirm session
    if user_id in receipt_confirm_sessions:
        touch_session(user_id)
        await handle_receipt_confirm_session(user_id, text, update)
        return

    # Handle active meeting recap session
    if user_id in meeting_sessions:
        touch_session(user_id)
        await handle_meeting_session(user_id, text, update)
        return

    # Handle active expense onboarding session
    if user_id in expense_sessions:
        touch_session(user_id)
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

    # Handle active confirm session (destructive actions: delete contact/bill/restaurant/event, rename category)
    if user_id in confirm_sessions:
        touch_session(user_id)
        cs = confirm_sessions[user_id]
        action = cs.get("action")
        args = cs.get("args", [])
        if lower in ["yes", "y"]:
            del confirm_sessions[user_id]
            session_timestamps.pop(user_id, None)
            if action == "delete_contact":
                reply = delete_contact(args[0])
            elif action == "rename_category":
                reply = rename_category(args[0], args[1])
            elif action == "delete_bill":
                reply = delete_bill(args[0])
            elif action == "delete_restaurant":
                reply = delete_restaurant(args[0])
            elif action == "delete_event":
                reply = delete_calendar_event(args[0])
            else:
                reply = "Done."
            await update.message.reply_text(reply)
        elif lower in ["no", "n", "cancel"]:
            del confirm_sessions[user_id]
            session_timestamps.pop(user_id, None)
            await update.message.reply_text("Cancelled.")
        else:
            await update.message.reply_text(f"{cs.get('target', 'Confirm?')} (yes / no)")
        return

    # Handle active portfolio delete session
    if user_id in portfolio_delete_sessions:
        touch_session(user_id)
        pd_session = portfolio_delete_sessions[user_id]
        if lower in ["cancel", "nevermind", "nvm"]:
            del portfolio_delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return
        if text.strip().isdigit():
            idx = int(text.strip()) - 1
            rows = pd_session.get("rows", [])
            if 0 <= idx < len(rows):
                sheet_row, _ = rows[idx]
                del portfolio_delete_sessions[user_id]
                result = delete_portfolio_row(sheet_row)
                await update.message.reply_text(result)
            else:
                await update.message.reply_text("Invalid number. Try again or 'cancel'.")
            return
        elif lower.startswith("search "):
            query = text[7:].strip()
            results = search_portfolio_by_ticker(query)
            if not results:
                await update.message.reply_text(f"No holdings found matching '{query}'.")
            elif len(results) == 1:
                sheet_row, _ = results[0]
                del portfolio_delete_sessions[user_id]
                result = delete_portfolio_row(sheet_row)
                await update.message.reply_text(result)
            else:
                portfolio_delete_sessions[user_id] = {"step": "pick", "rows": results}
                await update.message.reply_text(format_portfolio_delete_list(results))
            return
        else:
            await update.message.reply_text("Reply with a number, 'search [ticker]', or 'cancel'.")
            return

    # Handle active CRM edit session
    if user_id in edit_sessions:
        await handle_edit_session(user_id, text, update)
        return

    # Pending flight confirmation — intercept the next message entirely
    if overseas_state.get("_pending_flight"):
        pf = overseas_state["_pending_flight"]

        # Waiting for return flight number after single-flight input
        if overseas_state.get("_awaiting_return_flight"):
            stripped = text.strip()
            lower_stripped = stripped.lower()
            if lower_stripped in ["no", "n", "not yet", "skip", "none"]:
                overseas_state.pop("_awaiting_return_flight", None)
                # Proceed with just outbound — fall through to Y handling below
                text = "Y"
            elif extract_flight_number(stripped):
                overseas_state.pop("_awaiting_return_flight", None)
                ret_num = extract_flight_number(stripped)
                ret_data = lookup_flight(ret_num)
                if ret_data:
                    ret_dep = format_flight_time(ret_data["dep_time"])
                    ret_arr = format_flight_time(ret_data["arr_time"])
                    ret_data["flight"] = ret_num
                    pf["return_flight_data"] = ret_data
                    overseas_state["_pending_flight"] = pf
                    arr_label = pf.get("arr_city") or pf.get("arr_airport") or pf.get("arr_iata", "")
                    dep_fmt = format_flight_time(pf.get("dep_time", ""))
                    arr_fmt = format_flight_time(pf.get("arr_time", ""))
                    reply = (
                        f"Got it ✈️\n"
                        f"Outbound: {pf['flight_number']} {dep_fmt} → {arr_fmt} ({arr_label})\n"
                        f"Return: {ret_num} {ret_dep} → {ret_arr} (SIN)\n"
                        f"\nReply Y to confirm."
                    )
                else:
                    reply = f"Couldn't find {ret_num} on AviationStack. Reply Y to log just the departure, or try another flight number."
                if reply:
                    try:
                        await update.message.reply_text(reply, parse_mode="Markdown")
                    except Exception:
                        await update.message.reply_text(reply)
                return
            else:
                reply = "Reply with a return flight number (e.g. OD805) or 'no' to log just the departure."
                await update.message.reply_text(reply)
                return

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
            if scheduled:
                ret_str = ""
                if return_flight_data:
                    ret_dep = format_flight_time(return_flight_data.get("dep_time", ""))
                    ret_arr = format_flight_time(return_flight_data.get("arr_time", ""))
                    ret_str = f"\nReturn: {return_flight_data.get('flight', '')} {ret_dep} → {ret_arr}"
                reply = (
                    f"Got it ✈️ Overseas mode will activate at departure: {dep_fmt}\n"
                    f"Destination: {dest} ({curr}){ret_str}\n"
                    f"I'll send a confirmation when it kicks in."
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
                reply = (
                    f"Overseas mode on ✈️\n"
                    f"Destination: {dest}\nCurrency: {curr}\n"
                    f"I'll log expenses in {curr} with SGD equivalent."
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
        result = save_contact(text[5:])
        if result.startswith("_DUPLICATE_:"):
            existing_name = result.split(":", 1)[1]
            pending_contact_saves[user_id] = {"data": text[5:], "existing_name": existing_name}
            reply = f"*{existing_name}* already exists in your CRM.\nUpdate the existing contact or save as new? (update / new)"
        else:
            reply = result
    elif lower.startswith("find ") or lower.startswith("pull up "):
        name = text[5:] if lower.startswith("find ") else text[8:]
        reply = find_contact(name)
    elif lower.startswith("note "):
        reply = add_note(text[5:])
    elif lower.startswith("followup "):
        reply = set_followup(text[9:])
    elif lower.startswith("update "):
        reply = update_field(text[7:])
    elif lower.startswith("delete ") and not lower.startswith("delete event") and not lower.startswith("delete expense") and not lower.startswith("delete bill") and not lower.startswith("delete restaurant") and not lower.startswith("delete last"):
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
    elif lower.startswith("edit ") and not lower.startswith("edit last expense") and not lower.startswith("edit expense"):
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
        if user_id in receipt_confirm_sessions:
            del receipt_confirm_sessions[user_id]
        session_timestamps.pop(user_id, None)
        reply = "Cancelled."

    # Profile
    elif lower == "reload profile":
        try:
            setup_em_profile()
            reply = "✅ Profile reloaded successfully."
        except Exception as e:
            reply = f"⚠️ Couldn't reload profile: {str(e)}"

    # Missed items dismissal
    elif lower in ["skip missed", "dismiss missed", "skip all missed"]:
        reply = "All missed follow-ups dismissed ✅"
    elif lower.startswith("skip ") and not lower.startswith("skip missed bills"):
        name = text[5:].strip()
        reply = f"Follow-up for {name} dismissed ✅"
    elif lower in ["skip missed bills", "dismiss missed bills"]:
        reply = "Missed bill reminders dismissed ✅"

    # Missed market summary response
    elif lower in ["yes", "y"] and market_summary_pending.get(user_id):
        market_summary_pending.pop(user_id, None)
        reply = get_market_summary_now()

    # Card defaults
    elif re.match(r"set default card for .+ to .+", lower):
        m = re.match(r"set default card for (.+?) to (.+)", lower)
        if m:
            category_name = m.group(1).strip().title()
            card_name = m.group(2).strip().title()
            reply = set_card_default_category(card_name, category_name)
        else:
            reply = "Try: 'set default card for FnB to Maybank'"


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
        if not cancelled:
            reply = "Couldn't find a matching reminder to cancel."
        elif cancelled[0].startswith("_DISAMBIG_:"):
            entries = cancelled[0][len("_DISAMBIG_:"):].split("|")
            lines = ["Found multiple matching reminders — which one to cancel?"]
            for i, entry in enumerate(entries, 1):
                parts = entry.split(":", 1)
                msg = parts[1] if len(parts) > 1 else entry
                lines.append(f"{i}. {msg}")
            lines.append("\nReply with the number or 'all' to cancel all.")
            todo_disambig_sessions[user_id] = {"tasks": [e.split(":")[0] for e in entries], "action": "cancel_reminder", "entries": entries}
            reply = "\n".join(lines)
        else:
            reply = f"Cancelled: {', '.join(cancelled)}"
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
    elif any(lower == p or lower.startswith(p) for p in [
        "what merchants", "my merchants", "merchant map", "list merchants",
        "show merchants", "what merchants do we have", "saved merchants",
        "known merchants"
    ]):
        reply = get_merchant_list()
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
    elif lower in ["trip history", "my trips", "past trips", "trips"]:
        reply = format_trip_history()
    elif lower in ["current trip", "active trip", "am i overseas", "overseas status"]:
        if overseas_state.get("active"):
            dest = overseas_state.get("destination", "Unknown")
            curr = overseas_state.get("currency", "SGD")
            dep = overseas_state.get("dep_flight", "")
            ret_data = overseas_state.get("return_flight")
            ret_flight = ret_data.get("flight", "") if isinstance(ret_data, dict) else ""
            reply = f"✈️ Active trip: {dest} ({curr})"
            if dep:
                reply += f"\nOutbound: {dep}"
            if ret_flight:
                reply += f"\nReturn: {ret_flight}"
        else:
            reply = "No active trip — you're in SG mode."
    elif lower.startswith("edit last expense ") or lower.startswith("edit expense "):
        # Strip the command prefix and pass the rest as the edit text
        edit_text = re.sub(r"^edit (?:last )?expense\s+", "", text, flags=re.IGNORECASE).strip()
        if edit_text:
            reply = edit_last_expense(edit_text)
        else:
            reply = show_last_expense()
    elif lower.startswith("rename category "):
        m = re.search(r"rename category (.+?) to (.+)", lower)
        if m:
            old_cat = m.group(1).strip()
            new_cat = m.group(2).strip()
            confirm_sessions[user_id] = {"action": "rename_category", "args": [old_cat, new_cat], "target": f"Rename {old_cat} to {new_cat}?"}
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
    elif re.match(r"add return flight\s+([A-Z]{1,3}\d{2,4}[A-Z]?)", text.upper()):
        m = re.match(r"add return flight\s+([A-Z]{1,3}\d{2,4}[A-Z]?)", text.upper())
        ret_num = m.group(1)
        ret_data = lookup_flight(ret_num)
        if ret_data:
            ret_dep = format_flight_time(ret_data["dep_time"])
            ret_arr = format_flight_time(ret_data["arr_time"])
            ret_data["flight"] = ret_num
            overseas_state["return_flight"] = ret_data
            reply = f"Return flight added: {ret_num} {ret_dep} → {ret_arr} (SIN) ✅"
        else:
            reply = f"Couldn't find {ret_num} on AviationStack. Try again closer to the flight date."
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
        result = handle_save_restaurant(text)
        if result.startswith("_NEEDS_LOCATION_:"):
            parts = result.split(":", 2)
            name = parts[1] if len(parts) > 1 else ""
            country = parts[2] if len(parts) > 2 else "Singapore"
            pending_restaurant_saves[user_id] = {"name": name, "country": country, "step": "awaiting_location"}
            reply = f"⚠️ Couldn't read that Maps link — what's the location for {name}?\n(e.g. 313 Orchard Road, Singapore)"
        elif result.startswith("_DUPLICATE_RESTAURANT_:"):
            parts = result.split(":", 5)
            pending_restaurant_saves[user_id] = {
                "step": "duplicate", "existing": parts[1], "name": parts[2],
                "location": parts[3], "country": parts[4],
                "tags": parts[5] if len(parts) > 5 else "", "notes": ""
            }
            reply = f"*{parts[1]}* is already in your list.\nUpdate existing or save as new? (update / new)"
        else:
            reply = result
    elif lower.startswith("search restaurants "):
        reply = search_restaurants(text[19:].strip())

    # Stock Commands
    elif lower in ["portfolio", "my portfolio", "holdings", "portfolio performance"]:
        reply = get_portfolio_performance()
    elif lower in ["delete from portfolio", "remove from portfolio", "delete holding", "remove holding",
                   "portfolio delete", "clear holding"]:
        rows = get_portfolio_rows()
        if not rows:
            reply = "Nothing in your portfolio to remove."
        else:
            portfolio_delete_sessions[user_id] = {"step": "pick", "rows": rows}
            reply = format_portfolio_delete_list(rows)
    elif lower.startswith("delete portfolio ") or lower.startswith("remove portfolio "):
        query = re.sub(r"^(delete|remove) portfolio\s+", "", lower).strip().upper()
        results = search_portfolio_by_ticker(query)
        if not results:
            reply = f"No holdings found matching '{query}'."
        elif len(results) == 1:
            sheet_row, _ = results[0]
            reply = delete_portfolio_row(sheet_row)
        else:
            portfolio_delete_sessions[user_id] = {"step": "pick", "rows": results}
            reply = format_portfolio_delete_list(results)
    elif lower in ["market summary", "market today", "how is the market", "market update",
                   "weekly market summary", "how are markets"]:
        reply = get_market_summary_now()
    elif is_stock_request(text):
        result = handle_stock_request(text)
        if result:
            reply = result

    # Todo Commands
    elif lower.startswith("todo "):
        reply = add_todo(text[5:])
    elif lower.startswith("done "):
        result = complete_todo(text[5:])
        if result.startswith("_DISAMBIG_TODO_COMPLETE_:"):
            tasks = result.split(":", 1)[1].split("|")
            todo_disambig_sessions[user_id] = {"tasks": tasks, "action": "complete"}
            lines = ["Found multiple matching tasks — which one?"]
            for i, t in enumerate(tasks, 1):
                lines.append(f"{i}. {t}")
            lines.append("\nReply with the number.")
            reply = "\n".join(lines)
        else:
            reply = result
    elif lower.startswith("delete todo ") or lower.startswith("remove todo "):
        task_name = re.sub(r"^(delete|remove) todo\s+", "", lower).strip()
        result = delete_todo(task_name)
        if result.startswith("_DISAMBIG_TODO_DELETE_:"):
            tasks = result.split(":", 1)[1].split("|")
            todo_disambig_sessions[user_id] = {"tasks": tasks, "action": "delete"}
            lines = ["Found multiple matching tasks — which one?"]
            for i, t in enumerate(tasks, 1):
                lines.append(f"{i}. {t}")
            lines.append("\nReply with the number.")
            reply = "\n".join(lines)
        else:
            reply = result
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
        issues = []
        # Google Drive
        if not DRIVE_FOLDERS:
            issues.append("• Google Drive: not connected — check GOOGLE_CREDENTIALS in Railway")
        # Sheets
        try:
            spreadsheet.worksheet("Expenses")
        except Exception as e:
            issues.append(f"• Sheets: connection error — {str(e)[:60]}")
        # Scheduler
        if not _scheduler or not _scheduler.running:
            issues.append("• Scheduler: not running — reminders and scheduled jobs are down, restart the bot")
        # Profile
        if not em_profile or not em_profile.get("version"):
            issues.append("• Profile: not loaded — reply 'reload profile' to retry")
        # iCloud
        try:
            get_calendar("Personal")
        except Exception as e:
            issues.append(f"• iCloud Calendar: unreachable — check ICLOUD_USERNAME / ICLOUD_PASSWORD in Railway")
        # Anthropic API
        if _anthropic_failure_count >= ANTHROPIC_FAILURE_THRESHOLD:
            issues.append("• Anthropic API: repeated failures detected — check API key or Anthropic status page")

        if not issues:
            reply = "✅ Systems all green"
        else:
            lines = ["⚠️ Issues detected:\n"] + issues
            reply = "\n".join(lines)

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
            result = handle_save_restaurant(text)
            if result and result.startswith("_NEEDS_LOCATION_:"):
                parts = result.split(":", 2)
                name = parts[1] if len(parts) > 1 else ""
                country = parts[2] if len(parts) > 2 else "Singapore"
                pending_restaurant_saves[user_id] = {"name": name, "country": country, "step": "awaiting_location"}
                reply = f"⚠️ Couldn't read that Maps link — what's the location for {name}?\n(e.g. 313 Orchard Road, Singapore)"
            elif result and result.startswith("_DUPLICATE_RESTAURANT_:"):
                parts = result.split(":", 5)
                pending_restaurant_saves[user_id] = {
                    "step": "duplicate", "existing": parts[1], "name": parts[2],
                    "location": parts[3], "country": parts[4],
                    "tags": parts[5] if len(parts) > 5 else "", "notes": ""
                }
                reply = f"*{parts[1]}* is already in your list.\nUpdate existing or save as new? (update / new)"
            else:
                reply = result
        elif is_restaurant_search(text):
            reply = handle_search_restaurants(text)
        elif is_bill_request(text):
            reply = handle_new_bill(text)
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

async def check_missed_items_on_startup(app):
    """Check for missed follow-ups, bill reminders, reminders, and market summary while offline."""
    try:
        today = date.today()
        yesterday = today - timedelta(days=1)
        missed_followups = []
        missed_bills = []
        missed_reminders = []

        # Missed follow-ups
        try:
            sheet = crm_sheet()
            records = sheet.get_all_records()
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

        # Missed bill reminders
        try:
            ws = bills_sheet()
            records = ws.get_all_records()
            for r in records:
                due_day = int(r.get("Due Date", 0) or 0)
                if due_day and yesterday.day == due_day:
                    missed_bills.append(r.get("Name", "?"))
        except Exception as e:
            print(f"Missed bills check error: {e}")

        # Missed reminders
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

        # Send missed follow-ups notification
        if missed_followups:
            lines = ["⚠️ Missed follow-ups while offline:"]
            for f in missed_followups:
                lines.append(f"• {f['name']} — was due {f['date']}")
            lines.append("\nReply 'followups' for details, 'skip missed' to dismiss all, or 'skip [name]' for one.")
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text="\n".join(lines))

        # Send missed bills notification
        if missed_bills:
            lines = ["⚠️ Missed bill reminder(s) while offline:"]
            for b in missed_bills:
                lines.append(f"• {b}")
            lines.append("\nReply 'skip missed bills' to dismiss.")
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text="\n".join(lines))

        # Fire missed reminders immediately
        for rem in missed_reminders:
            try:
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🔔 Reminder (missed while offline): {rem['message']}"
                )
                # Update recurrence or mark sent
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

        # Check missed Monday market summary
        try:
            if today.weekday() == 0:  # Monday
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text="Missed the Monday market summary while offline — want me to send it now? (yes / no)"
                )
                market_summary_pending[YOUR_CHAT_ID] = True
        except Exception as e:
            print(f"Missed market summary check error: {e}")

    except Exception as e:
        print(f"check_missed_items_on_startup error: {e}")

async def post_init(app):
    global _scheduler, _app_ref
    # Run infrastructure setup and capture health status
    health = run_infrastructure_setup()

    # Restore overseas mode if there was an active trip before restart
    restore_overseas_from_trips()

    timezone = pytz.timezone("Asia/Kuala_Lumpur")
    scheduler = AsyncIOScheduler(timezone=timezone, misfire_grace_time=30)
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
    scheduler.add_job(check_icloud_daily, "cron", hour=9, minute=5, args=[app])

    # Price alerts — check every 15 minutes
    scheduler.add_job(check_price_alerts, "interval", minutes=15, args=[app])

    # Weekly market summary — Monday 8am
    scheduler.add_job(send_weekly_market_summary, "cron", day_of_week="mon", hour=8, minute=0, args=[app])

    # FX rate refresh — 8am and 8pm daily
    scheduler.add_job(refresh_fx_rates, "cron", hour=8, minute=0)
    scheduler.add_job(refresh_fx_rates, "cron", hour=20, minute=0)

    scheduler.start()
    health["Scheduler"] = "✅ Running"
    print("✅ Scheduler started — follow-ups + bills at 9am, birthdays at 12pm + 2pm, reminders every minute, market Monday 8am")

    # Send startup message if new deployment
    await send_startup_message(app, health)

    # Check for missed items while offline
    await check_missed_items_on_startup(app)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    print("Em is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
