import re
import json
import httpx
from datetime import date, datetime, timedelta
import state
from config import (
    LOCATION_CONTEXT_WORDS, TIMEZONE, AVIATIONSTACK_API_KEY, YOUR_CHAT_ID
)
from clients import client
from sheets import trips_sheet, get_sheet
from helpers import generate_trip_id

def save_trip(destination, currency, check_in="", check_out="",
              hotel_name="", hotel_local_name="", hotel_address="", notes=""):
    try:
        ws = trips_sheet()
        trip_id = generate_trip_id()
        ws.append_row([
            trip_id, destination, currency,
            check_in, check_out,
            hotel_name, hotel_local_name, hotel_address,
            notes, "active"
        ])
        return trip_id
    except Exception as e:
        print(f"save_trip error: {e}")
        return None

def close_trip(trip_id=None):
    try:
        ws = trips_sheet()
        records = ws.get_all_records()
        for i, row in enumerate(records, start=2):
            if row.get("Status") == "active" and (trip_id is None or row.get("Trip ID") == trip_id):
                ws.update_cell(i, 10, "closed")
                return True
    except Exception as e:
        print(f"close_trip error: {e}")
    return False

def get_active_trip():
    try:
        ws = trips_sheet()
        records = ws.get_all_records()
        for row in reversed(records):
            if row.get("Status") == "active":
                return row
    except Exception as e:
        print(f"get_active_trip error: {e}")
    return None

def restore_overseas_from_trips():
    try:
        trip = get_active_trip()
        if not trip:
            return False
        try:
            dest = trip.get("Destination", "") or ""
        except Exception:
            dest = ""
        try:
            curr = trip.get("Currency", "SGD") or "SGD"
        except Exception:
            curr = "SGD"
        if not dest or not curr or curr == "SGD":
            return False
        state.overseas_state["active"] = True
        state.overseas_state["destination"] = dest
        state.overseas_state["currency"] = curr
        state.overseas_state["currencies"] = [curr]
        state.overseas_state["trip_destinations"] = [dest]
        state.overseas_state["trip_start"] = trip.get("Check In", "")
        print(f"✅ Restored overseas mode: {dest} ({curr})")
        return True
    except Exception as e:
        print(f"restore_overseas_from_trips error: {e}")
    return False

def get_trip_history(n=5):
    try:
        ws = trips_sheet()
        records = ws.get_all_records()
        return list(reversed(records[-n:])) if records else []
    except Exception as e:
        print(f"get_trip_history error: {e}")
        return []

def format_trip_history():
    trips = get_trip_history(10)
    if not trips:
        return "No trips logged yet."
    lines = ["*Trip History*\n"]
    for t in trips:
        status = "✈️ Active" if t.get("Status") == "active" else "✅ Done"
        dest = t.get("Destination", "—")
        curr = t.get("Currency", "")
        check_in = t.get("Check In", "")
        check_out = t.get("Check Out", "")
        hotel = t.get("Hotel Name", "")
        line = f"{status} {dest} ({curr})"
        if check_in:
            line += f"\n{check_in}"
            if check_out:
                line += f" → {check_out}"
        if hotel:
            line += f" | {hotel}"
        lines.append(line)
    return "\n\n".join(lines)

def persist_trip_setup():
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        ts = state.overseas_state.get("_trip_setup")
        data = json.dumps(ts) if ts else ""
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("Key") == "trip_setup_state":
                sheet.update_cell(i + 2, 2, data)
                return
        sheet.append_row(["trip_setup_state", data])
    except Exception as e:
        print(f"persist_trip_setup error: {e}")

def load_trip_setup_from_sheet():
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        records = sheet.get_all_records()
        for r in records:
            if r.get("Key") == "trip_setup_state":
                raw = r.get("Value", "")
                if raw:
                    ts = json.loads(raw)
                    state.overseas_state["_trip_setup"] = ts
                    print(f"Restored _trip_setup from sheet: step={ts.get('step')}")
                return
    except Exception as e:
        print(f"load_trip_setup_from_sheet error: {e}")

def extract_flight_number(text):
    matches = re.findall(r'\b([A-Z]{1,3}\d{2,4}[A-Z]?)\b', text.upper())
    return matches[0] if matches else None

def _parse_trip_dates(text):
    today = date.today()
    found = []
    seen = set()
    month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                 "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    yr = today.year

    def _add(d):
        if d not in seen:
            seen.add(d)
            found.append(d)

    for word, delta in [("today", 0), ("tomorrow", 1), ("tmr", 1), ("tmrw", 1)]:
        if word in text.lower():
            _add(today + timedelta(days=delta))
    for m in re.finditer(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:\s+(\d{4}))?\b', text, re.IGNORECASE):
        try:
            _add(date(int(m.group(3)) if m.group(3) else yr, month_map[m.group(2).lower()], int(m.group(1))))
        except ValueError:
            pass
    for m in re.finditer(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?\b', text, re.IGNORECASE):
        try:
            _add(date(int(m.group(3)) if m.group(3) else yr, month_map[m.group(1).lower()], int(m.group(2))))
        except ValueError:
            pass
    for m in re.finditer(r'\b(\d{4}-\d{2}-\d{2})\b', text):
        try:
            _add(datetime.strptime(m.group(1), "%Y-%m-%d").date())
        except ValueError:
            pass
    for m in re.finditer(r'\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b', text):
        try:
            y = int(m.group(3)) if m.group(3) else yr
            if y < 100:
                y += 2000
            _add(date(y, int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass
    return found

def _get_currency_for_dest(destination):
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content":
                f"What is the primary currency ISO code for {destination}? Reply with ONLY the 3-letter code."}]
        )
        code = resp.content[0].text.strip().upper()
        if re.match(r'^[A-Z]{3}$', code):
            return code
    except Exception as e:
        print(f"_get_currency_for_dest error: {e}")
    return "SGD"

def is_overseas_mode_request(text):
    lower = text.lower()
    flights = extract_flight_number(text)
    has_flight = bool(flights)
    has_multiple_flights = len(re.findall(r'\b[A-Z]{1,3}\d{2,4}[A-Z]?\b', text.upper())) >= 2
    if has_multiple_flights:
        return True
    travel_words = [
        "flying", "flight", "boarding", "departure", "departing", "departs",
        "returning", "flying back", "headed to", "heading to",
        "on tr", "on sq", "on ak", "on mh", "on od", "on ek", "on cx",
    ]
    if has_flight and any(w in lower for w in travel_words):
        return True
    if any(phrase in lower for phrase in [
        "overseas", "travelling", "traveling", "flying to", "arrived in",
        "back home", "i'm back", "landed in", "just landed", "just arrived",
        "returned home"
    ]):
        return True
    if "i'm in" in lower or "im in" in lower:
        return any(loc in lower for loc in LOCATION_CONTEXT_WORDS)
    return False

def deactivate_overseas_mode():
    try:
        close_trip()
    except Exception:
        pass
    state.overseas_state["active"] = False
    state.overseas_state["destination"] = ""
    state.overseas_state["currency"] = "SGD"
    state.overseas_state["currencies"] = []
    state.overseas_state["return_date"] = ""
    state.overseas_state["trip_start"] = None
    state.overseas_state["trip_destinations"] = []
    for job_key in ["dep_job_id", "return_job_id"]:
        job_id = state.overseas_state.get(job_key)
        if job_id and state._scheduler:
            try:
                state._scheduler.remove_job(job_id)
            except Exception:
                pass
        state.overseas_state[job_key] = None

async def activate_overseas_mode_scheduled(dest, curr, check_in, check_out):
    for d in [state.expense_sessions, state.receipt_confirm_sessions]:
        d.pop(YOUR_CHAT_ID, None)
    state.session_timestamps.pop(YOUR_CHAT_ID, None)
    state.overseas_state["active"] = True
    state.overseas_state["destination"] = dest
    state.overseas_state["currency"] = curr
    state.overseas_state["currencies"] = [curr] if curr != "SGD" else []
    state.overseas_state["trip_start"] = date.today().strftime("%d/%m/%Y")
    state.overseas_state["trip_destinations"] = [dest]
    save_trip(dest, curr, check_in=check_in, check_out=check_out)
    msg = f"Overseas mode on ✈️\nDestination: {dest}\nCurrency: {curr}\nI'll log expenses in {curr} with SGD equivalent."
    if state._app_ref:
        try:
            await state._app_ref.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
        except Exception as e:
            print(f"Failed to send overseas mode activation message: {e}")

async def deactivate_and_notify(app):
    import random
    dest = state.overseas_state.get("destination", "")
    deactivate_overseas_mode()
    greeting = random.choice(["Welcome back!", "Good to have you back!", "Hope the trip was great!"])
    msg = f"{greeting} Back in SG — switching to SGD. 🏠"
    try:
        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
    except Exception as e:
        print(f"Failed to send return notification: {e}")

def handle_overseas_request(text):
    lower = text.lower()
    if any(p in lower for p in ["back home", "returned", "i'm back", "landed back", "home now"]):
        import random
        greeting = random.choice(["Welcome back!", "Good to have you back!", "Hope the trip was great!"])
        deactivate_overseas_mode()
        return f"{greeting} Switching back to SGD. 🏠"
    flight_num = extract_flight_number(text)
    dest_match = re.search(r'(?:to|in|flying to|going to|headed to|heading to|trip to)\s+([A-Za-z][A-Za-z\s]{2,25}?)(?:\s+on|\s+\d|$|[,.])', text, re.IGNORECASE)
    dest_hint = dest_match.group(1).strip().title() if dest_match else None
    state.overseas_state["_trip_setup"] = {
        "step": "destination",
        "flight_number": flight_num or "",
        "destination": dest_hint or "",
        "check_in": "",
        "check_out": "",
        "currency": "",
        "hotel_name": "",
        "hotel_local_name": "",
        "hotel_address": "",
        "notes": "",
    }
    if dest_hint:
        state.overseas_state["_trip_setup"]["step"] = "check_in"
        persist_trip_setup()
        return f"Got it — {dest_hint} 🌏\nCheck-in date? (or 'skip')"
    persist_trip_setup()
    return "Where are you headed?"

async def _send_trip_confirm(update, ts):
    dest = ts.get("destination", "—")
    curr = ts.get("currency", "") or "auto-detect"
    check_in = ts.get("check_in", "") or "—"
    check_out = ts.get("check_out", "") or "—"
    flight = ts.get("flight_number", "")
    dep_display = ts.get("dep_time_display", "")
    hotel = ts.get("hotel_name", "") or "—"
    lines = [f"✈️ *{dest}* ({curr})"]
    lines.append(f"Check-in: {check_in} → Check-out: {check_out}")
    if flight:
        lines.append(f"Flight: {flight}" + (f" @ {dep_display}" if dep_display else ""))
    if hotel != "—":
        lines.append(f"Hotel: {hotel}")
    lines.append("\nConfirm? (Y / cancel)")
    ts["step"] = "confirm"
    state.overseas_state["_trip_setup"] = ts
    persist_trip_setup()
    try:
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception:
        await update.message.reply_text("\n".join(lines))
