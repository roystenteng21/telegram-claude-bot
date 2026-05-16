import re
import json
from datetime import date, datetime, timedelta
import asyncio
import state
from config import TIMEZONE, YOUR_CHAT_ID
from clients import client
from google.oauth2 import service_account
from googleapiclient.discovery import build
import os

# ---------------------------------------------------------------------------
# Calendar ID map
# ---------------------------------------------------------------------------

CALENDAR_IDS = {
    "Personal":       "094cd89eb5d2892c0a8c8a010705d5579b0f138de525eb9f4af504891100768b@group.calendar.google.com",
    "Appointment":    "28d3e5cc311ff0fb3db2c7e3ea179bdef7e8c4344521c94db05343d4c015e1e6@group.calendar.google.com",
    "Work Meeting":   "13be3f7c21c5c948b78f766de1b1c5f07f1fee3af7d6569fc35be8726abeef72@group.calendar.google.com",
    "Estate Planning":"288badc142d88a5535d2c256a55a85b014e27a7dcd59a01cd7a401289d1b0522@group.calendar.google.com",
    "Closing":        "0c29a7d3c34c0b2239a9e0a35e061b8ed06f7dfb5d18036bdd58106959f49541@group.calendar.google.com",
    "Urgent":         "c4fb92c5b60367040fd5a00702fd5416dd1d9b3d2b4fbdffcda1ce3adcd35349@group.calendar.google.com",
    "Appointment":    "28d3e5cc311ff0fb3db2c7e3ea179bdef7e8c4344521c94db05343d4c015e1e6@group.calendar.google.com",
}

KNOWN_CALENDARS = list(CALENDAR_IDS.keys())

# ---------------------------------------------------------------------------
# Google Calendar service
# ---------------------------------------------------------------------------

def _get_service():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)


def _get_calendar_id(name):
    if not name:
        return CALENDAR_IDS["Personal"]
    for cal_name, cal_id in CALENDAR_IDS.items():
        if name.lower() == cal_name.lower():
            return cal_id
    for cal_name, cal_id in CALENDAR_IDS.items():
        if name.lower() in cal_name.lower() or cal_name.lower() in name.lower():
            return cal_id
    return CALENDAR_IDS["Personal"]


# ---------------------------------------------------------------------------
# Calendar name matching
# ---------------------------------------------------------------------------

def _match_calendar_name(raw):
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
# Date resolution — programmatic, never left to Haiku
# ---------------------------------------------------------------------------

def _resolve_date_anchors():
    today = date.today()
    anchors = {"today": today.strftime("%d %b %Y")}
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
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
# Parse calendar request via Haiku
# ---------------------------------------------------------------------------

def parse_calendar_request(text):
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
    return json.loads(clean)


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
# Write event to Google Calendar
# ---------------------------------------------------------------------------

async def write_calendar_event(parsed):
    try:
        start = datetime.strptime(parsed["start"], "%d %b %Y %H:%M")
        end = datetime.strptime(parsed["end"], "%d %b %Y %H:%M")
    except Exception as e:
        return f"❌ Date parse error: {e}"

    cal_name = parsed.get("calendar", "Personal")
    cal_id = _get_calendar_id(cal_name)

    event_body = {
        "summary": parsed.get("title", "Event"),
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TIMEZONE},
        "end":   {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),   "timeZone": TIMEZONE},
    }
    if parsed.get("location"):
        event_body["location"] = parsed["location"]
    if parsed.get("notes"):
        event_body["description"] = parsed["notes"]

    try:
        service = await asyncio.to_thread(_get_service)
        await asyncio.to_thread(
            lambda: service.events().insert(calendarId=cal_id, body=event_body).execute()
        )
    except Exception as e:
        return f"❌ Couldn't add event: {str(e)}"

    try:
        start_fmt = f"{start.strftime('%a %d %b %Y')}, {start.strftime('%I:%M%p').lstrip('0').lower()} – {end.strftime('%I:%M%p').lstrip('0').lower()}"
    except Exception:
        start_fmt = parsed["start"]

    reply = f"✅ *{parsed.get('title')}* added to {cal_name}\n{start_fmt}"
    if parsed.get("location"):
        reply += f"\n📍 {parsed['location']}"
    return reply


# ---------------------------------------------------------------------------
# smart_add_event — parse + store in confirm session (no immediate write)
# ---------------------------------------------------------------------------

def smart_add_event(text, user_id):
    try:
        parsed = parse_calendar_request(text)
    except json.JSONDecodeError:
        return "⚠️ Couldn't parse that — try: cal Branson appointment fri 10-12pm"
    except Exception as e:
        return "⚠️ Couldn't parse that — try: cal Branson appointment fri 10-12pm"

    if not parsed.get("title") or not parsed.get("start"):
        return "⚠️ Couldn't parse that — try: cal Branson appointment fri 10-12pm"

    try:
        start_dt = datetime.strptime(parsed["start"], "%d %b %Y %H:%M")
        if start_dt.year < date.today().year:
            return f"⚠️ That date looks wrong ({parsed['start']}) — can you clarify the date?"
    except Exception:
        pass

    parsed["calendar"] = _match_calendar_name(parsed.get("calendar", ""))
    state.calendar_confirm_sessions[user_id] = {"parsed": parsed}
    return format_calendar_confirm(parsed)


# ---------------------------------------------------------------------------
# Apply inline edit during confirm session
# ---------------------------------------------------------------------------

def apply_calendar_edit(user_id, edit_text):
    if user_id not in state.calendar_confirm_sessions:
        return "No pending calendar event to edit."

    parsed = state.calendar_confirm_sessions[user_id]["parsed"]

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
        if field in ("start", "end", "title", "location", "notes"):
            parsed[field] = value
        elif field == "time":
            parsed["start"] = value

        state.calendar_confirm_sessions[user_id]["parsed"] = parsed
        return format_calendar_confirm(parsed)
    except Exception:
        return "⚠️ Couldn't apply that edit — try again, e.g. 'change time to 2-4pm'"


# ---------------------------------------------------------------------------
# Get events
# ---------------------------------------------------------------------------

async def get_events(days=1):
    try:
        service = await asyncio.to_thread(_get_service)
        now = datetime.utcnow().isoformat() + "Z"
        end_dt = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"

        all_events = []
        for cal_name, cal_id in CALENDAR_IDS.items():
            try:
                result = await asyncio.to_thread(
                    lambda cid=cal_id: service.events().list(
                        calendarId=cid,
                        timeMin=now,
                        timeMax=end_dt,
                        singleEvents=True,
                        orderBy="startTime"
                    ).execute()
                )
                for e in result.get("items", []):
                    start_raw = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date")
                    all_events.append((e.get("summary", "No title"), start_raw, cal_name))
            except Exception:
                continue

        if not all_events:
            label = "today" if days == 1 else "this week"
            return f"📅 No events {label}"

        all_events.sort(key=lambda x: x[1] or "")
        label = "Today's events" if days == 1 else "This week's events"
        response = f"📅 *{label}:*\n\n"
        for summary, start_raw, cal_name in all_events:
            try:
                if "T" in start_raw:
                    dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    time_str = dt.strftime("%d %b, %I:%M%p").lstrip("0").lower()
                else:
                    time_str = start_raw
            except Exception:
                time_str = start_raw or "?"
            response += f"• *{summary}* — {time_str} ({cal_name})\n"
        return response
    except Exception as e:
        return f"⚠️ Calendar error: {str(e)}"


# ---------------------------------------------------------------------------
# Find upcoming events (for delete flow)
# ---------------------------------------------------------------------------

async def find_upcoming_events(title_query):
    try:
        service = await asyncio.to_thread(_get_service)
        now = datetime.utcnow().isoformat() + "Z"
        end_dt = (datetime.utcnow() + timedelta(days=365)).isoformat() + "Z"

        matches = []
        for cal_name, cal_id in CALENDAR_IDS.items():
            try:
                result = await asyncio.to_thread(
                    lambda cid=cal_id: service.events().list(
                        calendarId=cid,
                        timeMin=now,
                        timeMax=end_dt,
                        singleEvents=True,
                        orderBy="startTime"
                    ).execute()
                )
                for e in result.get("items", []):
                    summary = e.get("summary", "")
                    if title_query.lower() in summary.lower():
                        start_raw = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date")
                        matches.append(({"event_id": e["id"], "cal_id": cal_id}, summary, start_raw))
            except Exception:
                continue

        return matches, None
    except Exception as e:
        return None, f"⚠️ Calendar error: {str(e)}"


# ---------------------------------------------------------------------------
# Delete calendar event
# ---------------------------------------------------------------------------

async def delete_calendar_event(summary_or_meta):
    try:
        service = await asyncio.to_thread(_get_service)
        # If passed a dict with event_id + cal_id (from confirm session)
        if isinstance(summary_or_meta, dict):
            await asyncio.to_thread(
                lambda: service.events().delete(
                    calendarId=summary_or_meta["cal_id"],
                    eventId=summary_or_meta["event_id"]
                ).execute()
            )
            return f"✅ Event deleted"

        # Fallback: search by title
        matches, err = await find_upcoming_events(summary_or_meta)
        if err:
            return err
        if not matches:
            return f"❌ No upcoming event found matching '{summary_or_meta}'"
        meta, summary, _ = matches[0]
        await asyncio.to_thread(
            lambda: service.events().delete(
                calendarId=meta["cal_id"],
                eventId=meta["event_id"]
            ).execute()
        )
        return f"✅ *{summary}* deleted"
    except Exception as e:
        return f"❌ Error deleting event: {str(e)}"


# ---------------------------------------------------------------------------
# Daily iCloud check — replaced with Google Calendar connectivity check
# ---------------------------------------------------------------------------

async def check_icloud_daily(app):
    # No-op — Google Calendar has no equivalent downtime pattern
    pass


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
