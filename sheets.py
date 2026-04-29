import json
import time as _time
from datetime import date, datetime

from clients import spreadsheet, drive_service
import config
from config import (
    RECEIPTS_FOLDER_ID, CARDS_SCHEMA, INITIAL_CARDS, YOUR_CHAT_ID,
    RAILWAY_DEPLOYMENT_ID,
)


# --- Quota Retry ---

def sheets_call_with_retry(fn, *args, max_retries=2, **kwargs):
    """Wrap a Google Sheets API call with quota retry logic."""
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if "quota" in err_str or "rate" in err_str or "429" in err_str:
                print(f"Sheets quota hit — retrying in 5s (attempt {attempt+1})")
                _time.sleep(5)
            else:
                raise
    raise Exception("Google Sheets rate limit exceeded after retries.")


# --- Sheet Accessors ---

def get_sheet(name):
    try:
        return spreadsheet.worksheet(name)
    except Exception as e:
        print(f"Warning: Could not get sheet '{name}': {e}")
        return None

def crm_sheet():
    return get_sheet("CRM")

def todo_sheet():
    return get_sheet("Todos")

def expenses_sheet():
    return get_sheet("Expenses")

def merchant_map_sheet():
    return get_sheet("Merchant Map")

def restaurants_sheet():
    return get_sheet("Restaurants")

def portfolio_sheet():
    return spreadsheet.worksheet("Portfolio")

def trips_sheet():
    return spreadsheet.worksheet("Trips")

def reminders_sheet():
    try:
        return spreadsheet.worksheet("Reminders")
    except Exception:
        print("Reminders sheet not found — creating it")
        ws = spreadsheet.add_worksheet(title="Reminders", rows=500, cols=8)
        ws.append_row(["ID", "Message", "Scheduled Time", "Recurrence", "Status", "Attempts", "Contact"])
        return ws

def bills_sheet():
    return spreadsheet.worksheet("Bills")

def cards_sheet():
    return spreadsheet.worksheet("Cards")

def em_log_sheet():
    return get_sheet("Em Log")


# --- Drive Helpers ---

def get_or_create_drive_folder(name, parent_id=None):
    """Get a Drive folder by name (under parent), or create it."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(
        q=query, fields="files(id, name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = drive_service.files().create(
        body=meta, fields="id", supportsAllDrives=True
    ).execute()
    print(f"Created Drive folder: {name}")
    return folder["id"]


def setup_drive():
    """Create Em's Drive folder structure."""
    em_id = get_or_create_drive_folder("Em")
    meeting_notes_id = get_or_create_drive_folder("Meeting Notes", em_id)
    backups_id = get_or_create_drive_folder("Backups", em_id)
    settings_id = get_or_create_drive_folder("Settings", em_id)
    print("✅ Drive folders setup complete")
    return {
        "em": em_id,
        "receipts": RECEIPTS_FOLDER_ID,
        "meeting_notes": meeting_notes_id,
        "backups": backups_id,
        "settings": settings_id
    }


# --- CRM Header Migration ---

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
        old_idx = {h: i for i, h in enumerate(old_h)}

        def get_old(row, col_name):
            i = old_idx.get(col_name)
            return row[i] if i is not None and i < len(row) else ""

        migrated = [new_headers]
        for row in rows:
            name = get_old(row, "Name")
            if not name:
                continue
            migrated.append([
                name,
                get_old(row, "Alias"),
                get_old(row, "Birthday"),
                get_old(row, "Where We Met"),
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


# --- Dev Notes + Em Log Setup Content ---

DEV_NOTES_CONTENT = [
    ["Section", "Content", "Last Updated"],
    ["Coding Standard — R1", "Route by cost: exact match → regex → keyword → cached lookup → live data → Claude. Never call Claude for routing decisions.", "2026-04-26"],
    ["Coding Standard — R2", "Single pass, immediate exit. Once a handler matches, execution stops. No fallback re-runs detectors. Loops exit on first match.", "2026-04-26"],
    ["Coding Standard — R3", "One sheet read per request, at the latest possible moment. Cached in memory, invalidated only on write. No sheet read during routing.", "2026-04-26"],
    ["Coding Standard — R4", "Haiku for classification/extraction/JSON under 200 tokens. Sonnet for reasoning/conversation/multi-step only. Set min max_tokens.", "2026-04-26"],
    ["Coding Standard — R5", "Typing indicator before every external API call. Parallel calls use asyncio.gather(). Nothing blocks the event loop.", "2026-04-26"],
    ["Coding Standard — R6", "Session messages never reach main routing chain. Session data is flat dict with explicit fields. Sessions time out cleanly.", "2026-04-26"],
    ["Coding Standard — R7", "Cache hierarchy: in-memory → cached sheet read → live sheet read → external API. Each layer reached only if above misses.", "2026-04-26"],
    ["Coding Standard — R8", "Every branch sets reply or returns. Empty reply is a bug. Claude fallback for genuine unknown intent only.", "2026-04-26"],
    ["Architecture — Routing", "Primary elif chain runs once. No else block re-runs detectors. Session handlers exit before reaching main router.", "2026-04-26"],
    ["Architecture — Caches", "_merchant_cache, _card_names_cache, _system_prompt_cache. Invalidate at write site only. Warm card cache on startup.", "2026-04-26"],
    ["Architecture — Models", "Haiku: is_calendar_request, parse_expense_text_v2, classification. Sonnet: conversation fallback, reasoning, market narrative.", "2026-04-26"],
    ["Architecture — Sheets", "setup_sheets() uses single worksheets() call. No repeated API reads in setup. Em Log and Dev Notes never read in routing.", "2026-04-26"],
    ["Deploy Flow", "git add bot.py → git commit -m '[msg]' → git push origin main → watch Railway build → em status in Telegram → test changed features.", "2026-04-26"],
    ["Handoff Rule", "4 lines max: (1) last deployed commit, (2) bot.py status, (3) mid-session context if hanging, (4) Read Dev Notes + Em Log before starting.", "2026-04-26"],
]

EM_LOG_HEADERS_BACKLOG = ["Priority", "Item", "Stage", "Notes", "Added"]
EM_LOG_HEADERS_SESSION = ["Date", "Session", "Built", "Fixed", "Pending", "Commit"]

INITIAL_BACKLOG = [
    ["🔴", "log_expense: add error handling — financial data silently lost on sheet failure", "Step 3", "Wrap append_row in try/except, notify user if write fails, do not delete session until write confirmed", "2026-04-26"],
    ["🔴", "Session deleted before write confirmed — expense unrecoverable on failure", "Step 3", "Move del receipt_confirm_sessions[user_id] to after log_expense succeeds", "2026-04-26"],
    ["🔴", "save_merchant_memory silent fail — merchant never learned if sheet write fails", "Step 3", "Add error handling, log failure, do not silently swallow", "2026-04-26"],
    ["🟠", "sheets_call_with_retry uses time.sleep(60) — blocks entire event loop", "Step 3", "Replace with asyncio.sleep(60) inside async context", "2026-04-26"],
    ["🟠", "get_calendar uses time.sleep(3) on retry — blocks event loop on every calendar request", "Step 3", "Replace with asyncio.sleep or remove retry sleep", "2026-04-26"],
    ["🟠", "find_row: 5 full passes over CRM records, no cache — 1000 iterations per lookup", "Step 3", "Single-pass with match tiers, add CRM cache invalidated on write", "2026-04-26"],
    ["🟠", "check_and_fire_reminders: full sheet read every minute + find_row inside loop", "Step 3", "Cache pending reminders in memory, only re-read on write. Remove find_row from loop.", "2026-04-26"],
    ["🟠", "Duplicate routing: is_reminder_request 3x, is_stock_request 2x, others twice", "Step 3", "Eliminate else block, merge missing handlers into primary elif chain", "2026-04-26"],
    ["🟡", "Missing env var guard at startup — cryptic crash if TELEGRAM_TOKEN or ANTHROPIC_API_KEY unset", "Step 3", "Add explicit check and clear error message before app starts", "2026-04-26"],
    ["🟡", "float() cast on unvalidated Claude output in parse_expense_text_v2 — unhandled exception", "Step 3", "Validate amount field before cast, return user-friendly error if invalid", "2026-04-26"],
]

INITIAL_SESSION = [
    ["2026-04-26", "Session 6",
     "Flight date fix (extract_flight_dates + AviationStack date param); Perf fixes (merchant cache, card cache, system prompt cache, history cap 20); AviationStack fallback; Dev Notes + Em Log tabs; em whats pending; setup_sheets single API call",
     "Flight dates ignored (now passed to AviationStack); Merchant map re-read every expense; Card names re-read every parse; System prompt rebuilt every Claude call; setup_sheets called worksheets() 4x",
     "All 19 issues (Step 3); Stage 3 trip features",
     "8679c99c"],
    ["2026-04-29", "Session 7",
     "Modularisation — split bot.py into 16 modules (config, clients, state, sheets, profile, crm, expenses, reminders, calendar, todos, stocks, trips, meetings, routing, scheduler, bot). Zero logic changes.",
     "Dead code fix from Session 6 (duplicate elif branches)",
     "Trips schema migration + AviationStack strip (Session 8); all 19 backlog items (Step 3)",
     "TBD"],
]


def _setup_dev_notes(existing):
    """Create or overwrite Dev Notes tab with current coding standards."""
    try:
        if "Dev Notes" not in existing:
            ws = spreadsheet.add_worksheet(title="Dev Notes", rows=50, cols=3)
            existing.append("Dev Notes")
            print("Created Dev Notes tab")
        else:
            ws = spreadsheet.worksheet("Dev Notes")
        ws.clear()
        ws.update(range_name='A1', values=DEV_NOTES_CONTENT)
        try:
            ws.format('A1:C1', {'textFormat': {'bold': True}})
        except Exception:
            pass
        print("✅ Dev Notes populated")
    except Exception as e:
        print(f"Dev Notes setup error: {e}")


def _setup_em_log(existing):
    """Create Em Log tab with Backlog and Session History sections."""
    try:
        if "Em Log" not in existing:
            ws = spreadsheet.add_worksheet(title="Em Log", rows=200, cols=6)
            existing.append("Em Log")
            print("Created Em Log tab")
        else:
            ws = spreadsheet.worksheet("Em Log")
            if ws.row_values(1):
                print("Em Log already populated — skipping init")
                return

        ws.append_row(["── BACKLOG (max 10) ──", "", "", "", "", ""])
        ws.append_row(EM_LOG_HEADERS_BACKLOG)
        for row in INITIAL_BACKLOG[:10]:
            ws.append_row(row)

        ws.append_row(["", "", "", "", "", ""])
        ws.append_row(["── SESSION HISTORY (max 10) ──", "", "", "", "", ""])
        ws.append_row(EM_LOG_HEADERS_SESSION)
        for row in INITIAL_SESSION:
            ws.append_row(row)

        try:
            ws.format('A1:F1', {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9}})
        except Exception:
            pass
        print("✅ Em Log populated")
    except Exception as e:
        print(f"Em Log setup error: {e}")


# --- Main Sheet Setup ---

def setup_sheets():
    """Rename Sheet1 to CRM if needed, create all required tabs.
    Single worksheets() call at start — no repeated API reads."""
    existing = [ws.title for ws in spreadsheet.worksheets()]

    CRM_HEADERS = [
        "Name", "Alias", "Birthday", "Relationship", "Context", "Notes",
        "Follow Up Date", "Follow Up Notes", "Last Updated", "Birthday Greeted",
        "Referred By", "Referral Date", "Email", "Address"
    ]

    if "CRM" not in existing:
        try:
            sheet1 = spreadsheet.worksheet("Sheet1")
            sheet1.update_title("CRM")
            existing.append("CRM")
            print("Renamed Sheet1 -> CRM")
        except Exception:
            if "CRM" not in existing:
                ws = spreadsheet.add_worksheet(title="CRM", rows=1000, cols=20)
                ws.append_row(CRM_HEADERS)
                existing.append("CRM")
                print("Created CRM tab")

    try:
        crm_ws = spreadsheet.worksheet("CRM")
        current_headers = crm_ws.row_values(1)
        if current_headers != CRM_HEADERS:
            _migrate_crm_headers(crm_ws, current_headers, CRM_HEADERS)
    except Exception as e:
        print(f"CRM header check error: {e}")

    if "Todos" not in existing:
        ws = spreadsheet.add_worksheet(title="Todos", rows=500, cols=3)
        ws.append_row(["Task", "Status", "Added"])
        existing.append("Todos")
        print("Created Todos tab")

    required_tabs = {
        "Meeting Notes": ["Event Name", "Topic", "Summary", "Action Items", "Date"],
        "Expenses": ["Date", "Merchant", "Amount", "Currency", "SGD Amount", "Category",
                     "Card", "Receipt Link", "Reconciled", "Notes"],
        "Bills": ["Name", "Bank", "Due Date", "Estimated Amount", "Notes"],
        "Merchant Map": ["Merchant", "Category", "Card"],
        "Restaurants": ["Name", "Location", "Country", "Tags", "Notes"],
        "Portfolio": ["Stock", "Quantity", "Buy Price", "Buy Date", "Notes"],
        "Trips": ["Trip ID", "Destination", "Currency", "Dep Flight", "Dep Time",
                  "Return Flight", "Return Time", "Status", "Started", "Ended"],
        "Reminders": ["ID", "Message", "Scheduled Time", "Recurrence", "Status", "Attempts", "Contact"],
        "Settings": ["Key", "Value"],
    }

    for tab_name, headers in required_tabs.items():
        if tab_name not in existing:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=500, cols=len(headers))
            ws.append_row(headers)
            existing.append(tab_name)
            print(f"Created tab: {tab_name}")

    try:
        if "Cards" not in existing:
            cards_ws = spreadsheet.add_worksheet(title="Cards", rows=100, cols=4)
            existing.append("Cards")
        else:
            cards_ws = spreadsheet.worksheet("Cards")
        cards_ws.clear()
        cards_ws.append_row(CARDS_SCHEMA)
        for card_row in INITIAL_CARDS:
            cards_ws.append_row(card_row)
        print("✅ Cards sheet initialised with new schema")
    except Exception as e:
        print(f"Cards sheet setup error: {e}")

    _setup_dev_notes(existing)
    _setup_em_log(existing)

    print("✅ Sheets setup complete")


# --- Em Log Write Functions ---

def add_session_to_em_log(date_str, session_name, built, fixed, pending, commit):
    """Append a session row to Em Log. Enforces 10-row cap."""
    try:
        ws = em_log_sheet()
        if not ws:
            return
        all_values = ws.get_all_values()

        session_header_row = None
        for i, row in enumerate(all_values):
            if row and "SESSION HISTORY" in str(row[0]):
                session_header_row = i + 1
                break
        if not session_header_row:
            return

        data_start = session_header_row + 2
        session_rows = [i + 1 for i, row in enumerate(all_values)
                        if i >= data_start and any(row)]

        while len(session_rows) >= 10:
            ws.delete_rows(session_rows[0])
            session_rows.pop(0)

        ws.append_row([date_str, session_name, built, fixed, pending, commit])
        print(f"Session logged to Em Log: {session_name}")
    except Exception as e:
        print(f"add_session_to_em_log error: {e}")


def add_backlog_item(priority, item, stage="", notes=""):
    """Add item to Em Log backlog. Enforces 10-item cap."""
    try:
        ws = em_log_sheet()
        if not ws:
            return "Em Log sheet not found"
        all_values = ws.get_all_values()

        backlog_header_row = None
        backlog_col_header_row = None
        for i, row in enumerate(all_values):
            if row and "BACKLOG" in str(row[0]):
                backlog_header_row = i + 1
            if backlog_header_row and row and row[0] == "Priority":
                backlog_col_header_row = i + 1
                break
        if not backlog_col_header_row:
            return "Backlog section not found in Em Log"

        backlog_rows = []
        for i, row in enumerate(all_values):
            if i >= backlog_col_header_row and any(row) and "SESSION" not in str(row[0]) and row[0] != "Priority":
                backlog_rows.append(i + 1)
            if row and "SESSION HISTORY" in str(row[0]):
                break

        if len(backlog_rows) >= 10:
            ws.delete_rows(backlog_rows[-1])

        today = date.today().strftime("%Y-%m-%d")
        divider_row = None
        for i, row in enumerate(all_values):
            if i >= backlog_col_header_row and not any(row):
                divider_row = i + 1
                break

        if divider_row:
            ws.insert_row([priority, item, stage, notes, today], divider_row)
        else:
            ws.append_row([priority, item, stage, notes, today])

        return "Added to backlog ✅"
    except Exception as e:
        return f"add_backlog_item error: {e}"


def get_pending_backlog():
    """Return formatted backlog from Em Log for em whats pending command."""
    try:
        ws = em_log_sheet()
        if not ws:
            return "Em Log not found"
        all_values = ws.get_all_values()

        backlog_items = []
        in_backlog = False
        for row in all_values:
            if not row:
                continue
            if "BACKLOG" in str(row[0]):
                in_backlog = True
                continue
            if "SESSION HISTORY" in str(row[0]):
                break
            if in_backlog and row[0] not in ("Priority", "── BACKLOG (max 10) ──", ""):
                priority = row[0] if len(row) > 0 else ""
                item = row[1] if len(row) > 1 else ""
                stage = row[2] if len(row) > 2 else ""
                if item:
                    backlog_items.append((priority, item, stage))

        if not backlog_items:
            return "Backlog is empty ✅"

        lines = ["*Backlog*\n"]
        for priority, item, stage in backlog_items:
            stage_str = f" _[{stage}]_" if stage else ""
            lines.append(f"{priority} {item}{stage_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"Couldn't read backlog: {e}"


# --- Startup Message ---

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

        if RAILWAY_DEPLOYMENT_ID and RAILWAY_DEPLOYMENT_ID == last_deploy_id:
            return

        if RAILWAY_DEPLOYMENT_ID:
            if last_deploy_row:
                settings_ws.update_cell(last_deploy_row, 2, RAILWAY_DEPLOYMENT_ID)
            else:
                settings_ws.append_row(["last_deployment_id", RAILWAY_DEPLOYMENT_ID])

        issues = []
        for k, v in health.items():
            if k == "iCloud":
                continue
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
