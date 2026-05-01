# S22 Modularisation Plan
_Based on full dependency analysis of bot.py (8,571 lines)_

---

## The Core Problem: Circular Import Prevention

The golden rule: **imports flow in one direction only — lower layers never import from higher layers.**

```
config.py
    ↓
clients.py   (imports config)
    ↓
state.py     (imports config only — zero sheet/client deps)
    ↓
sheets.py    (imports config, clients)
    ↓
helpers.py   (imports config only — pure functions)
    ↓
feature modules  (import sheets, state, helpers, clients)
    ↓
routing.py   (imports all feature modules)
    ↓
bot.py       (entry point — imports routing, scheduler)
```

Nothing at a lower layer ever imports from a higher layer. If you find yourself needing to — that's a signal the function belongs in the lower layer, or state needs to be passed as a parameter.

---

## Module Definitions (12 files + entry point)

### Layer 0 — `config.py` (~80 lines)
**What goes here:** All env vars, constants, no imports from other modules.

```python
# Everything currently at lines 19–46 plus all constants scattered through the file:
TELEGRAM_TOKEN, ANTHROPIC_API_KEY, SHEET_ID, ICLOUD_USERNAME, ICLOUD_PASSWORD
AVIATIONSTACK_API_KEY, EXCHANGE_RATE_API_KEY, ALPHA_VANTAGE_API_KEY
YOUR_CHAT_ID, RAILWAY_DEPLOYMENT_ID, TIMEZONE
EXPENSE_CATEGORIES, CARDS_SCHEMA, INITIAL_CARDS
SGD_HIGH_AMOUNT_THRESHOLD, SESSION_TIMEOUT_MINUTES, SESSION_TIMEOUT_MESSAGES
EXPENSE_CATEGORY_EMOJI, EXPENSE_MERCHANT_OVERRIDES
LOCATION_CONTEXT_WORDS, QUESTION_WORDS, COMMAND_PREFIXES
MARKET_INDICES, MARKET_FLAGS_MAP, SGX_TICKER_MAP, HK_TICKER_MAP
SOURCE_LABELS, _OBSCURE_SOURCES, _HEADLINE_REJECT
BILL_REMINDER_GREETINGS, EDIT_FIELD_SYNONYMS, MEETING_START_PHRASES, MEETING_DONE_PHRASES
DATE_FORMATS, ANTHROPIC_FAILURE_THRESHOLD
DEV_NOTES_CONTENT, EM_LOG_HEADERS_BACKLOG, EM_LOG_HEADERS_SESSION
INITIAL_BACKLOG, INITIAL_SESSION
```

**Imports:** `os`, `pytz`, `dotenv` only. Zero project imports.

---

### Layer 1a — `clients.py` (~60 lines)
**What goes here:** Google Sheets client, Drive service, Anthropic client — all singletons.

```python
# Google creds setup (lines 49–62)
SCOPES, gc, spreadsheet, drive_service
# Anthropic client (line 757)
client = Anthropic(api_key=ANTHROPIC_API_KEY)
```

**Imports:** `config` only. Zero project imports from higher layers.

---

### Layer 1b — `state.py` (~60 lines)
**What goes here:** All mutable in-memory dicts and flags. No logic — just declarations.

```python
# All session dicts
expense_sessions, receipt_confirm_sessions, delete_sessions, confirm_sessions
meeting_sessions, edit_sessions, todo_disambig_sessions, portfolio_delete_sessions
pending_contact_saves, pending_restaurant_saves, excel_import_sessions
recon_sessions, interrupted_sessions, market_summary_pending
session_timestamps

# Profile + feature state
em_profile = {}
overseas_state = {"active": False, "destination": "", "currency": "SGD", ...}
conversation_histories = {}
birthday_pending = {}
price_alerts = {}
manual_fx_rates = {}
cached_fx_rates = {}

# API health flags
_anthropic_failure_count, _anthropic_down_notified
_icloud_down, _icloud_last_notified

# Caches (declared here, invalidated at write sites)
_crm_cache, _crm_cache_ts
_merchant_cache, _merchant_cache_ts
_card_names_cache
_system_prompt_cache, _system_prompt_overseas_key
_pending_reminders_cache, _pending_reminders_cache_ts
_scheduler, _app_ref
DRIVE_FOLDERS, RECEIPTS_FOLDER_ID
```

**Imports:** `config` only. Zero sheet or client imports.

**Why state is separate from config:** Config is immutable constants. State mutates at runtime. Keeping them separate means feature modules can import state without pulling in the sheet client, and vice versa.

---

### Layer 2 — `sheets.py` (~600 lines)
**What goes here:** All sheet accessors, setup functions, Em Log management.

```python
# Sheet accessors
get_sheet(), crm_sheet(), todo_sheet(), expenses_sheet(), merchant_map_sheet()
reminders_sheet(), cards_sheet(), bills_sheet(), restaurants_sheet()
portfolio_sheet(), trips_sheet(), em_log_sheet()

# Setup
setup_sheets(), _setup_dev_notes(), _setup_em_log(), _write_em_log_fresh()
_migrate_backlog_status(), _migrate_crm_headers(), reconcile_backlog_status()

# Em Log operations
log_error_to_em_log(), add_session_to_em_log(), add_backlog_item()
get_pending_backlog()

# Retry wrapper
sheets_call_with_retry()
```

**Imports:** `config`, `clients`, `state` (for `em_profile` in save_em_profile — see note below).

**Note:** `save_em_profile` and `setup_em_profile` both touch `spreadsheet` and `em_profile`. They go in `sheets.py` and import `state.em_profile` directly — this is safe because state → sheets is the correct direction (sheets imports state, not the other way around). Wait — sheets is Layer 2, state is Layer 1b. sheets CAN import state. ✅

---

### Layer 3 — `helpers.py` (~120 lines)
**What goes here:** Pure utility functions with no sheet or state dependencies.

```python
format_date(), calculate_age(), format_contact()
parse_date_flexible(), generate_reminder_id()
generate_trip_id(), get_next_recurrence()
format_price(), get_source_label()
_looks_like_command(), looks_like_new_intent()
send_safe()  # only dep is telegram Update — acceptable
```

**Imports:** `config` only. No sheets, no state, no client.

---

### Layer 4 — Feature Modules

Each feature module imports from: `config`, `clients`, `state`, `sheets`, `helpers`. Feature modules **never import from each other** — the one exception is noted below.

#### `crm.py` (~650 lines)
```python
_get_crm_records(), _invalidate_crm_cache(), find_row(), find_all_rows()
disambiguate_contacts(), save_contact(), find_contact(), add_note()
set_followup(), update_field(), update_contact_field_natural(), delete_contact()
search_contacts(), list_contacts(), get_stats()
upcoming_followups(), overdue_followups(), upcoming_birthdays(), last_contact()
set_referral(), get_referrals_by(), get_all_referrals(), get_top_referrers()
parse_excel_column_order(), handle_excel_import()
format_contact() → moved to helpers.py (used by crm + meetings)
detect_crm_natural_update()
```
**Cross-feature note:** `handle_meeting_session` calls `find_row` from crm. Meetings imports crm — this is acceptable as a one-way dependency (meetings → crm, not crm → meetings). ✅

#### `expenses.py` (~500 lines)
```python
# Cards sub-section
get_cards_live(), get_card_names_live(), get_card_default_for_category()
get_card_by_last4(), set_card_default_category(), fuzzy_match_card()
fuzzy_match_category(), rename_category()

# Merchant memory
_get_merchant_records(), _invalidate_merchant_cache(), get_merchant_memory()
save_merchant_memory(), delete_merchant(), get_merchant_emoji()
get_merchant_list()

# Expense core
parse_expense_text_v2(), log_expense(), handle_expense_text()
format_expense_confirmation(), format_expense_logged()
_finalise_receipt_confirm(), handle_expense_session()
handle_receipt_confirm_session()
is_expense_input(), is_log_prefix_input(), is_bare_merchant_input()
_invalidate_expense_cache(), check_same_day_duplicate(), parse_multi_field_edit()

# Expense queries
get_monthly_summary(), get_expense_report(), get_expense_categories()
get_recent_expenses(), get_last_expense(), show_last_expense()
delete_last_expense(), delete_expense_by_row(), search_expenses_by_merchant()
format_delete_list(), edit_last_expense(), get_trip_summary()
```

#### `fx.py` (~260 lines)
```python
get_fx_rate(), save_manual_fx_rate(), persist_fx_rates_to_sheet()
load_fx_rates_from_sheet(), parse_manual_fx_input(), refresh_fx_rates()
```
**Used by:** expenses.py (imports fx). fx never imports expenses. ✅

#### `reminders.py` (~360 lines)
```python
add_reminder(), cancel_reminder_by_keyword(), list_reminders()
check_and_fire_reminders(), handle_new_reminder(), handle_reschedule()
parse_reminder_request(), parse_reschedule_request()
is_reminder_request(), is_reschedule_request(), is_cancel_reminder_request()
last_fired_reminder, _pending_reminders_cache, _pending_reminders_cache_ts
```

#### `calendar.py` (~230 lines)
```python
get_calendar(), check_icloud_daily()
get_events(), delete_calendar_event(), is_calendar_request()
smart_add_event(), handle_edit_session()
```

#### `todos.py` (~70 lines)
```python
add_todo(), complete_todo(), delete_todo(), list_todos()
```

#### `meetings.py` (~260 lines)
```python
is_meeting_start(), is_meeting_done(), extract_event_name()
tag_crm_contacts(), format_recap_confirmation(), process_meeting_notes()
save_meeting_recap(), search_meeting_notes(), handle_meeting_session()
```
**Imports crm** for `find_row` and `tag_crm_contacts`. One-way only. ✅

#### `bills.py` (~175 lines)
```python
parse_bill_request(), add_bill(), list_bills(), delete_bill()
get_cycle_expenses(), send_bill_reminders(), is_bill_request(), handle_new_bill()
```

#### `restaurants.py` (~530 lines)
```python
parse_restaurant_save(), lookup_restaurant_from_maps(), save_restaurant()
format_restaurant_saved(), search_restaurants(), list_restaurants(), delete_restaurant()
is_restaurant_review_request(), get_restaurant_emoji(), get_restaurant_review()
is_restaurant_suggestion_request(), get_similar_restaurants()
is_restaurant_save(), is_restaurant_search(), infer_restaurant_location()
handle_save_restaurant(), handle_search_restaurants()
```

#### `stocks.py` (~1000 lines)
```python
normalise_ticker(), fetch_weekly_change(), fetch_price()
_fetch_rss_headlines_for_stock(), _generate_price_movement_summary()
fetch_stock_summary(), format_price(), get_source_label()
log_portfolio_buy(), get_portfolio_holdings(), get_portfolio_performance()
get_portfolio_rows(), format_portfolio_delete_list(), delete_portfolio_row()
search_portfolio_by_ticker(), set_price_alert(), parse_stock_request()
suggest_stocks(), check_price_alerts(), send_weekly_market_summary()
is_stock_request(), handle_stock_request()
fetch_market_rss_headlines(), get_market_summary_now()
persist_price_alerts_to_sheet(), load_price_alerts_from_sheet()
handle_statement_upload()
```

#### `trips.py` (~120 lines)
```python
save_trip(), close_trip(), get_active_trip(), restore_overseas_from_trips()
get_trip_history(), format_trip_history()
is_overseas_mode_request(), extract_flight_number(), _parse_trip_dates()
_get_currency_for_dest(), handle_overseas_request(), _send_trip_confirm()
deactivate_overseas_mode(), activate_overseas_mode_scheduled(), deactivate_and_notify()
persist_trip_setup(), load_trip_setup_from_sheet()
```

---

### Layer 5 — `sessions.py` (~200 lines)
**What goes here:** Session lifecycle — timeout, persist, restore. Sits above feature modules because it knows about all session dicts.

```python
touch_session(), is_session_expired(), check_session_timeouts(), clear_all_sessions()
get_active_session_label(), get_active_session()
persist_sessions_to_sheet(), load_sessions_from_sheet()
persist_birthday_pending(), load_birthday_pending_from_sheet()
persist_trip_setup() → moved here from trips (it only touches Settings sheet + state)
```

**Why above feature modules:** `check_session_timeouts` references all session dicts. `get_active_session_label` references all session dicts. They don't belong in any single feature module. ✅

---

### Layer 6 — `routing.py` (~1700 lines)
**What goes here:** `handle_message`, `_handle_message_inner`, `build_system_prompt`, `send_safe`, `check_missed_items_on_startup`.

**Imports:** Every feature module. This is the only file that imports everything — and it imports everything at the top, statically. No dynamic imports, no conditionals.

```python
from config import *
from clients import client, YOUR_CHAT_ID
from state import *
from sheets import *
from helpers import *
from crm import *
from expenses import *
from fx import *
from reminders import *
from calendar import *
from todos import *
from meetings import *
from bills import *
from restaurants import *
from stocks import *
from trips import *
from sessions import *
```

---

### Layer 7 — `infrastructure.py` (~150 lines)
**What goes here:** `run_infrastructure_setup`, `setup_drive`, `setup_em_profile`, `save_em_profile`, `send_startup_message`, `track_anthropic_call`, `notify_anthropic_down`.

**Imports:** `sheets`, `clients`, `state`, `config`.

---

### Layer 8 — `bot.py` (entry point, ~60 lines)
**What goes here:** `post_init`, `main`, scheduler wiring only.

```python
from routing import handle_message
from infrastructure import run_infrastructure_setup, send_startup_message, check_missed_items_on_startup
from sessions import *
from state import _scheduler, _app_ref
from config import TELEGRAM_TOKEN, YOUR_CHAT_ID
```

---

## Complete Import Graph (no cycles possible)

```
config ──────────────────────────────────────────┐
  ↓                                              │
clients ──────────────────────────────────────┐  │
  ↓                                           │  │
state ──────────────────────────────────────┐ │  │
  ↓                                         │ │  │
sheets ──────────────────────────────────┐  │ │  │
  ↓                                      │  │ │  │
helpers ─────────────────────────────┐   │  │ │  │
  ↓                                  │   │  │ │  │
[feature modules]  ←─ all import ────┴───┘  │ │  │
  crm, expenses, fx, reminders,     these   │ │  │
  calendar, todos, meetings, bills,  four   │ │  │
  restaurants, stocks, trips                │ │  │
  ↓                                         │ │  │
sessions  ←──────────────────────────────────┘ │  │
  ↓                                             │  │
routing  ←───────────────────────────────────────┘  │
  ↓                                                  │
infrastructure  ←────────────────────────────────────┘
  ↓
bot.py (entry point)
```

---

## The Only Permitted Cross-Feature Import

`meetings.py` imports `crm.find_row` for contact tagging. This is the only feature→feature import. It is one-way (crm never imports meetings). Explicitly documented in both files.

All other cross-feature calls are routed through routing.py's handler dispatch — no feature module calls another feature module's handler functions.

---

## deploy.py Changes

Currently deploy.py copies one file. After modularisation it needs to copy all module files. The deploy command becomes:

```bash
python3 ~/telegram-claude-bot/deploy.py "commit msg" "Session N" "built" "fixed" "pending"
```

deploy.py updated to copy: `config.py clients.py state.py sheets.py helpers.py crm.py expenses.py fx.py reminders.py calendar.py todos.py meetings.py bills.py restaurants.py stocks.py trips.py sessions.py routing.py infrastructure.py bot.py`

All files live flat in `~/telegram-claude-bot/`. Railway sees them as one package.

---

## What Changes in test_em.py

test_em.py currently imports from bot.py directly:
```python
from bot import is_expense_input, is_reminder_request, ...
```

After modularisation, imports change to:
```python
from expenses import is_expense_input
from reminders import is_reminder_request
from stocks import is_stock_request
# etc.
```

All 91 tests remain valid — only import lines change.

---

## Build Order for S22

1. `config.py` — extract all constants, verify nothing broken
2. `clients.py` — move gc, spreadsheet, drive_service, Anthropic client
3. `state.py` — move all mutable dicts and flags
4. `sheets.py` — move all sheet accessors and setup functions
5. `helpers.py` — move pure utility functions
6. `crm.py` — move CRM functions, update imports
7. `expenses.py` + `fx.py` — move expense + FX functions
8. `reminders.py`, `calendar.py`, `todos.py`, `meetings.py` — move each
9. `bills.py`, `restaurants.py`, `stocks.py`, `trips.py` — move each
10. `sessions.py` — move session management
11. `routing.py` — move handle_message and dispatcher
12. `infrastructure.py` — move setup functions
13. `bot.py` — slim entry point
14. Update `test_em.py` imports
15. Run all 91 tests — zero failures required before deploy
16. Update `deploy.py` to copy all files
17. Deploy

---

## Circular Import Red Flags to Watch For

These would be bugs — flag immediately if seen:

- `state.py` importing from `sheets.py` → **BANNED**
- `config.py` importing from anything → **BANNED**
- `crm.py` importing from `expenses.py` → **BANNED**
- `expenses.py` importing from `routing.py` → **BANNED**
- Any feature module importing from `routing.py` → **BANNED**
- `meetings.py` importing from any feature module other than `crm` → **BANNED**

The test: if module A imports module B, and module B also imports module A — that's a circular import. Python will throw `ImportError: cannot import name X from partially initialized module`. The layer diagram above makes this impossible if followed strictly.
