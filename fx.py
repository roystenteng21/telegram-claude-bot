import re
import json
import pytz
import httpx
from datetime import date, datetime
import state
from config import EXCHANGE_RATE_API_KEY, TIMEZONE
from sheets import get_sheet

def get_fx_rate(currency):
    """Return SGD rate for given currency. Uses cache if under 12hrs, else fetches fresh."""
    if currency == "SGD":
        return 1.0
    cache_key = f"{currency}_SGD"
    cached = state.cached_fx_rates.get(cache_key)
    if cached:
        age = datetime.now(pytz.utc) - cached["fetched_at"]
        if age.total_seconds() < 43200:
            return cached["rate"]
    if EXCHANGE_RATE_API_KEY:
        try:
            url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/pair/{currency}/SGD"
            with httpx.Client(timeout=8) as hx:
                resp = hx.get(url)
            data = resp.json()
            if data.get("result") == "success":
                rate = float(data["conversion_rate"])
                state.cached_fx_rates[cache_key] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
                if currency in state.manual_fx_rates:
                    del state.manual_fx_rates[currency]
                return rate
        except Exception as e:
            print(f"FX fetch error for {currency}: {e}")
    manual = state.manual_fx_rates.get(currency)
    if manual:
        today_str = date.today().strftime("%Y-%m-%d")
        if manual.get("date") == today_str:
            return manual["rate"]
        print(f"Using previous manual rate for {currency}: {manual['rate']}")
        return manual["rate"]
    return None

def save_manual_fx_rate(currency, rate, sgd_per_unit=True):
    if not sgd_per_unit:
        rate = 1.0 / rate if rate else 0
    state.manual_fx_rates[currency] = {
        "rate": rate,
        "date": date.today().strftime("%Y-%m-%d"),
        "sgd_per_unit": sgd_per_unit
    }
    state.cached_fx_rates[f"{currency}_SGD"] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
    persist_fx_rates_to_sheet()

def persist_fx_rates_to_sheet():
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        records = sheet.get_all_records()
        fx_data = json.dumps({
            k: {"rate": v["rate"], "fetched_at": v["fetched_at"].isoformat()}
            for k, v in state.cached_fx_rates.items()
            if isinstance(v.get("fetched_at"), datetime)
        })
        for i, r in enumerate(records):
            if r.get("Key") == "cached_fx_rates":
                sheet.update_cell(i + 2, 2, fx_data)
                return
        sheet.append_row(["cached_fx_rates", fx_data])
    except Exception as e:
        print(f"persist_fx_rates_to_sheet error: {e}")

def load_fx_rates_from_sheet():
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        records = sheet.get_all_records()
        for r in records:
            if r.get("Key") == "cached_fx_rates":
                raw = r.get("Value", "")
                if not raw:
                    return
                data = json.loads(raw)
                cutoff = datetime.now(pytz.utc) - __import__("datetime").timedelta(hours=12)
                for k, v in data.items():
                    try:
                        fetched_at = datetime.fromisoformat(v["fetched_at"])
                        if fetched_at.tzinfo is None:
                            fetched_at = pytz.utc.localize(fetched_at)
                        if fetched_at > cutoff:
                            state.cached_fx_rates[k] = {"rate": v["rate"], "fetched_at": fetched_at}
                    except Exception as e:
                        print(f"load_fx_rates_from_sheet: bad entry {k}: {e}")
                return
    except Exception as e:
        print(f"load_fx_rates_from_sheet error: {e}")

def parse_manual_fx_input(text, currency):
    """Parse manual FX rate input. Returns ((rate, sgd_per_unit), display_str) or (None, None)."""
    try:
        text = text.strip().replace(",", "")
        text_lower = text.lower()
        partial_currency_map = {
            "my": "myr", "rm": "myr", "rp": "idr",
            "bt": "thb", "baht": "thb", "vnd": "vnd", "dong": "vnd",
            "php": "php", "peso": "php",
        }
        for partial, full in partial_currency_map.items():
            text_lower = re.sub(rf"\b{partial}\b", full, text_lower)
        nums = re.findall(r"[\d]+\.[\d]+|[\d]+", text_lower)
        if not nums:
            return None, None
        to_pattern = re.search(r"1\s*sgd\s+(?:to|=|:)\s*([\d.]+)\s*\w*", text_lower)
        if to_pattern:
            num = float(to_pattern.group(1))
            if num <= 0:
                return None, None
            display = f"$1 SGD = {num:.4f} {currency}"
            return (num, False), display
        curr_to_sgd = re.search(
            rf"1\s*{re.escape(currency.lower())}\s+(?:to|=|:)\s*([\d.]+)\s*(?:sgd)?", text_lower
        )
        if curr_to_sgd:
            num = float(curr_to_sgd.group(1))
            if num <= 0:
                return None, None
            display = f"1 {currency} = ${num:.4f} SGD"
            return (num, True), display
        num_match = re.search(r"[\d.]+", text)
        if not num_match:
            return None, None
        num = float(num_match.group())
        if num <= 0:
            return None, None
        if "sgd" in text_lower and currency.lower() in text_lower:
            sgd_pos = text_lower.index("sgd")
            curr_pos = text_lower.index(currency.lower()) if currency.lower() in text_lower else -1
            if curr_pos > sgd_pos:
                display = f"$1 SGD = {num:.4f} {currency}"
                return (num, False), display
            else:
                display = f"1 {currency} = ${num:.4f} SGD"
                return (num, True), display
        elif num > 10:
            display = f"$1 SGD = {num:.2f} {currency}"
            return (num, False), display
        else:
            display = f"1 {currency} = ${num:.4f} SGD"
            return (num, True), display
    except Exception as e:
        print(f"parse_manual_fx_input error: {e}")
        return None, None

async def refresh_fx_rates(app=None):
    """Refresh cached FX rates for all active overseas currencies. Called twice daily."""
    try:
        currencies = [c for c in state.overseas_state.get("currencies", []) if c != "SGD"]
        if not currencies:
            return
        for currency in currencies:
            cache_key = f"{currency}_SGD"
            if EXCHANGE_RATE_API_KEY:
                try:
                    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/pair/{currency}/SGD"
                    async with httpx.AsyncClient(timeout=8) as hx:
                        resp = await hx.get(url)
                    data = resp.json()
                    if data.get("result") == "success":
                        rate = float(data["conversion_rate"])
                        state.cached_fx_rates[cache_key] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
                        print(f"FX refresh: 1 {currency} = {rate} SGD")
                except Exception as e:
                    print(f"FX refresh error for {currency}: {e}")
    except Exception as e:
        print(f"refresh_fx_rates error: {e}")
