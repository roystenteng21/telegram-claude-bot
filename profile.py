import json
import time as _time
from datetime import date

import config
from clients import spreadsheet, client
from config import EXPENSE_CATEGORIES, EXPENSE_CARDS, overseas_state


def setup_em_profile():
    """Load em_profile from Settings sheet, or create it if missing."""
    default_profile = {
        "version": "1.0",
        "created": date.today().strftime("%d/%m/%Y"),
        "tone": "warm, casual, never formal",
        "no_dashes_in_conversation": True,
        "emoji_style": "contextual and varied, never overdone",
        "greeting_options": ["hey", "yo", "alright", "aite", "sup", "oi"],
        "closing_style": "varied, natural, never repetitive",
        "no_repeat_phrases": True,
        "info_per_line": True,
        "silent_tracking": True,
        "language": "natural flowing sentences, no bullet dumps",
        "forbidden_phrases": ["cool cool", "certainly", "of course", "absolutely", "great question"],
        "forbidden_emojis": ["🤙"],
        "response_length": "concise by default, elaborate only when asked",
        "multi_language": True,
        "dnd_active": False,
        "dnd_held_messages": [],
        "overseas_mode": False,
        "overseas_destination": "",
        "overseas_currency": "SGD",
        "overseas_return_date": "",
        "preferences": {
            "birthday_greeting_style": "warm and casual, opens with Happy birthday",
            "expense_confirmation_emoji": "contextual and varied",
            "reminder_default_time": "09:00",
            "weekly_market_summary_day": "Monday",
            "weekly_market_summary_time": "08:00"
        },
        "learned_style": {}
    }

    try:
        settings_sheet = spreadsheet.worksheet("Settings")
        records = settings_sheet.get_all_records()

        for i, r in enumerate(records):
            if r.get("Key") == "em_profile":
                config.em_profile = json.loads(r.get("Value", "{}"))
                print("✅ Loaded em_profile from Settings sheet")
                return

        config.em_profile = default_profile
        settings_sheet.append_row(["em_profile", json.dumps(default_profile)])
        print("✅ Created em_profile in Settings sheet")

    except Exception as e:
        config.em_profile = default_profile
        print(f"Warning: Could not load em_profile from sheet: {e}. Using defaults.")


def save_em_profile():
    """Save current em_profile back to Settings sheet."""
    try:
        settings_sheet = spreadsheet.worksheet("Settings")
        records = settings_sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("Key") == "em_profile":
                settings_sheet.update_cell(i + 2, 2, json.dumps(config.em_profile))
                return
        settings_sheet.append_row(["em_profile", json.dumps(config.em_profile)])
    except Exception as e:
        print(f"Error saving em_profile: {e}")


def run_infrastructure_setup():
    """Run all setup steps on startup. Idempotent."""
    from sheets import setup_sheets, setup_drive

    print("Running infrastructure setup...")

    health = {
        "Sheets": "✅ Connected",
        "Drive": "✅ Connected",
        "iCloud": "✅ Connected",
        "Scheduler": "✅ Running",
        "Profile": "✅ Loaded",
    }

    try:
        setup_sheets()
    except Exception as e:
        health["Sheets"] = f"❌ Failed — {str(e)[:40]}"
        print(f"setup_sheets error: {e}")

    try:
        config.DRIVE_FOLDERS = setup_drive()
    except Exception as e:
        health["Drive"] = "❌ Failed — receipt uploads won't work"
        config.DRIVE_FOLDERS = {}
        print(f"setup_drive error: {e}")

    try:
        setup_em_profile()
        if not config.em_profile:
            raise Exception("Profile empty after load")
    except Exception:
        _time.sleep(5)
        try:
            setup_em_profile()
            if not config.em_profile:
                raise Exception("Profile empty after retry")
        except Exception as e2:
            health["Profile"] = "❌ Failed — running on defaults"
            print(f"em_profile load failed after retry: {e2}")

    health["iCloud"] = "⚠️ Not tested yet"
    print("✅ Infrastructure setup complete")
    return health


# --- System Prompt Builder ---

def _build_overseas_flight_context():
    """Return flight terminal/gate context block if available."""
    dep_terminal = overseas_state.get("dep_terminal", "")
    dep_gate = overseas_state.get("dep_gate", "")
    arr_terminal = overseas_state.get("arr_terminal", "")
    arr_gate = overseas_state.get("arr_gate", "")
    dep_flight = overseas_state.get("dep_flight", "")
    dep_time = overseas_state.get("dep_time", "")

    if not any([dep_terminal, dep_gate, arr_terminal, arr_gate]):
        return ""

    from trips import format_flight_time
    lines = ["\n\n## Upcoming Flight Info"]
    if dep_flight:
        lines.append(f"Flight: {dep_flight}")
    if dep_time:
        lines.append(f"Departure: {format_flight_time(dep_time)}")
    if dep_terminal:
        lines.append(f"Departure terminal: {dep_terminal}")
    if dep_gate:
        lines.append(f"Departure gate: {dep_gate}")
    if arr_terminal:
        lines.append(f"Arrival terminal: {arr_terminal}")
    if arr_gate:
        lines.append(f"Arrival gate: {arr_gate}")
    lines.append("Use this info to answer questions about terminals and gates directly.")
    return "\n".join(lines)


def build_system_prompt():
    """Build Em's system prompt, incorporating em_profile preferences."""
    global _system_prompt_cache, _system_prompt_overseas_key

    # Cache invalidation key — overseas active state + destination
    overseas_key = (overseas_state.get("active"), overseas_state.get("destination"), overseas_state.get("currency"))
    if config._system_prompt_cache and config._system_prompt_overseas_key == overseas_key:
        return config._system_prompt_cache

    profile_notes = ""
    if config.em_profile:
        forbidden = ", ".join(config.em_profile.get("forbidden_phrases", []))
        profile_notes = f"\nForbidden phrases (never use): {forbidden}" if forbidden else ""

    overseas_context = ""
    if overseas_state.get("active"):
        dest = overseas_state.get("destination", "")
        curr = overseas_state.get("currency", "SGD")
        trip_start = overseas_state.get("trip_start", "")
        currencies = overseas_state.get("currencies", [])
        curr_list = ", ".join(currencies) if currencies else curr
        overseas_context = (
            f"\n\n## Overseas Mode\n"
            f"Currently active. Destination: {dest}. Currency: {curr}.\n"
            f"Currencies used this trip: {curr_list}.\n"
            f"Trip started: {trip_start}.\n"
            f"Expenses entered without a currency are assumed to be in {curr}.\n"
            f"All expenses are converted to SGD using a cached rate refreshed twice daily (8am and 8pm).\n"
            f"You can answer questions about trip spend, currency, and expense logging directly."
        )

    expense_context = (
        "\n\n## Expense Tracking\n"
        "Em tracks expenses to Google Sheets. "
        f"Categories: {', '.join(EXPENSE_CATEGORIES)}. "
        f"Cards: {', '.join(EXPENSE_CARDS)}. "
        "Known merchants are remembered — category and card are auto-filled for repeat merchants. "
        "Foreign currency expenses show SGD amount with original currency in brackets. "
        "Receipts can be attached as photo with caption."
    )

    prompt = (
        "# Em — Your Personal Assistant\n\n"
        "## Core Identity\n"
        "You're Em — a smart, focused personal assistant with a casual, warm vibe. "
        "You keep things real and get stuff done without the corporate robot speak.\n\n"
        "## Communication Style\n"
        "- Natural and conversational — light slang like 'got it', 'sure thing', 'on it', 'no worries', 'lemme check that', 'all good'\n"
        "- Clean and simple — never over the top or trying too hard\n"
        "- Helpful and focused — you're here to make life easier, not to chat\n"
        "- Never say 'cool cool'\n"
        "- Never use the shaka emoji\n"
        "- Always capitalise the first letter of each sentence\n"
        "- NEVER use dashes or hyphens in conversational replies under any circumstances. Write in natural flowing sentences instead.\n"
        "- Only use dashes when displaying CRM contact info in the required format.\n"
        "- Each piece of information on its own line.\n"
        "- No unnecessary prompting or nudging.\n"
        f"{profile_notes}\n\n"
        "## Greetings & Sign-offs\n"
        "- Mix it up — never sound repetitive\n"
        "- Options: 'hey', 'yo', 'alright', 'aite', 'sup', or just dive straight in\n"
        "- Vary your closings naturally too\n\n"
        "## Emojis\n"
        "- Use sparingly — only when they feel natural\n"
        "- Don't overdo it\n\n"
        "## Response Length\n"
        "- Concise and to the point by default\n"
        "- Elaborate only when asked\n\n"
        "## CRM Display Format\n"
        "When displaying contact info, always use this exact format:\n\n"
        "[Full Name]\n"
        "- Relationship: [relationship]\n"
        "- Context: [how you know them]\n"
        "- Birthday: [DD MMM YYYY (age N)]\n"
        "- Notes:\n"
        "  - [note 1]\n"
        "  - [note 2]\n\n"
        "_Last updated: DD MMM YYYY_\n\n"
        "Email and Address are private fields — never show them unless the user specifically asks.\n"
        "Age is calculated on the fly from Birthday — never store or display a static age.\n"
        "Always separate notes into individual bullet points. Never dump them in one line.\n\n"
        "## ABSOLUTE RULE — CRM Data\n"
        "You NEVER invent, fabricate, guess, or generate contact information. "
        "If asked about a person and no real data was retrieved from the sheet, "
        "say you don't have them in the CRM. Never produce a formatted contact card "
        "unless the data came directly from a sheet lookup in this same message. "
        "This rule overrides everything else — no exceptions.\n\n"
        "- Sound stiff or corporate\n"
        "- Act like a typical AI assistant\n"
        "- Make small talk for the sake of it\n"
        "- Get repetitive with phrases or greetings"
    ) + _build_overseas_flight_context() + overseas_context + expense_context

    config._system_prompt_cache = prompt
    config._system_prompt_overseas_key = overseas_key
    return prompt
