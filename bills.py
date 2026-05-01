import re
import json
from datetime import date, datetime
import state
from config import BILL_REMINDER_GREETINGS, YOUR_CHAT_ID
from clients import client
from sheets import bills_sheet, expenses_sheet

def parse_bill_request(text):
    prompt = (
        f"Extract bill details from: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- name: string (bill name, e.g. 'Citi Credit Card', 'Netflix', 'Electricity')\n"
        f"- bank: string (bank or provider name, or empty)\n"
        f"- due_day: number (day of month the bill is due, e.g. 15)\n"
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

def add_bill(name, bank, due_day, estimated_amount, notes=""):
    sheet = bills_sheet()
    sheet.append_row([name, bank, str(due_day), str(estimated_amount), notes])

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
            lines.append(f"• {r.get('Name', '')} (due day {r.get('Due Date', '')}){amt_str}")
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
                due_day = int(r.get("Due Date", 0))
                if not due_day:
                    continue
                if today.day <= due_day:
                    next_due = today.replace(day=due_day)
                else:
                    if today.month == 12:
                        next_due = today.replace(year=today.year + 1, month=1, day=due_day)
                    else:
                        next_due = today.replace(month=today.month + 1, day=due_day)
                days_away = (next_due - today).days
                if days_away == 7:
                    name = r.get("Name", "")
                    estimated = r.get("Estimated Amount", "")
                    cycle_total = get_cycle_expenses(name, due_day)
                    amount_str = f"${cycle_total:.2f} logged this cycle" if cycle_total > 0 else (f"~${estimated}" if estimated and str(estimated) != "0" else "amount unknown")
                    greeting = random.choice(BILL_REMINDER_GREETINGS)
                    msg = (
                        f"{greeting} — your {name} bill is due in 7 days ({next_due.strftime('%d %b')}).\n"
                        f"{amount_str}."
                    )
                    await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg)
            except Exception as e:
                print(f"Bill reminder error for {r.get('Name', '?')}: {e}")
    except Exception as e:
        print(f"Error in send_bill_reminders: {e}")

def is_bill_request(text):
    lower = text.lower()
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
        due_day = parsed.get("due_day", 0)
        estimated_amount = parsed.get("estimated_amount", 0)
        notes = parsed.get("notes", "")
        if not name or not due_day:
            return "Couldn't get the bill details. Try: 'my Citi bill is due on the 15th, usually around $800'."
        add_bill(name, bank, due_day, estimated_amount, notes)
        amt_str = f", estimated ~${estimated_amount}" if estimated_amount else ""
        return f"Got it, I'll remind you about your {name} bill 7 days before it's due (day {due_day} of each month{amt_str})."
    except Exception as e:
        return f"❌ Couldn't save that bill: {str(e)}"
