import json
import pytz
from datetime import datetime, timedelta, date

import config
from config import (
    TIMEZONE, SESSION_TIMEOUT_MINUTES, SESSION_TIMEOUT_MESSAGES,
    EXCHANGE_RATE_API_KEY, YOUR_CHAT_ID,
    expense_sessions, delete_sessions, portfolio_delete_sessions,
    confirm_sessions, receipt_confirm_sessions, edit_sessions,
    meeting_sessions, session_timestamps, todo_disambig_sessions,
    overseas_state,
)


# --- Session Touch / Timeout ---

def touch_session(user_id):
    """Record current time for session timeout tracking."""
    config.session_timestamps[user_id] = datetime.now(TIMEZONE)


def is_session_expired(user_id):
    """Return True if the session for user_id has exceeded SESSION_TIMEOUT_MINUTES."""
    ts = config.session_timestamps.get(user_id)
    if not ts:
        return True
    elapsed = (datetime.now(TIMEZONE) - ts).total_seconds() / 60
    return elapsed > SESSION_TIMEOUT_MINUTES


def clear_all_sessions(user_id):
    """Clear all active sessions for a user."""
    for d in [config.expense_sessions, config.delete_sessions, config.portfolio_delete_sessions,
              config.confirm_sessions, config.receipt_confirm_sessions, config.recon_sessions,
              config.edit_sessions, config.meeting_sessions, config.session_timestamps]:
        d.pop(user_id, None)


async def check_session_timeouts(user_id, update):
    """Check all active sessions for timeout. Returns True if any session was expired and cleared."""
    if not is_session_expired(user_id):
        return False

    expired = False
    if user_id in config.expense_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["expense"])
        del config.expense_sessions[user_id]
        expired = True
    if user_id in config.delete_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["delete"])
        del config.delete_sessions[user_id]
        expired = True
    if user_id in config.portfolio_delete_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["portfolio_delete"])
        del config.portfolio_delete_sessions[user_id]
        expired = True
    if user_id in config.confirm_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["confirm"])
        del config.confirm_sessions[user_id]
        expired = True
    if user_id in config.receipt_confirm_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["receipt_confirm"])
        del config.receipt_confirm_sessions[user_id]
        expired = True
    if user_id in config.edit_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["edit"])
        del config.edit_sessions[user_id]
        expired = True
    if user_id in config.meeting_sessions:
        await update.message.reply_text(SESSION_TIMEOUT_MESSAGES["meeting"])
        del config.meeting_sessions[user_id]
        expired = True

    config.session_timestamps.pop(user_id, None)
    return expired


# --- FX Rates ---

import requests

def get_fx_rate(currency):
    """Return SGD rate for given currency. Uses cache if under 12hrs, else fetches fresh.
    Falls back to manual rate if API unavailable. No Claude estimate."""
    if currency == "SGD":
        return 1.0
    cache_key = f"{currency}_SGD"

    cached = config.cached_fx_rates.get(cache_key)
    if cached:
        age = datetime.now(pytz.utc) - cached["fetched_at"]
        if age.total_seconds() < 43200:
            return cached["rate"]

    if EXCHANGE_RATE_API_KEY:
        try:
            url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/pair/{currency}/SGD"
            resp = requests.get(url, timeout=8)
            data = resp.json()
            if data.get("result") == "success":
                rate = float(data["conversion_rate"])
                config.cached_fx_rates[cache_key] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
                print(f"FX cache updated: 1 {currency} = {rate} SGD")
                if currency in config.manual_fx_rates:
                    del config.manual_fx_rates[currency]
                return rate
            else:
                print(f"FX API non-success for {currency}: {data.get('error-type', data)}")
        except Exception as e:
            print(f"FX fetch error for {currency}: {e}")

    manual = config.manual_fx_rates.get(currency)
    if manual:
        today_str = date.today().strftime("%Y-%m-%d")
        if manual.get("date") == today_str:
            return manual["rate"]
        print(f"Using previous manual rate for {currency}: {manual['rate']}")
        return manual["rate"]

    return None


def save_manual_fx_rate(currency, rate, sgd_per_unit=True):
    """Save a manually entered FX rate."""
    if not sgd_per_unit:
        rate = 1.0 / rate if rate else 0
    config.manual_fx_rates[currency] = {
        "rate": rate,
        "date": date.today().strftime("%Y-%m-%d"),
        "sgd_per_unit": sgd_per_unit
    }
    config.cached_fx_rates[f"{currency}_SGD"] = {"rate": rate, "fetched_at": datetime.now(pytz.utc)}
    print(f"Manual FX rate saved: 1 {currency} = {rate} SGD")
    persist_fx_rates_to_sheet()


def persist_fx_rates_to_sheet():
    """Save cached FX rates to Settings sheet so they survive restarts."""
    from sheets import get_sheet
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        records = sheet.get_all_records()
        fx_data = json.dumps({
            k: {"rate": v["rate"], "fetched_at": v["fetched_at"].isoformat()}
            for k, v in config.cached_fx_rates.items()
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
    """Load cached FX rates from Settings sheet on startup."""
    from sheets import get_sheet
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
                cutoff = datetime.now(pytz.utc) - timedelta(hours=12)
                for k, v in data.items():
                    try:
                        fetched_at = datetime.fromisoformat(v["fetched_at"])
                        if fetched_at.tzinfo is None:
                            fetched_at = pytz.utc.localize(fetched_at)
                        if fetched_at > cutoff:
                            config.cached_fx_rates[k] = {"rate": v["rate"], "fetched_at": fetched_at}
                            print(f"Restored FX rate: {k} = {v['rate']}")
                    except Exception as e:
                        print(f"load_fx_rates_from_sheet: bad entry {k}: {e}")
                return
    except Exception as e:
        print(f"load_fx_rates_from_sheet error: {e}")


def parse_manual_fx_input(text, currency):
    """Parse a manually entered FX rate. Returns (rate, display_str) or (None, None)."""
    import re
    text = text.strip()
    # Pattern: "1 SGD = X CURR" or "1 CURR = X SGD" or just a number
    sgd_to_curr = re.search(r'1\s*SGD\s*(?:=|to)\s*([\d.]+)\s*' + re.escape(currency), text, re.IGNORECASE)
    curr_to_sgd = re.search(r'1\s*' + re.escape(currency) + r'\s*(?:=|to)\s*([\d.]+)\s*SGD', text, re.IGNORECASE)
    bare_number = re.search(r'^([\d.]+)$', text)

    if curr_to_sgd:
        rate = float(curr_to_sgd.group(1))
        display = f"1 {currency} = {rate} SGD"
        return (rate, True), display
    elif sgd_to_curr:
        rate = float(sgd_to_curr.group(1))
        sgd_rate = 1.0 / rate if rate else 0
        display = f"1 SGD = {rate} {currency}"
        return (sgd_rate, True), display
    elif bare_number:
        rate = float(bare_number.group(1))
        display = f"1 {currency} = {rate} SGD"
        return (rate, True), display

    return None, None


async def refresh_fx_rates():
    """Refresh all cached FX rates. Called twice daily by scheduler."""
    currencies_to_refresh = list(set(
        k.replace("_SGD", "") for k in config.cached_fx_rates
    ))
    refreshed = 0
    for currency in currencies_to_refresh:
        if currency == "SGD":
            continue
        if EXCHANGE_RATE_API_KEY:
            try:
                url = f"https://v6.exchangerate-api.com/v6/{EXCHANGE_RATE_API_KEY}/pair/{currency}/SGD"
                resp = requests.get(url, timeout=8)
                data = resp.json()
                if data.get("result") == "success":
                    rate = float(data["conversion_rate"])
                    config.cached_fx_rates[f"{currency}_SGD"] = {
                        "rate": rate,
                        "fetched_at": datetime.now(pytz.utc)
                    }
                    refreshed += 1
            except Exception as e:
                print(f"refresh_fx_rates: {currency} error: {e}")
    if refreshed:
        persist_fx_rates_to_sheet()
    print(f"FX refresh done — {refreshed} rate(s) updated")


# --- Anthropic Health ---

async def notify_anthropic_down(app):
    """Send Anthropic API down notification if threshold reached."""
    if config._anthropic_failure_count >= config.ANTHROPIC_FAILURE_THRESHOLD and not config._anthropic_down_notified:
        config._anthropic_down_notified = True
        try:
            await app.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=(
                    "⚠️ Anthropic API appears to be down — some features unavailable:\n"
                    "• Receipt vision parsing\n"
                    "• Expense text parsing\n"
                    "• Birthday greetings\n"
                    "• Reminder parsing\n"
                    "• Market narrative\n\n"
                    "I'll notify you when it recovers."
                )
            )
        except Exception as e:
            print(f"notify_anthropic_down error: {e}")


def track_anthropic_call(success):
    """Track Anthropic API call success/failure for downtime detection."""
    if success:
        if config._anthropic_down_notified:
            config._anthropic_down_notified = False
        config._anthropic_failure_count = 0
    else:
        config._anthropic_failure_count += 1
