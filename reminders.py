import re
import json
from datetime import date, datetime, timedelta
import state
from config import TIMEZONE, YOUR_CHAT_ID
from clients import client
from sheets import reminders_sheet, get_sheet, log_error_to_em_log
from helpers import generate_reminder_id, get_next_recurrence

def parse_reminder_request(text):
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
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"parse_reminder_request error: {e}")
        return None

def parse_reschedule_request(text, original_message):
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
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"parse_reschedule_request JSON error: {e} | raw: {raw[:100]}")
        return None

def add_reminder(message, scheduled_time_str, recurrence="once", contact=""):
    state._pending_reminders_cache = None
    sheet = reminders_sheet()
    reminder_id = generate_reminder_id()
    sheet.append_row([reminder_id, message, scheduled_time_str, recurrence, "pending", "0", contact])
    return reminder_id

def cancel_reminder_by_keyword(keyword):
    global state
    sheet = reminders_sheet()
    records = sheet.get_all_records()
    matches = [
        (i + 2, r) for i, r in enumerate(records)
        if keyword.lower() in r.get("Message", "").lower() and r.get("Status") == "pending"
    ]
    if not matches:
        return []
    if len(matches) > 1:
        return ["_DISAMBIG_:" + "|".join(f"{row}:{r.get('Message','')} — {r.get('Scheduled Time','')}"
                                          for row, r in matches)]
    row_idx, r = matches[0]
    sheet.update_cell(row_idx, 5, "cancelled")
    state._pending_reminders_cache = None
    return [r.get("Message", "")]

def list_reminders():
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

async def check_and_fire_reminders(app):
    """Check pending reminders every minute. Uses in-memory cache — re-reads every 5 min or on invalidation."""
    try:
        now = datetime.now(TIMEZONE).replace(second=0, microsecond=0)
        cache_age = (datetime.now(TIMEZONE) - state._pending_reminders_cache_ts).total_seconds() if state._pending_reminders_cache_ts else 999
        if state._pending_reminders_cache is None or cache_age > 300:
            sheet = reminders_sheet()
            records = sheet.get_all_records()
            state._pending_reminders_cache = [(i, r) for i, r in enumerate(records) if r.get("Status") == "pending"]
            state._pending_reminders_cache_ts = datetime.now(TIMEZONE)
        pending_records = state._pending_reminders_cache
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
            if scheduled <= now:
                try:
                    attempts = int(r.get("Attempts", 0))
                    message = r.get("Message", "")
                    recurrence = r.get("Recurrence", "once")
                    contact = r.get("Contact", "")
                    row = i + 2
                    reminder_msg = f"🔔 Reminder: {message}"
                    if contact:
                        from crm import _get_crm_records
                        crm_records = _get_crm_records()
                        contact_l = contact.lower()
                        matched = next((r for r in crm_records if r.get("Name", "").lower() == contact_l), None)
                        if matched:
                            notes = matched.get("Notes", "")
                            if notes:
                                reminder_msg += f"\n\n({contact}: {notes.split(';')[0].strip()})"
                    await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=reminder_msg)
                    state.last_fired_reminder[YOUR_CHAT_ID] = {
                        "id": r.get("ID"), "message": message, "row": row
                    }
                    if recurrence != "once":
                        next_time = get_next_recurrence(scheduled_str, recurrence)
                        sheet = reminders_sheet()
                        if next_time:
                            sheet.update_cell(row, 3, next_time)
                            sheet.update_cell(row, 6, "0")
                        else:
                            sheet.update_cell(row, 5, "sent")
                        state._pending_reminders_cache = None
                    else:
                        sheet = reminders_sheet()
                        if attempts == 0:
                            retry_time = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
                            sheet.update_cell(row, 3, retry_time)
                            sheet.update_cell(row, 6, "1")
                        else:
                            sheet.update_cell(row, 5, "sent")
                        state._pending_reminders_cache = None
                except Exception as e:
                    print(f"check_and_fire_reminders: failed to fire reminder id={r.get('ID', '?')}: {e}")
    except Exception as e:
        print(f"Error in check_and_fire_reminders: {e}")

def is_reminder_request(text):
    lower = text.lower()
    triggers = [
        "remind me", "set a reminder", "set me a reminder",
        "reminder for", "reminder at", "reminder to",
        "don't let me forget", "dont let me forget",
        "i need to remember to", "i need to remember",
        "don't let me forget to", "dont let me forget to",
        "alert me when", "alert me to", "alert me if",
        "notify me when", "notify me about", "notify me to",
        "ping me at", "ping me when", "ping me to",
        "drop me a reminder", "drop a reminder", "send me a reminder"
    ]
    return any(t in lower for t in triggers)

def is_reschedule_request(text):
    lower = text.lower()
    triggers = ["remind me again", "snooze", "again in", "remind again", "push it to", "reschedule"]
    return any(t in lower for t in triggers)

def is_cancel_reminder_request(text):
    lower = text.lower()
    return ("cancel" in lower or "delete" in lower or "remove" in lower) and "reminder" in lower

def handle_new_reminder(text):
    try:
        from crm import find_row
        parsed = parse_reminder_request(text)
        if parsed is None:
            return "⚠️ Couldn't parse that — what are the reminder details?\n(e.g. Call John, 30 Apr 2026, 9am)"
        message = parsed.get("message", text)
        scheduled_time = parsed.get("scheduled_time", "")
        recurrence = parsed.get("recurrence", "once")
        contact = parsed.get("contact", "")
        if not scheduled_time:
            return "Couldn't figure out when to remind you. Try: 'remind me to call James tomorrow at 3pm'."
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
        return "⚠️ Couldn't parse that — what are the reminder details?\n(e.g. Call John, 30 Apr 2026, 9am)"

def handle_reschedule(text, user_id):
    try:
        last = state.last_fired_reminder.get(user_id)
        if not last:
            return "Not sure which reminder you mean. Can you be more specific?"
        parsed = parse_reschedule_request(text, last["message"])
        if not parsed:
            return "Couldn't parse the reschedule — try 'remind me again in 2 hours'."
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
        return f"❌ Couldn't reschedule: {str(e)}"
