import os
import json
import pytz
from datetime import date, datetime
from config import (
    CARDS_SCHEMA, INITIAL_CARDS, DEV_NOTES_CONTENT, EM_LOG_HEADERS_BACKLOG,
    EM_LOG_HEADERS_SESSION, INITIAL_BACKLOG, INITIAL_SESSION
)
from clients import spreadsheet, drive_service
import state

# ── Sheet accessors ────────────────────────────────────────────────────────────

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
    return spreadsheet.worksheet("Expenses")

def merchant_map_sheet():
    return spreadsheet.worksheet("Merchant Map")

def reminders_sheet():
    try:
        return spreadsheet.worksheet("Reminders")
    except Exception:
        print("Reminders sheet not found — creating it")
        ws = spreadsheet.add_worksheet(title="Reminders", rows=500, cols=8)
        ws.append_row(["ID", "Message", "Scheduled Time", "Recurrence", "Status", "Attempts", "Contact"])
        return ws

def cards_sheet():
    return spreadsheet.worksheet("Cards")

def bills_sheet():
    return spreadsheet.worksheet("Bills")

def restaurants_sheet():
    return spreadsheet.worksheet("Restaurants")

def portfolio_sheet():
    return spreadsheet.worksheet("Portfolio")

def trips_sheet():
    return spreadsheet.worksheet("Trips")

def em_log_sheet():
    return get_sheet("Em Log")

# ── Retry wrapper ──────────────────────────────────────────────────────────────

def sheets_call_with_retry(fn, *args, max_retries=2, **kwargs):
    """Wrap a Google Sheets API call with quota retry logic."""
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if "quota" in err_str or "rate" in err_str or "429" in err_str:
                print(f"Sheets quota hit — retrying immediately (attempt {attempt+1})")
            else:
                raise
    raise Exception("Google Sheets rate limit exceeded after retries.")

# ── Em Log operations ──────────────────────────────────────────────────────────

def log_error_to_em_log(source: str, error: str):
    """Append a runtime error row to Em Log backlog section. Fire-and-forget."""
    try:
        ws = em_log_sheet()
        if not ws:
            return
        today = datetime.now(pytz.timezone("Asia/Kuala_Lumpur")).strftime("%Y-%m-%d %H:%M")
        ws.append_row(["🔴 ERROR", f"[{source}] {error}", "Runtime", today, today])
    except Exception:
        pass

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
        backlog_col_header_row = None
        backlog_header_row = None
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
            ws.insert_row([priority, item, stage, notes, today, "🔲 Outstanding"], divider_row)
        else:
            ws.append_row([priority, item, stage, notes, today, "🔲 Outstanding"])
        return "Added to backlog ✅"
    except Exception as e:
        return f"add_backlog_item error: {e}"

def get_pending_backlog():
    """Return formatted backlog from Em Log — Outstanding items only."""
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
                status = row[5] if len(row) > 5 else "🔲 Outstanding"
                if item and "Done" not in status:
                    backlog_items.append((priority, item, stage))
        if not backlog_items:
            return "Backlog is empty ✅"
        lines = ["*Backlog — Outstanding*\n"]
        for priority, item, stage in backlog_items:
            stage_str = f" _[{stage}]_" if stage else ""
            lines.append(f"{priority} {item}{stage_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Couldn't read backlog: {e}"

# ── Setup functions ────────────────────────────────────────────────────────────

MODULE_REGISTRY_HEADERS = ["Module", "File", "Layer", "Key Functions", "Imports From", "Last Changed", "Session", "Status"]

MODULE_REGISTRY_MODULAR = [
    ["config",         "config.py",         "0", "all constants, env vars, TIMEZONE, EXPENSE_CATEGORIES", "none", "2026-05-01", "S22", "✅ Active"],
    ["clients",        "clients.py",        "1", "gc, spreadsheet, drive_service, client (Anthropic)", "config", "2026-05-01", "S22", "✅ Active"],
    ["state",          "state.py",          "1", "overseas_state, expense_sessions, receipt_confirm_sessions, em_profile, all session dicts, all caches", "config", "2026-05-01", "S22", "✅ Active"],
    ["sheets",         "sheets.py",         "2", "get_sheet, all sheet accessors, setup_sheets, em log ops", "config, clients, state", "2026-05-01", "S22", "✅ Active"],
    ["helpers",        "helpers.py",        "3", "format_date, calculate_age, format_contact, send_safe, looks_like_new_intent, parse_date_flexible", "config", "2026-05-01", "S22", "✅ Active"],
    ["crm",            "crm.py",            "4", "find_row, save_contact, find_contact, add_note, upcoming_birthdays, detect_crm_natural_update", "config, clients, state, sheets, helpers", "2026-05-01", "S22", "✅ Active"],
    ["expenses",       "expenses.py",       "4", "log_expense, parse_expense_text_v2, handle_expense_text, is_expense_input, get_merchant_memory, cards", "config, clients, state, sheets, helpers", "2026-05-01", "S22", "✅ Active"],
    ["fx",             "fx.py",             "4", "get_fx_rate, refresh_fx_rates, parse_manual_fx_input, persist_fx_rates_to_sheet", "config, clients, state, sheets", "2026-05-01", "S22", "✅ Active"],
    ["reminders",      "reminders.py",      "4", "add_reminder, check_and_fire_reminders, is_reminder_request, handle_new_reminder", "config, clients, state, sheets", "2026-05-01", "S22", "✅ Active"],
    ["calendar",       "cal.py",            "4", "get_calendar, smart_add_event, is_calendar_request, get_events", "config, clients, state, sheets, helpers", "2026-05-01", "S22", "✅ Active"],
    ["todos",          "todos.py",          "4", "add_todo, complete_todo, delete_todo, list_todos", "config, clients, sheets, helpers", "2026-05-01", "S22", "✅ Active"],
    ["meetings",       "meetings.py",       "4", "handle_meeting_session, process_meeting_notes, save_meeting_recap, is_meeting_start", "config, clients, state, sheets, helpers, crm", "2026-05-01", "S22", "✅ Active"],
    ["bills",          "bills.py",          "4", "add_bill, list_bills, send_bill_reminders, is_bill_request", "config, clients, state, sheets", "2026-05-01", "S22", "✅ Active"],
    ["restaurants",    "restaurants.py",    "4", "save_restaurant, search_restaurants, get_restaurant_review, is_restaurant_suggestion_request", "config, clients, state, sheets", "2026-05-01", "S22", "✅ Active"],
    ["stocks",         "stocks.py",         "4", "handle_stock_request, is_stock_request, check_price_alerts, get_market_summary_now, fetch_stock_summary", "config, clients, state, sheets", "2026-05-01", "S22", "✅ Active"],
    ["trips",          "trips.py",          "4", "save_trip, handle_overseas_request, is_overseas_mode_request, restore_overseas_from_trips", "config, clients, state, sheets", "2026-05-01", "S22", "✅ Active"],
    ["sessions",       "sessions.py",       "5", "touch_session, check_session_timeouts, get_active_session_label, persist_sessions_to_sheet", "config, clients, state, sheets", "2026-05-01", "S22", "✅ Active"],
    ["routing",        "routing.py",        "6", "handle_message, _handle_message_inner, build_system_prompt", "all modules", "2026-05-01", "S22", "✅ Active"],
    ["infrastructure", "infrastructure.py", "7", "run_infrastructure_setup, setup_drive, setup_em_profile, send_startup_message", "config, clients, state, sheets", "2026-05-01", "S22", "✅ Active"],
    ["bot",            "bot.py",            "8", "post_init, main, scheduler wiring", "routing, infrastructure, sessions, config", "2026-05-01", "S22", "✅ Active"],
]

def _setup_dev_notes(existing):
    """Create or update Dev Notes tab."""
    try:
        if "Dev Notes" not in existing:
            ws = spreadsheet.add_worksheet(title="Dev Notes", rows=200, cols=3)
            existing.append("Dev Notes")
            ws.clear()
            for row in DEV_NOTES_CONTENT:
                ws.append_row(row)
            try:
                ws.format('A1:C1', {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.9, 'green': 0.95, 'blue': 1.0}})
            except Exception:
                pass
            print("✅ Dev Notes tab created")
        else:
            ws = spreadsheet.worksheet("Dev Notes")
            all_values = ws.get_all_values()
            existing_sections = {row[0] for row in all_values[1:] if row}
            new_rows = [r for r in DEV_NOTES_CONTENT[1:] if r[0] not in existing_sections]
            for row in new_rows:
                ws.append_row(row)
            if new_rows:
                print(f"✅ Dev Notes: added {len(new_rows)} new section(s)")
    except Exception as e:
        print(f"Dev Notes setup error: {e}")

def _setup_em_log(existing):
    """Create Em Log tab with Backlog and Session History sections."""
    try:
        if "Em Log" not in existing:
            ws = spreadsheet.add_worksheet(title="Em Log", rows=200, cols=6)
            existing.append("Em Log")
            print("Created Em Log tab")
            _write_em_log_fresh(ws)
            return
        ws = spreadsheet.worksheet("Em Log")
        all_values = ws.get_all_values()
        has_backlog = any("BACKLOG" in str(row[0]) for row in all_values if row)
        has_session = any("SESSION HISTORY" in str(row[0]) for row in all_values if row)
        if not all_values or not has_session:
            ws.clear()
            _write_em_log_fresh(ws)
            return
        if not has_backlog:
            print("Em Log missing backlog section — injecting...")
            backlog_rows = [
                ["── BACKLOG (max 10) ──", "", "", "", "", ""],
                EM_LOG_HEADERS_BACKLOG,
            ] + [list(r) for r in INITIAL_BACKLOG[:10]] + [
                ["", "", "", "", "", ""],
            ]
            for i, row in enumerate(backlog_rows, start=1):
                ws.insert_row(row, i)
            print("✅ Em Log backlog section injected")
            return
        _migrate_backlog_status(ws, all_values)
        print("Em Log already populated — skipping full init")
    except Exception as e:
        print(f"Em Log setup error: {e}")

def _write_em_log_fresh(ws):
    """Write full Em Log structure from scratch."""
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

def _migrate_backlog_status(ws, all_values):
    """Backfill Status column (col F) for backlog rows that are missing it."""
    in_backlog = False
    header_passed = False
    updates = []
    for i, row in enumerate(all_values):
        if not row:
            continue
        if "BACKLOG" in str(row[0]):
            in_backlog = True
            continue
        if "SESSION HISTORY" in str(row[0]):
            break
        if in_backlog:
            if row[0] == "Priority":
                header_passed = True
                continue
            if header_passed and row[0] not in ("── BACKLOG (max 10) ──", ""):
                status = row[5] if len(row) > 5 else ""
                if not status:
                    updates.append((i + 1, 6, "🔲 Outstanding"))
    for sheet_row, col, val in updates:
        try:
            ws.update_cell(sheet_row, col, val)
        except Exception as e:
            print(f"Status migration error row {sheet_row}: {e}")
    if updates:
        print(f"✅ Migrated {len(updates)} backlog rows — Status column added")

def _setup_module_registry(existing):
    """Create Module Registry tab if missing."""
    try:
        if "Module Registry" not in existing:
            ws = spreadsheet.add_worksheet(title="Module Registry", rows=50, cols=8)
            existing.append("Module Registry")
            ws.append_row(MODULE_REGISTRY_HEADERS)
            for row in MODULE_REGISTRY_MODULAR:
                ws.append_row(row)
            try:
                ws.format('A1:H1', {'textFormat': {'bold': True},
                                    'backgroundColor': {'red': 0.85, 'green': 0.92, 'blue': 0.98}})
            except Exception:
                pass
            print("✅ Module Registry tab created")
        else:
            print("Module Registry already exists — skipping init")
    except Exception as e:
        print(f"Module Registry setup error: {e}")

def update_module_registry(module_name, file_name, last_changed, session, status="✅ Active"):
    """Update a single module's row in the Module Registry."""
    try:
        ws = get_sheet("Module Registry")
        if not ws:
            return
        all_values = ws.get_all_values()
        if len(all_values) <= 1:
            return
        target_row = None
        for i, row in enumerate(all_values[1:], start=2):
            if row and row[1] == file_name:
                target_row = i
                break
        if target_row:
            ws.update_cell(target_row, 6, last_changed)
            ws.update_cell(target_row, 7, session)
            ws.update_cell(target_row, 8, status)
        else:
            planned = next((r for r in MODULE_REGISTRY_MODULAR if r[1] == file_name), None)
            if planned:
                row_data = list(planned)
                row_data[5] = last_changed
                row_data[6] = session
                row_data[7] = status
                ws.append_row(row_data)
            else:
                ws.append_row([module_name, file_name, "?", "", "", last_changed, session, status])
        print(f"Module Registry updated: {file_name}")
    except Exception as e:
        print(f"update_module_registry error for {file_name}: {e}")

def get_module_registry():
    """Return Module Registry as list of dicts."""
    try:
        ws = get_sheet("Module Registry")
        if not ws:
            return []
        return ws.get_all_records()
    except Exception as e:
        print(f"get_module_registry error: {e}")
        return []

def _migrate_crm_headers(ws, old_headers, new_headers):
    """Migrate CRM sheet from old column layout to new layout."""
    try:
        all_data = ws.get_all_values()
        if not all_data:
            ws.update(range_name='A1', values=[new_headers])
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
                name, get_old(row, "Alias"), get_old(row, "Birthday"),
                get_old(row, "Where We Met"), get_old(row, "Context"), get_old(row, "Notes"),
                get_old(row, "Follow Up Date"), get_old(row, "Follow Up Notes"),
                get_old(row, "Last Updated"), get_old(row, "Birthday Greeted"),
                get_old(row, "Referred By"), get_old(row, "Referral Date"),
                get_old(row, "Email"), get_old(row, "Address"),
            ])
        ws.clear()
        if migrated:
            ws.update(range_name='A1', values=migrated)
        print(f"✅ CRM migrated ({len(migrated)-1} contacts)")
    except Exception as e:
        print(f"Error migrating CRM headers: {e}")

def setup_sheets():
    """Rename Sheet1 to CRM if needed, create all required tabs."""
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
        except Exception:
            if "CRM" not in existing:
                ws = spreadsheet.add_worksheet(title="CRM", rows=1000, cols=20)
                ws.append_row(CRM_HEADERS)
                existing.append("CRM")
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
    required_tabs = {
        "Meeting Notes": ["Event Name", "Topic", "Summary", "Action Items", "Date"],
        "Expenses": ["Date", "Merchant", "Amount", "Currency", "SGD Amount", "Category", "Card", "Receipt Link", "Reconciled", "Notes"],
        "Bills": ["Name", "Bank", "Due Date", "Estimated Amount", "Notes"],
        "Merchant Map": ["Merchant", "Category", "Card"],
        "Restaurants": ["Name", "Location", "Country", "Tags", "Notes"],
        "Portfolio": ["Stock", "Quantity", "Buy Price", "Buy Date", "Notes"],
        "Trips": ["Trip ID", "Destination", "Currency", "Check In", "Check Out", "Hotel Name", "Hotel Local Name", "Hotel Address", "Notes", "Status"],
        "Reminders": ["ID", "Message", "Scheduled Time", "Recurrence", "Status", "Attempts", "Contact"],
        "Settings": ["Key", "Value"],
    }
    for tab_name, headers in required_tabs.items():
        if tab_name not in existing:
            ws = spreadsheet.add_worksheet(title=tab_name, rows=500, cols=len(headers))
            ws.append_row(headers)
            existing.append(tab_name)
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
    except Exception as e:
        print(f"Cards sheet setup error: {e}")
    _setup_dev_notes(existing)
    _setup_em_log(existing)
    _setup_module_registry(existing)
    print("✅ Sheets setup complete")

def reconcile_backlog_status():
    """Mark all backlog items Done on startup — called by infrastructure setup."""
    try:
        ws = em_log_sheet()
        if not ws:
            return
        all_values = ws.get_all_values()
        in_backlog = False
        header_passed = False
        for i, row in enumerate(all_values):
            if not row:
                continue
            if "BACKLOG" in str(row[0]):
                in_backlog = True
                continue
            if "SESSION HISTORY" in str(row[0]):
                break
            if in_backlog:
                if row[0] == "Priority":
                    header_passed = True
                    continue
                if header_passed and row[0] not in ("── BACKLOG (max 10) ──", ""):
                    status = row[5] if len(row) > 5 else ""
                    if status and "Done" not in status:
                        try:
                            ws.update_cell(i + 1, 6, "✅ Done")
                        except Exception as e:
                            print(f"reconcile_backlog_status: row {i+1} error: {e}")
    except Exception as e:
        print(f"reconcile_backlog_status error: {e}")
