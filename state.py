from config import TIMEZONE

# --- Session dicts ---
expense_sessions = {}
delete_sessions = {}
portfolio_delete_sessions = {}
confirm_sessions = {}
receipt_confirm_sessions = {}
edit_sessions = {}
meeting_sessions = {}
excel_import_sessions = {}
recon_sessions = {}
interrupted_sessions = {}
todo_disambig_sessions = {}
market_summary_pending = {}
session_timestamps = {}
calendar_confirm_sessions = {}

# --- Pending saves ---
pending_contact_saves = {}
pending_restaurant_saves = {}

# --- Profile + feature state ---
em_profile = {}
overseas_state = {
    "active": False,
    "destination": "",
    "currency": "SGD",
    "currencies": [],
    "return_date": "",
    "dep_job_id": None,
    "return_job_id": None,
    "trip_start": None,
    "trip_destinations": [],
}

conversation_histories = {}
birthday_pending = {}
price_alerts = {}
manual_fx_rates = {}
cached_fx_rates = {}

# --- API health flags ---
_anthropic_failure_count = 0
_anthropic_down_notified = False
_icloud_down = False
_icloud_last_notified = None

# --- Caches ---
_crm_cache = None
_crm_cache_ts = None
_CRM_CACHE_TTL = 300  # seconds

_merchant_cache = None
_merchant_cache_ts = None
_MERCHANT_CACHE_TTL = 300

_card_names_cache = None
_system_prompt_cache = None
_system_prompt_overseas_key = None

_pending_reminders_cache = None
_pending_reminders_cache_ts = None

# In-memory same-day expense dedup
_same_day_expense_cache: set = set()
_same_day_expense_cache_date: str = ""

# Category emoji inference cache
_category_emoji_cache = {}

# Last fired reminder per user (for reschedule)
last_fired_reminder = {}

# --- Scheduler + app ref ---
_scheduler = None
_app_ref = None

# --- Drive folders ---
DRIVE_FOLDERS = {}
RECEIPTS_FOLDER_ID = ""

# --- Calendar last added ---
calendar_last_added = {}
