import re
import json
from datetime import date, datetime, timedelta
import state
from config import ICLOUD_USERNAME, ICLOUD_PASSWORD, TIMEZONE, YOUR_CHAT_ID
from clients import client
import caldav

def get_calendar(name=None):
    global state
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

def smart_add_event(text, user_id):
    try:
        today = date.today()
        tomorrow = today + timedelta(days=1)
        calendar_names = [
            "Estate Planning", "Work Meeting", "Personal",
            "Closing", "Urgent", "MinimaList", "Appointment"
        ]
        parse_prompt = f"""Today is {today.strftime('%d %b %Y')} ({today.strftime('%A')}).
Tomorrow is {tomorrow.strftime('%d %b %Y')}.

Available calendars: {', '.join(calendar_names)}

The user wants to add a calendar event. Extract the details from this message:
"{text}"

Respond ONLY with a JSON object in this exact format, nothing else:
{{
  "title": "event title",
  "start": "DD/MM/YYYY HH:MM",
  "end": "DD/MM/YYYY HH:MM",
  "notes": "any notes or empty string",
  "calendar": "exact calendar name from the list above or Personal if unclear"
}}

Rules:
- If no end time given, assume 1 hour after start
- If AM/PM not specified, use context to determine (6pm not 6am for dinner etc)
- Match calendar name exactly from the available list
- If no calendar specified, use Personal"""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": parse_prompt}]
        )
        raw = response.content[0].text.strip()
        clean = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        title = parsed.get("title", "")
        start_str = parsed.get("start", "")
        end_str = parsed.get("end", "")
        notes = parsed.get("notes", "")
        cal_name = parsed.get("calendar", "Personal")
        if not title or not start_str or not end_str:
            return "❌ Couldn't parse the event details. Try something like: schedule dinner with James tomorrow 7pm"
        start = datetime.strptime(start_str, "%d/%m/%Y %H:%M")
        end = datetime.strptime(end_str, "%d/%m/%Y %H:%M")
        calendar = get_calendar(cal_name)
        if not calendar:
            calendar = get_calendar("Personal")
        ics = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
            f"SUMMARY:{title}\n"
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}\n"
            f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}\n"
            f"DESCRIPTION:{notes}\n"
            "END:VEVENT\nEND:VCALENDAR"
        )
        calendar.add_event(ics)
        return (
            f"✅ Done!\n\n"
            f"📅 *{title}*\n"
            f"🕐 {start.strftime('%d %b %Y, %H:%M')} → {end.strftime('%H:%M')}\n"
            f"📁 {calendar.name}\n"
            + (f"📝 {notes}" if notes else "")
        )
    except json.JSONDecodeError:
        return "⚠️ Couldn't parse that — what are the event details?\n(e.g. Team Offsite, 30 Apr 2026, 2pm)"
    except Exception as e:
        if "iCloud" in str(e) or "caldav" in str(e).lower() or "calendar" in str(e).lower():
            return "⚠️ Couldn't connect to iCloud Calendar — check credentials in Railway."
        return "⚠️ Couldn't parse that — what are the event details?\n(e.g. Team Offsite, 30 Apr 2026, 2pm)"
