import os
import json
import pytz
from dotenv import load_dotenv

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
RECEIPTS_FOLDER_ID = os.getenv("RECEIPTS_FOLDER_ID", "14pG1lNPANRwehiW_xSFjoHzt-AUk5-Xb")

# --- Google Sheets Scopes ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# --- Timezone ---
TIMEZONE = pytz.timezone("Asia/Kuala_Lumpur")

# --- Expense Constants ---
EXPENSE_CATEGORIES = ["FnB", "Entertainment", "Personal", "Family", "Work", "Transport", "Shopping", "Travel"]
EXPENSE_CARDS = ["Citi", "Maybank", "Amex", "UOB"]  # fallback only — live list read from Cards sheet

# Cards sheet schema
CARDS_SCHEMA = ["Card Name", "Last 4", "Default Category", "Notes"]
INITIAL_CARDS = [
    ["Maybank", "4002", "FnB", ""],
    ["Citi", "1176", "General", ""],
    ["UOB", "5372", "", ""],
    ["Amex", "1008", "", ""],
]

# --- Stock Market Constants ---
MARKET_INDICES = {
    "US": {"^GSPC": "S&P 500"},
    "China": {"000001.SS": "Shanghai"},
    "India": {"^NSEI": "Nifty 50"},
}

MARKET_FLAGS_MAP = {
    "US": "🇺🇸",
    "China": "🇨🇳",
    "India": "🇮🇳",
}

SGX_TICKER_MAP = {
    "dbs": "D05.SI", "d05": "D05.SI",
    "ocbc": "O39.SI", "o39": "O39.SI",
    "uob": "U11.SI", "u11": "U11.SI",
    "singtel": "Z74.SI", "z74": "Z74.SI",
    "capitaland": "9CI.SI", "capitaland investment": "9CI.SI", "9ci": "9CI.SI", "cli": "9CI.SI",
    "keppel": "BN4.SI", "bn4": "BN4.SI",
    "wilmar": "F34.SI", "f34": "F34.SI",
    "sia": "C6L.SI", "singapore airlines": "C6L.SI", "c6l": "C6L.SI",
    "jardine": "J36.SI",
    "thai bev": "Y92.SI", "thaibev": "Y92.SI",
}

HK_TICKER_MAP = {
    "tencent": "0700.HK", "0700": "0700.HK", "TENCENT": "0700.HK",
    "alibaba": "9988.HK", "9988": "9988.HK", "ALIBABA": "9988.HK",
    "meituan": "3690.HK", "3690": "3690.HK", "MEITUAN": "3690.HK",
    "hsbc": "0005.HK", "0005": "0005.HK", "HSBC": "0005.HK",
    "aia": "1299.HK", "1299": "1299.HK", "AIA": "1299.HK",
    "byd": "1211.HK", "1211": "1211.HK", "BYD": "1211.HK",
    "xiaomi": "1810.HK", "1810": "1810.HK", "XIAOMI": "1810.HK",
    "jd": "9618.HK", "9618": "9618.HK", "JD": "9618.HK",
    "netease": "9999.HK", "9999": "9999.HK", "NETEASE": "9999.HK",
    "cnooc": "0883.HK", "0883": "0883.HK", "CNOOC": "0883.HK",
}

# --- Anthropic Health Tracking ---
_anthropic_failure_count = 0
_anthropic_down_notified = False
ANTHROPIC_FAILURE_THRESHOLD = 3

# --- iCloud Health Tracking ---
_icloud_down = False
_icloud_last_notified = None

# --- Manual FX Rate Cache ---
# { currency: { "rate": float, "date": str, "sgd_per_unit": bool } }
manual_fx_rates = {}

# --- Session Timestamps ---
# { user_id: datetime }
session_timestamps = {}

# --- Drive Folder IDs (populated during setup) ---
DRIVE_FOLDERS = {}

# --- Em Profile (loaded at startup) ---
em_profile = {}

# --- Global Scheduler Reference (set in post_init) ---
_scheduler = None
_app_ref = None

# --- Overseas Mode State ---
overseas_state = {
    "active": False,
    "destination": "",
    "currency": "SGD",
    "currencies": [],
    "return_date": "",
    "dep_job_id": None,
    "return_job_id": None,
    "return_flight": None,
    "trip_start": None,
    "trip_destinations": [],
}

# --- Session State Dicts ---
expense_sessions = {}
delete_sessions = {}
portfolio_delete_sessions = {}
confirm_sessions = {}
receipt_confirm_sessions = {}
todo_disambig_sessions = {}
market_summary_pending = {}
interrupted_sessions = {}
recon_sessions = {}
meeting_sessions = {}
birthday_pending = {}
last_fired_reminder = {}
excel_import_sessions = {}
pending_contact_saves = {}
pending_restaurant_saves = {}
edit_sessions = {}
price_alerts = {}

# --- Session Timeout ---
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

# --- Caches ---
_merchant_cache = None
_card_names_cache = None
_system_prompt_cache = None
_system_prompt_overseas_key = None
_crm_cache = None
_pending_reminders_cache = None
cached_fx_rates = {}
conversation_histories = {}
