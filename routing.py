# routing.py — Part 1: imports, helpers, session handlers
import re
import io
import base64
from datetime import date, datetime

from telegram import Update
from telegram.ext import ContextTypes

import config
from config import TIMEZONE, YOUR_CHAT_ID, overseas_state
from clients import client, drive_service, spreadsheet
from state import touch_session, check_session_timeouts, get_fx_rate
from profile import build_system_prompt, setup_em_profile
from sheets import get_or_create_drive_folder, get_pending_backlog
from crm import (
    save_contact, find_contact, add_note, set_followup, update_field,
    delete_contact, search_contacts, list_contacts, get_stats,
    upcoming_followups, overdue_followups, upcoming_birthdays, last_contact,
    get_all_referrals, get_top_referrers, get_referrals_by, set_referral,
    find_row, detect_crm_natural_update, update_contact_field_natural,
    check_birthday_acknowledgement, parse_excel_column_order, handle_excel_import,
)
from expenses import (
    handle_expense_text, handle_expense_session, handle_receipt_confirm_session,
    handle_statement_upload, handle_new_bill, list_bills, delete_bill,
    get_expense_categories, get_merchant_list, delete_merchant, delete_last_expense,
    get_recent_expenses, format_delete_list, search_expenses_by_merchant, delete_expense_by_row,
    show_last_expense, edit_last_expense, get_expense_report, rename_category,
    set_card_default_category, list_cards, is_expense_input, is_log_prefix_input,
    is_bare_merchant_input, is_bill_request, parse_expense_text, get_trip_summary,
)
from reminders import (
    list_reminders, cancel_reminder_by_keyword, handle_new_reminder, handle_reschedule,
    is_reminder_request, is_reschedule_request, is_cancel_reminder_request,
)
from calendar import (
    get_events, delete_calendar_event, smart_add_event, is_calendar_request,
    search_meeting_notes, get_calendar,
)
from todos import add_todo, complete_todo, delete_todo, list_todos, todo_sheet
from stocks import (
    handle_stock_request, get_market_summary_now, send_weekly_market_summary,
    is_stock_request, get_portfolio_rows, format_portfolio_delete_list,
    delete_portfolio_row, search_portfolio_by_ticker, set_price_alert, suggest_stocks,
)
from trips import (
    handle_overseas_request, is_overseas_mode_request, extract_flight_number,
    extract_all_flight_numbers, extract_flight_dates, format_flight_time, lookup_flight,
    get_dest_info_from_iata, save_trip, deactivate_overseas_mode,
    activate_overseas_mode_scheduled, format_trip_history,
)
from meetings import handle_meeting_session, is_meeting_start, extract_event_name

_RESTAURANT_FNS = {}

def _r(fn_name):
    return _RESTAURANT_FNS.get(fn_name)

def register_restaurant_fns(**fns):
    _RESTAURANT_FNS.update(fns)

def get_active_session_label(user_id):
    if user_id in config.receipt_confirm_sessions:
        return "expense confirmation"
    if user_id in config.expense_sessions:
        return "expense entry"
    if user_id in config.meeting_sessions:
        return "meeting recap"
    if user_id in config.edit_sessions:
        return "contact edit"
    if user_id in config.confirm_sessions:
        action = config.confirm_sessions[user_id].get("action", "")
        labels = {
            "delete_contact": "contact deletion", "delete_bill": "bill deletion",
            "delete_restaurant": "restaurant deletion", "delete_event": "event deletion",
            "rename_category": "category rename",
        }
        return labels.get(action, "confirmation")
    if user_id in config.delete_sessions:
        return "expense deletion"
    if user_id in config.portfolio_delete_sessions:
        return "portfolio deletion"
    if user_id in config.excel_import_sessions:
        return "contact import"
    if user_id in config.pending_restaurant_saves:
        return "restaurant save"
    if user_id in config.pending_contact_saves:
        return "contact save"
    if user_id in config.todo_disambig_sessions:
        return "todo action"
    if overseas_state.get("_pending_flight"):
        return "flight setup"
    return None

_SESSION_REPLY_TOKENS = {
    "yes", "y", "no", "n", "cancel", "skip", "done", "update", "new",
    "confirm", "ok", "okay", "sure", "nope", "yep", "yup"
}

def looks_like_new_intent(text):
    lower = text.strip().lower()
    if lower in _SESSION_REPLY_TOKENS:
        return False
    if re.match(r"^\d+$", lower.strip()):
        return False
    words = lower.split()
    if len(words) <= 2:
        return False
    triggers = [
        "remind me", "set a reminder", "add reminder", "spent ", "paid ", "bought ",
        "expense ", "save ", "add contact", "find ", "pull up ", "meeting recap",
        "overseas", "i'm in ", "flying to", "what's ", "how is ", "market summary",
        "alert me", "price of ", "check ", "bill ", "my ", "schedule ", "book ",
        "todo ", "add to my list", "remind", "cancel reminder", "delete ",
    ]
    return any(lower.startswith(t) for t in triggers)

async def send_safe(target, text, parse_mode=None):
    MAX = 4096
    if len(text) <= MAX:
        try:
            await target.reply_text(text, parse_mode=parse_mode)
        except Exception:
            await target.reply_text(text)
        return
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    for chunk in chunks:
        try:
            await chunk if False else await target.reply_text(chunk, parse_mode=parse_mode)
        except Exception:
            await target.reply_text(chunk)

async def handle_edit_session(user_id, text, update):
    session = config.edit_sessions[user_id]
    step = session["step"]
    fields = ["alias", "birthday", "relationship", "context", "notes",
              "follow up date", "follow up notes", "email", "address"]
    if step == "choose_field":
        field = text.lower().strip()
        if field == "cancel":
            del config.edit_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return
        if field not in fields:
            await update.message.reply_text(
                f"Pick a field to edit:\n1. Alias\n2. Birthday\n3. Relationship\n4. Context\n"
                f"5. Notes\n6. Follow up date\n7. Follow up notes\n8. Email\n9. Address\n\n"
                f"Or type *cancel* to exit.", parse_mode="Markdown"
            )
            return
        session["field"] = field
        session["step"] = "enter_value"
        config.edit_sessions[user_id] = session
        await update.message.reply_text(f"Enter the new value for *{field.title()}*:", parse_mode="Markdown")
    elif step == "enter_value":
        field = session["field"]
        name = session["name"]
        result = update_field(f"{name}, {field}, {text}")
        del config.edit_sessions[user_id]
        await update.message.reply_text(result, parse_mode="Markdown")


async def _handle_message_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    # Document handler
    if update.message.document:
        doc = update.message.document
        fname = doc.file_name or ""
        if fname.lower().endswith(".csv") or (fname.lower().endswith((".xlsx", ".xls")) and "statement" in fname.lower()):
            tg_file = await doc.get_file()
            file_bytes = bytes(await tg_file.download_as_bytearray())
            await handle_statement_upload(file_bytes, fname, user_id, update)
            return
        if fname.lower().endswith((".xlsx", ".xls")):
            tg_file = await doc.get_file()
            file_bytes = bytes(await tg_file.download_as_bytearray())
            try:
                import openpyxl
                wb_peek = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
                ws_peek = wb_peek.active
                first_row = next(ws_peek.iter_rows(max_row=1, values_only=True), None)
                auto_cols = [str(c).strip().replace("\xa0","") for c in first_row if c] if first_row else []
            except Exception as e:
                print(f"Excel header auto-detect error: {e}")
                auto_cols = []
            if auto_cols:
                await update.message.reply_text(f"Detected columns: {\', \'.join(auto_cols)}\nImporting now...")
                await handle_excel_import(file_bytes, auto_cols, update)
            elif user_id in config.excel_import_sessions and config.excel_import_sessions[user_id].get("step") == "awaiting_file":
                col_order = config.excel_import_sessions[user_id].get("column_order", [])
                del config.excel_import_sessions[user_id]
                await handle_excel_import(file_bytes, col_order, update)
            else:
                config.excel_import_sessions[user_id] = {"step": "awaiting_columns", "file_bytes": file_bytes}
                await update.message.reply_text("Got the file but couldn't read its headers. Tell me the column order.")
        else:
            await update.message.reply_text("I can only import .xlsx or .xls files for CRM.")
        return

    # Photo handler
    if update.message.photo:
        caption = (update.message.caption or "").strip()
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())
        receipt_link = ""
        drive_file_id = ""
        today_obj = date.today()
        month_folder_name = today_obj.strftime("%Y-%m")
        today_str = today_obj.strftime("%Y-%m")
        try:
            from googleapiclient.http import MediaIoBaseUpload
            receipts_root = config.DRIVE_FOLDERS.get("receipts", "")
            if receipts_root:
                month_folder_id = get_or_create_drive_folder(month_folder_name, receipts_root)
                temp_name = f"{today_str}-receipt-{photo.file_id[:8]}.jpg"
                media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
                file_meta = {"name": temp_name, "parents": [month_folder_id]}
                uploaded = drive_service.files().create(body=file_meta, media_body=media, fields="id,webViewLink", supportsAllDrives=True).execute()
                drive_file_id = uploaded.get("id", "")
                receipt_link = uploaded.get("webViewLink", "")
        except Exception as e:
            print(f"Receipt upload error: {e}")

        def rename_receipt_in_drive(merchant_name):
            if not drive_file_id or not merchant_name:
                return
            try:
                safe_merchant = re.sub(r"[^a-zA-Z0-9\-_]", "", merchant_name.replace(" ", "-").lower())
                drive_service.files().update(fileId=drive_file_id, body={"name": f"{today_str}-{safe_merchant}.jpg"}, supportsAllDrives=True).execute()
            except Exception as e:
                print(f"Receipt rename error: {e}")

        if not caption:
            await update.message.reply_text("Got the receipt \U0001f9fe Reading it now...")
            try:
                img_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
                curr = overseas_state.get("currency", "SGD") if overseas_state.get("active") else "SGD"
                vision_resp = client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=300,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                        {"type": "text", "text": f"This is a receipt. Extract merchant name, total amount, currency (default {curr}). Copy the merchant name exactly from the top of the receipt. Use the final total. Reply ONLY as: MERCHANT | AMOUNT | CURRENCY"}
                    ]}]
                )
                vision_text = vision_resp.content[0].text.strip()
                parts = [p.strip() for p in vision_text.split("|")]
                if len(parts) == 3:
                    merchant_v, amount_v, currency_v = parts
                    rename_receipt_in_drive(merchant_v)
                    reply, needs_session, session_data = handle_expense_text(f"{merchant_v} {amount_v} {currency_v}", user_id, receipt_link=receipt_link)
                    if needs_session and session_data:
                        config.receipt_confirm_sessions[user_id] = session_data
                    if reply:
                        await send_safe(update.message, reply, parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"Read the receipt but got unexpected format: \'{vision_text}\'. Try adding a caption.")
            except Exception as e:
                await update.message.reply_text(f"\u274c Couldn't read the receipt ({type(e).__name__}: {str(e)[:80]}). Add a caption and resend.")
            return

        if is_expense_input(caption):
            parsed = parse_expense_text(caption)
            if parsed and parsed.get("merchant"):
                rename_receipt_in_drive(parsed["merchant"])
            reply, needs_session, session_data = handle_expense_text(caption, user_id, receipt_link=receipt_link)
            if needs_session and session_data:
                config.receipt_confirm_sessions[user_id] = session_data
            if reply:
                await send_safe(update.message, reply, parse_mode="Markdown")
        else:
            await update.message.reply_text("Got the receipt but couldn't read that as an expense. Try a caption like '45.50 Ichiran'.")
        return

    text = update.message.text.strip()
    lower = text.lower()

    bday_handled, bday_reply = check_birthday_acknowledgement(text)
    if bday_handled:
        if bday_reply:
            await update.message.reply_text(bday_reply)
        return

    if user_id in config.excel_import_sessions:
        session = config.excel_import_sessions[user_id]
        if session.get("step") == "awaiting_columns":
            cols = parse_excel_column_order(text)
            if cols:
                if session.get("file_bytes"):
                    file_bytes = session["file_bytes"]
                    del config.excel_import_sessions[user_id]
                    await update.message.reply_text(f"Got it — columns: {\', \'.join(cols)}. Importing now...")
                    await handle_excel_import(file_bytes, cols, update)
                else:
                    session["column_order"] = cols
                    session["step"] = "awaiting_file"
                    config.excel_import_sessions[user_id] = session
                    await update.message.reply_text(f"Got it — columns: {\', \'.join(cols)}. Now send the Excel file.")
            else:
                await update.message.reply_text("Couldn't parse that. Try: 'Name, Email, Date of Birth, Alias'")
            return

    await check_session_timeouts(user_id, update)

    active_label = get_active_session_label(user_id)
    if active_label and looks_like_new_intent(text):
        if user_id in config.interrupted_sessions:
            pending = config.interrupted_sessions[user_id]
            if text.strip().lower() in ["yes", "y"]:
                for d in [config.receipt_confirm_sessions, config.expense_sessions, config.meeting_sessions,
                          config.edit_sessions, config.confirm_sessions, config.delete_sessions,
                          config.portfolio_delete_sessions, config.excel_import_sessions,
                          config.pending_restaurant_saves, config.pending_contact_saves, config.todo_disambig_sessions]:
                    d.pop(user_id, None)
                overseas_state.pop("_pending_flight", None)
                config.session_timestamps.pop(user_id, None)
                del config.interrupted_sessions[user_id]
                update.message.text = pending["pending_text"]
                text = pending["pending_text"]
                lower = text.lower()
            elif text.strip().lower() in ["no", "n"]:
                del config.interrupted_sessions[user_id]
                await update.message.reply_text(f"Got it — continuing with your {pending['label']}.")
                return
            else:
                await update.message.reply_text(f"Reply yes to switch, or no to continue with your {pending['label']}.")
                return
        else:
            config.interrupted_sessions[user_id] = {"label": active_label, "pending_text": text}
            await update.message.reply_text(f"You're mid-{active_label} — did you mean to do something else?\nReply yes to switch, or no to continue.")
            return

    if user_id in config.receipt_confirm_sessions:
        touch_session(user_id)
        await handle_receipt_confirm_session(user_id, text, update)
        return

    if user_id in config.meeting_sessions:
        touch_session(user_id)
        await handle_meeting_session(user_id, text, update)
        return

    if user_id in config.expense_sessions:
        touch_session(user_id)
        await handle_expense_session(user_id, text, update)
        return

    if user_id in config.delete_sessions:
        session = config.delete_sessions[user_id]
        if lower in ["cancel", "nevermind", "never mind", "nvm"]:
            del config.delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return
        if session.get("step") == "pick":
            if text.strip().isdigit():
                idx = int(text.strip()) - 1
                expenses = session.get("expenses", [])
                if 0 <= idx < len(expenses):
                    sheet_row, _ = expenses[idx]
                    reply = delete_expense_by_row(sheet_row)
                    del config.delete_sessions[user_id]
                    await update.message.reply_text(reply)
                else:
                    await update.message.reply_text("Invalid number. Try again or 'search [merchant]'.")
                return
            elif lower.startswith("search "):
                query = text[7:].strip()
                results = search_expenses_by_merchant(query)
                if not results:
                    await update.message.reply_text(f"No expenses found matching '{query}'.")
                elif len(results) == 1:
                    sheet_row, r = results[0]
                    reply = delete_expense_by_row(sheet_row)
                    del config.delete_sessions[user_id]
                    await update.message.reply_text(reply)
                else:
                    config.delete_sessions[user_id] = {"step": "pick", "expenses": results}
                    await update.message.reply_text(format_delete_list(results))
                return
            else:
                await update.message.reply_text("Reply with a number, 'search [merchant]', or 'cancel'.")
                return

    if user_id in config.confirm_sessions:
        touch_session(user_id)
        cs = config.confirm_sessions[user_id]
        action = cs.get("action")
        args = cs.get("args", [])
        if lower in ["yes", "y"]:
            del config.confirm_sessions[user_id]
            config.session_timestamps.pop(user_id, None)
            if action == "delete_contact":
                reply = delete_contact(args[0])
            elif action == "rename_category":
                reply = rename_category(args[0], args[1])
            elif action == "delete_bill":
                reply = delete_bill(args[0])
            elif action == "delete_restaurant":
                del_fn = _r("delete_restaurant")
                reply = del_fn(args[0]) if del_fn else "Not loaded."
            elif action == "delete_event":
                reply = delete_calendar_event(args[0])
            else:
                reply = "Done."
            await update.message.reply_text(reply)
        elif lower in ["no", "n", "cancel"]:
            del config.confirm_sessions[user_id]
            config.session_timestamps.pop(user_id, None)
            await update.message.reply_text("Cancelled.")
        else:
            await update.message.reply_text(f"{cs.get('target', 'Confirm?')} (yes / no)")
        return

    if user_id in config.portfolio_delete_sessions:
        touch_session(user_id)
        pd_session = config.portfolio_delete_sessions[user_id]
        if lower in ["cancel", "nevermind", "nvm"]:
            del config.portfolio_delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return
        if text.strip().isdigit():
            idx = int(text.strip()) - 1
            rows = pd_session.get("rows", [])
            if 0 <= idx < len(rows):
                sheet_row, _ = rows[idx]
                del config.portfolio_delete_sessions[user_id]
                result = delete_portfolio_row(sheet_row)
                await update.message.reply_text(result)
            else:
                await update.message.reply_text("Invalid number.")
            return
        elif lower.startswith("search "):
            query = text[7:].strip()
            results = search_portfolio_by_ticker(query)
            if not results:
                await update.message.reply_text(f"No holdings found matching '{query}'.")
            elif len(results) == 1:
                sheet_row, _ = results[0]
                del config.portfolio_delete_sessions[user_id]
                result = delete_portfolio_row(sheet_row)
                await update.message.reply_text(result)
            else:
                config.portfolio_delete_sessions[user_id] = {"step": "pick", "rows": results}
                await update.message.reply_text(format_portfolio_delete_list(results))
            return
        else:
            await update.message.reply_text("Reply with a number, 'search [ticker]', or 'cancel'.")
            return

    if user_id in config.edit_sessions:
        await handle_edit_session(user_id, text, update)
        return

    reply = _route_main(text, lower, user_id, update)
    if reply is None:
        # Conversation fallback
        if user_id not in config.conversation_histories:
            config.conversation_histories[user_id] = []
        config.conversation_histories[user_id].append({"role": "user", "content": text})
        if len(config.conversation_histories[user_id]) > 20:
            config.conversation_histories[user_id] = config.conversation_histories[user_id][-20:]
        system_prompt = build_system_prompt()
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=1024,
                system=system_prompt, messages=config.conversation_histories[user_id]
            )
            reply = response.content[0].text
            config.conversation_histories[user_id].append({"role": "assistant", "content": reply})
        except Exception as e:
            print(f"Claude conversation error: {e}")
            reply = None

    if not reply:
        try:
            fallback = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=300,
                system="You are Em. The user sent a message no handler recognised. Acknowledge in one casual sentence then suggest one thing the developer could add. 2-3 sentences max.",
                messages=[{"role": "user", "content": text}]
            )
            reply = fallback.content[0].text
        except Exception:
            reply = "Not sure how to handle that one."

    if reply:
        await send_safe(update.message, reply, parse_mode="Markdown")


def _route_main(text, lower, user_id, update):
    """Synchronous main routing chain. Returns reply string or None for fallback."""
    # NOTE: This function handles all deterministic routing.
    # Async session handlers are called before this in _handle_message_inner.
    # Pending flight handler below needs to be async — it's handled inline in _handle_message_inner.
    # This function is called only when no session or pending flight is active.
    pass
