import re
import json
from datetime import date, datetime, timedelta
import state
from config import ICLOUD_USERNAME, ICLOUD_PASSWORD, TIMEZONE, YOUR_CHAT_ID
from clients import client
import caldav

KNOWN_CALENDARS = [
    "Estate Planning", "Work Meeting", "Personal",
    "Closing", "Urgent", "MinimaList", "Appointment"
]

# ---------------------------------------------------------------------------
# Credential guard
# ---------------------------------------------------------------------------

def _credentials_ok():
    return bool(ICLOUD_USERNAME and ICLOUD_PASSWORD)

def _no_credentials_msg():
    return "⚠️ iCloud Calendar isn't configured — check ICLOUD_USERNAME and ICLOUD_PASSWORD in Railway."


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def get_calendar(name=None):
    def _attempt():
        caldav_client = caldav.DAVClient(
            url="https://caldav.icloud.com",
            username=ICLOUD_USERNAME,
            password=ICLOUD_PASSWORD
        )
        principal = caldav_client.principal()
        calendars = principal.calendars()
        if name:
            for cal in calendars:
                if name.lower() in cal.name.lower():
                    return cal
            return None
        return calendars[0] if calendars else None

    try:
        result = _attempt()
        state._icloud_down = False
        return result
    except Exception:
        pass
    try:
        result = _attempt()
        state._icloud_down = False
        return result
    except Exception as e:
        state._icloud_down = True
        print(f"iCloud Calendar unavailable after retry: {e}")
        return None


async def check_icloud_daily(app):
    try:
        today = date.today()
        if state._icloud_down:
            if state._icloud_last_notified != today:
                state._icloud_last_notified = today
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text="⚠️ iCloud Calendar still unavailable — calendar features won't work.\nCheck your credentials in Railway."
                )
        else:
            cal = get_calendar()
            if cal is None and state._icloud_last_notified != today:
                state._icloud_last_notified = today
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text="⚠️ iCloud Calendar unavailable — calendar features won't work.\nCheck your credentials in Railway."
                )
    except Exception as e:
        print(f"check_icloud_daily error: {e}")


# ---------------------------------------------------------------------------
# Date resolution — programmatic, never left to Haiku
# ---------------------------------------------------------------------------

def _resolve_date_anchors():
    """Return a dict of label -> resolved date string for injection into prompts."""
    today = date.today()
    anchors = {"today": today.strftime("%d %b %Y")}
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    # This week and next week for each day
    for offset in range(1, 15):
        d = today + timedelta(days=offset)
        label = day_names[d.weekday()]
        if label not in anchors:
            anchors[label] = d.strftime("%d %b %Y")
        next_label = f"next {label}"
        if next_label not in anchors and offset >= 7:
            anchors[next_label] = d.strftime("%d %b %Y")
    anchors["tomorrow"] = (today + timedelta(days=1)).strftime("%d %b %Y")
    anchors["next week"] = (today + timedelta(days=7)).strftime("%d %b %Y")
    return anchors


def _build_date_anchor_block():
    anchors = _resolve_date_anchors()
    today = date.today()
    lines = [f"Today is {today.strftime('%d %b %Y')} ({today.strftime('%A')})."]
    lines.append("Resolved date references (use these exactly — do not infer or guess):")
    for label, resolved in anchors.items():
        lines.append(f"  '{label}' = {resolved}")
    lines.append(f"Current year is {today.year}. Never use a past year.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Calendar name matching
# ---------------------------------------------------------------------------

def _match_calendar_name(raw):
    """Match a raw string to the closest known calendar name."""
    if not raw:
        return "Personal"
    raw_lower = raw.lower()
    for cal in KNOWN_CALENDARS:
        if raw_lower == cal.lower():
            return cal
    for cal in KNOWN_CALENDARS:
        if raw_lower in cal.lower() or cal.lower() in raw_lower:
            return cal
    return "Personal"


# ---------------------------------------------------------------------------
# Parse calendar request via Haiku
# ---------------------------------------------------------------------------

def parse_calendar_request(text):
    """
    Parse a natural language calendar request into structured fields.
    Returns dict with keys: title, calendar, start, end, location, notes
    or None on failure.
    """
    date_block = _build_date_anchor_block()
    known_cals = ", ".join(KNOWN_CALENDARS)

    prompt = f"""{date_block}

Available calendars: {known_cals}

Parse this calendar request:
"{text}"

Respond ONLY with a JSON object — no other text, no markdown:
{{
  "title": "event title",
  "calendar": "exact calendar name from the list or Personal if unclear",
  "start": "DD MMM YYYY HH:MM",
  "end": "DD MMM YYYY HH:MM",
  "location": "location or empty string",
  "notes": "notes or empty string"
}}

Rules:
- title: the person or event name only, no calendar name in the title
- calendar: match exactly from the available list; if the user says 'appointment' use 'Appointment'
- start/end: always include the full year; never use a past year
- if no end time given, assume 1 hour after start
- if AM/PM not specified, use context (dinner = pm, morning = am)
- location: only if explicitly stated after the time, otherwise empty string
- never invent details not in the request"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    clean = raw.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(clean)
    return parsed


# ---------------------------------------------------------------------------
# Format confirmation message
# ---------------------------------------------------------------------------

def format_calendar_confirm(parsed):
    try:
        start = datetime.strptime(parsed["start"], "%d %b %Y %H:%M")
        end = datetime.strptime(parsed["end"], "%d %b %Y %H:%M")
        time_str = f"{start.strftime('%a %d %b %Y')}, {start.strftime('%I:%M%p').lstrip('0').lower()} – {end.strftime('%I:%M%p').lstrip('0').lower()}"
    except Exception:
        time_str = f"{parsed.get('start', '')} – {parsed.get('end', '')}"

    lines = [
        f"📅 *{parsed.get('title', '')}*",
        f"🗓 {parsed.get('calendar', 'Personal')}",
        "",
        time_str,
    ]
    if parsed.get("location"):
        lines.append(f"📍 {parsed['location']}")
    lines.append("")
    lines.append("Add it in? (yes / edit)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write event to iCloud
# ---------------------------------------------------------------------------

def write_calendar_event(parsed):
    """Write a parsed event dict to iCloud. Returns success/error string."""
    try:
        start = datetime.strptime(parsed["start"], "%d %b %Y %H:%M")
        end = datetime.strptime(parsed["end"], "%d %b %Y %H:%M")
    except Exception as e:
        return f"❌ Date parse error: {e}"

    calendar = get_calendar(parsed.get("calendar", "Personal"))
    if not calendar:
        calendar = get_calendar("Personal")
    if not calendar:
        return "⚠️ Couldn't connect to iCloud Calendar — check credentials in Railway."

    location_line = f"LOCATION:{parsed.get('location', '')}\n" if parsed.get("location") else ""
    notes_line = f"DESCRIPTION:{parsed.get('notes', '')}\n" if parsed.get("notes") else ""

    ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
        f"SUMMARY:{parsed.get('title', 'Event')}\n"
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}\n"
        f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}\n"
        f"{location_line}"
        f"{notes_line}"
        "END:VEVENT\nEND:VCALENDAR"
    )
    calendar.add_event(ics)

    try:
        start_fmt = f"{start.strftime('%a %d %b %Y')}, {start.strftime('%I:%M%p').lstrip('0').lower()} – {end.strftime('%I:%M%p').lstrip('0').lower()}"
    except Exception:
        start_fmt = parsed["start"]

    reply = f"✅ *{parsed.get('title')}* added to {calendar.name}\n{start_fmt}"
    if parsed.get("location"):
        reply += f"\n📍 {parsed['location']}"
    return reply


# ---------------------------------------------------------------------------
# smart_add_event — parse + store in confirm session (no immediate write)
# ---------------------------------------------------------------------------

def smart_add_event(text, user_id):
    if not _credentials_ok():
        return _no_credentials_msg()

    try:
        parsed = parse_calendar_request(text)
    except json.JSONDecodeError:
        return "⚠️ Couldn't parse that — try: cal Branson appointment fri 10-12pm"
    except Exception as e:
        if "caldav" in str(e).lower() or "icloud" in str(e).lower():
            return "⚠️ Couldn't connect to iCloud Calendar — check credentials in Railway."
        return "⚠️ Couldn't parse that — try: cal Branson appointment fri 10-12pm"

    if not parsed.get("title") or not parsed.get("start"):
        return "⚠️ Couldn't parse that — try: cal Branson appointment fri 10-12pm"

    # Validate year — never accept a past year
    try:
        start_dt = datetime.strptime(parsed["start"], "%d %b %Y %H:%M")
        if start_dt.year < date.today().year:
            return f"⚠️ That date looks wrong ({parsed['start']}) — can you clarify the date?"
    except Exception:
        pass

    # Normalise calendar name
    parsed["calendar"] = _match_calendar_name(parsed.get("calendar", ""))

    state.calendar_confirm_sessions[user_id] = {"parsed": parsed}
    return format_calendar_confirm(parsed)


# ---------------------------------------------------------------------------
# Handle edit during confirm session
# ---------------------------------------------------------------------------

def apply_calendar_edit(user_id, edit_text):
    """
    Apply an inline edit to a pending calendar confirm session.
    Returns updated confirmation string, or error string.
    """
    if user_id not in state.calendar_confirm_sessions:
        return "No pending calendar event to edit."

    parsed = state.calendar_confirm_sessions[user_id]["parsed"]
    lower = edit_text.lower().strip()

    # Build prompt to extract the field change
    prompt = f"""The user wants to edit a calendar event field.
Current event:
- title: {parsed.get('title')}
- calendar: {parsed.get('calendar')}
- start: {parsed.get('start')}
- end: {parsed.get('end')}
- location: {parsed.get('location', '')}

User edit request: "{edit_text}"

{_build_date_anchor_block()}

Respond ONLY with a JSON object — no other text:
{{
  "field": "title|calendar|start|end|location",
  "value": "new value"
}}

For start/end use format: DD MMM YYYY HH:MM
For calendar, match from: {', '.join(KNOWN_CALENDARS)}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        edit = json.loads(raw)
        field = edit.get("field", "")
        value = edit.get("value", "")

        if field == "calendar":
            value = _match_calendar_name(value)

        if field in ("start", "end"):
            parsed[field] = value
        elif field == "time":
            parsed["start"] = value
        elif field in parsed:
            parsed[field] = value
        
        state.calendar_confirm_sessions[user_id]["parsed"] = parsed
        return format_calendar_confirm(parsed)
    except Exception as e:
        return f"⚠️ Couldn't apply that edit — try again, e.g. 'change time to 2-4pm'"


# ---------------------------------------------------------------------------
# Get events
# ---------------------------------------------------------------------------

def get_events(days=1):
    if not _credentials_ok():
        return _no_credentials_msg()
    try:
        calendar = get_calendar("Personal")
        if not calendar:
            return "⚠️ Couldn't connect to iCloud Calendar — check credentials in Railway."
        start = datetime.now()
        end = start + timedelta(days=days)
        events = calendar.date_search(start=start, end=end)
        if not events:
            label = "today" if days == 1 else "this week"
            return f"📅 No events {label}"
        label = "Today's events" if days == 1 else "This week's events"
        response = f"📅 *{label}:*\n\n"
        for event in events:
            e = event.vobject_instance.vevent
            summary = str(e.summary.value)
            dtstart = e.dtstart.value
            if hasattr(dtstart, 'strftime'):
                response += f"• *{summary}* — {dtstart.strftime('%d %b, %H:%M')}\n"
            else:
                response += f"• *{summary}* — {dtstart}\n"
        return response
    except Exception as e:
        return f"⚠️ Calendar error: {str(e)} — check your iCloud credentials in Railway."


# ---------------------------------------------------------------------------
# Delete calendar event — find + confirm, no immediate delete
# ---------------------------------------------------------------------------

def find_upcoming_events(title_query):
    """Search upcoming events matching title. Returns list of (event_obj, summary, start_dt)."""
    if not _credentials_ok():
        return None, _no_credentials_msg()
    try:
        calendar = get_calendar()
        if not calendar:
            return None, "⚠️ Couldn't connect to iCloud Calendar — check credentials in Railway."
        start = datetime.now()
        end = start + timedelta(days=365)
        events = calendar.date_search(start=start, end=end)
        matches = []
        for event in events:
            e = event.vobject_instance.vevent
            summary = str(e.summary.value)
            if title_query.lower() in summary.lower():
                dtstart = e.dtstart.value
                matches.append((event, summary, dtstart))
        return matches, None
    except Exception as e:
        return None, f"⚠️ Calendar error: {str(e)}"


def delete_calendar_event(title):
    """Kept for confirm_session action compatibility — deletes by stored event obj."""
    if not _credentials_ok():
        return _no_credentials_msg()
    try:
        calendar = get_calendar()
        if not calendar:
            return "❌ Could not connect to iCloud Calendar"
        start = datetime.now()
        end = start + timedelta(days=365)
        events = calendar.date_search(start=start, end=end)
        for event in events:
            e = event.vobject_instance.vevent
            if title.lower() in str(e.summary.value).lower():
                event.delete()
                return f"✅ *{str(e.summary.value)}* deleted"
        return f"❌ No upcoming event found matching '{title}'"
    except Exception as e:
        return f"❌ Error deleting event: {str(e)}"


# ---------------------------------------------------------------------------
# is_calendar_request — unchanged
# ---------------------------------------------------------------------------

def is_calendar_request(text):
    lower = text.lower().strip()
    QUERY_TRIGGERS = [
        "what's on", "whats on", "what is on", "do i have anything",
        "any events", "what have i got", "what do i have",
        "calendar today", "calendar tomorrow", "calendar this week",
        "show my calendar", "what's happening", "whats happening",
    ]
    if any(t in lower for t in QUERY_TRIGGERS):
        return True
    CALENDAR_TRIGGERS = [
        "schedule ", "add event", "create event", "new event",
        "book ", "set up a meeting", "set up meeting",
        "pencil in", "block out", "block off",
        "put in my calendar", "add to calendar", "add to my calendar",
        "add a meeting", "add meeting", "meeting at ", "meeting on ",
        "dinner at ", "dinner on ", "lunch at ", "lunch on ",
        "drinks at ", "drinks on ", "breakfast at ",
        "call at ", "call on ", "appointment at ", "appointment on ",
        "catch up at ", "catch up on ", "event at ", "event on ",
        "remind me on calendar", "add reminder to calendar",
    ]
    if any(t in lower for t in CALENDAR_TRIGGERS):
        return True
    EXCLUSIONS = [
        "remind me", "reminder", "spent", "paid", "bought", "cost",
        "expense", "bill", "price", "how much", "what time", "what's the time",
        "stock", "portfolio", "restaurant", "save ", "note ", "find ",
        "search ", "todo", "weather",
    ]
    if any(e in lower for e in EXCLUSIONS):
        return False
    TIME_WORDS = [
        "tomorrow", "tonight", "monday", "tuesday", "wednesday",
        "thursday", "friday", "saturday", "sunday", "next week",
        "this evening", "this afternoon", "this morning", "next month",
        "on the ", "this friday", "this saturday", "this sunday",
    ]
    AT_PATTERN = bool(re.search(r'\b(at|@)\s*\d{1,2}(:\d{2})?\s*(am|pm)?\b', lower))
    DATE_PATTERN = bool(re.search(r'\b\d{1,2}[\/\-]\d{1,2}\b', lower))
    has_time_anchor = AT_PATTERN or DATE_PATTERN or any(w in lower for w in TIME_WORDS)
    if not has_time_anchor:
        return False
    AMBIGUOUS_SIGNALS = [
        "meet", "meeting", "catch up", "dinner", "lunch", "drinks",
        "coffee", "call", "appointment", "event", "session",
        "interview", "presentation", "visit",
    ]
    if any(s in lower for s in AMBIGUOUS_SIGNALS) or AT_PATTERN:
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=10,
                messages=[{"role": "user", "content":
                    f'Is this asking to add a calendar event? Reply YES or NO only.\n\n"{text}"'}]
            )
            return response.content[0].text.strip().upper() == "YES"
        except Exception as e:
            print(f"is_calendar_request API fallback error: {e}")
            return False
    return False
