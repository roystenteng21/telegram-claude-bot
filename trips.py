import json
import re
import requests
from datetime import date, datetime, timedelta

import config
from config import (
    AVIATIONSTACK_API_KEY, TIMEZONE, YOUR_CHAT_ID,
    overseas_state, expense_sessions, receipt_confirm_sessions, session_timestamps,
)
from clients import client
from sheets import trips_sheet


# --- Location constants ---

LOCATION_CONTEXT_WORDS = {
    "japan", "korea", "thailand", "malaysia", "indonesia", "vietnam", "philippines",
    "australia", "china", "hong kong", "taiwan", "india", "uk", "england", "france",
    "germany", "italy", "spain", "usa", "america", "canada", "dubai", "uae",
    "tokyo", "osaka", "seoul", "bangkok", "kuala lumpur", "kl", "jakarta", "bali",
    "sydney", "melbourne", "beijing", "shanghai", "guangzhou", "shenzhen",
    "taipei", "mumbai", "delhi", "london", "paris", "berlin", "rome", "barcelona",
    "new york", "los angeles", "chicago", "toronto", "vancouver",
    "hkg", "nrt", "icn", "bkk", "kul", "cgk", "sin", "syd", "pek", "pvg",
    "tpe", "bom", "del", "lhr", "cdg", "txl", "fco", "jfk", "lax", "yyz",
}

SG_CITY_KEYWORDS = {"singapore", "changi"}
SG_IATA_CODES = {"SIN", "SLM"}


# --- Trip Sheet CRUD ---

def generate_trip_id():
    return date.today().strftime("TRIP-%Y%m%d")

def save_trip(destination, currency, dep_flight="", dep_time="", return_flight="", return_time=""):
    try:
        ws = trips_sheet()
        trip_id = generate_trip_id()
        ws.append_row([
            trip_id, destination, currency,
            dep_flight, dep_time, return_flight, return_time,
            "active", datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M"), ""
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
            if row.get("Status") == "active":
                if trip_id is None or row.get("Trip ID") == trip_id:
                    ws.update_cell(i, 8, "closed")
                    ws.update_cell(i, 10, datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M"))
                    return True
        return False
    except Exception as e:
        print(f"close_trip error: {e}")
        return False

def get_active_trip():
    try:
        ws = trips_sheet()
        records = ws.get_all_records()
        for row in records:
            if row.get("Status") == "active":
                return row
        return None
    except Exception as e:
        print(f"get_active_trip error: {e}")
        return None

def restore_overseas_from_trips():
    """On startup, restore overseas_state from active Trips row if one exists."""
    try:
        trip = get_active_trip()
        if not trip:
            return
        overseas_state["active"] = True
        overseas_state["destination"] = trip.get("Destination", "")
        overseas_state["currency"] = trip.get("Currency", "SGD")
        overseas_state["currencies"] = [trip.get("Currency", "SGD")] if trip.get("Currency", "SGD") != "SGD" else []
        overseas_state["trip_start"] = trip.get("Started", "")
        overseas_state["trip_destinations"] = [trip.get("Destination", "")] if trip.get("Destination") else []
        print(f"Restored overseas mode from Trips sheet: {trip.get('Destination')}")
    except Exception as e:
        print(f"restore_overseas_from_trips error: {e}")

def get_trip_history():
    try:
        ws = trips_sheet()
        records = ws.get_all_records()
        return [r for r in records if r.get("Destination")]
    except Exception as e:
        print(f"get_trip_history error: {e}")
        return []

def format_trip_history():
    try:
        records = get_trip_history()
        if not records:
            return "No trips recorded yet."
        lines = ["*Trip History:*\n"]
        for r in records:
            dest = r.get("Destination", "")
            curr = r.get("Currency", "")
            started = r.get("Started", "")
            ended = r.get("Ended", "")
            status = r.get("Status", "")
            dep = r.get("Dep Flight", "")
            ret = r.get("Return Flight", "")
            line = f"✈️ {dest} ({curr})"
            if started:
                line += f" — {started}"
            if ended:
                line += f" to {ended}"
            if dep:
                line += f"\n  Out: {dep}"
            if ret:
                line += f" → Return: {ret}"
            if status == "active":
                line += " 🟢 active"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching trip history: {str(e)}"


# --- Flight helpers ---

def extract_flight_number(text):
    matches = re.findall(r'\b([A-Z]{1,3}\d{2,4}[A-Z]?)\b', text.upper())
    return matches[0] if matches else None

def extract_all_flight_numbers(text):
    return re.findall(r'\b([A-Z]{1,3}\d{2,4}[A-Z]?)\b', text.upper())

def extract_flight_dates(text):
    today = date.today()
    found = []
    seen_dates = set()
    for word, delta in [("today", 0), ("tomorrow", 1), ("tmr", 1), ("tmrw", 1)]:
        if word in text.lower():
            d = today + timedelta(days=delta)
            if d not in seen_dates:
                seen_dates.add(d)
                found.append(d)
    month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                 "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    current_year = today.year
    for m in re.finditer(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:\s+(\d{4}))?\b', text, re.IGNORECASE):
        day = int(m.group(1))
        mon = month_map[m.group(2).lower()]
        yr = int(m.group(3)) if m.group(3) else current_year
        try:
            d = date(yr, mon, day)
            if d not in seen_dates:
                seen_dates.add(d)
                found.append(d)
        except ValueError:
            pass
    for m in re.finditer(r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?\b', text, re.IGNORECASE):
        mon = month_map[m.group(1).lower()]
        day = int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else current_year
        try:
            d = date(yr, mon, day)
            if d not in seen_dates:
                seen_dates.add(d)
                found.append(d)
        except ValueError:
            pass
    for m in re.finditer(r'\b(\d{4}-\d{2}-\d{2})\b', text):
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            if d not in seen_dates:
                seen_dates.add(d)
                found.append(d)
        except ValueError:
            pass
    return found

def format_flight_time(iso_str):
    if not iso_str:
        return "time unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d %b, %H:%M")
    except Exception as e:
        print(f"format_flight_time: bad ISO string '{iso_str}': {e}")
        return iso_str[:16]

def lookup_flight(flight_number, flight_date=None):
    if not AVIATIONSTACK_API_KEY:
        print("Flight lookup: no AVIATIONSTACK_API_KEY set")
        return None
    try:
        url = "http://api.aviationstack.com/v1/flights"
        params = {"access_key": AVIATIONSTACK_API_KEY, "flight_iata": flight_number.upper()}
        if flight_date:
            if hasattr(flight_date, "strftime"):
                params["flight_date"] = flight_date.strftime("%Y-%m-%d")
            else:
                params["flight_date"] = str(flight_date)
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        print(f"AviationStack {flight_number}: {json.dumps(data)[:400]}")
        if "error" in data:
            print(f"AviationStack API error: {data['error']}")
            return None
        flights = data.get("data", [])
        if not flights:
            print(f"AviationStack: no data for {flight_number}")
            return None
        f = flights[0]
        dep = f.get("departure", {})
        arr = f.get("arrival", {})
        return {
            "flight": flight_number.upper(),
            "dep_airport": dep.get("airport", ""),
            "dep_iata": dep.get("iata", ""),
            "dep_time": dep.get("scheduled", ""),
            "dep_terminal": dep.get("terminal", ""),
            "dep_gate": dep.get("gate", ""),
            "arr_airport": arr.get("airport", ""),
            "arr_iata": arr.get("iata", ""),
            "arr_city": arr.get("city") or arr.get("airport", ""),
            "arr_time": arr.get("scheduled", ""),
            "arr_terminal": arr.get("terminal", ""),
            "arr_gate": arr.get("gate", ""),
        }
    except Exception as e:
        print(f"Flight lookup error for {flight_number}: {e}")
        return None

def get_dest_info_from_iata(iata_code, airport_name):
    prompt = (
        f"Airport IATA code: '{iata_code}', airport name: '{airport_name}'.\n"
        f"Return ONLY JSON: {{\"destination\": \"city name\", \"currency\": \"3-letter ISO code\"}}"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=60,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"get_dest_info_from_iata JSON parse error: {e} | raw: {raw}")
        return {"destination": airport_name or iata_code, "currency": "SGD"}


# --- Overseas State Management ---

def is_overseas_mode_request(text):
    lower = text.lower()
    flights = extract_flight_number(text)
    has_flight = bool(flights)
    has_multiple_flights = len(re.findall(r'\b[A-Z]{1,3}\d{2,4}[A-Z]?\b', text.upper())) >= 2

    if has_multiple_flights:
        return True

    travel_words = [
        "flying", "flight", "boarding", "departure", "departing",
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
    overseas_state["active"] = False
    overseas_state["destination"] = ""
    overseas_state["currency"] = "SGD"
    overseas_state["currencies"] = []
    overseas_state["return_date"] = ""
    overseas_state["return_flight"] = None
    overseas_state["trip_start"] = None
    overseas_state["trip_destinations"] = []
    for job_key in ["dep_job_id", "return_job_id"]:
        job_id = overseas_state.get(job_key)
        if job_id and config._scheduler:
            try:
                config._scheduler.remove_job(job_id)
            except Exception:
                pass
        overseas_state[job_key] = None


async def activate_overseas_mode_scheduled(dest, curr, return_flight_data=None):
    for d in [config.expense_sessions, config.receipt_confirm_sessions]:
        d.pop(YOUR_CHAT_ID, None)
    config.session_timestamps.pop(YOUR_CHAT_ID, None)

    overseas_state["active"] = True
    overseas_state["destination"] = dest
    overseas_state["currency"] = curr
    overseas_state["currencies"] = [curr] if curr != "SGD" else []
    overseas_state["trip_start"] = date.today().strftime("%d/%m/%Y")
    overseas_state["trip_destinations"] = [dest]

    ret_flight_num = return_flight_data.get("flight", "") if return_flight_data else ""
    ret_time_str = return_flight_data.get("dep_time", "") if return_flight_data else ""
    dep_flight = overseas_state.get("dep_flight", "")
    dep_time = overseas_state.get("dep_time", "")
    save_trip(dest, curr, dep_flight, dep_time, ret_flight_num, ret_time_str)

    msg = f"Overseas mode on ✈️\nDestination: {dest}\nCurrency: {curr}\nI'll log expenses in {curr} with SGD equivalent."
    if return_flight_data:
        ret_dep = format_flight_time(return_flight_data.get("dep_time", ""))
        msg += f"\nReturn flight: {return_flight_data.get('flight', '')} departs {ret_dep}"
        ret_arr_str = return_flight_data.get("arr_time", "")
        if ret_arr_str and config._scheduler and config._app_ref:
            try:
                ret_arr_dt = datetime.fromisoformat(ret_arr_str.replace("Z", "+00:00"))
                ret_arr_local = ret_arr_dt.astimezone(TIMEZONE)
                job = config._scheduler.add_job(
                    deactivate_and_notify, "date",
                    run_date=ret_arr_local, args=[config._app_ref]
                )
                overseas_state["return_job_id"] = job.id
            except Exception as e:
                print(f"Failed to schedule return deactivation: {e}")

    if config._app_ref:
        try:
            await config._app_ref.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
        except Exception as e:
            print(f"Failed to send overseas mode activation message: {e}")


async def deactivate_and_notify(app):
    import random
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

    RETURN_INTENT = ["returning on", "returning on the", "flying back on", "back on",
                     "return on", "coming back on", "heading back on"]

    def _has_return_intent(msg, flight_num):
        lower_msg = msg.lower()
        for phrase in RETURN_INTENT:
            if phrase in lower_msg and flight_num.lower() in lower_msg:
                if lower_msg.index(phrase) < lower_msg.index(flight_num.lower()):
                    return True
        return False

    def _is_sg_arrival(flight_info, force_return=False):
        if force_return:
            return True
        if not flight_info:
            return False
        iata = (flight_info.get("arr_iata") or "").upper()
        city = (flight_info.get("arr_city") or "").lower()
        airport = (flight_info.get("arr_airport") or "").lower()
        return (iata in SG_IATA_CODES or
                any(k in city for k in SG_CITY_KEYWORDS) or
                any(k in airport for k in SG_CITY_KEYWORDS))

    def _extract_leg2_dest(msg, flight2):
        f2 = re.escape(flight2)
        patterns = [
            rf"{f2}\s+(?:[A-Z]{{3}}\s+)?to\s+([A-Za-z][A-Za-z\s]{{2,25}}?)(?:\s+on\s+|\s+Mon|\s+Tue|\s+Wed|\s+Thu|\s+Fri|\s+Sat|\s+Sun|$)",
            rf"then\s+{f2}\s+(?:[A-Z]{{3}}\s+)?to\s+([A-Za-z][A-Za-z\s]{{2,25}}?)(?:\s+on\s+|$)",
        ]
        STOPWORDS = {"on", "the", "a", "an", "my", "this", "next"}
        for pat in patterns:
            m = re.search(pat, msg, re.IGNORECASE)
            if m:
                dest = m.group(1).strip()
                if dest.lower() not in STOPWORDS and len(dest) > 2:
                    return dest
        return None

    all_flights = extract_all_flight_numbers(text)
    if all_flights and AVIATIONSTACK_API_KEY:
        outbound = all_flights[0]
        return_flight_num = all_flights[1] if len(all_flights) > 1 else None

        flight_dates = extract_flight_dates(text)
        dep_date = flight_dates[0] if len(flight_dates) > 0 else None
        ret_date = flight_dates[1] if len(flight_dates) > 1 else None

        flight_data = lookup_flight(outbound, flight_date=dep_date)
        if flight_data:
            dep_fmt = format_flight_time(flight_data["dep_time"])
            arr_fmt = format_flight_time(flight_data["arr_time"])
            arr_label = flight_data["arr_city"] or flight_data["arr_airport"] or flight_data["arr_iata"]

            pending = {
                "flight_number": outbound,
                "dep_time": flight_data["dep_time"],
                "dep_terminal": flight_data.get("dep_terminal", ""),
                "dep_gate": flight_data.get("dep_gate", ""),
                "arr_time": flight_data["arr_time"],
                "arr_airport": flight_data["arr_airport"],
                "arr_iata": flight_data["arr_iata"],
                "arr_city": flight_data["arr_city"],
                "arr_terminal": flight_data.get("arr_terminal", ""),
                "arr_gate": flight_data.get("arr_gate", ""),
                "return_flight_data": None,
            }

            reply = f"Found {outbound} ✈️\nDeparts: {dep_fmt}\nArrives: {arr_fmt} → {arr_label}\n"

            if return_flight_num:
                force_return = _has_return_intent(text, return_flight_num)
                ret_data = lookup_flight(return_flight_num, flight_date=ret_date)
                if ret_data:
                    ret_dep = format_flight_time(ret_data["dep_time"])
                    ret_arr = format_flight_time(ret_data["arr_time"])
                    ret_data["flight"] = return_flight_num
                    if _is_sg_arrival(ret_data, force_return=force_return):
                        pending["return_flight_data"] = ret_data
                        reply += f"Return: {return_flight_num} departs {ret_dep}, arrives {ret_arr}\n"
                    else:
                        leg2_dest = _extract_leg2_dest(text, return_flight_num)
                        if leg2_dest:
                            pending["leg2_flight"] = return_flight_num
                            pending["leg2_dest"] = leg2_dest
                        reply += f"Connecting: {return_flight_num} departs {ret_dep}, arrives {ret_arr}\n"

            if _is_sg_arrival(flight_data):
                if overseas_state.get("active"):
                    deactivate_overseas_mode()
                    reply += "\nBack in SG — switching to SGD mode. 🏠"
                else:
                    reply += "\nLooks like you're arriving in SG — no overseas mode needed."
                overseas_state.pop("_pending_flight", None)
                return reply

            overseas_state["_pending_flight"] = pending
            overseas_state["dep_flight"] = outbound
            overseas_state["dep_time"] = flight_data["dep_time"]
            overseas_state["dep_terminal"] = flight_data.get("dep_terminal", "")
            overseas_state["dep_gate"] = flight_data.get("dep_gate", "")
            overseas_state["arr_terminal"] = flight_data.get("arr_terminal", "")
            overseas_state["arr_gate"] = flight_data.get("arr_gate", "")

            dest_info = get_dest_info_from_iata(flight_data["arr_iata"], flight_data["arr_airport"])
            dest = dest_info.get("destination", arr_label)
            curr = dest_info.get("currency", "SGD")
            pending["dest"] = dest
            pending["curr"] = curr

            dep_dt_str = flight_data.get("dep_time", "")
            if dep_dt_str:
                try:
                    dep_dt = datetime.fromisoformat(dep_dt_str.replace("Z", "+00:00"))
                    dep_local = dep_dt.astimezone(TIMEZONE)
                    reply += f"\nSchedule overseas mode for {dest} ({curr}) at departure ({dep_fmt})? (Y/N)"
                    overseas_state["_awaiting_return_flight"] = {"dep_dt": dep_local, "dest": dest, "curr": curr}
                except Exception as e:
                    print(f"handle_overseas_request: dep_time parse error: {e}")
                    reply += f"\nActivate overseas mode for {dest} ({curr})? (Y/N)"
            else:
                reply += f"\nActivate overseas mode for {dest} ({curr})? (Y/N)"

            return reply

        else:
            return (
                f"Found flight {outbound} but couldn't look up the details (AviationStack limit or connectivity issue).\n"
                f"Where are you heading? I can still set up overseas mode manually."
            )

    # Manual overseas mode (no flight numbers or no AviationStack key)
    dest_info = {}
    if all_flights:
        flight_num = all_flights[0]
        return (
            f"Got flight {flight_num} but no API key to look it up.\n"
            f"Where are you heading and what currency? (e.g. 'Japan, JPY')"
        )

    for loc in LOCATION_CONTEXT_WORDS:
        if loc in lower:
            dest_info = get_dest_info_from_iata("", loc.title())
            break

    dest = dest_info.get("destination", "")
    curr = dest_info.get("currency", "")

    if dest and curr and curr != "SGD":
        from state import get_fx_rate
        overseas_state["active"] = True
        overseas_state["destination"] = dest
        overseas_state["currency"] = curr
        overseas_state["currencies"] = [curr]
        overseas_state["trip_start"] = date.today().strftime("%d/%m/%Y")
        overseas_state["trip_destinations"] = [dest]
        save_trip(dest, curr)
        get_fx_rate(curr)
        return f"Overseas mode on ✈️\nDestination: {dest}\nCurrency: {curr}\nI'll log expenses in {curr} with SGD equivalent."

    return (
        "Couldn't figure out the destination from that — where are you headed and what currency?\n"
        "(e.g. 'Japan, JPY' or 'flying to BKK SQ123')"
    )
