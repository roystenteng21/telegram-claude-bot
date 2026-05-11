import re
import json
import asyncio
from datetime import date, datetime
import state
from config import (
    EXPENSE_CATEGORIES, EXPENSE_CARDS, EXPENSE_CATEGORY_EMOJI,
    EXPENSE_MERCHANT_OVERRIDES, SGD_HIGH_AMOUNT_THRESHOLD, EDIT_FIELD_SYNONYMS
)
from clients import client
from sheets import expenses_sheet, merchant_map_sheet, cards_sheet, get_sheet, log_error_to_em_log
from helpers import format_date
from fx import get_fx_rate, parse_manual_fx_input, save_manual_fx_rate
import time as _time

# ── Merchant cache ─────────────────────────────────────────────────────────────

def _get_merchant_records():
    now = _time.monotonic()
    if state._merchant_cache is None or state._merchant_cache_ts is None or (now - state._merchant_cache_ts) > state._MERCHANT_CACHE_TTL:
        try:
            state._merchant_cache = merchant_map_sheet().get_all_records()
            state._merchant_cache_ts = now
        except Exception as e:
            print(f"_get_merchant_records error: {e}")
            return []
    return state._merchant_cache

def _invalidate_merchant_cache():
    state._merchant_cache = None
    state._merchant_cache_ts = None

def _invalidate_expense_cache():
    state._same_day_expense_cache = set()
    state._same_day_expense_cache_date = ""

# ── Merchant memory ────────────────────────────────────────────────────────────

def get_merchant_memory(merchant):
    try:
        records = _get_merchant_records()
        merchant_lower = merchant.lower().strip()
        for r in records:
            if r.get("Merchant", "").lower() == merchant_lower:
                return r.get("Category", ""), r.get("Card", ""), r.get("Merchant", "")
        for r in records:
            known = r.get("Merchant", "").lower().strip()
            if known and (known in merchant_lower or merchant_lower in known):
                return r.get("Category", ""), r.get("Card", ""), r.get("Merchant", "")
        merchant_words = [w for w in merchant_lower.split() if len(w) > 2]
        for r in records:
            known = r.get("Merchant", "").lower().strip()
            known_words = [w for w in known.split() if len(w) > 2]
            if merchant_words and known_words and merchant_words[0] == known_words[0]:
                return r.get("Category", ""), r.get("Card", ""), r.get("Merchant", "")
    except Exception as e:
        print(f"get_merchant_memory error for '{merchant}': {e}")
    return None, None, None

def save_merchant_memory(merchant, category, card):
    try:
        sheet = merchant_map_sheet()
        sheet.append_row([merchant, category, card])
        _invalidate_merchant_cache()
        return True
    except Exception as e:
        print(f"save_merchant_memory failed for '{merchant}': {e}")
        log_error_to_em_log("save_merchant_memory", f"{merchant} — {e}")
        return False

def delete_merchant(merchant_name):
    try:
        sheet = merchant_map_sheet()
        records = sheet.get_all_values()
        if len(records) <= 1:
            return "No merchants saved yet."
        matches = []
        for i, row in enumerate(records[1:], start=2):
            if row and merchant_name.lower() in row[0].lower():
                matches.append((i, row[0]))
        if not matches:
            return f"No merchant found matching '{merchant_name}'."
        if len(matches) == 1:
            row_idx, found_name = matches[0]
            sheet.delete_rows(row_idx)
            _invalidate_merchant_cache()
            return f"Deleted '{found_name}' from merchant memory ✅\nNext time you log there, I'll ask for category and card again."
        lines = [f"Found {len(matches)} merchants matching '{merchant_name}' — which one?"]
        for i, (_, name) in enumerate(matches, 1):
            lines.append(f"{i}. {name}")
        lines.append("\nReply 'delete merchant [exact name]' to remove a specific one.")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error deleting merchant: {e}"

def get_merchant_emoji(category, merchant):
    merchant_lower = merchant.lower() if merchant else ""
    overrides = EXPENSE_MERCHANT_OVERRIDES.get(category, [])
    for keywords, emoji in overrides:
        if any(kw in merchant_lower for kw in keywords):
            return emoji
    if category in EXPENSE_CATEGORY_EMOJI:
        return EXPENSE_CATEGORY_EMOJI[category]
    if category:
        if category in state._category_emoji_cache:
            return state._category_emoji_cache[category]
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=5,
                messages=[{"role": "user", "content": f"Return a single emoji that best represents the expense category '{category}'. Return ONLY the emoji, nothing else."}]
            )
            inferred = resp.content[0].text.strip()
            state._category_emoji_cache[category] = inferred
            return inferred
        except Exception:
            pass
    return "💳"

def get_merchant_list():
    try:
        sheet = merchant_map_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No merchants saved yet. They are added automatically when you log a new expense."
        by_cat = {}
        for r in records:
            merchant = r.get("Merchant", "").strip()
            cat = r.get("Category", "").strip() or "Uncategorised"
            card = r.get("Card", "").strip()
            if not merchant:
                continue
            by_cat.setdefault(cat, [])
            entry = merchant + (f" ({card})" if card else "")
            by_cat[cat].append(entry)
        count = sum(len(v) for v in by_cat.values())
        lines = [f"Merchant Map ({count} merchants):\n"]
        for cat in sorted(by_cat):
            lines.append(f"*{cat}*")
            for m in sorted(by_cat[cat]):
                lines.append(f"  {m}")
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        return f"❌ Error fetching merchants: {e}"

# ── Cards ──────────────────────────────────────────────────────────────────────

def get_cards_live():
    if state._card_names_cache is not None:
        return state._card_names_cache
    try:
        ws = cards_sheet()
        state._card_names_cache = ws.get_all_records()
        return state._card_names_cache
    except Exception as e:
        print(f"get_cards_live error: {e}")
        return []

def get_card_names_live():
    cards = get_cards_live()
    names = [c.get("Card Name", "") for c in cards if c.get("Card Name")]
    return names if names else EXPENSE_CARDS

def get_card_default_for_category(category):
    try:
        cards = get_cards_live()
        for c in cards:
            default_cat = c.get("Default Category", "").strip().lower()
            if default_cat and default_cat == category.strip().lower():
                return c.get("Card Name", "")
        cat_lower = category.strip().lower()
        if cat_lower in ["fnb", "food", "dining", "f&b"]:
            return "Maybank"
        if cat_lower in ["grab", "transport"]:
            return "Amex"
        return "Citi"
    except Exception as e:
        print(f"get_card_default_for_category error: {e}")
        return "Citi"

def get_card_by_last4(last4):
    try:
        cards = get_cards_live()
        for c in cards:
            if str(c.get("Last 4", "")).strip() == str(last4).strip():
                return c.get("Card Name", "")
    except Exception as e:
        print(f"get_card_by_last4 error: {e}")
    return None

def set_card_default_category(card_name, category):
    try:
        ws = cards_sheet()
        records = ws.get_all_records()
        for i, r in enumerate(records, start=2):
            if r.get("Card Name", "").lower() == card_name.lower():
                ws.update_cell(i, 3, category)
                return f"Updated — {card_name} will now default to {category} ✅"
        return f"Card '{card_name}' not found. Your cards: {', '.join(get_card_names_live())}"
    except Exception as e:
        return f"❌ Couldn't update card default: {str(e)}"

def fuzzy_match_card(text):
    cards = get_card_names_live()
    text_lower = text.strip().lower()
    for c in cards:
        if c.lower() == text_lower:
            return c, True
    for c in cards:
        if text_lower in c.lower() or c.lower() in text_lower:
            return c, True
    for c in cards:
        if len(text_lower) >= 3 and c.lower().startswith(text_lower[:3]):
            return c, False
    return None, False

def fuzzy_match_category(text):
    text_lower = text.strip().lower()
    for c in EXPENSE_CATEGORIES:
        if c.lower() == text_lower:
            return c, True
    synonyms = {
        "food": "FnB", "dining": "FnB", "restaurant": "FnB", "eat": "FnB", "f&b": "FnB",
        "fun": "Entertainment", "movie": "Entertainment", "movies": "Entertainment",
        "gym": "Personal", "health": "Personal",
        "taxi": "Transport", "grab": "Transport", "uber": "Transport", "mrt": "Transport", "bus": "Transport",
        "clothes": "Shopping", "shop": "Shopping",
        "trip": "Travel", "holiday": "Travel", "flight": "Travel",
        "office": "Work", "business": "Work",
    }
    if text_lower in synonyms:
        return synonyms[text_lower], True
    for c in EXPENSE_CATEGORIES:
        if text_lower in c.lower() or c.lower() in text_lower:
            return c, False
    return None, False

def rename_category(old_name, new_name):
    old_title = next((c for c in EXPENSE_CATEGORIES if c.lower() == old_name.lower()), None)
    if not old_title:
        return f"Category '{old_name}' not found. Current categories: {', '.join(EXPENSE_CATEGORIES)}"
    new_clean = new_name.strip()
    # Update in-place
    idx = EXPENSE_CATEGORIES.index(old_title)
    EXPENSE_CATEGORIES[idx] = new_clean
    updated = 0
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if all_values:
            headers = all_values[0]
            if "Category" in headers:
                col_idx = headers.index("Category")
                for i, row in enumerate(all_values[1:], start=2):
                    if len(row) > col_idx and row[col_idx] == old_title:
                        sheet.update_cell(i, col_idx + 1, new_clean)
                        updated += 1
    except Exception as e:
        print(f"rename_category expenses error: {e}")
    try:
        sheet = merchant_map_sheet()
        all_values = sheet.get_all_values()
        if all_values:
            headers = all_values[0]
            if "Category" in headers:
                col_idx = headers.index("Category")
                for i, row in enumerate(all_values[1:], start=2):
                    if len(row) > col_idx and row[col_idx] == old_title:
                        sheet.update_cell(i, col_idx + 1, new_clean)
    except Exception as e:
        print(f"rename_category merchant map error: {e}")
    try:
        ws = cards_sheet()
        records = ws.get_all_records()
        for i, r in enumerate(records, start=2):
            if r.get("Default Category", "").strip().lower() == old_title.lower():
                ws.update_cell(i, 3, new_clean)
        state._card_names_cache = None
    except Exception as e:
        print(f"rename_category cards sheet error: {e}")
    return f"Renamed '{old_title}' to '{new_clean}'. Updated {updated} expense(s)."

# ── Expense core ───────────────────────────────────────────────────────────────

def parse_expense_text(text):
    return parse_expense_text_v2(text)

def parse_expense_text_v2(text):
    overseas_currency = state.overseas_state["currency"] if state.overseas_state["active"] else "SGD"
    live_cats = ", ".join(EXPENSE_CATEGORIES)
    live_cards = ", ".join(get_card_names_live())
    prompt = (
        f"Extract expense details from this message: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- merchant: string (brand name only, strip legal suffixes)\n"
        f"- amount: number (just the number, no currency symbol)\n"
        f"- currency: string (3-letter ISO code. If not mentioned, default to '{overseas_currency}')\n"
        f"- category: string (one of: {live_cats}) or empty if unclear\n"
        f"- card: string (one of: {live_cards}) or empty if not mentioned\n\n"
        f"Return ONLY the JSON."
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        if "amount" in parsed:
            try:
                parsed["amount"] = float(parsed["amount"])
            except (ValueError, TypeError):
                parsed["amount"] = None
        return parsed
    except Exception as e:
        print(f"parse_expense_text Claude error: {e}")
        return None

def log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link="", reconciled="No", notes=""):
    try:
        today = date.today().strftime("%d/%m/%Y")
        sheet = expenses_sheet()
        sheet.append_row([
            today, merchant, amount, currency, sgd_amount,
            category, card, receipt_link, reconciled, notes
        ])
        _invalidate_expense_cache()
        return True
    except Exception as e:
        print(f"log_expense failed: {e}")
        log_error_to_em_log("log_expense", f"{merchant} {amount} {currency} — {e}")
        return False

def format_expense_confirmation(merchant, amount, currency, category, card, sgd_amount=None,
                                receipt_saved=False, last4=None, high_amount=False,
                                use_manual_rate=False, missing_fields=None):
    emoji = get_merchant_emoji(category, merchant)
    lines = [f"{emoji} {merchant}"]
    if currency != "SGD" and sgd_amount is not None:
        amount_str = f"${sgd_amount:.2f} ({amount:,.0f} {currency})"
    else:
        amount_str = f"${amount:.2f}" if amount else "⚠️ Amount?"
    cat_str = "⚠️ Category?" if not category else category
    if not card:
        card_str = "⚠️ Card?"
    elif last4:
        card_str = f"{card} (*{last4})"
    else:
        card_str = card
    detail_line = f"{amount_str} | {cat_str} | {card_str}"
    lines.append(detail_line)
    if high_amount:
        lines.append("⚠️ Amount looks high — is this correct?")
    if use_manual_rate:
        lines.append("⚠️ Using manual rate — verify if needed")
    if receipt_saved:
        lines.append("🧾 Receipt saved to Drive")
    if missing_fields:
        if len(missing_fields) >= 3:
            prompt = "yes / enter missing values / skip"
        elif len(missing_fields) == 2:
            prompt = f"yes / enter {' & '.join(f.title() for f in missing_fields)} / skip"
        else:
            prompt = f"yes / enter {missing_fields[0].title()} / skip"
        lines.append(f"\nLog this? ({prompt})")
    else:
        lines.append("\nLog this? (yes / edit / skip)")
    return "\n".join(lines)

def format_expense_logged(merchant, amount, currency, category, card, sgd_amount=None, last4=None):
    emoji = get_merchant_emoji(category, merchant)
    if currency != "SGD" and sgd_amount is not None:
        amount_str = f"${sgd_amount:.2f} ({amount:,.0f} {currency})"
    else:
        amount_str = f"${amount:.2f}"
    card_str = f"{card} (*{last4})" if last4 else card
    return f"{emoji} {merchant}\n{amount_str} | {category} | {card_str}\n\nLogged ✅"

def check_same_day_duplicate(merchant, amount, currency):
    today = date.today().strftime("%d/%m/%Y")
    try:
        if state._same_day_expense_cache_date != today:
            sheet = expenses_sheet()
            records = sheet.get_all_records()
            state._same_day_expense_cache = {
                (r.get("Date", ""), r.get("Merchant", "").lower(),
                 str(r.get("Amount", "")), r.get("Currency", ""))
                for r in records if r.get("Date") == today
            }
            state._same_day_expense_cache_date = today
        return (today, merchant.lower(), str(amount), currency) in state._same_day_expense_cache
    except Exception as e:
        print(f"Duplicate check error: {e}")
    return False

def parse_multi_field_edit(text, field_keywords=None):
    if field_keywords is None:
        field_keywords = {
            "merchant": "merchant", "shop": "merchant", "store": "merchant", "place": "merchant",
            "amount": "amount", "price": "amount", "total": "amount", "cost": "amount",
            "currency": "currency",
            "category": "category", "cat": "category",
            "card": "card", "payment": "card", "pay": "card",
        }
    result = {}
    text = re.sub(r'"([^"]+)"', lambda m: m.group(0).replace(" ", "_SPACE_"), text)
    tokens = text.strip().split()
    i = 0
    current_field = None
    current_value_parts = []

    def flush():
        if current_field and current_value_parts:
            val = " ".join(current_value_parts).replace("_SPACE_", " ").strip('"')
            result[current_field] = val

    while i < len(tokens):
        token_lower = tokens[i].lower()
        if token_lower in field_keywords:
            flush()
            current_field = field_keywords[token_lower]
            current_value_parts = []
        elif current_field:
            current_value_parts.append(tokens[i])
        i += 1
    flush()
    return result

def handle_expense_text(text, user_id, receipt_link="", last4=None):
    try:
        parsed = parse_expense_text(text)
        if not parsed:
            return ("Couldn't read that as an expense — try: 'Starbucks $5.60' or 'spent $12 on lunch'."), False, None
        merchant = parsed.get("merchant", "Unknown")
        try:
            amount = float(parsed.get("amount", 0) or 0)
        except (ValueError, TypeError):
            amount = 0
        currency = parsed.get("currency", "SGD")
        category = parsed.get("category", "")
        card = parsed.get("card", "")
        if not merchant or merchant.lower() in ("unknown", "") or amount == 0:
            return ("Wasn't sure what to do with that — did you mean to log an expense, "
                    "or were you asking something else?"), False, None
        if amount <= 0:
            return "Amount can't be zero or negative — what's the correct amount?", False, None
        known_cat, known_card, canonical = get_merchant_memory(merchant)
        if canonical:
            merchant = canonical
        if known_cat and not category:
            category = known_cat
        explicit_card = card
        if not explicit_card:
            if category:
                card = get_card_default_for_category(category)
            else:
                card = "Citi"
        if last4 and not explicit_card:
            matched_card = get_card_by_last4(last4)
            if matched_card:
                card = matched_card
        sgd_amount = amount
        use_manual_rate = False
        if currency != "SGD":
            rate = get_fx_rate(currency)
            if rate is None:
                manual = state.manual_fx_rates.get(currency)
                if manual:
                    rate = manual["rate"]
                    use_manual_rate = True
                else:
                    session = {
                        "merchant": merchant, "amount": amount, "currency": currency,
                        "category": category, "card": card, "step": "fx_rate",
                        "receipt_link": receipt_link, "last4": last4
                    }
                    return (
                        f"Couldn't get the {currency}/SGD rate right now.\n"
                        f"Enter the exchange rate:\n"
                        f"• SGD to {currency}: e.g. 110 (meaning $1 SGD = {currency} 110)\n"
                        f"• {currency} to SGD: e.g. 0.0093 (meaning 1 {currency} = $0.0093 SGD)"
                    ), True, session
            sgd_amount = round(amount * rate, 2)
            if state.overseas_state["active"] and currency not in state.overseas_state["currencies"]:
                state.overseas_state["currencies"].append(currency)
        high_amount = sgd_amount > SGD_HIGH_AMOUNT_THRESHOLD
        if check_same_day_duplicate(merchant, amount, currency):
            session = {
                "merchant": merchant, "amount": amount, "currency": currency,
                "sgd_amount": sgd_amount, "category": category, "card": card,
                "step": "duplicate_confirm", "receipt_link": receipt_link, "last4": last4
            }
            return (f"Heads up — looks like you already logged {merchant} {currency} {amount:,.0f} today. Log it again? (yes / no)"), True, session
        missing = []
        if not category:
            missing.append("category")
        if not amount:
            missing.append("amount")
        is_new_merchant = not bool(known_cat)
        if not missing and not high_amount and not use_manual_rate and not is_new_merchant:
            success = log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link=receipt_link)
            if not success:
                return "⚠️ Failed to save expense — please try again.", False, None
            save_merchant_memory(merchant, category, card)
            return format_expense_logged(merchant, amount, currency, category, card, sgd_amount, last4), False, None
        session = {
            "merchant": merchant, "amount": amount, "currency": currency,
            "sgd_amount": sgd_amount, "category": category, "card": card,
            "step": "receipt_confirm", "missing_fields": missing,
            "receipt_link": receipt_link, "last4": last4,
            "high_amount": high_amount, "use_manual_rate": use_manual_rate,
            "is_new_merchant": is_new_merchant
        }
        state.receipt_confirm_sessions[user_id] = session
        from sessions import touch_session
        touch_session(user_id)
        confirmation = format_expense_confirmation(
            merchant, amount, currency, category, card, sgd_amount,
            receipt_saved=bool(receipt_link), last4=last4,
            high_amount=high_amount, use_manual_rate=use_manual_rate,
            missing_fields=missing
        )
        return confirmation, False, None
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        return (f"❌ Couldn't parse that as an expense ({type(e).__name__}: {str(e)[:60]}).\n"
                "Try: 'log [merchant] [amount]' — e.g. 'log Starbucks $5.60'"), False, None
    except Exception as e:
        return f"❌ Something went wrong logging that ({type(e).__name__}: {str(e)[:80]}). Try again or use 'log [merchant] [amount]'.", False, None

async def handle_receipt_confirm_session(user_id, text, update):
    session = state.receipt_confirm_sessions.get(user_id)
    if not session:
        return False
    from sessions import touch_session
    touch_session(user_id)
    lower = text.strip().lower()
    if lower in ["skip", "cancel", "no", "n", "nope", "nah"]:
        del state.receipt_confirm_sessions[user_id]
        state.session_timestamps.pop(user_id, None)
        receipt_link = session.get("receipt_link", "")
        if receipt_link:
            await update.message.reply_text("Entry not logged — receipt saved to Drive if you need it later.")
        else:
            await update.message.reply_text("Entry not logged.")
        return True
    if session.get("step") == "fx_rate":
        currency = session.get("currency", "")
        parsed_rate, display = parse_manual_fx_input(text, currency)
        if parsed_rate is None:
            await update.message.reply_text(
                f"Couldn't read that rate — try one of these formats:\n"
                f"• `3.11` (1 {currency} = 3.11 SGD)\n"
                f"• `1 SGD to 3.11 {currency}`\n"
                f"• `1 {currency} = 3.11 SGD`"
            )
            return True
        num, sgd_per_unit = parsed_rate
        save_manual_fx_rate(currency, num, sgd_per_unit)
        rate = await asyncio.to_thread(get_fx_rate, currency)
        session["sgd_amount"] = round(session["amount"] * rate, 2)
        session["step"] = "receipt_confirm"
        session["use_manual_rate"] = True
        state.receipt_confirm_sessions[user_id] = session
        await update.message.reply_text(
            f"Got it — using {display} for today.\n\n" +
            format_expense_confirmation(
                session["merchant"], session["amount"], session["currency"],
                session.get("category", ""), session.get("card", ""),
                session.get("sgd_amount"), receipt_saved=bool(session.get("receipt_link")),
                last4=session.get("last4"), high_amount=session.get("high_amount", False),
                use_manual_rate=True, missing_fields=session.get("missing_fields", [])
            )
        )
        return True
    if session.get("step") == "duplicate_confirm":
        if lower in ["yes", "y", "yep", "yeah", "yup"]:
            state.receipt_confirm_sessions[user_id] = session
            await update.message.reply_text(
                format_expense_confirmation(
                    session["merchant"], session["amount"], session["currency"],
                    session.get("category", ""), session.get("card", ""),
                    session.get("sgd_amount"), receipt_saved=bool(session.get("receipt_link")),
                    last4=session.get("last4"), high_amount=session.get("high_amount", False),
                    use_manual_rate=session.get("use_manual_rate", False),
                    missing_fields=session.get("missing_fields", [])
                )
            )
        else:
            del state.receipt_confirm_sessions[user_id]
            await update.message.reply_text("Skipped duplicate.")
        return True
    if lower in ["yes", "y", "yep", "yeah", "yup"]:
        await _finalise_receipt_confirm(user_id, session, update)
        return True
    card_match, _ = fuzzy_match_card(text.strip())
    if card_match and len(text.strip().split()) == 1:
        session["card"] = card_match
        await _finalise_receipt_confirm(user_id, session, update)
        return True
    edit_field_map = {
        "edit name": "merchant", "edit merchant": "merchant",
        "edit amount": "amount", "edit price": "amount",
        "edit category": "category", "edit cat": "category",
        "edit card": "card", "edit payment": "card",
    }
    if lower in edit_field_map:
        field = edit_field_map[lower]
        session["_awaiting_edit"] = field
        state.receipt_confirm_sessions[user_id] = session
        field_label = {"merchant": "merchant name", "amount": "amount", "category": "category", "card": "card"}[field]
        await update.message.reply_text(f"Enter the new {field_label}:")
        return True
    if session.get("_awaiting_edit"):
        field = session.pop("_awaiting_edit")
        state.receipt_confirm_sessions[user_id] = session
        synthetic = f"{field} {text.strip()}"
        edits = parse_multi_field_edit(synthetic)
        if edits:
            for f, value in edits.items():
                if f == "merchant":
                    session["merchant"] = value
                elif f == "amount":
                    try:
                        session["amount"] = float(re.sub(r"[^\d.]", "", value))
                        if session["currency"] == "SGD":
                            session["sgd_amount"] = session["amount"]
                    except ValueError:
                        pass
                elif f == "category":
                    matched_cat, _ = fuzzy_match_category(value)
                    if matched_cat:
                        session["category"] = matched_cat
                        session["card"] = get_card_default_for_category(matched_cat)
                elif f == "card":
                    matched_card, _ = fuzzy_match_card(value)
                    if matched_card:
                        session["card"] = matched_card
        state.receipt_confirm_sessions[user_id] = session
        await update.message.reply_text(
            format_expense_confirmation(
                session["merchant"], session["amount"], session["currency"],
                session.get("category", ""), session.get("card", ""),
                session.get("sgd_amount"), receipt_saved=bool(session.get("receipt_link")),
                last4=session.get("last4"), high_amount=session.get("high_amount", False),
                use_manual_rate=session.get("use_manual_rate", False),
                missing_fields=session.get("missing_fields", [])
            )
        )
        return True
    edits = parse_multi_field_edit(text)
    if edits:
        merchant_val = edits.get("merchant", "")
        field_keys = {"merchant", "amount", "currency", "category", "card", "price", "total", "cost", "shop", "store", "place", "payment", "pay", "cat"}
        if merchant_val:
            words = merchant_val.lower().split()
            collision = [w for w in words if w in field_keys]
            if collision and '"' not in text:
                lines = ["Just to confirm:"]
                for f, v in edits.items():
                    lines.append(f"{f.title()}: {v}")
                for f in ["amount", "category", "card"]:
                    if f not in edits:
                        val = session.get(f, "")
                        if val:
                            lines.append(f"{f.title()}: {val}")
                lines.append("\nyes / no / edit")
                session["_pending_edits"] = edits
                state.receipt_confirm_sessions[user_id] = session
                await update.message.reply_text("\n".join(lines))
                return True
        for field, value in edits.items():
            if field == "merchant":
                session["merchant"] = value
                known_cat, known_card, canonical = get_merchant_memory(value)
                if canonical:
                    session["merchant"] = canonical
                if known_cat and not edits.get("category"):
                    session["category"] = known_cat
                if session.get("category") and not edits.get("card"):
                    session["card"] = get_card_default_for_category(session["category"])
            elif field == "amount":
                try:
                    session["amount"] = float(re.sub(r"[^\d.]", "", value))
                    if session["currency"] == "SGD":
                        session["sgd_amount"] = session["amount"]
                except ValueError:
                    pass
            elif field == "currency":
                session["currency"] = value.upper()
            elif field == "category":
                matched_cat, _ = fuzzy_match_category(value)
                if matched_cat:
                    session["category"] = matched_cat
                    if not edits.get("card"):
                        session["card"] = get_card_default_for_category(matched_cat)
            elif field == "card":
                matched_card, _ = fuzzy_match_card(value)
                if matched_card:
                    session["card"] = matched_card
        session["missing_fields"] = [f for f in session.get("missing_fields", []) if not session.get(f)]
        session["high_amount"] = session.get("sgd_amount", 0) > SGD_HIGH_AMOUNT_THRESHOLD
        await _finalise_receipt_confirm(user_id, session, update)
        return True
    if session.get("_pending_edits"):
        if lower in ["yes", "y"]:
            edits = session.pop("_pending_edits")
            for field, value in edits.items():
                session[field] = value
            await _finalise_receipt_confirm(user_id, session, update)
        elif lower in ["no", "n"]:
            session.pop("_pending_edits", None)
            state.receipt_confirm_sessions[user_id] = session
            await update.message.reply_text(
                'Re-enter with quotes for merchant names containing field keywords.\n'
                'e.g. merchant "Gift Card Store" category Shopping'
            )
        return True
    await update.message.reply_text("Didn't catch that — reply yes to log, skip to cancel, or edit fields (e.g. category FnB card Citi)")
    return True

async def _finalise_receipt_confirm(user_id, session, update):
    merchant = session["merchant"]
    amount = session["amount"]
    currency = session["currency"]
    category = session.get("category", "")
    card = session.get("card", "Citi")
    sgd_amount = session.get("sgd_amount", amount)
    receipt_link = session.get("receipt_link", "")
    last4 = session.get("last4")
    is_new_merchant = session.get("is_new_merchant", False)
    success = log_expense(merchant, amount, currency, sgd_amount, category, card, receipt_link=receipt_link)
    if not success:
        await update.message.reply_text("⚠️ Failed to save expense to sheet — please try again or log manually.")
        return
    del state.receipt_confirm_sessions[user_id]
    state.session_timestamps.pop(user_id, None)
    if is_new_merchant and category and card:
        if not save_merchant_memory(merchant, category, card):
            print(f"Warning: merchant memory not saved for '{merchant}' — will re-ask next time")
    reply = format_expense_logged(merchant, amount, currency, category, card, sgd_amount, last4)
    try:
        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(reply)

async def handle_expense_session(user_id, text, update):
    session = state.expense_sessions.get(user_id)
    if not session:
        return
    from sessions import touch_session
    touch_session(user_id)
    step = session.get("step")
    if step == "fx_rate":
        currency = session.get("currency", "")
        parsed_rate, display = parse_manual_fx_input(text, currency)
        if parsed_rate is None:
            await update.message.reply_text(
                f"Couldn't read that rate — try one of these formats:\n"
                f"• `3.11` (1 {currency} = 3.11 SGD)\n"
                f"• `1 SGD to 3.11 {currency}`\n"
                f"• `1 {currency} = 3.11 SGD`"
            )
            return
        num, sgd_per_unit = parsed_rate
        save_manual_fx_rate(currency, num, sgd_per_unit)
        rate = await asyncio.to_thread(get_fx_rate, currency)
        session["sgd_amount"] = round(session["amount"] * rate, 2)
        session["use_manual_rate"] = True
        del state.expense_sessions[user_id]
        state.receipt_confirm_sessions[user_id] = {**session, "step": "receipt_confirm",
                                              "missing_fields": [], "is_new_merchant": True}
        touch_session(user_id)
        await update.message.reply_text(
            f"Got it — using {display} for today.\n\n" +
            format_expense_confirmation(
                session["merchant"], session["amount"], currency,
                session.get("category", ""), session.get("card", ""),
                session["sgd_amount"], use_manual_rate=True, missing_fields=[]
            )
        )
        return
    if step == "duplicate_confirm":
        lower = text.strip().lower()
        if lower in ["yes", "y", "yep", "yeah", "yup"]:
            state.receipt_confirm_sessions[user_id] = {**session, "step": "receipt_confirm"}
            touch_session(user_id)
            confirmation = format_expense_confirmation(
                session["merchant"], session["amount"], session["currency"],
                session.get("category", ""), session.get("card", ""),
                session.get("sgd_amount"), missing_fields=[]
            )
            await update.message.reply_text(confirmation)
        else:
            del state.expense_sessions[user_id]
            await update.message.reply_text("Skipped.")
        return

# ── Expense queries ────────────────────────────────────────────────────────────

def delete_last_expense():
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return "No expenses to delete."
        last_row = len(all_values)
        last = all_values[last_row - 1]
        sheet.delete_rows(last_row)
        return f"Deleted last expense: {last[1]} ${last[2]}"
    except Exception as e:
        return f"❌ Error deleting expense: {str(e)}"

def get_recent_expenses(n=5):
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return []
        headers = all_values[0]
        rows = all_values[1:]
        recent = rows[-(n):]
        result = []
        for i, row in enumerate(recent):
            sheet_row = len(all_values) - len(recent) + i + 1
            d = {headers[j]: row[j] if j < len(row) else "" for j in range(len(headers))}
            result.append((sheet_row, d))
        return list(reversed(result))
    except Exception as e:
        print(f"get_recent_expenses error: {e}")
        return []

def format_delete_list(expenses):
    lines = ["Which expense to delete?\n"]
    for i, (_, r) in enumerate(expenses, 1):
        merchant = r.get("Merchant", "?")
        amount = r.get("Amount", "")
        currency = r.get("Currency", "SGD")
        sgd = r.get("SGD Amount", "")
        date_str = r.get("Date", "")
        category = r.get("Category", "")
        if currency != "SGD" and sgd:
            amt_str = f"${float(sgd):.2f} ({currency} {float(amount):,.0f})"
        else:
            amt_str = f"${float(amount):.2f}" if amount else "$?"
        lines.append(f"{i}. {merchant} — {amt_str} | {category} | {date_str}")
    lines.append("\nReply with a number, or 'search [merchant]' to find another.")
    return "\n".join(lines)

def search_expenses_by_merchant(query):
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return []
        headers = all_values[0]
        rows = all_values[1:]
        q = query.lower().strip()
        matches = []
        for i, row in enumerate(rows):
            d = {headers[j]: row[j] if j < len(row) else "" for j in range(len(headers))}
            if q in d.get("Merchant", "").lower():
                matches.append((i + 2, d))
        return list(reversed(matches))[:10]
    except Exception as e:
        print(f"search_expenses_by_merchant error: {e}")
        return []

def delete_expense_by_row(sheet_row):
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if sheet_row < 2 or sheet_row > len(all_values):
            return "Couldn't find that expense."
        row = all_values[sheet_row - 1]
        merchant = row[1] if len(row) > 1 else "?"
        amount = row[2] if len(row) > 2 else "?"
        sheet.delete_rows(sheet_row)
        return f"Deleted: {merchant} ${amount}"
    except Exception as e:
        return f"Error deleting expense: {str(e)}"

def get_last_expense():
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        if not records:
            return None
        return records[-1]
    except Exception as e:
        print(f"get_last_expense error: {e}")
        return None

def edit_last_expense(edit_text):
    try:
        sheet = expenses_sheet()
        all_values = sheet.get_all_values()
        if len(all_values) <= 1:
            return "No expenses to edit."
        headers = all_values[0]
        last_row_idx = len(all_values)
        edits = parse_multi_field_edit(edit_text, field_keywords=EDIT_FIELD_SYNONYMS)
        if not edits:
            return "Couldn't parse that edit. Try: 'edit merchant Starbucks category FnB'"
        applied = []
        merchant_changed = False
        for field_key, new_value in edits.items():
            col_name = EDIT_FIELD_SYNONYMS.get(field_key.lower())
            if not col_name or col_name not in headers:
                continue
            col_idx = headers.index(col_name) + 1
            if col_name == "Amount":
                try:
                    new_value = str(float(re.sub(r"[^\d.]", "", new_value)))
                except ValueError:
                    continue
            elif col_name == "Category":
                matched_cat, _ = fuzzy_match_category(new_value)
                if not matched_cat:
                    continue
                new_value = matched_cat
            elif col_name == "Card":
                matched_card, _ = fuzzy_match_card(new_value)
                if not matched_card:
                    continue
                new_value = matched_card
            elif col_name == "Merchant":
                merchant_changed = True
            sheet.update_cell(last_row_idx, col_idx, new_value)
            applied.append((col_name, new_value))
        if not applied:
            return "Couldn't match any valid fields to edit."
        if merchant_changed:
            new_merchant = next((v for k, v in edits.items() if EDIT_FIELD_SYNONYMS.get(k) == "Merchant"), "")
            if new_merchant:
                known_cat, known_card, canonical = get_merchant_memory(new_merchant)
                if canonical:
                    m_col = headers.index("Merchant") + 1
                    sheet.update_cell(last_row_idx, m_col, canonical)
                if known_cat and "Category" not in [c for c, _ in applied]:
                    c_col = headers.index("Category") + 1
                    sheet.update_cell(last_row_idx, c_col, known_cat)
                    applied.append(("Category", known_cat))
                if known_card and "Card" not in [c for c, _ in applied]:
                    cd_col = headers.index("Card") + 1
                    sheet.update_cell(last_row_idx, cd_col, known_card)
                    applied.append(("Card", known_card))
        updated_values = sheet.get_all_values()
        updated_row_data = updated_values[last_row_idx - 1] if len(updated_values) >= last_row_idx else all_values[-1]
        updated = {headers[i]: updated_row_data[i] if i < len(updated_row_data) else "" for i in range(len(headers))}
        merchant = updated.get("Merchant", "")
        amount = updated.get("Amount", "")
        currency = updated.get("Currency", "SGD")
        sgd_amount = updated.get("SGD Amount", amount)
        category = updated.get("Category", "")
        card = updated.get("Card", "")
        return format_expense_logged(merchant, float(amount) if amount else 0,
                                     currency, category, card,
                                     float(sgd_amount) if sgd_amount else None)
    except Exception as e:
        return f"❌ Error editing expense: {str(e)}"

def show_last_expense():
    r = get_last_expense()
    if not r:
        return "No expenses logged yet."
    currency = r.get("Currency", "SGD")
    amount = r.get("Amount", "")
    sgd = r.get("SGD Amount", "")
    _merchant = r.get('Merchant', '')
    _cat = r.get('Category', '')
    _emoji = get_merchant_emoji(_cat, _merchant)
    lines = ["Last expense:", f"{_emoji} {_merchant}"]
    if currency != "SGD" and sgd:
        lines.append(f"${float(sgd):.2f} ({currency} {float(amount):,.0f})")
    else:
        lines.append(f"${float(amount):.2f}" if amount else "")
    lines.append(f"🗂 {r.get('Category', '')} | 💳 {r.get('Card', '')}")
    lines.append(f"📅 {r.get('Date', '')}")
    lines.append("\nTo edit: 'edit [field] to [value]' e.g. 'edit category to Transport'")
    return "\n".join(l for l in lines if l)

def get_trip_summary():
    trip_start = state.overseas_state.get("trip_start")
    destinations = state.overseas_state.get("trip_destinations", [])
    if not trip_start:
        return "No trip data — overseas mode hasn't been activated this session."
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        start_dt = datetime.strptime(trip_start, "%d/%m/%Y").date()
        trip_records = []
        for r in records:
            d = r.get("Date", "")
            if not d:
                continue
            try:
                dt = datetime.strptime(d, "%d/%m/%Y").date()
                if dt >= start_dt:
                    trip_records.append(r)
            except Exception:
                continue
        if not trip_records:
            return "No expenses logged for this trip yet."
        total_sgd = sum(float(r.get("SGD Amount") or r.get("Amount") or 0) for r in trip_records)
        cat_totals = {}
        currency_totals = {}
        for r in trip_records:
            cat = r.get("Category", "Other")
            amt = float(r.get("SGD Amount") or r.get("Amount") or 0)
            cat_totals[cat] = cat_totals.get(cat, 0) + amt
            curr = r.get("Currency", "SGD")
            orig = float(r.get("Amount") or 0)
            if curr != "SGD":
                currency_totals[curr] = currency_totals.get(curr, 0) + orig
        dest_str = " → ".join(destinations) if destinations else "trip"
        lines = [f"Trip summary — {dest_str}", f"From {trip_start}\n"]
        for cat, amt in sorted(cat_totals.items(), key=lambda x: -x[1]):
            lines.append(f"{cat}: SGD ${amt:.2f}")
        lines.append(f"\nTotal: SGD ${total_sgd:.2f}")
        if currency_totals:
            lines.append("\nSpend by currency:")
            for curr, amt in currency_totals.items():
                lines.append(f"{curr}: {amt:,.0f}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error generating trip summary: {str(e)}"

def get_monthly_summary(month=None, year=None):
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No expenses logged yet."
        today = date.today()
        target_month = month or today.month
        target_year = year or today.year
        month_records = []
        for r in records:
            d = r.get("Date", "")
            if not d:
                continue
            try:
                dt = datetime.strptime(d, "%d/%m/%Y")
                if dt.month == target_month and dt.year == target_year:
                    month_records.append(r)
            except Exception:
                pass
        if not month_records:
            return f"No expenses for {datetime(target_year, target_month, 1).strftime('%B %Y')}."
        total = sum(float(r.get("SGD Amount", 0) or r.get("Amount", 0)) for r in month_records)
        cat_totals = {c: 0 for c in EXPENSE_CATEGORIES}
        for r in month_records:
            cat = r.get("Category", "")
            if cat in cat_totals:
                amt = float(r.get("SGD Amount", 0) or r.get("Amount", 0))
                cat_totals[cat] += amt
        card_totals = {c: 0 for c in EXPENSE_CARDS}
        for r in month_records:
            card = r.get("Card", "")
            if card in card_totals:
                amt = float(r.get("SGD Amount", 0) or r.get("Amount", 0))
                card_totals[card] += amt
        month_label = datetime(target_year, target_month, 1).strftime("%B %Y")
        lines = [f"Monthly Summary — {month_label}\n"]
        for cat, amt in cat_totals.items():
            if amt > 0:
                lines.append(f"{cat}: ${amt:.2f}")
        lines.append(f"\nTotal: ${total:.2f}")
        lines.append("\nBy card:")
        for card, amt in card_totals.items():
            if amt > 0:
                lines.append(f"{card}: ${amt:.2f}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error generating summary: {str(e)}"

def get_expense_categories():
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        cats = sorted(set(r.get("Category", "").strip() for r in records if r.get("Category", "").strip()))
        if not cats:
            cats = EXPENSE_CATEGORIES
    except Exception:
        cats = EXPENSE_CATEGORIES
    numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(cats))
    return f"Your expense categories:\n\n{numbered}"

def get_expense_report(report_type="monthly"):
    return get_monthly_summary()

# ── Detectors ──────────────────────────────────────────────────────────────────

def is_log_prefix_input(text):
    lower = text.lower()
    if not lower.startswith("log ") or len(text) <= 4:
        return False
    # Exclude non-expense log intents
    exclusions = ["log into crm", "log that", "log it", "log the message",
                  "log bills", "log bill", "log this"]
    if any(lower.startswith(e) for e in exclusions):
        return False
    return True

def is_bare_merchant_input(text):
    if not re.search(r"\$[\d,.]+", text):
        return False
    first_word = text.strip().split()[0]
    known_cat, _, _ = get_merchant_memory(first_word)
    return bool(known_cat)

def is_expense_input(text):
    from trips import extract_flight_number
    if extract_flight_number(text):
        return False
    lower = text.lower()
    if re.search(r"\d+ shares|shares of |shares @|shares at \$", lower):
        return False
    exclusions = [
        "delete expense", "remove expense", "undo expense",
        "edit expense", "edit last expense",
        "rename category", "expense report", "monthly report",
        "trip summary", "last expense", "show last expense",
        "what expense categories", "list my categories", "what categories",
        "show categories", "what are my expense", "list categories",
        "expense categories", "my categories",
        "what merchants", "my merchants", "merchant map", "list merchants",
        "show merchants", "known merchants"
    ]
    if any(lower.startswith(e) or lower == e for e in exclusions):
        return False
    triggers = ["spent", "paid", "$", "sgd", "charged", "bought", "grabbed",
                "receipt", "cost me", "picked up",
                "recorded in", "will it be", "logged in", "how much have i"]
    return any(t in lower for t in triggers)
