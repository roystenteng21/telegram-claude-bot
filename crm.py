import re
import json
from datetime import date, datetime
import state
from config import DATE_FORMATS
from clients import client, spreadsheet
from sheets import crm_sheet, get_sheet, log_error_to_em_log
from helpers import format_date, calculate_age, format_contact, parse_date_flexible

# ── CRM record cache ───────────────────────────────────────────────────────────

def _invalidate_crm_cache():
    state._crm_cache = None
    state._crm_cache_ts = None

def _get_crm_records():
    """Return CRM records from cache, fetching from sheet if stale or TTL expired."""
    import time as _time
    now = _time.monotonic()
    if state._crm_cache is None or state._crm_cache_ts is None or (now - state._crm_cache_ts) > state._CRM_CACHE_TTL:
        sheet = crm_sheet()
        state._crm_cache = sheet.get_all_records() if sheet else []
        state._crm_cache_ts = now
    return state._crm_cache

# ── Core lookup ────────────────────────────────────────────────────────────────

def find_row(name):
    """Single-pass CRM lookup across Name, Alias, and first name."""
    records = _get_crm_records()
    name_lower = name.strip().lower()
    exact, alias_exact, sub_name, sub_alias, first = [], [], [], [], []
    for i, r in enumerate(records):
        full = r.get("Name", "")
        alias = r.get("Alias", "")
        full_l = full.lower()
        alias_l = alias.lower()
        first_name = full_l.split()[0] if full_l else ""
        alias_first = alias_l.split()[0] if alias_l else ""
        if full_l == name_lower:
            return i + 2, r
        if alias_l == name_lower:
            alias_exact.append((i + 2, r))
        elif name_lower in full_l:
            sub_name.append((i + 2, r))
        elif name_lower in alias_l:
            sub_alias.append((i + 2, r))
        elif name_lower in (first_name, alias_first):
            first.append((i + 2, r))
    for tier in (alias_exact, sub_name, sub_alias, first):
        if len(tier) == 1:
            return tier[0]
        if len(tier) > 1:
            return "disambig", disambiguate_contacts(tier)
    return None, None

def find_all_rows(name):
    """Like find_row but returns all matches for disambiguation."""
    records = _get_crm_records()
    name_lower = name.strip().lower()
    results = []
    for i, r in enumerate(records):
        full_name = r.get("Name", "").lower()
        alias = r.get("Alias", "").lower()
        first_name = full_name.split()[0] if full_name else ""
        alias_first = alias.split()[0] if alias else ""
        if (name_lower in full_name or name_lower in alias or
                name_lower == first_name or name_lower == alias_first):
            results.append((i + 2, r))
    return results

def disambiguate_contacts(matches):
    """Return a disambiguation prompt if multiple contacts match."""
    names = [r.get("Name", "?") for _, r in matches]
    options = " or ".join(f"*{n}*" for n in names)
    return f"Did you mean {options}?"

# ── CRUD ───────────────────────────────────────────────────────────────────────

def save_contact(data, force_new=False):
    try:
        sheet = crm_sheet()
        parts = [p.strip() for p in data.split(",")]
        while len(parts) < 13:
            parts.append("")
        name = parts[0]
        alias = parts[1] if len(parts) > 1 else ""
        birthday = parts[2] if len(parts) > 2 else ""
        relationship = parts[3] if len(parts) > 3 else ""
        context = parts[4] if len(parts) > 4 else ""
        notes = parts[5] if len(parts) > 5 else ""
        followup_date = parts[6] if len(parts) > 6 else ""
        followup_notes = parts[7] if len(parts) > 7 else ""
        last_updated = date.today().strftime("%d/%m/%Y")
        if not name:
            return "❌ Name is required"
        if not force_new:
            existing_row, existing_record = find_row(name)
            if existing_record and existing_row != "disambig":
                return f"_DUPLICATE_:{name}"
        sheet.append_row([
            name, alias, birthday, relationship, context, notes,
            followup_date, followup_notes, last_updated, "", "", "", "", ""
        ])
        _invalidate_crm_cache()
        return f"✅ Contact saved!\n\n" + format_contact({
            "Name": name, "Alias": alias, "Birthday": birthday,
            "Relationship": relationship, "Context": context,
            "Notes": notes, "Follow Up Date": followup_date,
            "Follow Up Notes": followup_notes, "Last Updated": last_updated
        })
    except Exception as e:
        return f"❌ Error saving contact: {str(e)}"

def find_contact(name, show_private=False):
    try:
        results = find_all_rows(name)
        if not results:
            return f"❌ No contact found for '{name}'"
        if len(results) > 1:
            return disambiguate_contacts(results)
        return format_contact(results[0][1], show_private=show_private)
    except Exception as e:
        return f"❌ Error finding contact: {str(e)}"

def add_note(data):
    try:
        sheet = crm_sheet()
        # Split on first " - " (space-hyphen-space) to avoid splitting hyphenated names
        if " - " in data:
            parts = data.split(" - ", 1)
        else:
            return "❌ Format: note Name - your note here"
        name = parts[0].strip()
        note = parts[1].strip()
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        existing = record.get("Notes", "")
        new_note = f"{existing}; {note}" if existing else note
        sheet.update_cell(row_num, 6, new_note)
        sheet.update_cell(row_num, 9, date.today().strftime("%d/%m/%Y"))
        _invalidate_crm_cache()
        return f"✅ Note added to *{record.get('Name')}*"
    except Exception as e:
        return f"❌ Error adding note: {str(e)}"

def set_followup(data):
    try:
        sheet = crm_sheet()
        parts = [p.strip() for p in data.split(",", 2)]
        if len(parts) < 2:
            return "❌ Format: followup Name, DD/MM/YYYY, notes"
        name = parts[0]
        followup_date = parts[1]
        followup_notes = parts[2] if len(parts) > 2 else ""
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        sheet.update_cell(row_num, 7, followup_date)
        sheet.update_cell(row_num, 8, followup_notes)
        sheet.update_cell(row_num, 9, date.today().strftime("%d/%m/%Y"))
        return f"✅ Follow up set for *{record.get('Name')}* on {format_date(followup_date)}"
    except Exception as e:
        return f"❌ Error setting follow up: {str(e)}"

def update_field(data):
    try:
        sheet = crm_sheet()
        parts = [p.strip() for p in data.split(",", 2)]
        if len(parts) < 3:
            return "❌ Format: update Name, field, new value"
        name, field, value = parts
        field_map = {
            "alias": 2, "birthday": 3, "relationship": 4, "context": 5,
            "notes": 6, "follow up date": 7, "follow up notes": 8,
            "referred by": 11, "referral date": 12, "email": 13, "address": 14
        }
        col = field_map.get(field.lower())
        if not col:
            valid = ", ".join(field_map.keys())
            return f"❌ Unknown field '{field}'. Options: {valid}"
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        sheet.update_cell(row_num, col, value)
        sheet.update_cell(row_num, 9, date.today().strftime("%d/%m/%Y"))
        _invalidate_crm_cache()
        return f"✅ {field.title()} updated for *{record.get('Name')}*"
    except Exception as e:
        return f"❌ Error updating contact: {str(e)}"

def update_contact_field_natural(name, field, value):
    return update_field(f"{name}, {field}, {value}")

def delete_contact(name):
    try:
        sheet = crm_sheet()
        row_num, record = find_row(name)
        if not record:
            all_values = sheet.get_all_values()
            name_lower = name.strip().lower()
            for i, row in enumerate(all_values[1:], start=2):
                if any(name_lower == str(cell).strip().lower() for cell in row if cell):
                    display = next((str(c).strip() for c in row if c), name)
                    sheet.delete_rows(i)
                    _invalidate_crm_cache()
                    return f"✅ Entry *{display}* deleted"
            return f"❌ No contact found for '{name}'"
        sheet.delete_rows(row_num)
        _invalidate_crm_cache()
        return f"✅ Contact *{record.get('Name')}* deleted"
    except Exception as e:
        return f"❌ Error deleting contact: {str(e)}"

def search_contacts(keyword):
    try:
        records = _get_crm_records()
        results = [r for r in records if any(keyword.lower() in str(v).lower() for v in r.values())]
        if not results:
            return f"No contacts found matching '{keyword}' in the CRM."
        return f"🔍 *{len(results)} result(s) for '{keyword}':*\n\n" + "\n\n".join(format_contact(r) for r in results)
    except Exception as e:
        return f"❌ Error searching: {str(e)}"

def list_contacts():
    try:
        records = _get_crm_records()
        if not records:
            return "❌ No contacts found"
        response = f"📋 *{len(records)} contact(s):*\n\n"
        for r in records:
            rel = r.get("Relationship", "") or r.get("Context", "") or "Unknown"
            response += f"👤 {r.get('Name', '')} — {rel}\n"
        return response
    except Exception as e:
        return f"❌ Error listing contacts: {str(e)}"

def get_stats():
    try:
        records = _get_crm_records()
        today = date.today()
        total = len(records)
        followups_due = 0
        birthdays_month = 0
        for r in records:
            fu = r.get("Follow Up Date", "")
            if fu:
                try:
                    fu_date = datetime.strptime(fu, "%d/%m/%Y").date()
                    if fu_date >= today:
                        followups_due += 1
                except Exception:
                    pass
            bday = r.get("Birthday", "")
            if bday:
                try:
                    b = datetime.strptime(bday, "%d/%m/%Y").date()
                    this_year = b.replace(year=today.year)
                    if this_year < today:
                        this_year = b.replace(year=today.year + 1)
                    if (this_year - today).days <= 30:
                        birthdays_month += 1
                except Exception:
                    pass
        return (f"📊 *CRM Stats*\n\n👥 Total contacts: {total}\n"
                f"📅 Upcoming follow ups: {followups_due}\n"
                f"🎂 Birthdays in next 30 days: {birthdays_month}")
    except Exception as e:
        return f"❌ Error getting stats: {str(e)}"

def upcoming_followups():
    try:
        records = _get_crm_records()
        today = date.today()
        upcoming = []
        for r in records:
            fu_date = r.get("Follow Up Date", "")
            if fu_date:
                try:
                    fu = datetime.strptime(fu_date, "%d/%m/%Y").date()
                    if fu >= today:
                        upcoming.append((fu, r))
                except Exception:
                    pass
        if not upcoming:
            return "✅ No upcoming follow ups!"
        upcoming.sort(key=lambda x: x[0])
        response = "📅 *Upcoming Follow Ups:*\n\n"
        for fu, r in upcoming:
            response += f"👤 *{r.get('Name')}* — {format_date(r.get('Follow Up Date'))}\n"
            if r.get('Follow Up Notes'):
                response += f"  - {r.get('Follow Up Notes')}\n"
            response += "\n"
        return response
    except Exception as e:
        return f"❌ Error fetching follow ups: {str(e)}"

def overdue_followups():
    try:
        records = _get_crm_records()
        today = date.today()
        overdue = []
        for r in records:
            fu_date = r.get("Follow Up Date", "")
            if fu_date:
                try:
                    fu = datetime.strptime(fu_date, "%d/%m/%Y").date()
                    if fu < today:
                        overdue.append((fu, r))
                except Exception:
                    pass
        if not overdue:
            return "✅ No overdue follow ups!"
        overdue.sort(key=lambda x: x[0])
        response = "⚠️ *Overdue Follow Ups:*\n\n"
        for fu, r in overdue:
            days_ago = (today - fu).days
            response += f"👤 *{r.get('Name')}* — {format_date(r.get('Follow Up Date'))} ({days_ago} days ago)\n"
            if r.get('Follow Up Notes'):
                response += f"  - {r.get('Follow Up Notes')}\n"
            response += "\n"
        return response
    except Exception as e:
        return f"❌ Error fetching overdue: {str(e)}"

def upcoming_birthdays(days=30):
    try:
        records = _get_crm_records()
        today = date.today()
        upcoming = []
        for r in records:
            bday_str = r.get("Birthday", "")
            if bday_str:
                try:
                    bday = datetime.strptime(bday_str, "%d/%m/%Y").date()
                    this_year = bday.replace(year=today.year)
                    if this_year < today:
                        this_year = bday.replace(year=today.year + 1)
                    days_away = (this_year - today).days
                    if days_away <= days:
                        upcoming.append((days_away, r))
                except Exception:
                    pass
        if not upcoming:
            return f"🎂 No birthdays in the next {days} days!"
        upcoming.sort(key=lambda x: x[0])
        label = "next 7 days" if days == 7 else "next 30 days"
        response = f"🎂 *Birthdays in the {label}:*\n\n"
        for days_away, r in upcoming:
            response += f"👤 *{r.get('Name')}* — {format_date(r.get('Birthday'))} ({'today! 🎉' if days_away == 0 else f'in {days_away} days'})\n"
        return response
    except Exception as e:
        return f"❌ Error fetching birthdays: {str(e)}"

def last_contact(name):
    try:
        row_num, record = find_row(name)
        if not record:
            return f"❌ No contact found for '{name}'"
        last = record.get("Last Updated", "")
        if last:
            return f"👤 *{record.get('Name')}*\n📅 Last updated: {format_date(last)}"
        return f"👤 *{record.get('Name')}*\n📅 No updates recorded yet"
    except Exception as e:
        return f"❌ Error: {str(e)}"

# ── Referral tracking ──────────────────────────────────────────────────────────

def set_referral(referrer_name, referred_name):
    try:
        sheet = crm_sheet()
        row_num, record = find_row(referred_name)
        if not record:
            return f"❌ No contact found for '{referred_name}'. Add them first."
        today = date.today().strftime("%d/%m/%Y")
        sheet.update_cell(row_num, 11, referrer_name)
        sheet.update_cell(row_num, 12, today)
        sheet.update_cell(row_num, 9, today)
        if not record.get("Relationship", ""):
            sheet.update_cell(row_num, 4, "Prospect")
        return f"✅ Got it — {referred_name} was referred by {referrer_name} (Referral Date: {format_date(today)})"
    except Exception as e:
        return f"❌ Error recording referral: {str(e)}"

def get_referrals_by(referrer_name):
    try:
        records = _get_crm_records()
        results = [r for r in records if r.get("Referred By", "").lower() == referrer_name.lower()]
        if not results:
            return f"No referrals found from {referrer_name}."
        lines = [f"*Referrals from {referrer_name}:*\n"]
        for r in results:
            ref_date = format_date(r.get("Referral Date", "")) if r.get("Referral Date") else "unknown date"
            lines.append(f"👤 {r.get('Name')} — {r.get('Relationship', 'Prospect')} (referred {ref_date})")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {str(e)}"

def get_all_referrals():
    try:
        records = _get_crm_records()
        referrals = [r for r in records if r.get("Referred By", "")]
        if not referrals:
            return "No referrals recorded yet."
        grouped = {}
        for r in referrals:
            ref_by = r.get("Referred By", "Unknown")
            grouped.setdefault(ref_by, []).append(r)
        lines = ["*All Referrals:*\n"]
        for referrer, contacts in sorted(grouped.items(), key=lambda x: -len(x[1])):
            lines.append(f"*{referrer}* ({len(contacts)} referral{'s' if len(contacts) != 1 else ''})")
            for c in contacts:
                lines.append(f"  → {c.get('Name')} ({c.get('Relationship', 'Prospect')})")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {str(e)}"

def get_top_referrers():
    try:
        records = _get_crm_records()
        counts = {}
        for r in records:
            ref_by = r.get("Referred By", "")
            if ref_by:
                counts[ref_by] = counts.get(ref_by, 0) + 1
        if not counts:
            return "No referrals recorded yet."
        ranked = sorted(counts.items(), key=lambda x: -x[1])
        lines = ["*Top Referrers:*\n"]
        for i, (name, count) in enumerate(ranked, 1):
            lines.append(f"{i}. {name} — {count} referral{'s' if count != 1 else ''}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {str(e)}"

# ── Excel import ───────────────────────────────────────────────────────────────

def parse_excel_column_order(text):
    text = re.sub(r'\d+[\.\)]\s*', '', text)
    parts = [p.strip() for p in re.split(r'[,\n]', text) if p.strip()]
    return parts

async def handle_excel_import(file_bytes, column_order, update):
    try:
        import io
        import openpyxl
        from sheets import crm_sheet as _crm_sheet
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
        ws_xl = wb.active
        rows = list(ws_xl.iter_rows(values_only=True))
        data_rows = rows
        if rows:
            first = [str(c).strip() if c else "" for c in rows[0]]
            if any(h.lower() in ["name", "email", "alias", "date of birth", "dob", "birthday",
                                  "relationship", "address", "context", "referred by"] for h in first):
                data_rows = rows[1:]
        sheet = _crm_sheet()
        existing_records = sheet.get_all_records()
        existing_names_set = set()
        for r in existing_records:
            n = r.get("Name", "").strip().lower()
            if n:
                existing_names_set.add(n)
        col_map = {}
        for i, col_name in enumerate(column_order):
            col_map[col_name.strip().lower()] = i

        def get_col(row, *keys):
            for k in keys:
                idx = col_map.get(k.lower())
                if idx is not None and idx < len(row):
                    val = row[idx]
                    if val is not None:
                        return str(val).strip().replace('\xa0', '').strip()
            return ""

        def normalise_birthday(val):
            if val is None:
                return ""
            if isinstance(val, datetime):
                return val.strftime("%d/%m/%Y")
            s = str(val).strip()
            if not s or s.lower() == "none":
                return ""
            for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y",
                        "%d %b %Y", "%d %B %Y", "%Y-%m-%d %H:%M:%S"]:
                try:
                    return datetime.strptime(s, fmt).strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
            try:
                from datetime import timedelta
                serial = int(float(s))
                epoch = datetime(1899, 12, 30)
                return (epoch + timedelta(days=serial)).strftime("%d/%m/%Y")
            except (ValueError, TypeError):
                pass
            return s

        def normalise_name(raw):
            if not raw:
                return ""
            cleaned = str(raw).replace('\xa0', '').strip()
            if cleaned.isupper():
                cleaned = cleaned.title()
            return cleaned

        imported = 0
        skipped = 0
        today = date.today().strftime("%d/%m/%Y")
        rows_to_append = []
        for row in data_rows:
            if not any(c for c in row if c is not None):
                continue
            bday_idx = col_map.get("birthday")
            bday_raw = row[bday_idx] if bday_idx is not None and bday_idx < len(row) else None
            birthday = normalise_birthday(bday_raw)
            name = normalise_name(get_col(row, "Name", "name"))
            if not name:
                continue
            if name.lower() in existing_names_set:
                skipped += 1
                continue
            alias = get_col(row, "Alias", "alias")
            relationship = get_col(row, "Relationship", "relationship")
            context = get_col(row, "Context", "context")
            notes = get_col(row, "Notes", "notes")
            followup_date = get_col(row, "Follow Up Date", "follow up date")
            followup_notes = get_col(row, "Follow Up Notes", "follow up notes")
            referred_by = get_col(row, "Referred By", "referred by")
            referral_date = get_col(row, "Referral Date", "referral date")
            email_raw = get_col(row, "Email", "email")
            email = str(email_raw).strip().lower() if email_raw else ""
            address = get_col(row, "Address", "address")
            rows_to_append.append([
                name, alias, birthday, relationship, context, notes,
                followup_date, followup_notes, today, "", referred_by, referral_date, email, address
            ])
            existing_names_set.add(name.lower())
            imported += 1
        if rows_to_append:
            sheet.append_rows(rows_to_append)
        msg = f"✅ Import done — {imported} contact(s) added"
        if skipped:
            msg += f", {skipped} skipped (already exist)"
        await update.message.reply_text(msg)
    except ImportError:
        await update.message.reply_text("openpyxl isn't installed. Run `pip install openpyxl` and redeploy.")
    except Exception as e:
        await update.message.reply_text(f"❌ Import failed: {str(e)}")

# ── Birthday ───────────────────────────────────────────────────────────────────

def persist_birthday_pending():
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        today_str = date.today().strftime("%d/%m/%Y")
        data = json.dumps({"date": today_str, "pending": state.birthday_pending})
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("Key") == "birthday_pending":
                sheet.update_cell(i + 2, 2, data)
                return
        sheet.append_row(["birthday_pending", data])
    except Exception as e:
        print(f"persist_birthday_pending error: {e}")

def load_birthday_pending_from_sheet():
    try:
        sheet = get_sheet("Settings")
        if not sheet:
            return
        records = sheet.get_all_records()
        for r in records:
            if r.get("Key") == "birthday_pending":
                raw = r.get("Value", "")
                if raw:
                    loaded = json.loads(raw)
                    if loaded.get("date") == date.today().strftime("%d/%m/%Y"):
                        state.birthday_pending = loaded.get("pending", {})
                        if state.birthday_pending:
                            print(f"Restored {len(state.birthday_pending)} birthday pending(s) from sheet")
                return
    except Exception as e:
        print(f"load_birthday_pending_from_sheet error: {e}")

def ensure_birthday_greeted_column():
    pass

def get_birthday_greeted_col():
    return 10

def generate_birthday_greeting(name, age, relationship, context, notes):
    context_str = f"Relationship: {relationship}. Context: {context}. Notes: {notes or 'none'}."
    style_ref = ""
    try:
        from sheets import get_sheet
        sheet = get_sheet("Settings")
        if sheet:
            records = sheet.get_all_records()
            for r in records:
                if r.get("Key") == "birthday_message_style":
                    style_ref = r.get("Value", "")
                    break
    except Exception:
        pass
    style_note = f"\n\nStyle reference (match this tone and length):\n{style_ref}" if style_ref else ""
    greeting_prompt = (
        f"Write a warm, casual birthday greeting that someone could copy and paste to send to {name}, "
        f"who is turning {age} today.\n\n"
        f"Context: {context_str}{style_note}\n\n"
        f"Rules:\n"
        f"- Must open with Happy birthday\n"
        f"- Casual and warm throughout, never stiff or corporate\n"
        f"- Add one personal touch based on their notes if relevant\n"
        f"- End with a warm closing line\n"
        f"- No dashes anywhere\n"
        f"- Do NOT use: Hope you had a great one, Hope its been a good one\n"
        f"- 2 to 4 sentences max\n"
        f"- Write it in first person, ready to copy and send"
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": greeting_prompt}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"generate_birthday_greeting Claude error for {name}: {e}")
        return f"Happy birthday {name}! Hope you have a wonderful day ahead."

async def send_birthday_reminders(app):
    import asyncio
    from config import YOUR_CHAT_ID
    try:
        ensure_birthday_greeted_column()
        records = _get_crm_records()
        today = date.today()
        for i, r in enumerate(records):
            bday_str = r.get("Birthday", "")
            if not bday_str:
                continue
            try:
                bday = datetime.strptime(bday_str, "%d/%m/%Y").date()
                if bday.day != today.day or bday.month != today.month:
                    continue
                name = r.get("Name", "")
                age = calculate_age(bday_str)
                notes = r.get("Notes", "")
                relationship = r.get("Relationship", "")
                context = r.get("Context", "")
                already_greeted = r.get("Birthday Greeted", "")
                if already_greeted == today.strftime("%d/%m/%Y"):
                    continue
                greeting = await asyncio.to_thread(
                    generate_birthday_greeting, name, age, relationship, context, notes
                )
                msg = (
                    f"It's {name}'s birthday today! Turning {age}. 🎂\n\n"
                    f"Here's a message you can send:\n\n"
                    f"{greeting}\n\n"
                    f"Reply 'sent' when you've wished them, or 'skip' to dismiss."
                )
                await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
                state.birthday_pending[name] = {"row": i + 2, "greeted": False, "greeting": greeting}
                persist_birthday_pending()
            except Exception as e:
                print(f"Birthday reminder error for {r.get('Name', '?')}: {e}")
                from helpers import alert_error
                await alert_error("send_birthday_reminders", f"{r.get('Name', '?')}: {e}")
    except Exception as e:
        print(f"Error in send_birthday_reminders: {e}")

async def send_birthday_followups(app):
    from config import YOUR_CHAT_ID
    try:
        still_pending = {k: v for k, v in state.birthday_pending.items() if not v.get("greeted")}
        for name, data in still_pending.items():
            try:
                msg = (
                    f"Did you get a chance to wish {name} happy birthday? 🎂\n\n"
                    f"Here's that message again:\n\n"
                    f"{data['greeting']}\n\n"
                    f"Reply 'sent' when you've wished them, or 'skip' to dismiss."
                )
                await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
            except Exception as e:
                print(f"Error sending 2pm follow-up for {name}: {e}")
        # Do NOT clear birthday_pending here — wait for explicit sent/skip
    except Exception as e:
        print(f"Error in send_birthday_followups: {e}")

def mark_birthday_greeted(name):
    try:
        col = get_birthday_greeted_col()
        if not col:
            return
        row_num, record = find_row(name)
        if record and row_num != "disambig":
            sheet = crm_sheet()
            sheet.update_cell(row_num, col, date.today().strftime("%d/%m/%Y"))
    except Exception as e:
        print(f"Error marking birthday greeted for {name}: {e}")

def mark_birthday_not_sent(name):
    try:
        col = get_birthday_greeted_col()
        if not col:
            return
        row_num, record = find_row(name)
        if record and row_num != "disambig":
            sheet = crm_sheet()
            sheet.update_cell(row_num, col, "not sent")
    except Exception as e:
        print(f"Error marking birthday not sent for {name}: {e}")

async def auto_clear_birthday_pending(app):
    """Called at midnight — marks any ungreeted birthday contacts as not sent."""
    from config import YOUR_CHAT_ID
    try:
        ungreeted = {k: v for k, v in state.birthday_pending.items() if not v.get("greeted")}
        for name in ungreeted:
            mark_birthday_not_sent(name)
        state.birthday_pending = {}
        persist_birthday_pending()
    except Exception as e:
        print(f"auto_clear_birthday_pending error: {e}")

def check_birthday_acknowledgement(text):
    if not state.birthday_pending:
        return False, None
    lower = text.strip().lower()
    # Birthday message paste detection — birthday keywords present
    birthday_keywords = ["happy birthday", "birthday", "celebrate", "wishing you", "bday"]
    if any(kw in lower for kw in birthday_keywords) and len(text) > 40:
        # Store as style reference in Settings
        try:
            from sheets import get_sheet
            sheet = get_sheet("Settings")
            if sheet:
                records = sheet.get_all_records()
                for i, r in enumerate(records):
                    if r.get("Key") == "birthday_message_style":
                        sheet.update_cell(i + 2, 2, text)
                        return True, "Noted 👍"
                sheet.append_row(["birthday_message_style", text])
            return True, "Noted 👍"
        except Exception as e:
            print(f"birthday style save error: {e}")
            return True, "Noted 👍"

    sent_variants = [
        "sent", "done", "skip", "skipped", "sent it", "sent!",
        "yeah sent it", "already sent", "ok done", "yep sent",
        "yup sent", "sent already", "done already", "ok skip",
        "yeah skip", "just sent", "just sent it", "i sent it",
        "birthday message sent", "birthday text sent", "bday text sent",
    ]
    # Also catch "sent [name] birthday" or "[name] sent" patterns
    is_sent = lower in sent_variants
    if not is_sent:
        if re.search(r"\bsent\b", lower) and re.search(r"\bbirthday|bday|text|message\b", lower):
            is_sent = True
        elif re.search(r"\bsent\b", lower):
            # "sent jolyn" or "jolyn sent" — check if name matches pending
            for name in state.birthday_pending:
                if name.lower().split()[0] in lower:
                    is_sent = True
                    break
    if not is_sent:
        return False, None

    skip = lower in ["skip", "skipped", "ok skip", "yeah skip"]
    acknowledged = []
    for name, data in list(state.birthday_pending.items()):
        if not data.get("greeted"):
            data["greeted"] = True
            if skip:
                mark_birthday_not_sent(name)
                acknowledged.append(name)
            else:
                mark_birthday_greeted(name)
                acknowledged.append(name)
    if acknowledged:
        persist_birthday_pending()
        return True, "Got it ✅"
    return False, None

async def send_followup_reminders(app):
    from config import YOUR_CHAT_ID
    try:
        records = _get_crm_records()
        today = date.today()
        for i, r in enumerate(records, start=2):
            fu_date_str = r.get("Follow Up Date", "")
            if not fu_date_str:
                continue
            fu_date = parse_date_flexible(fu_date_str)
            if fu_date is None:
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=(
                        f"⚠️ Couldn't read the follow-up date for *{r.get('Name', '?')}*.\n"
                        f"What's the correct date? (e.g. 25 Apr 2026)\n"
                        f"Reply: followup date {r.get('Name', '')} [date]"
                    ),
                    parse_mode="Markdown"
                )
                continue
            if fu_date == today:
                message = (
                    f"🔔 *Follow up reminder!*\n\n"
                    f"👤 *{r.get('Name')}*\n"
                    f"📝 {r.get('Follow Up Notes') or 'No notes'}\n\n"
                    f"Don't forget to reach out today!"
                )
                await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending follow up reminders: {e}")

# ── Natural language update detection ──────────────────────────────────────────

def detect_crm_natural_update(text):
    lower = text.lower()
    ref_match = re.search(
        r"([a-z][a-z\s']+?)\s+referred\s+([a-z][a-z\s']+?)(?:\s+to\s+me|\s+to\s+us)?\.?$",
        lower
    )
    if ref_match:
        referrer = ref_match.group(1).strip().title()
        referred = ref_match.group(2).strip().title()
        return ("referral", referrer, referred, None)
    is_match = re.search(
        r"([a-z][a-z\s']+?)'s\s+(email|address|alias|birthday|relationship|context|notes)\s+is\s+(.+)",
        lower
    )
    if is_match:
        name = is_match.group(1).strip().title()
        field = is_match.group(2).strip()
        value = is_match.group(3).strip().rstrip(".")
        return ("update", name, field, value)
    update_match = re.search(
        r"update\s+([a-z][a-z\s']+?)'s\s+(email|address|alias|birthday|relationship|context|notes)\s+to\s+(.+)",
        lower
    )
    if update_match:
        name = update_match.group(1).strip().title()
        field = update_match.group(2).strip()
        value = update_match.group(3).strip().rstrip(".")
        return ("update", name, field, value)
    ask_private = re.search(
        r"(?:what'?s?\s+)?([a-z][a-z\s']+?)'s\s+(email|address)",
        lower
    )
    if ask_private and any(w in lower for w in ["what", "show", "tell", "give"]):
        name = ask_private.group(1).strip().title()
        field = ask_private.group(2).strip()
        return ("show_private", name, field, None)
    return None
