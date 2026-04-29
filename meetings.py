import json
from datetime import date

import config
from clients import client, spreadsheet


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
    lower = text.lower()
    for phrase in ["recap for", "notes for", "meeting notes for", "taking notes for",
                   "meeting recap for", "log meeting for"]:
        if phrase in lower:
            idx = lower.index(phrase) + len(phrase)
            return text[idx:].strip().strip(".,!?")
    return ""

def tag_crm_contacts(text):
    try:
        from crm import _get_crm_records
        records = _get_crm_records()
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
    """Send notes to Claude and get structured recap as JSON. Falls back to raw if Claude fails."""
    combined = "\n".join(notes_list)
    prompt = (
        f"You are processing meeting notes for an event called: {event_name or 'unknown event'}\n\n"
        f"Here are the raw notes:\n{combined}\n\n"
        f"Extract and return a JSON object with these fields:\n"
        f"- event_name: string\n"
        f"- topic: string (1 line summary)\n"
        f"- summary: string (2-4 sentences)\n"
        f"- action_items: list of strings\n"
        f"- contacts_mentioned: list of strings\n"
        f"- foreign_phrases: dict (original → English translation)\n\n"
        f"Return ONLY the JSON object, no markdown, no preamble."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw), False
    except Exception as e:
        print(f"process_meeting_notes Claude error: {e}")
        return {
            "event_name": event_name or "Untitled",
            "topic": "Raw notes — processing failed",
            "summary": combined,
            "action_items": [],
            "contacts_mentioned": [],
            "foreign_phrases": {}
        }, True

def format_recap_confirmation(recap):
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
        print(f"save_meeting_recap error: {e}")
        return False, str(e)


async def handle_meeting_session(user_id, text, update):
    """Handle an active meeting recap session. Returns True if handled."""
    from state import touch_session
    session = config.meeting_sessions.get(user_id)
    if not session:
        return False

    touch_session(user_id)
    lower = text.strip().lower()

    if lower == "cancel":
        del config.meeting_sessions[user_id]
        config.session_timestamps.pop(user_id, None)
        await update.message.reply_text("Recap cancelled.")
        return True

    step = session.get("step")

    # Awaiting event name
    if step == "get_name":
        session["event_name"] = text.strip()
        session["step"] = "collecting"
        config.meeting_sessions[user_id] = session
        await update.message.reply_text(f"Got it — taking notes for {text.strip()}. Send your notes and say done when finished.")
        return True

    # Confirming recap
    if step == "confirming":
        if lower in ["y", "yes", "save", "confirm"]:
            recap = session.get("pending_recap", {})
            success, err = save_meeting_recap(recap)
            del config.meeting_sessions[user_id]
            config.session_timestamps.pop(user_id, None)
            if success:
                await update.message.reply_text(f"Saved ✅ — {recap.get('event_name', 'Meeting')} recap is in your Meeting Notes sheet.")
            else:
                await update.message.reply_text(f"Couldn't save: {err}")
            return True
        elif lower in ["e", "edit"]:
            await update.message.reply_text("What would you like to change? (topic / summary / action items)")
            session["step"] = "editing"
            config.meeting_sessions[user_id] = session
            return True
        elif lower in ["n", "no", "cancel", "discard"]:
            del config.meeting_sessions[user_id]
            config.session_timestamps.pop(user_id, None)
            await update.message.reply_text("Recap discarded.")
            return True
        else:
            await update.message.reply_text("Reply Y to save or E to edit.")
            return True

    # Editing recap
    if step == "editing":
        recap = session.get("pending_recap", {})
        lower_edit = lower
        if "topic" in lower_edit:
            new_topic = text.strip()
            for keyword in ["topic:", "topic "]:
                if keyword in lower_edit:
                    new_topic = text[lower_edit.index(keyword) + len(keyword):].strip()
                    break
            recap["topic"] = new_topic
        elif "summary" in lower_edit:
            new_summary = text.strip()
            if "summary:" in lower_edit or "summary " in lower_edit:
                key = "summary:" if "summary:" in lower_edit else "summary "
                new_summary = text[lower_edit.index(key) + len(key):].strip()
            recap["summary"] = new_summary
        elif "action" in lower_edit:
            items = [i.strip() for i in text.split(",") if i.strip() and "action" not in i.lower()]
            recap["action_items"] = items
        session["pending_recap"] = recap
        session["step"] = "confirming"
        config.meeting_sessions[user_id] = session
        await update.message.reply_text(format_recap_confirmation(recap))
        return True

    # Collecting notes
    if is_meeting_done(text):
        if not session.get("notes"):
            await update.message.reply_text("No notes yet — send some notes first.")
            return True
        try:
            event_name = session.get("event_name", "")
            recap, is_raw = process_meeting_notes(event_name, session["notes"])
            session["pending_recap"] = recap
            session["step"] = "confirming"
            config.meeting_sessions[user_id] = session
            touch_session(user_id)
            if is_raw:
                await update.message.reply_text(
                    "⚠️ Took too long to process — saved your raw notes.\n"
                    "Reply 'retry recap' to process them when ready, or Y to save as-is."
                )
            await update.message.reply_text(format_recap_confirmation(recap))
        except Exception as e:
            await update.message.reply_text(f"Couldn't process the notes: {str(e)}. Try again.")
        return True

    session["notes"].append(text)
    config.meeting_sessions[user_id] = session
    return True


async def handle_meeting_edit_session(user_id, update):
    """Handles confirm_sessions for meeting-related edits — not needed separately."""
    pass
