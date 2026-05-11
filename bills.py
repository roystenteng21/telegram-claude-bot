import re
import json
from datetime import date, datetime
import state
from config import BILL_REMINDER_GREETINGS, YOUR_CHAT_ID
from clients import client
from sheets import bills_sheet, expenses_sheet

def parse_bill_request(text):
    from datetime import date
    today = date.today().strftime("%d %b %Y")
    prompt = (
        f"Today is {today}. Extract bill details from: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- name: string (bill name, e.g. 'Citi Credit Card', 'Netflix', 'Electricity')\n"
        f"- bank: string (bank or provider name, or empty)\n"
        f"- due_date: string (full due date in DD/MM/YYYY format, e.g. '18/05/2026'. If only day+month given, assume current or next occurrence. If no date, empty string.)\n"
        f"- estimated_amount: number (estimated amount in SGD, or 0 if not mentioned)\n"
        f"- notes: string (any extra notes, or empty)\n\n"
        f"Return ONLY the JSON."
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"parse_bill_request JSON error: {e} | raw: {raw[:100]}")
        return None

def add_bill(name, bank, due_date, estimated_amount, notes=""):
    sheet = bills_sheet()
    sheet.append_row([name, bank, due_date, str(estimated_amount), notes])

def list_bills():
    try:
        sheet = bills_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No bills set up yet."
        lines = ["Your bills:\n"]
        for r in records:
            amt = r.get("Estimated Amount", "")
            amt_str = f" — ~${amt}" if amt and str(amt) != "0" else ""
            due = r.get("Due Date", "")
            lines.append(f"• {r.get('Name', '')} (due {due}){amt_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error listing bills: {str(e)}"

def delete_bill(name):
    try:
        sheet = bills_sheet()
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if name.lower() in r.get("Name", "").lower():
                sheet.delete_rows(i + 2)
                return f"Deleted bill: {r.get('Name')}"
        return f"No bill found matching '{name}'."
    except Exception as e:
        return f"❌ Error deleting bill: {str(e)}"

def get_cycle_expenses(card_name, due_day):
    try:
        sheet = expenses_sheet()
        records = sheet.get_all_records()
        today = date.today()
        if today.day >= due_day:
            cycle_start = today.replace(day=due_day)
        else:
            if today.month == 1:
                cycle_start = today.replace(year=today.year - 1, month=12, day=due_day)
            else:
                cycle_start = today.replace(month=today.month - 1, day=due_day)
        total = 0
        for r in records:
            try:
                dt = datetime.strptime(r.get("Date", ""), "%d/%m/%Y").date()
                card = r.get("Card", "")
                if dt >= cycle_start and card_name.lower() in card.lower():
                    total += float(r.get("SGD Amount", 0) or r.get("Amount", 0))
            except Exception as e:
                print(f"get_cycle_expenses: bad row: {e}")
        return total
    except Exception as e:
        print(f"get_cycle_expenses error: {e}")
        return 0

async def send_bill_reminders(app):
    import random
    try:
        sheet = bills_sheet()
        records = sheet.get_all_records()
        today = date.today()
        for r in records:
            try:
                due_str = r.get("Due Date", "")
                if not due_str:
                    continue
                due_date = None
                for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"]:
                    try:
                        due_date = datetime.strptime(due_str, fmt).date()
                        break
                    except ValueError:
                        continue
                if not due_date:
                    continue
                days_away = (due_date - today).days
                if days_away == 7:
                    name = r.get("Name", "")
                    estimated = r.get("Estimated Amount", "")
                    amount_str = f"~${estimated}" if estimated and str(estimated) != "0" else "amount unknown"
                    greeting = random.choice(BILL_REMINDER_GREETINGS)
                    msg = (
                        f"{greeting} — your {name} bill is due in 7 days ({due_date.strftime('%d %b')}).\n"
                        f"{amount_str}."
                    )
                    await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
                elif days_away == 3:
                    name = r.get("Name", "")
                    estimated = r.get("Estimated Amount", "")
                    amount_str = f"~${estimated}" if estimated and str(estimated) != "0" else "amount unknown"
                    greeting = random.choice(BILL_REMINDER_GREETINGS)
                    msg = (
                        f"{greeting} — your {name} bill is due in 3 days ({due_date.strftime('%d %b')}).\n"
                        f"{amount_str}."
                    )
                    await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
                elif days_away == 0:
                    name = r.get("Name", "")
                    estimated = r.get("Estimated Amount", "")
                    amount_str = f"~${estimated}" if estimated and str(estimated) != "0" else "amount unknown"
                    msg = f"📅 Your {name} bill is due today. {amount_str}."
                    await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
            except Exception as e:
                print(f"Bill reminder error for {r.get('Name', '?')}: {e}")
    except Exception as e:
        print(f"Error in send_bill_reminders: {e}")

def is_bill_request(text):
    lower = text.lower()
    if re.search(r"\bnew\s+bill\b", lower):
        return True
    if re.search(r"\bbills?\b", lower) and re.search(r"\$[\d,.]+|\d+[\d,.]*\s*(sgd|myr|usd)?", lower):
        return True
    if re.search(r"\bbills?\b", lower) and re.search(r"\bdue\b", lower):
        return True
    has_due_on_the = bool(re.search(r"due on the \d{1,2}", lower))
    triggers = ["bill is due", "bill due", "set up a bill", "add a bill",
                "credit card bill", "due every", "my citi bill", "my maybank bill",
                "my amex bill", "my uob bill", "my credit card bill"]
    return has_due_on_the or any(t in lower for t in triggers)

def handle_new_bill(text):
    try:
        parsed = parse_bill_request(text)
        name = parsed.get("name", "")
        bank = parsed.get("bank", "")
        due_date_str = parsed.get("due_date", "")
        estimated_amount = parsed.get("estimated_amount", 0)
        notes = parsed.get("notes", "")
        if not name or not due_date_str:
            return "Couldn't get the bill details. Try: 'new bill Citi $800 due 25 May'."
        add_bill(name, bank, due_date_str, estimated_amount, notes)
        amt_str = f", ~${estimated_amount}" if estimated_amount else ""
        # Check days away for immediate reminder
        try:
            due_date = None
            for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"]:
                try:
                    due_date = datetime.strptime(due_date_str, fmt).date()
                    break
                except ValueError:
                    continue
            if due_date:
                days_away = (due_date - date.today()).days
                reminder_note = ""
                if days_away <= 0:
                    reminder_note = "\n⚠️ This bill is due today or overdue."
                elif days_away <= 3:
                    reminder_note = f"\n📅 Due in {days_away} day(s) — I'll remind you on the due date."
                elif days_away <= 7:
                    reminder_note = f"\n📅 Due in {days_away} day(s) — I'll remind you 3 days before."
                else:
                    reminder_note = f"\n📅 Due in {days_away} day(s) — I'll remind you 7 days before."
            else:
                reminder_note = ""
        except Exception:
            reminder_note = ""
        return f"Got it — {name} bill logged for {due_date_str}{amt_str}.{reminder_note}"
    except Exception as e:
        return f"❌ Couldn't save that bill: {str(e)}"
