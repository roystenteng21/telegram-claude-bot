import re
import json
from datetime import date, datetime, timedelta
import asyncio
import pytz
import state
from config import TIMEZONE, YOUR_CHAT_ID, TEXT_SHORTCUTS
from clients import client
from google.oauth2 import service_account
from googleapiclient.discovery import build
import os

# ---------------------------------------------------------------------------
# Calendar ID map
# ---------------------------------------------------------------------------

CALENDAR_IDS = {
    "Personal":        "094cd89eb5d2892c0a8c8a010705d5579b0f138de525eb9f4af504891100768b@group.calendar.google.com",
    "Appointment":     "28d3e5cc311ff0fb3db2c7e3ea179bdef7e8c4344521c94db05343d4c015e1e6@group.calendar.google.com",
    "Work Meeting":    "13be3f7c21c5c948b78f766de1b1c5f07f1fee3af7d6569fc35be8726abeef72@group.calendar.google.com",
    "Estate Planning": "288badc142d88a5535d2c256a55a85b014e27a7dcd59a01cd7a401289d1b0522@group.calendar.google.com",
    "Closing":         "0c29a7d3c34c0b2239a9e0a35e061b8ed06f7dfb5d18036bdd58106959f49541@group.calendar.google.com",
    "Urgent":          "c4fb92c5b60367040fd5a00702fd5416dd1d9b3d2b4fbdffcda1ce3adcd35349@group.calendar.google.com",
}

KNOWN_CALENDARS = list(CALENDAR_IDS.keys())

# ---------------------------------------------------------------------------
# Google Calendar service (cached — build() fetches discovery doc over HTTP)
# ---------------------------------------------------------------------------

def _get_service():
    """Build a new Calendar service per call — avoids shared state across threads."""
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
# Display helpers
# ---------------------------------------------------------------------------

def _fmt_event_time(dtstr):
    """Format a dateTime or date string for display. Returns (time_str, is_allday)."""
    if not dtstr:
        return "?", False
    if "T" in dtstr:
        try:
            dt = datetime.fromisoformat(dtstr.replace("Z", "+00:00"))
            return dt.strftime("%I:%M%p").lstrip("0").lower(), False
        except Exception:
            return dtstr, False
    return "all day", True


def _fmt_event_row(summary, cal_name, dtstart, dtend):
    """Format a single event row for disambiguation lists."""
    start_str, is_allday = _fmt_event_time(dtstart)
    if is_allday:
        try:
            d = datetime.strptime(dtstart, "%Y-%m-%d")
            date_label = d.strftime("%a %d %b %Y")
        except Exception:
            date_label = dtstart
        return f"{summary} — {cal_name} | {date_label} (all day)"
    try:
        dt = datetime.fromisoformat(dtstart.replace("Z", "+00:00"))
        date_label = dt.strftime("%a %d %b %Y")
    except Exception:
        date_label = dtstart
    end_str, _ = _fmt_event_time(dtend)
    return f"{summary} — {cal_name} | {date_label}, {start_str} – {end_str}"


def _fmt_delete_success(summary, cal_name, dtstart):
    """Format delete success message."""
    if not dtstart or "T" not in dtstart:
        return f"✅ *{summary}* deleted — {cal_name}"
    try:
        dt = datetime.fromisoformat(dtstart.replace("Z", "+00:00"))
        date_label = dt.strftime("%a %d %b %Y, %-I:%M%p").lower()
        return f"✅ *{summary}* deleted — {cal_name} | {date_label}"
    except Exception:
        return f"✅ *{summary}* deleted — {cal_name}"


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
- title: the person or event name only — if any word in the input matches a calendar name from the available list, it belongs in the calendar field, never in the title. Day-of-week words (monday, tuesday, wednesday, thursday, friday, saturday, sunday, mon, tue, wed, thu, fri, sat, sun) must NEVER appear in the title — they belong in the date field only
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
    if not response.content:
        raise ValueError("Empty response from Haiku")
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
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write event to Google Calendar
# ---------------------------------------------------------------------------

async def write_calendar_event(parsed):
    try:
        start = datetime.strptime(parsed["start"], "%d %b %Y %H:%M")
        end = datetime.strptime(parsed["end"], "%d %b %Y %H:%M")
    except Exception as e:
        return f"❌ Date parse error: {e}", None, None

    cal_name = parsed.get("calendar", "Personal")
    cal_id = _get_calendar_id(cal_name)

    event_body = {
        "summary": parsed.get("title", "Event"),
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": str(TIMEZONE)},
        "end":   {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),   "timeZone": str(TIMEZONE)},
    }
    if parsed.get("location"):
        event_body["location"] = parsed["location"]
    if parsed.get("notes"):
        event_body["description"] = parsed["notes"]

    created_event = None
    try:
        service = await asyncio.to_thread(_get_service)
        for attempt in range(2):
            try:
                created_event = await asyncio.to_thread(
                    lambda: service.events().insert(calendarId=cal_id, body=event_body).execute()
                )
                break
            except BrokenPipeError:
                if attempt == 0:
                    await asyncio.sleep(1)
                    service = await asyncio.to_thread(_get_service)
                else:
                    return "❌ Couldn't add event: connection error — try again", None, None
            except Exception as e:
                return f"❌ Couldn't add event: {str(e)}", None, None
    except Exception as e:
        return f"❌ Couldn't add event: {str(e)}", None, None

    event_id = created_event.get("id") if created_event else None

    try:
        start_fmt = f"{start.strftime('%a %d %b %Y')}, {start.strftime('%I:%M%p').lstrip('0').lower()} – {end.strftime('%I:%M%p').lstrip('0').lower()}"
    except Exception:
        start_fmt = parsed["start"]

    reply = f"✅ *{parsed.get('title')}* added to {cal_name}\n{start_fmt}"
    if parsed.get("location"):
        reply += f"\n📍 {parsed['location']}"
    return reply, event_id, cal_id


# ---------------------------------------------------------------------------
# smart_add_event — parse then write immediately
# ---------------------------------------------------------------------------

async def smart_add_event(text, user_id):
    try:
        parsed = await asyncio.to_thread(parse_calendar_request, text)
    except Exception:
        return "⚠️ Couldn't parse that — try: cal Dentist Personal 23 May 10am"

    if not parsed.get("title") or not parsed.get("start"):
        return "⚠️ Couldn't parse that — try: cal Dentist Personal 23 May 10am"

    GENERIC_TITLES = {"event", "appointment", "meeting", "cal", "calendar", "add", "new"}
    if parsed.get("title", "").strip().lower() in GENERIC_TITLES:
        return "What's the event called? Give me a title, date and time — e.g. cal Dentist 5 Jun 10am"

    try:
        start_dt = datetime.strptime(parsed["start"], "%d %b %Y %H:%M")
        if start_dt.year < date.today().year:
            return f"⚠️ That date looks wrong ({parsed['start']}) — can you clarify?"
    except Exception:
        pass

    parsed["calendar"] = _match_calendar_name(parsed.get("calendar", ""))

    # Strip calendar name words and TEXT_SHORTCUTS expansions from title
    cal_words = set()
    for cal in KNOWN_CALENDARS:
        for word in cal.lower().split():
            cal_words.add(word)
    # Also add expanded forms of TEXT_SHORTCUTS that map to calendar-related words
    for shortcut, expansion in TEXT_SHORTCUTS.items():
        if expansion.lower() in cal_words or any(expansion.lower() in cal.lower() for cal in KNOWN_CALENDARS):
            cal_words.add(shortcut.lower())
            cal_words.add(expansion.lower())
    # Always strip routing prefix words
    cal_words.update({"cal", "add", "new", "create"})
    title_words = parsed.get("title", "").split()
    cleaned_title = " ".join(w for w in title_words if w.lower() not in cal_words)
    if cleaned_title.strip():
        parsed["title"] = cleaned_title.strip()

    # Write immediately
    write_result, event_id, cal_id = await write_calendar_event(parsed)
    if write_result.startswith("❌"):
        return write_result

    # Format summary
    summary = format_calendar_confirm(parsed)

    # Em-generated short confirmation line
    CONFIRM_LINES = [
        "Done, it's in!", "Added!", "All set!", "Got it, locked in!",
        "On the calendar!", "Sorted!", "Done and dusted!", "It's in!"
    ]
    try:
        confirm_resp = await asyncio.to_thread(
            lambda: client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=15,
                messages=[{"role": "user", "content":
                    "Reply with ONE short casual confirmation under 6 words. No emoji, no quotes, no list, just the phrase. Example: Done, it's in!"}]
            )
        )
        confirm_line = confirm_resp.content[0].text.strip().strip('"').split("\n")[0]
        if len(confirm_line) > 40 or not confirm_line:
            raise ValueError("bad response")
    except Exception:
        import random
        confirm_line = random.choice(CONFIRM_LINES)

    # Fix E: async overlap check — fire-and-warn, non-blocking
    asyncio.create_task(_check_overlap_and_notify(parsed, event_id, cal_id))

    return f"{summary}\n\n{confirm_line}"


async def _check_overlap_and_notify(parsed, new_event_id, new_cal_id):
    """Background task: check for same-day time clashes after event write. Sends follow-up if clash found."""
    try:
        new_title = parsed.get("title", "Event")
        try:
            new_start = datetime.strptime(parsed["start"], "%d %b %Y %H:%M")
            new_end = datetime.strptime(parsed["end"], "%d %b %Y %H:%M")
        except Exception:
            return

        day_start = TIMEZONE.localize(datetime(new_start.year, new_start.month, new_start.day, 0, 0, 0))
        day_end = TIMEZONE.localize(datetime(new_start.year, new_start.month, new_start.day, 23, 59, 59))
        time_min = day_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = day_end.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        service = await asyncio.to_thread(_get_service)
        clashes = []
        for cal_name, cal_id in CALENDAR_IDS.items():
            try:
                result = await asyncio.to_thread(
                    lambda cid=cal_id: service.events().list(
                        calendarId=cid,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime"
                    ).execute()
                )
                for e in result.get("items", []):
                    if e.get("id") == new_event_id:
                        continue
                    e_start_raw = e.get("start", {}).get("dateTime")
                    e_end_raw = e.get("end", {}).get("dateTime")
                    if not e_start_raw or not e_end_raw:
                        continue
                    try:
                        e_start = datetime.fromisoformat(e_start_raw.replace("Z", "+00:00")).replace(tzinfo=None)
                        e_end = datetime.fromisoformat(e_end_raw.replace("Z", "+00:00")).replace(tzinfo=None)
                    except Exception:
                        continue
                    # Overlap: new_start < e_end AND new_end > e_start
                    if new_start < e_end and new_end > e_start:
                        e_summary = e.get("summary", "Unknown event")
                        e_start_fmt = e_start.strftime("%-I:%M%p").lstrip("0").lower()
                        e_end_fmt = e_end.strftime("%-I:%M%p").lstrip("0").lower()
                        clashes.append(f"*{e_summary}* ({e_start_fmt}–{e_end_fmt}, {cal_name})")
            except Exception:
                continue

        if clashes and state._app_ref:
            new_start_fmt = new_start.strftime("%-I:%M%p").lstrip("0").lower()
            new_end_fmt = new_end.strftime("%-I:%M%p").lstrip("0").lower()
            clash_list = "\n".join(f"• {c}" for c in clashes)
            msg = (
                f"⚠️ Heads up — *{new_title}* ({new_start_fmt}–{new_end_fmt}) clashes with:\n"
                f"{clash_list}"
            )
            await state._app_ref.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        print(f"_check_overlap_and_notify error: {e}")


# ---------------------------------------------------------------------------
# Get events
# ---------------------------------------------------------------------------

async def get_events(days=1):
    try:
        service = await asyncio.to_thread(_get_service)
        now = datetime.utcnow().isoformat() + "Z"

        today = date.today()
        if days == 7:
            days_until_sunday = (6 - today.weekday()) % 7
            if days_until_sunday == 0:
                days_until_sunday = 7
            end_date = today + timedelta(days=days_until_sunday)
        else:
            end_date = today
        kl_eod = TIMEZONE.localize(datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59))
        end_dt = kl_eod.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
            return f"Nothing on {label} 👍"

        all_events.sort(key=lambda x: x[1] or "")
        label = "Today" if days == 1 else "This Week"
        response = f"📅 *{label}:*\n\n"
        for summary, start_raw, cal_name in all_events:
            try:
                if "T" in start_raw:
                    dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    dt = dt.astimezone(TIMEZONE)
                    time_str = dt.strftime("%d %b, %-I:%M%p").lstrip("0").lower()
                else:
                    # All-day event — format date only
                    d = datetime.strptime(start_raw, "%Y-%m-%d")
                    time_str = d.strftime("%a %d %b") + " (all day)"
            except Exception:
                time_str = start_raw or "?"
            response += f"• *{summary}* — {time_str} ({cal_name})\n"
        return response
    except Exception as e:
        return f"⚠️ Calendar error: {str(e)}"


async def get_events_for_date(date_str):
    """Fetch events for a specific date. date_str format: 'DD MMM YYYY'"""
    try:
        target = datetime.strptime(date_str, "%d %b %Y").date()
    except Exception:
        return f"⚠️ Couldn't parse date: {date_str}"
    try:
        service = await asyncio.to_thread(_get_service)
        kl_midnight = TIMEZONE.localize(datetime(target.year, target.month, target.day, 0, 0, 0))
        kl_eod = TIMEZONE.localize(datetime(target.year, target.month, target.day, 23, 59, 59))
        time_min = kl_midnight.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = kl_eod.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        all_events = []
        for cal_name, cal_id in CALENDAR_IDS.items():
            try:
                result = await asyncio.to_thread(
                    lambda cid=cal_id: service.events().list(
                        calendarId=cid,
                        timeMin=time_min,
                        timeMax=time_max,
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
            return f"Nothing on {target.strftime('%a %d %b')} 👍"
        all_events.sort(key=lambda x: x[1] or "")
        response = f"📅 *{target.strftime('%a %d %b %Y')}:*\n\n"
        for summary, start_raw, cal_name in all_events:
            try:
                if start_raw and "T" in start_raw:
                    dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    dt = dt.astimezone(TIMEZONE)
                    time_str = dt.strftime("%-I:%M%p").lstrip("0").lower()
                else:
                    time_str = "all day"
            except Exception:
                time_str = "?"
            response += f"• *{summary}* — {time_str} ({cal_name})\n"
        return response
    except Exception as e:
        return f"⚠️ Calendar error: {str(e)}"


async def get_events_for_date_range(start_date, end_date):
    """Fetch events between two dates (date objects). Used for next week view."""
    try:
        service = await asyncio.to_thread(_get_service)
        kl_start = TIMEZONE.localize(datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0))
        kl_end = TIMEZONE.localize(datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59))
        time_min = kl_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = kl_end.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        all_events = []
        for cal_name, cal_id in CALENDAR_IDS.items():
            try:
                result = await asyncio.to_thread(
                    lambda cid=cal_id: service.events().list(
                        calendarId=cid,
                        timeMin=time_min,
                        timeMax=time_max,
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
            return f"Nothing on next week 👍"
        all_events.sort(key=lambda x: x[1] or "")
        label = f"Next Week ({start_date.strftime('%d %b')} – {end_date.strftime('%d %b')})"
        response = f"📅 *{label}:*\n\n"
        for summary, start_raw, cal_name in all_events:
            try:
                if start_raw and "T" in start_raw:
                    dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    dt = dt.astimezone(TIMEZONE)
                    time_str = dt.strftime("%a %d %b, %-I:%M%p").lstrip("0").lower()
                else:
                    d = datetime.strptime(start_raw, "%Y-%m-%d")
                    time_str = d.strftime("%a %d %b") + " (all day)"
            except Exception:
                time_str = start_raw or "?"
            response += f"• *{summary}* — {time_str} ({cal_name})\n"
        return response
    except Exception as e:
        return f"⚠️ Calendar error: {str(e)}"


# ---------------------------------------------------------------------------
# Find upcoming events (for delete flow)
# ---------------------------------------------------------------------------

async def find_upcoming_events(title_query, cal_filter=None, days=90, max_results=4):
    """
    Returns (matches, err, capped) where each match is:
      ({"event_id": ..., "cal_id": ..., "cal_name": ...}, summary, dtstart, dtend)
    cal_filter: optional calendar name string to restrict search
    capped: True if results were truncated to max_results
    """
    try:
        service = await asyncio.to_thread(_get_service)
        now = datetime.utcnow().isoformat() + "Z"
        end_dt = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"

        matches = []
        for cal_name, cal_id in CALENDAR_IDS.items():
            if cal_filter and cal_name.lower() != cal_filter.lower():
                continue
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
                        end_raw = e.get("end", {}).get("dateTime") or e.get("end", {}).get("date")
                        matches.append((
                            {"event_id": e["id"], "cal_id": cal_id, "cal_name": cal_name},
                            summary, start_raw, end_raw
                        ))
            except Exception:
                continue

        matches.sort(key=lambda x: x[2] or "")
        capped = len(matches) > max_results
        return matches[:max_results], None, capped
    except Exception as e:
        return None, f"⚠️ Calendar error: {str(e)}", False


# ---------------------------------------------------------------------------
# Delete calendar event
# ---------------------------------------------------------------------------

async def delete_calendar_event(summary_or_meta):
    try:
        service = await asyncio.to_thread(_get_service)
        if isinstance(summary_or_meta, dict):
            cal_name = summary_or_meta.get("cal_name", "")
            dtstart = summary_or_meta.get("dtstart", "")
            summary = summary_or_meta.get("summary", "Event")
            await asyncio.to_thread(
                lambda: service.events().delete(
                    calendarId=summary_or_meta["cal_id"],
                    eventId=summary_or_meta["event_id"]
                ).execute()
            )
            return _fmt_delete_success(summary, cal_name, dtstart)

        # Fallback: search by title
        matches, err, _ = await find_upcoming_events(summary_or_meta)
        if err:
            return err
        if not matches:
            return f"❌ No upcoming event found matching '{summary_or_meta}'"
        meta, summary, dtstart, _ = matches[0]
        await asyncio.to_thread(
            lambda: service.events().delete(
                calendarId=meta["cal_id"],
                eventId=meta["event_id"]
            ).execute()
        )
        return _fmt_delete_success(summary, meta.get("cal_name", ""), dtstart)
    except Exception as e:
        return f"❌ Error deleting event: {str(e)}"


# ---------------------------------------------------------------------------
# Edit existing calendar event
# ---------------------------------------------------------------------------

async def edit_calendar_event(text, user_id, expand_search=False, matched_meta=None, matched_summary=None, matched_dtstart=None, matched_dtend=None):
    """Parse 'edit cal [event] [field] [value]' and apply the change."""

    # C1: strip "delete" to prevent confusion with delete flow
    clean_text = re.sub(r'\bdelete\b', '', text, flags=re.IGNORECASE).strip()

    # Fix A: expand TEXT_SHORTCUTS before Haiku parse to avoid event_query corruption
    for shortcut, expansion in TEXT_SHORTCUTS.items():
        clean_text = re.sub(r'\b' + re.escape(shortcut) + r'\b', expansion, clean_text, flags=re.IGNORECASE)

    date_block = _build_date_anchor_block()
    known_cals = ", ".join(KNOWN_CALENDARS)

    parse_prompt = f"""{date_block}

Available calendars: {known_cals}

Parse this calendar edit request:
"{clean_text}"

Respond ONLY with a JSON object — no other text:
{{
  "event_query": "the event name to search for",
  "calendar_filter": "calendar name if explicitly mentioned, or empty string",
  "field": "title|calendar|start|end_only|location, or empty string if only date/time_range",
  "value": "new value for that field, or empty string",
  "date": "DD MMM YYYY if a new date is specified, or empty string",
  "time_range": "HH:MM-HH:MM if both start and end time are given, or empty string"
}}

Rules:
- event_query: event name only — never include calendar names or date/time words in it
- calendar_filter: only populate if a calendar name from the available list is explicitly mentioned. Do NOT put it in event_query
- date: populate if the user specifies a new date (e.g. "23 may", "next friday"). Use resolved dates above. Leave empty if only time is changing
- time_range: populate if both start AND end time are given (e.g. "6.30pm to 9.30pm" → "18:30-21:30"). When set, field/value may be empty
- field/value: use for single-field changes:
  - "start": single new start time only (end unchanged). value = HH:MM (24h)
  - "end_only": only end time changing. value = HH:MM (24h)
  - "title": new event title
  - "calendar": new calendar name
  - "location": new location
- If both date and time_range are given, populate both
- All time values in 24h HH:MM format"""

    try:
        response = await asyncio.to_thread(
            lambda: client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": parse_prompt}]
            )
        )
        raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
    except Exception:
        return "⚠️ Couldn't parse that — try: edit cal Branson time 11am"

    event_query = parsed.get("event_query", "").strip()
    field = parsed.get("field", "").strip()
    value = parsed.get("value", "").strip()
    date_val = parsed.get("date", "").strip()
    time_range_val = parsed.get("time_range", "").strip()
    cal_filter_raw = parsed.get("calendar_filter", "").strip()
    cal_filter = _match_calendar_name(cal_filter_raw) if cal_filter_raw else None

    if not event_query:
        return "⚠️ Couldn't parse that — try: edit cal Branson time 11am"

    # Fix 6b: regex override for calendar field — deterministic, bypasses Haiku ambiguity
    # Matches "calendar <cal_name>" anywhere in clean_text after stripping event_query
    cal_pattern = re.compile(r'\bcalendar\s+(.+)$', re.IGNORECASE)
    cal_match = cal_pattern.search(clean_text)
    if cal_match:
        candidate = cal_match.group(1).strip()
        matched_cal = _match_calendar_name(candidate)
        # Only override if candidate loosely matches a known calendar
        if any(candidate.lower() in c.lower() or c.lower() in candidate.lower() for c in KNOWN_CALENDARS):
            field = "calendar"
            value = matched_cal

    has_change = (field and value) or date_val or time_range_val

    # Fix 6: if matched_meta provided but has_change=False, re-prompt instead of re-searching
    if matched_meta and not has_change:
        return f"⚠️ Couldn't parse that — what would you like to change for *{matched_summary}*?\n(time / date / calendar / location / title)"

    # If called from awaiting_edit_field with a stored event, skip re-search
    if matched_meta and has_change:
        return await _apply_event_edit(matched_meta, matched_summary, field, value, matched_dtstart, matched_dtend, date_val=date_val, time_range_val=time_range_val)

    # Search first — need to know match count before deciding flow
    days = 365 if expand_search else 90
    matches, err, capped = await find_upcoming_events(event_query, cal_filter=cal_filter, days=days, max_results=4)
    if err:
        return err

    # If calendar filter returned nothing, retry without filter
    if cal_filter and not matches:
        matches, err, capped = await find_upcoming_events(event_query, days=days, max_results=4)
        if err:
            return err
        if matches:
            cal_filter = None
            cal_filter_raw = None

    if not matches:
        return f"No upcoming event found matching '{event_query}'"

    if len(matches) > 1:
        lines = ["Found multiple matching events — which one?\n"]
        for i, (meta, summary, dtstart, dtend) in enumerate(matches, 1):
            lines.append(f"{i}. {_fmt_event_row(summary, meta.get('cal_name', ''), dtstart, dtend)}")
        if capped:
            lines.append("\nShowing next 4 — reply 'more' to see further ahead.")
        state.calendar_confirm_sessions[user_id] = {
            "step": "pick_edit",
            "edit_matches": matches,
            "field": field,
            "value": value,
            "date_val": date_val,
            "time_range_val": time_range_val,
            "event_query": event_query,
            "capped": capped,
            "awaiting_field": not has_change,
        }
        state.session_timestamps[user_id] = datetime.now(TIMEZONE)
        return "\n".join(lines)

    # Single match — if no change specified, prompt for field
    meta_single, summary_single, dtstart_single, dtend_single = matches[0]
    if not has_change:
        state.calendar_confirm_sessions[user_id] = {
            "step": "awaiting_edit_field",
            "event_query": event_query,
            "cal_filter": cal_filter_raw,
            "matched_meta": meta_single,
            "matched_summary": summary_single,
            "matched_dtstart": dtstart_single,
            "matched_dtend": dtend_single,
        }
        state.session_timestamps[user_id] = datetime.now(TIMEZONE)
        return f"What would you like to edit for *{summary_single}*?\n(time / date / calendar / location / title)"

    meta, summary, dtstart, dtend = matches[0]
    return await _apply_event_edit(meta, summary, field, value, dtstart, dtend, date_val=date_val, time_range_val=time_range_val)

async def _apply_event_edit(meta, summary, field, value, dtstart=None, dtend=None, date_val="", time_range_val=""):
    """Apply a single field edit to an existing Google Calendar event."""
    if not field and not date_val and not time_range_val:
        return "⚠️ No changes specified — what would you like to edit?\n(time / date / calendar / location / title)"
    try:
        service = await asyncio.to_thread(_get_service)
        event = await asyncio.to_thread(
            lambda: service.events().get(
                calendarId=meta["cal_id"],
                eventId=meta["event_id"]
            ).execute()
        )

        cal_name = meta.get("cal_name", "")

        # C4/C5: apply date change first (preserving existing times)
        if date_val:
            try:
                new_date = datetime.strptime(date_val, "%d %b %Y")
                new_date_str = new_date.strftime("%Y-%m-%d")
            except Exception:
                return f"⚠️ Couldn't parse date '{date_val}' — try: 23 May 2026"
            existing_start = event.get("start", {}).get("dateTime", "")
            existing_end = event.get("end", {}).get("dateTime", "")
            if existing_start and "T" in existing_start:
                try:
                    s_dt = datetime.fromisoformat(existing_start.replace("Z", "+00:00"))
                    e_dt = datetime.fromisoformat(existing_end.replace("Z", "+00:00")) if existing_end and "T" in existing_end else None
                    event["start"] = {"dateTime": f"{new_date_str}T{s_dt.strftime('%H:%M')}:00", "timeZone": str(TIMEZONE)}
                    if e_dt:
                        event["end"] = {"dateTime": f"{new_date_str}T{e_dt.strftime('%H:%M')}:00", "timeZone": str(TIMEZONE)}
                except Exception:
                    return "⚠️ Couldn't update event date"
            else:
                event["start"] = {"date": new_date_str}
                event["end"] = {"date": new_date_str}

        # C5: apply time_range (uses updated date if date_val also set)
        if time_range_val:
            existing_start_raw = event.get("start", {}).get("dateTime", "")
            if existing_start_raw and "T" in existing_start_raw:
                use_date = existing_start_raw.split("T")[0]
            else:
                return "⚠️ Couldn't read existing event date"
            try:
                parts = time_range_val.split("-")
                if len(parts) != 2:
                    raise ValueError
                tr_start = datetime.strptime(parts[0].strip(), "%H:%M")
                tr_end = datetime.strptime(parts[1].strip(), "%H:%M")
            except Exception:
                return "⚠️ Couldn't parse time range — try: 18:30-21:30"
            if tr_end <= tr_start:
                return "⚠️ End time can't be before or equal to start time — try again"
            event["start"] = {"dateTime": f"{use_date}T{tr_start.strftime('%H:%M')}:00", "timeZone": str(TIMEZONE)}
            event["end"] = {"dateTime": f"{use_date}T{tr_end.strftime('%H:%M')}:00", "timeZone": str(TIMEZONE)}

        # Single-field edits — only when field is set
        only_date_time = (date_val or time_range_val) and not field
        if not only_date_time and field:

            if field == "title":
                event["summary"] = value

            elif field == "calendar":
                new_cal_name = _match_calendar_name(value)
                new_cal_id = _get_calendar_id(new_cal_name)
                event.pop("id", None)
                event.pop("etag", None)
                event.pop("iCalUID", None)
                await asyncio.to_thread(
                    lambda: service.events().insert(calendarId=new_cal_id, body=event).execute()
                )
                await asyncio.to_thread(
                    lambda: service.events().delete(
                        calendarId=meta["cal_id"],
                        eventId=meta["event_id"]
                    ).execute()
                )
                return f"✅ *{summary}* moved to {new_cal_name}"

            elif field == "start":
                existing_start = event.get("start", {}).get("dateTime", "")
                try:
                    existing_dt = datetime.fromisoformat(existing_start.replace("Z", "+00:00"))
                    existing_date = existing_dt.strftime("%Y-%m-%d")
                except Exception:
                    return "⚠️ Couldn't read existing event start time"
                try:
                    new_time = datetime.strptime(value, "%H:%M")
                except Exception:
                    return "⚠️ Couldn't parse that time — use HH:MM format e.g. 18:30"
                new_start_iso = f"{existing_date}T{new_time.strftime('%H:%M')}:00"
                event["start"] = {"dateTime": new_start_iso, "timeZone": str(TIMEZONE)}
                # C9: if new start >= existing end, preserve original duration and shift end
                existing_end = event.get("end", {}).get("dateTime", "")
                if existing_end:
                    try:
                        end_dt = datetime.fromisoformat(existing_end.replace("Z", "+00:00"))
                        new_start_dt = datetime.fromisoformat(new_start_iso)
                        if new_start_dt >= end_dt.replace(tzinfo=None):
                            duration = end_dt.replace(tzinfo=None) - existing_dt.replace(tzinfo=None)
                            new_end_dt = new_start_dt + duration
                            event["end"] = {"dateTime": new_end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": str(TIMEZONE)}
                    except Exception:
                        pass

            elif field == "end_only":
                existing_start = event.get("start", {}).get("dateTime", "")
                try:
                    existing_dt = datetime.fromisoformat(existing_start.replace("Z", "+00:00"))
                    existing_date = existing_dt.strftime("%Y-%m-%d")
                    existing_start_time = existing_dt.replace(tzinfo=None)
                except Exception:
                    return "⚠️ Couldn't read existing event start time"
                try:
                    new_time = datetime.strptime(value, "%H:%M")
                except Exception:
                    return "⚠️ Couldn't parse that time — use HH:MM format e.g. 21:30"
                new_end_iso = f"{existing_date}T{new_time.strftime('%H:%M')}:00"
                new_end_dt = datetime.strptime(new_end_iso, "%Y-%m-%dT%H:%M:%S")
                if new_end_dt <= existing_start_time:
                    return "⚠️ End time can't be before or equal to start time — try again"
                event["end"] = {"dateTime": new_end_iso, "timeZone": str(TIMEZONE)}


            elif field == "location":
                event["location"] = value

        await asyncio.to_thread(
            lambda: service.events().update(
                calendarId=meta["cal_id"],
                eventId=meta["event_id"],
                body=event
            ).execute()
        )

        updated_start = event.get("start", {}).get("dateTime", "")
        updated_end = event.get("end", {}).get("dateTime", "")
        try:
            s_dt = datetime.fromisoformat(updated_start.replace("Z", "+00:00"))
            e_dt = datetime.fromisoformat(updated_end.replace("Z", "+00:00"))
            date_label = s_dt.strftime("%a %d %b %Y")
            time_label = f"{s_dt.strftime('%I:%M%p').lstrip('0').lower()} – {e_dt.strftime('%I:%M%p').lstrip('0').lower()}"
            return f"✅ *{summary}* updated — {cal_name} | {date_label}, {time_label}"
        except Exception:
            return f"✅ *{summary}* updated"

    except Exception as e:
        return f"❌ Couldn't update event: {str(e)}"


# ---------------------------------------------------------------------------
# is_calendar_request — unchanged
# ---------------------------------------------------------------------------

async def is_calendar_request(text):
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
            response = await asyncio.to_thread(
                lambda: client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    messages=[{"role": "user", "content":
                        f'Is this asking to add a calendar event? Reply YES or NO only.\n\n"{text}"'}]
                )
            )
            return response.content[0].text.strip().upper() == "YES"
        except Exception as e:
            print(f"is_calendar_request API fallback error: {e}")
            return False
    return False
