import json
from datetime import date
import state
from config import MEETING_START_PHRASES, MEETING_DONE_PHRASES, YOUR_CHAT_ID
from clients import client
from sheets import get_sheet
from crm import find_row, _get_crm_records  # one-way permitted cross-feature import

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

def process_meeting_notes(event_name, notes_list):
    combined = "\n".join(notes_list)
    word_count = len(combined.split())
    # M9: Haiku for short notes (<500 words), Sonnet for longer recaps
    model = "claude-sonnet-4-6" if word_count >= 500 else "claude-haiku-4-5-20251001"
    max_tokens = 800 if model == "claude-sonnet-4-6" else 600
    prompt = (
        f"You are processing meeting notes for an event called: {event_name or 'unknown event'}\n\n"
        f"Here are the raw notes:\n{combined}\n\n"
        f"Extract and return a JSON object with these fields:\n"
        f"- event_name: string\n"
        f"- topic: string (1 line summary)\n"
        f"- summary: string (2-4 sentences capturing key points)\n"
        f"- action_items: list of strings\n"
        f"- contacts_mentioned: list of strings\n"
        f"- foreign_phrases: dict\n\n"
        f"Return ONLY the JSON object, no markdown, no preamble."
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
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

def save_meeting_recap(recap):
    try:
        sheet = get_sheet("Meeting Notes")
        if not sheet:
            from clients import spreadsheet
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
    try:
        from clients import spreadsheet
        sheet = spreadsheet.worksheet("Meeting Notes")
        try:
            records = sheet.get_all_records()
        except Exception:
            return "⚠️ Couldn't search meeting notes right now — try again in a moment."
        if not records:
            return "No meeting notes saved yet."
        query_lower = query.lower().strip()
        results = [r for r in records if query_lower in " ".join(str(v).lower() for v in r.values())]
        if not results:
            return f"No meeting notes found matching '{query}'."
        lines = [f"Found {len(results)} recap(s):\n"]
        for r in results:
            lines.append(f"📋 {r.get('Event Name', '')} — {r.get('Date', '')}")
            lines.append(f"   {r.get('Topic', '')}")
            lines.append("")
        return "\n".join(lines)
    except Exception:
        return "⚠️ Couldn't search meeting notes right now — try again in a moment."

async def handle_meeting_session(user_id, text, update):
    session = state.meeting_sessions[user_id]
    step = session.get("step")
    if step == "confirming":
        if text.strip().upper() == "Y":
            saved, err = save_meeting_recap(session["pending_recap"])
            contacts = session["pending_recap"].get("contacts_mentioned", [])
            for name in contacts:
                row_num, record = find_row(name)
                if record and row_num != "disambig":
                    pass
            del state.meeting_sessions[user_id]
            state.session_timestamps.pop(user_id, None)
            if saved:
                await update.message.reply_text("Saved! 📋")
            else:
                await update.message.reply_text(
                    f"⚠️ Couldn't save recap to sheet — keeping it in memory.\n"
                    f"Reply 'retry save' to try again or 'send recap' to have me send it as a message."
                )
                state.meeting_sessions[user_id] = {**session, "step": "save_failed"}
                from sessions import touch_session
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
                del state.meeting_sessions[user_id]
                state.session_timestamps.pop(user_id, None)
                await update.message.reply_text("Saved! 📋")
            else:
                await update.message.reply_text("Still couldn't save — try again in a moment.")
        elif lower_t == "send recap":
            recap = session["pending_recap"]
            msg = format_recap_confirmation(recap).replace("\nReply Y to save or E to edit.", "")
            del state.meeting_sessions[user_id]
            state.session_timestamps.pop(user_id, None)
            await update.message.reply_text(msg)
        return
    if step == "get_name":
        session["event_name"] = text.strip()
        session["step"] = "collecting"
        await update.message.reply_text(
            f"Got it. Send your notes for {text.strip()} and say done when you're finished."
        )
        return
    if is_meeting_done(text):
        if not session.get("notes"):
            await update.message.reply_text("You haven't sent any notes yet. Send your notes then say done.")
            return
        await update.message.reply_text("On it, give me a sec...")
        try:
            recap, is_raw = process_meeting_notes(session.get("event_name", ""), session["notes"])
            session["pending_recap"] = recap
            session["step"] = "confirming"
            from sessions import touch_session
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
    session["notes"].append(text)
