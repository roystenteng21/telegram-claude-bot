import re
from datetime import date, datetime, timedelta
from config import DATE_FORMATS, QUESTION_WORDS, COMMAND_PREFIXES, TIMEZONE

# ── Date / contact helpers ─────────────────────────────────────────────────────

def format_date(date_str):
    try:
        d = datetime.strptime(date_str, "%d/%m/%Y")
        return d.strftime("%d %b %Y")
    except (ValueError, TypeError):
        return date_str

def calculate_age(birthday_str):
    try:
        bday = datetime.strptime(birthday_str, "%d/%m/%Y").date()
        today = date.today()
        age = today.year - bday.year - ((today.month, today.day) < (bday.month, bday.day))
        return str(age)
    except (ValueError, TypeError):
        return ""

def format_contact(r, show_private=False):
    name = r.get("Name", "")
    alias = r.get("Alias", "")
    birthday = r.get("Birthday", "")
    age = calculate_age(birthday) if birthday else ""
    relationship = r.get("Relationship", "")
    context = r.get("Context", "")
    notes_raw = r.get("Notes", "")
    followup_date = r.get("Follow Up Date", "")
    followup_notes = r.get("Follow Up Notes", "")
    last_updated = r.get("Last Updated", "")
    referred_by = r.get("Referred By", "")
    referral_date = r.get("Referral Date", "")
    email = r.get("Email", "")
    address = r.get("Address", "")
    lines = [f"*{name}*"]
    if alias:
        lines.append(f"- Also known as: {alias}")
    if relationship:
        lines.append(f"- Relationship: {relationship}")
    if context:
        lines.append(f"- Context: {context}")
    if birthday and age:
        lines.append(f"- Birthday: {format_date(birthday)} (age {age})")
    elif birthday:
        lines.append(f"- Birthday: {format_date(birthday)}")
    if notes_raw:
        note_items = [n.strip() for n in notes_raw.split(";") if n.strip()]
        lines.append("- Notes:")
        for n in note_items:
            lines.append(f"  - {n}")
    else:
        lines.append("- Notes:\n  - None")
    if followup_date:
        lines.append(f"- Follow Up: {format_date(followup_date)}")
        if followup_notes:
            lines.append(f"  - {followup_notes}")
    if referred_by:
        ref_line = f"- Referred by: {referred_by}"
        if referral_date:
            ref_line += f" on {format_date(referral_date)}"
        lines.append(ref_line)
    if show_private:
        if email:
            lines.append(f"- Email: {email}")
        if address:
            lines.append(f"- Address: {address}")
    if last_updated:
        lines.append(f"\n_Last updated: {format_date(last_updated)}_")
    return "\n".join(lines)

def parse_date_flexible(date_str):
    """Try multiple date formats. Returns date object or None."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None

def generate_reminder_id():
    """Simple unique ID based on timestamp."""
    return datetime.now().strftime("%Y%m%d%H%M%S")

def generate_trip_id():
    return date.today().strftime("TRIP-%Y%m%d")

def get_next_recurrence(scheduled_time_str, recurrence):
    """Calculate the next fire time for a recurring reminder."""
    try:
        dt = datetime.strptime(scheduled_time_str, "%Y-%m-%d %H:%M")
        if recurrence == "daily":
            return (dt + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        elif recurrence == "weekly" or "monday" in recurrence.lower() or "every" in recurrence.lower():
            return (dt + timedelta(weeks=1)).strftime("%Y-%m-%d %H:%M")
        elif recurrence == "monthly":
            if dt.month == 12:
                next_dt = dt.replace(year=dt.year + 1, month=1)
            else:
                next_dt = dt.replace(month=dt.month + 1)
            return next_dt.strftime("%Y-%m-%d %H:%M")
        return None
    except Exception as e:
        print(f"get_next_recurrence error: {e}")
        return None

def get_source_label(source_name):
    """Return a readable source label, or None if source is obscure/unknown."""
    from config import SOURCE_LABELS, _OBSCURE_SOURCES
    lower = source_name.lower()
    for key, label in SOURCE_LABELS.items():
        if key in lower:
            return label
    for obs in _OBSCURE_SOURCES:
        if obs in lower:
            return None
    return None

def _looks_like_command(text):
    """Return True if text looks like a command or question rather than an expense entry."""
    lower = text.lower().strip()
    first_word = lower.split()[0] if lower.split() else ""
    if first_word in QUESTION_WORDS:
        return True
    if any(lower.startswith(p) for p in COMMAND_PREFIXES):
        return True
    return False

# ── Session interrupt guard ────────────────────────────────────────────────────

_SESSION_REPLY_TOKENS = {
    "yes", "y", "no", "n", "cancel", "skip", "done", "update", "new",
    "confirm", "ok", "okay", "sure", "nope", "yep", "yup"
}

def looks_like_new_intent(text):
    """Return True if the message looks like a fresh command, not a session reply."""
    lower = text.strip().lower()
    if lower in _SESSION_REPLY_TOKENS:
        return False
    if re.match(r"^\d+$", lower.strip()):
        return False
    words = lower.split()
    if len(words) <= 2:
        return False
    new_intent_triggers = [
        "remind me", "set a reminder", "add reminder",
        "spent ", "paid ", "bought ", "expense ",
        "save ", "add contact", "find ", "pull up ",
        "meeting recap", "start a recap",
        "overseas", "i'm in ", "flying to",
        "what's ", "how is ", "market summary",
        "alert me", "price of ", "check ",
        "bill ", "my ", "schedule ", "book ",
        "todo ", "add to my list", "remind",
        "cancel reminder", "delete ",
    ]
    if any(lower.startswith(t) for t in new_intent_triggers):
        return True
    return False

# ── Safe message sender ────────────────────────────────────────────────────────

async def send_safe(target, text, parse_mode=None):
    """Send a message, splitting into chunks if over Telegram's 4096 char limit."""
    MAX = 4096
    if len(text) <= MAX:
        try:
            await target.reply_text(text, parse_mode=parse_mode)
        except Exception:
            await target.reply_text(text)
        return
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    for chunk in chunks:
        try:
            await target.reply_text(chunk, parse_mode=parse_mode)
        except Exception:
            await target.reply_text(chunk)

# ── Dev alert ─────────────────────────────────────────────────────────────────

async def alert_error(message: str):
    """Send a dev alert to Telegram on silent failures. Never raises."""
    try:
        import state
        if state._app_ref:
            from config import YOUR_CHAT_ID
            await state._app_ref.bot.send_message(chat_id=YOUR_CHAT_ID, text=message)
    except Exception as e:
        print(f"alert_error failed: {e}")
