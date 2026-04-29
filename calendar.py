import re
from datetime import datetime, timedelta

import caldav

import config
from config import ICLOUD_USERNAME, ICLOUD_PASSWORD, TIMEZONE, YOUR_CHAT_ID
from clients import client


# --- iCloud Health ---

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
        config._icloud_down = False
        return result
    except Exception:
        pass

    try:
        result = _attempt()
        config._icloud_down = False
        return result
    except Exception as e:
        config._icloud_down = True
        print(f"iCloud Calendar unavailable after retry: {e}")
        return None


async def check_icloud_daily(app):
    from datetime import date
    today = date.today()
    if config._icloud_down:
        if config._icloud_last_notified != today:
            config._icloud_last_notified = today
            await app.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text="⚠️ iCloud Calendar still unavailable — calendar features won't work.\nCheck your credentials in Railway."
            )
    else:
        try:
            cal = get_calendar()
            if cal is None and config._icloud_last_notified != today:
                config._icloud_last_notified = today
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text="⚠️ iCloud Calendar unavailable — calendar features won't work.\nCheck your credentials in Railway."
                )
        except Exception as e:
            print(f"check_icloud_daily error: {e}")


# --- Calendar Functions ---

def get_events(days=1):
    try:
        calendar = get_calendar("Personal")
        if not calendar:
            return "⚠️ Couldn't connect to iCloud Calendar — check that ICLOUD_USERNAME and ICLOUD_PASSWORD are set correctly in Railway."
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
        return f"⚠️ Calendar error: {str(e)} — if this keeps happening, check your iCloud credentials in Railway."


def delete_calendar_event(title):
    try:
        calendar = get_calendar()
        if not calendar:
            return "❌ Could not connect to iCloud Calendar"
        start = datetime.now()
        end = start + timedelta(days=30)
        events = calendar.date_search(start=start, end=end)
        for event in events:
            e = event.vobject_instance.vevent
            if title.lower() in str(e.summary.value).lower():
                event.delete()
                return f"✅ Event *{str(e.summary.value)}* deleted"
        return f"❌ No event found matching '{title}' in the next 30 days"
    except Exception as e:
        return f"❌ Error deleting event: {str(e)}"


def is_calendar_request(text):
    lower = text.lower().strip()

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
                model="claude-haiku-4-5-20251001", max_tokens=10,
                messages=[{"role": "user", "content":
                    f'Is this asking to add a calendar event? Reply YES or NO only.\n\n"{text}"'}]
            )
            return response.content[0].text.strip().upper() == "YES"
        except Exception as e:
            print(f"is_calendar_request API fallback error: {e}")
            return False

    return False


def smart_add_event(text, user_id):
    """Parse natural language event and add to iCloud Calendar."""
    try:
        calendar = get_calendar("Personal")
        if not calendar:
            return "⚠️ Couldn't connect to iCloud Calendar — check your credentials in Railway."

        now = datetime.now(TIMEZONE)
        prompt = (
            f"Today is {now.strftime('%A, %d %b %Y')} and the time is {now.strftime('%H:%M')} (Asia/Kuala_Lumpur).\n\n"
            f"Parse this event request and return ONLY a JSON object with:\n"
            f"- title: string (event name)\n"
            f"- start: string (ISO datetime, e.g. 2026-04-30T19:00:00)\n"
            f"- end: string (ISO datetime — if duration not specified, assume 1 hour)\n"
            f"- all_day: boolean\n\n"
            f"Request: {text}\n\nReturn ONLY the JSON."
        )
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        import json
        parsed = json.loads(raw)

        title = parsed.get("title", "New Event")
        start_str = parsed.get("start", "")
        end_str = parsed.get("end", "")

        if not start_str:
            return "⚠️ Couldn't parse that — what are the event details?\n(e.g. Team Offsite, 30 Apr 2026, 2pm)"

        start_dt = datetime.fromisoformat(start_str)
        end_dt = datetime.fromisoformat(end_str) if end_str else start_dt + timedelta(hours=1)

        from icalendar import Calendar as iCal, Event
        cal = iCal()
        cal.add('prodid', '-//Em Bot//caldav//EN')
        cal.add('version', '2.0')
        event = Event()
        event.add('summary', title)
        event.add('dtstart', start_dt)
        event.add('dtend', end_dt)
        import uuid
        event.add('uid', str(uuid.uuid4()))
        cal.add_component(event)

        calendar.save_event(cal.to_ical())

        start_fmt = start_dt.strftime("%d %b %Y at %I:%M %p")
        return f"✅ Added to your calendar: *{title}* on {start_fmt}"

    except Exception as e:
        return f"⚠️ Couldn't parse that — what are the event details?\n(e.g. Team Offsite, 30 Apr 2026, 2pm)"


def search_meeting_notes(query):
    try:
        sheet = spreadsheet_ref().worksheet("Meeting Notes")
        records = sheet.get_all_records()
        results = [r for r in records if query.lower() in str(r).lower()]
        if not results:
            return f"No meeting notes found matching '{query}'."
        lines = [f"*{len(results)} meeting(s) matching '{query}':*\n"]
        for r in results:
            lines.append(f"📝 *{r.get('Event Name', 'Untitled')}* — {r.get('Date', '')}")
            if r.get('Topic'):
                lines.append(f"  Topic: {r.get('Topic')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching meeting notes: {str(e)}"

def spreadsheet_ref():
    from clients import spreadsheet
    return spreadsheet
