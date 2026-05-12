import json
import threading
from datetime import datetime
import state
from config import SESSION_TIMEOUT_MINUTES, SESSION_TIMEOUT_MESSAGES, TIMEZONE, YOUR_CHAT_ID
from sheets import get_sheet

def touch_session(user_id):
    state.session_timestamps[user_id] = datetime.now(TIMEZONE)

def is_session_expired(user_id):
    ts = state.session_timestamps.get(user_id)
    if not ts:
        return True
    elapsed = (datetime.now(TIMEZONE) - ts).total_seconds() / 60
    return elapsed > SESSION_TIMEOUT_MINUTES

def clear_all_sessions(user_id):
    for d in [state.expense_sessions, state.delete_sessions, state.portfolio_delete_sessions,
              state.confirm_sessions, state.receipt_confirm_sessions, state.recon_sessions,
              state.edit_sessions, state.meeting_sessions, state.session_timestamps]:
        d.pop(user_id, None)

async def check_session_timeouts(user_id, update):
    if not is_session_expired(user_id):
        return False
    expired = False
    if user_id in state.expense_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["expense"])
        del state.expense_sessions[user_id]
        expired = True
    if user_id in state.delete_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["delete"])
        del state.delete_sessions[user_id]
        expired = True
    if user_id in state.portfolio_delete_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["portfolio_delete"])
        del state.portfolio_delete_sessions[user_id]
        expired = True
    if user_id in state.confirm_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["confirm"])
        del state.confirm_sessions[user_id]
        expired = True
    if user_id in state.receipt_confirm_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["receipt_confirm"])
        del state.receipt_confirm_sessions[user_id]
        expired = True
    if user_id in state.edit_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["edit"])
        del state.edit_sessions[user_id]
        expired = True
    if user_id in state.meeting_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["meeting"])
        del state.meeting_sessions[user_id]
        expired = True
    state.session_timestamps.pop(user_id, None)
    return expired

def get_active_session_label(user_id):
    if user_id in state.receipt_confirm_sessions:
        return "expense confirmation"
    if user_id in state.expense_sessions:
        return "expense entry"
    if user_id in state.meeting_sessions:
        return "meeting recap"
    if user_id in state.edit_sessions:
        return "contact edit"
    if user_id in state.confirm_sessions:
        action = state.confirm_sessions[user_id].get("action", "")
        labels = {
            "delete_contact": "contact deletion",
            "delete_bill": "bill deletion",
            "delete_restaurant": "restaurant deletion",
            "delete_event": "event deletion",
            "rename_category": "category rename",
        }
        return labels.get(action, "confirmation")
    if user_id in state.delete_sessions:
        return "expense deletion"
    if user_id in state.portfolio_delete_sessions:
        return "portfolio deletion"
    if user_id in state.excel_import_sessions:
        return "contact import"
    if user_id in state.pending_restaurant_saves:
        return "restaurant save"
    if user_id in state.pending_contact_saves:
        return "contact save"
    if user_id in state.todo_disambig_sessions:
        return "todo action"
    if state.birthday_pending:
        return "birthday acknowledgement"
    return None

def get_active_session(user_id):
    """Return (session_type, session_data) for the first active session found, or (None, None)."""
    for name, store in [
        ("receipt_confirm", state.receipt_confirm_sessions),
        ("expense", state.expense_sessions),
        ("meeting", state.meeting_sessions),
        ("edit", state.edit_sessions),
        ("confirm", state.confirm_sessions),
        ("delete", state.delete_sessions),
        ("portfolio_delete", state.portfolio_delete_sessions),
    ]:
        if user_id in store:
            return name, store[user_id]
    return None, None

# ── Auto-persist receipt confirm sessions ─────────────────────────────────────

def persist_sessions_to_sheet():
    """Persist receipt_confirm_sessions to Settings sheet. Fire-and-forget."""
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        data = json.dumps({str(k): v for k, v in state.receipt_confirm_sessions.items()})
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("Key") == "receipt_confirm_sessions":
                sheet.update_cell(i + 2, 2, data)
                return
        sheet.append_row(["receipt_confirm_sessions", data])
    except Exception as e:
        print(f"persist_sessions_to_sheet error: {e}")

def load_sessions_from_sheet():
    """Restore receipt_confirm_sessions from Settings sheet on startup."""
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        records = sheet.get_all_records()
        for r in records:
            if r.get("Key") == "receipt_confirm_sessions":
                raw = r.get("Value", "")
                if raw:
                    loaded = json.loads(raw)
                    now = datetime.now(TIMEZONE)
                    valid = {}
                    for k, v in loaded.items():
                        # DEBUG: age check — skip sessions older than 6 hours
                        # TODO: enable once confirmed stable in prod
                        # started = v.get("started_at", "")
                        # if started:
                        #     try:
                        #         age = (now - datetime.fromisoformat(started)).total_seconds() / 3600
                        #         if age > 6:
                        #             print(f"Dropped stale receipt_confirm_session (age {age:.1f}h)")
                        #             continue
                        #     except Exception:
                        #         pass
                        valid[k] = v
                    state.receipt_confirm_sessions.update({int(k): v for k, v in valid.items()})
                    if valid:
                        print(f"Restored {len(valid)} receipt confirm session(s) from sheet")
                return
    except Exception as e:
        print(f"load_sessions_from_sheet error: {e}")


def expire_stale_trip_setup():
    """Clear _trip_setup from state and Settings sheet if stale (>6h) or missing timestamp."""
    try:
        ts = state.overseas_state.get("_trip_setup")
        if not ts:
            return
        if state.overseas_state.get("active"):
            # Overseas mode is live — leave it alone
            return
        started = ts.get("started_at", "")
        stale = True
        if started:
            try:
                age_hours = (datetime.now(TIMEZONE) - datetime.fromisoformat(started)).total_seconds() / 3600
                stale = age_hours > 6
            except Exception:
                stale = True
        if stale:
            state.overseas_state.pop("_trip_setup", None)
            try:
                from trips import persist_trip_setup
                persist_trip_setup()
            except Exception as e:
                print(f"expire_stale_trip_setup persist error: {e}")
            print("Cleared stale _trip_setup on boot")
    except Exception as e:
        print(f"expire_stale_trip_setup error: {e}")


class _AutoPersistDict(dict):
    """Dict subclass that auto-persists to Settings sheet on write/delete."""
    _timer = None

    def _schedule_persist(self):
        if _AutoPersistDict._timer is not None:
            _AutoPersistDict._timer.cancel()
        _AutoPersistDict._timer = threading.Timer(2.0, persist_sessions_to_sheet)
        _AutoPersistDict._timer.daemon = True
        _AutoPersistDict._timer.start()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._schedule_persist()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._schedule_persist()

# Wire receipt_confirm_sessions to the auto-persist dict at module load
def _init_auto_persist():
    """Replace state.receipt_confirm_sessions with the auto-persisting version."""
    apd = _AutoPersistDict()
    apd.update(state.receipt_confirm_sessions)
    state.receipt_confirm_sessions = apd

_init_auto_persist()

def persist_birthday_pending():
    """Delegated to crm module — imported here for convenience in sessions."""
    from crm import persist_birthday_pending as _pbp
    _pbp()

def load_birthday_pending_from_sheet():
    from crm import load_birthday_pending_from_sheet as _lbp
    _lbp()
