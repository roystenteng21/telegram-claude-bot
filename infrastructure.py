import os
import json
from datetime import date
import state
from config import RAILWAY_DEPLOYMENT_ID, YOUR_CHAT_ID
from clients import spreadsheet, drive_service
from sheets import setup_sheets, get_sheet, reconcile_backlog_status, apply_boot_em_log

def get_or_create_drive_folder(name, parent_id=None):
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
    folder = drive_service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
    print(f"Created Drive folder: {name}")
    return folder["id"]

RECEIPTS_FOLDER_ID = os.getenv("RECEIPTS_FOLDER_ID", "")

def setup_drive():
    em_id = get_or_create_drive_folder("Em")
    meeting_notes_id = get_or_create_drive_folder("Meeting Notes", em_id)
    backups_id = get_or_create_drive_folder("Backups", em_id)
    receipts_id = get_or_create_drive_folder("Receipts", em_id)
    return {
        "em": em_id,
        "meeting_notes": meeting_notes_id,
        "backups": backups_id,
        "receipts": receipts_id,
    }

def setup_em_profile():
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
        for i, r in enumerate(records):
            if r.get("Key") == "em_profile":
                state.em_profile.update(json.loads(r.get("Value", "{}")))
                print("✅ Loaded em_profile from Settings sheet")
                return
        state.em_profile.update(default_profile)
        settings_sheet.append_row(["em_profile", json.dumps(default_profile)])
        print("✅ Created em_profile in Settings sheet")
    except Exception as e:
        state.em_profile.update(default_profile)
        print(f"Warning: Could not load em_profile from sheet: {e}. Using defaults.")

def save_em_profile():
    try:
        settings_sheet = spreadsheet.worksheet("Settings")
        records = settings_sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("Key") == "em_profile":
                settings_sheet.update_cell(i + 2, 2, json.dumps(state.em_profile))
                return
        settings_sheet.append_row(["em_profile", json.dumps(state.em_profile)])
    except Exception as e:
        print(f"Error saving em_profile: {e}")

def run_infrastructure_setup():
    print("Running infrastructure setup...")
    health = {
        "Sheets": "✅ Connected",
        "Drive": "✅ Connected",
        "iCloud": "⚠️ Not tested yet",
        "Scheduler": "✅ Running",
        "Profile": "✅ Loaded",
    }
    try:
        setup_sheets()
    except Exception as e:
        health["Sheets"] = f"❌ Failed — {str(e)[:40]}"
        print(f"setup_sheets error: {e}")
    try:
        state.DRIVE_FOLDERS = setup_drive()
    except Exception as e:
        health["Drive"] = "❌ Failed — receipt uploads won't work"
        state.DRIVE_FOLDERS = {}
        print(f"setup_drive error: {e}")
    try:
        setup_em_profile()
        if not state.em_profile:
            raise Exception("Profile empty after load")
    except Exception:
        import time as _time
        _time.sleep(1)
        try:
            setup_em_profile()
            if not state.em_profile:
                raise Exception("Profile empty after retry")
        except Exception as e2:
            health["Profile"] = "❌ Failed — running on defaults"
            print(f"em_profile load failed after retry: {e2}")
    try:
        reconcile_backlog_status()
    except Exception as e:
        print(f"reconcile_backlog_status error: {e}")
    try:
        apply_boot_em_log()
    except Exception as e:
        print(f"apply_boot_em_log error: {e}")
    print("✅ Infrastructure setup complete")
    return health

async def send_startup_message(app, health):
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

def track_anthropic_call(success):
    if success:
        if state._anthropic_down_notified:
            state._anthropic_down_notified = False
        state._anthropic_failure_count = 0
    else:
        state._anthropic_failure_count += 1

async def notify_anthropic_down(app):
    from config import ANTHROPIC_FAILURE_THRESHOLD
    if state._anthropic_failure_count >= ANTHROPIC_FAILURE_THRESHOLD and not state._anthropic_down_notified:
        state._anthropic_down_notified = True
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