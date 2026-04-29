"""
routing.py — Full message routing.
"""

import re
import io
import config as _config
from config import (
    YOUR_CHAT_ID, overseas_state,
    expense_sessions, receipt_confirm_sessions, session_timestamps,
    edit_sessions, confirm_sessions, delete_sessions, portfolio_delete_sessions,
    excel_import_sessions, meeting_sessions, interrupted_sessions,
    pending_restaurant_saves, pending_contact_saves, todo_disambig_sessions,
    conversation_histories, market_summary_pending,
    ANTHROPIC_FAILURE_THRESHOLD, _anthropic_failure_count, DRIVE_FOLDERS,
)
from clients import client, drive_service
from state import is_session_expired, check_session_timeouts, get_fx_rate
from sheets import get_pending_backlog, get_or_create_drive_folder
from profile import build_system_prompt, setup_em_profile
from crm import (
    save_contact, find_contact, find_row, add_note, set_followup, update_field,
    delete_contact, search_contacts, list_contacts, get_stats, upcoming_followups,
    overdue_followups, upcoming_birthdays, last_contact,
    get_all_referrals, get_top_referrers, get_referrals_by, set_referral,
    update_contact_field_natural, check_birthday_acknowledgement,
    detect_crm_natural_update, parse_excel_column_order, handle_excel_import,
    handle_edit_session,
)
from expenses import (
    handle_expense_text, handle_expense_session, handle_receipt_confirm_session,
    handle_statement_upload, is_expense_input, is_log_prefix_input, is_bare_merchant_input,
    is_bill_request, handle_new_bill, list_bills, delete_last_expense,
    get_recent_expenses, format_delete_list, search_expenses_by_merchant,
    delete_expense_by_row, show_last_expense, get_expense_report, get_trip_summary,
    get_expense_categories, get_merchant_list, delete_merchant,
    set_card_default_category, edit_last_expense, rename_category,
    parse_expense_text, delete_bill,
)
from reminders import (
    list_reminders, cancel_reminder_by_keyword, is_cancel_reminder_request,
    is_reschedule_request, handle_reschedule, is_reminder_request, handle_new_reminder,
)
from cal import (
    get_events, delete_calendar_event, is_calendar_request, smart_add_event,
    search_meeting_notes, get_calendar,
)
from todos import add_todo, complete_todo, delete_todo, list_todos
from stocks import (
    get_portfolio_performance, get_portfolio_rows, format_portfolio_delete_list,
    search_portfolio_by_ticker, delete_portfolio_row, get_market_summary_now,
    is_stock_request, handle_stock_request,
)
from trips import (
    format_trip_history, trips_sheet,
    is_overseas_mode_request, handle_overseas_request, extract_flight_number,
    extract_all_flight_numbers, extract_flight_dates, lookup_flight,
    format_flight_time, get_dest_info_from_iata, save_trip,
    activate_overseas_mode_scheduled,
)
from meetings import (
    is_meeting_start, extract_event_name, handle_meeting_session,
    handle_meeting_edit_session, MEETING_DONE_PHRASES,
)
from restaurants import (
    handle_save_restaurant, handle_search_restaurants, save_restaurant,
    format_restaurant_saved, delete_restaurant, search_restaurants,
    is_restaurant_save, is_restaurant_search, is_restaurant_suggestion_request,
    is_restaurant_review_request, get_similar_restaurants, get_restaurant_review,
)

from telegram import Update
from telegram.ext import ContextTypes


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
            await target.reply_text(chunk, parse_mode=parse_mode)
        except Exception:
            await target.reply_text(chunk)


def get_active_session_label(user_id):
    if user_id in receipt_confirm_sessions: return "expense confirmation"
    if user_id in expense_sessions: return "expense entry"
    if user_id in meeting_sessions: return "meeting recap"
    if user_id in edit_sessions: return "contact edit"
    if user_id in confirm_sessions:
        labels = {"delete_contact":"contact deletion","delete_bill":"bill deletion",
                  "delete_restaurant":"restaurant deletion","delete_event":"event deletion","rename_category":"category rename"}
        return labels.get(confirm_sessions[user_id].get("action",""),"confirmation")
    if user_id in delete_sessions: return "expense deletion"
    if user_id in portfolio_delete_sessions: return "portfolio deletion"
    if user_id in excel_import_sessions: return "contact import"
    if user_id in pending_restaurant_saves: return "restaurant save"
    if user_id in pending_contact_saves: return "contact save"
    if user_id in todo_disambig_sessions: return "todo action"
    if overseas_state.get("_pending_flight"): return "flight setup"
    return None


_SESSION_REPLY_TOKENS = {"yes","y","no","n","cancel","skip","done","update","new","confirm","ok","okay","sure","nope","yep","yup"}


def looks_like_new_intent(text):
    lower = text.strip().lower()
    if lower in _SESSION_REPLY_TOKENS: return False
    if re.match(r"^\d+$", lower.strip()): return False
    if len(lower.split()) <= 2: return False
    triggers = ["remind me","set a reminder","add reminder","spent ","paid ","bought ","expense ",
                "save ","add contact","find ","pull up ","meeting recap","start a recap",
                "overseas","i'm in ","flying to","what's ","how is ","market summary",
                "alert me","price of ","check ","bill ","my ","schedule ","book ",
                "todo ","add to my list","remind","cancel reminder","delete "]
    return any(lower.startswith(t) for t in triggers)


def _handle_restaurant_result(result, user_id):
    if result and result.startswith("_NEEDS_LOCATION_:"):
        parts = result.split(":",2)
        name = parts[1] if len(parts)>1 else ""
        country = parts[2] if len(parts)>2 else "Singapore"
        pending_restaurant_saves[user_id] = {"name":name,"country":country,"step":"awaiting_location"}
        return f"⚠️ Couldn't read that Maps link — what's the location for {name}?\n(e.g. 313 Orchard Road, Singapore)"
    elif result and result.startswith("_INFER_LOCATION_:"):
        parts = result.split(":",5)
        name = parts[1] if len(parts)>1 else ""
        country = parts[2] if len(parts)>2 else "Singapore"
        tags = parts[3] if len(parts)>3 else ""
        multiple = parts[4]=="1" if len(parts)>4 else False
        outlets = parts[5].split("|") if len(parts)>5 and parts[5] else []
        if multiple and len(outlets)>1:
            pending_restaurant_saves[user_id] = {"step":"awaiting_outlet","name":name,"country":country,"tags":tags,"outlets":outlets}
            outlet_list = "\n".join(f"{i+1}. {o}" for i,o in enumerate(outlets))
            return f"Found a few {name} outlets — which one, and any tags?\n{outlet_list}"
        else:
            location = outlets[0] if outlets else ""
            pending_restaurant_saves[user_id] = {"step":"awaiting_confirm","name":name,"country":country,"tags":tags,"location":location}
            return f"Got it — I have {name} at {location}. Is that right, and any tags? (yes / no, add tags or skip)"
    elif result and result.startswith("_DUPLICATE_RESTAURANT_:"):
        parts = result.split(":",5)
        pending_restaurant_saves[user_id] = {"step":"duplicate","existing":parts[1],"name":parts[2],
            "location":parts[3],"country":parts[4],"tags":parts[5] if len(parts)>5 else "","notes":""}
        return f"*{parts[1]}* is already in your list.\nUpdate existing or save as new? (update / new)"
    return result


async def _handle_message_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    # --- Document ---
    if update.message.document:
        doc = update.message.document
        fname = doc.file_name or ""
        if fname.lower().endswith(".csv") or (fname.lower().endswith((".xlsx",".xls")) and "statement" in fname.lower()):
            tg_file = await doc.get_file()
            file_bytes = bytes(await tg_file.download_as_bytearray())
            await handle_statement_upload(file_bytes, fname, user_id, update)
            return
        if fname.lower().endswith((".xlsx",".xls")):
            tg_file = await doc.get_file()
            file_bytes = bytes(await tg_file.download_as_bytearray())
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
                ws = wb.active
                first_row = next(ws.iter_rows(max_row=1,values_only=True),None)
                auto_cols = [str(c).strip().replace('\xa0','') for c in first_row if c] if first_row else []
            except Exception as e:
                print(f"Excel header auto-detect error: {e}")
                auto_cols = []
            if auto_cols:
                await update.message.reply_text(f"Detected columns: {', '.join(auto_cols)}\nImporting now...")
                await handle_excel_import(file_bytes, auto_cols, update)
            elif user_id in excel_import_sessions and excel_import_sessions[user_id].get("step")=="awaiting_file":
                col_order = excel_import_sessions[user_id].get("column_order",[])
                del excel_import_sessions[user_id]
                await handle_excel_import(file_bytes, col_order, update)
            else:
                excel_import_sessions[user_id] = {"step":"awaiting_columns","file_bytes":file_bytes}
                await update.message.reply_text("Got the file but couldn't read its headers. Tell me the column order — e.g. 'Name, Alias, Email, Date of Birth'")
        else:
            await update.message.reply_text("I can only import .xlsx or .xls files for CRM.")
        return

    # --- Photo ---
    if update.message.photo:
        caption = (update.message.caption or "").strip()
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())
        from datetime import date
        receipt_link = ""
        drive_file_id = ""
        today_obj = date.today()
        month_folder_name = today_obj.strftime("%Y-%m")
        today_str = today_obj.strftime("%Y-%m")
        try:
            from googleapiclient.http import MediaIoBaseUpload
            receipts_root = DRIVE_FOLDERS.get("receipts","")
            if receipts_root:
                month_folder_id = get_or_create_drive_folder(month_folder_name, receipts_root)
                temp_name = f"{today_str}-receipt-{photo.file_id[:8]}.jpg"
                media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
                file_meta = {"name":temp_name,"parents":[month_folder_id]}
                uploaded = drive_service.files().create(body=file_meta,media_body=media,fields="id,webViewLink",supportsAllDrives=True).execute()
                drive_file_id = uploaded.get("id","")
                receipt_link = uploaded.get("webViewLink","")
        except Exception as e:
            print(f"Receipt upload error: {e}")

        def rename_receipt_in_drive(merchant_name):
            if not drive_file_id or not merchant_name: return
            try:
                safe = re.sub(r"[^a-zA-Z0-9\-_]","",merchant_name.replace(" ","-").lower())
                drive_service.files().update(fileId=drive_file_id,body={"name":f"{today_str}-{safe}.jpg"},supportsAllDrives=True).execute()
            except Exception as e:
                print(f"Receipt rename error: {e}")

        if not caption:
            await update.message.reply_text("Got the receipt 🧾 Reading it now...")
            try:
                import base64
                img_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
                curr = overseas_state.get("currency","SGD") if overseas_state.get("active") else "SGD"
                vision_resp = client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=300,
                    messages=[{"role":"user","content":[
                        {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":img_b64}},
                        {"type":"text","text":(
                            f"This is a receipt. Extract: merchant name, total amount, and currency (default {curr} if not shown). "
                            "For the merchant name, copy it exactly as printed at the very top — including any numbers or prefixes. "
                            "Do not shorten, capitalise, or summarise. "
                            "For amount, use the final total (Net Total, Total, Grand Total — not subtotal). "
                            "Reply ONLY in this format: MERCHANT | AMOUNT | CURRENCY\nExample: 108 Matcha Saro | 85.70 | MYR"
                        )}
                    ]}]
                )
                vision_text = vision_resp.content[0].text.strip()
                parts = [p.strip() for p in vision_text.split("|")]
                if len(parts)==3:
                    merchant_v, amount_v, currency_v = parts
                    rename_receipt_in_drive(merchant_v)
                    reply, needs_session, session_data = handle_expense_text(f"{merchant_v} {amount_v} {currency_v}", user_id, receipt_link=receipt_link)
                    if needs_session and session_data:
                        expense_sessions[user_id] = session_data
                    if reply:
                        await send_safe(update.message, reply, parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"Read the receipt but got an unexpected format — got: '{vision_text}'\nTry adding a caption like '45.50 Ichiran'.")
            except Exception as e:
                print(f"Vision parse error: {e}")
                await update.message.reply_text(f"❌ Couldn't read the receipt automatically ({type(e).__name__}: {str(e)[:80]}). Add a caption like '45.50 Ichiran' and resend.")
            return

        if is_expense_input(caption):
            parsed = parse_expense_text(caption)
            if parsed and parsed.get("merchant"):
                rename_receipt_in_drive(parsed["merchant"])
            reply, needs_session, session_data = handle_expense_text(caption, user_id, receipt_link=receipt_link)
            if needs_session and session_data:
                expense_sessions[user_id] = session_data
            if reply:
                await send_safe(update.message, reply, parse_mode="Markdown")
        else:
            await update.message.reply_text("Got the receipt but couldn't read that as an expense. Try a caption like '1400 Ichiran' or 'spent 45 at Uniqlo'.")
        return

    # --- Text ---
    text = update.message.text.strip()
    lower = text.lower()

    bday_handled, bday_reply = check_birthday_acknowledgement(text)
    if bday_handled:
        if bday_reply: await update.message.reply_text(bday_reply)
        return

    if user_id in excel_import_sessions:
        session = excel_import_sessions[user_id]
        if session.get("step")=="awaiting_columns":
            cols = parse_excel_column_order(text)
            if cols:
                if session.get("file_bytes"):
                    file_bytes = session["file_bytes"]
                    del excel_import_sessions[user_id]
                    await update.message.reply_text(f"Got it — columns: {', '.join(cols)}. Importing now...")
                    await handle_excel_import(file_bytes, cols, update)
                else:
                    session["column_order"] = cols
                    session["step"] = "awaiting_file"
                    await update.message.reply_text(f"Got it — columns: {', '.join(cols)}. Now send the Excel file.")
            else:
                await update.message.reply_text("Couldn't parse that. Try: 'Name, Email, Date of Birth, Alias'")
            return

    if any(user_id in s for s in [expense_sessions,delete_sessions,portfolio_delete_sessions,
                                    confirm_sessions,receipt_confirm_sessions,edit_sessions,
                                    meeting_sessions,pending_restaurant_saves]):
        if is_session_expired(user_id):
            await check_session_timeouts(user_id, update)
            return

    active_label = get_active_session_label(user_id)
    if active_label and looks_like_new_intent(text):
        if user_id in interrupted_sessions:
            pending = interrupted_sessions[user_id]
            if text.strip().lower() in ["yes","y"]:
                for d in [receipt_confirm_sessions,expense_sessions,meeting_sessions,edit_sessions,
                          confirm_sessions,delete_sessions,portfolio_delete_sessions,excel_import_sessions,
                          pending_restaurant_saves,pending_contact_saves,todo_disambig_sessions]:
                    d.pop(user_id,None)
                overseas_state.pop("_pending_flight",None)
                session_timestamps.pop(user_id,None)
                del interrupted_sessions[user_id]
                update.message.text = pending["pending_text"]
                text = pending["pending_text"]
                lower = text.lower()
            elif text.strip().lower() in ["no","n"]:
                del interrupted_sessions[user_id]
                await update.message.reply_text(f"Got it — continuing with your {pending['label']}.")
                return
            else:
                await update.message.reply_text(f"Reply yes to switch, or no to continue with your {pending['label']}.")
                return
        else:
            if user_id not in interrupted_sessions:
                interrupted_sessions[user_id] = {"label":active_label,"pending_text":text}
            await update.message.reply_text(f"You're mid-{active_label} — did you mean to do something else?\nReply yes to switch, or no to continue.")
            return

    # pending restaurant saves
    if user_id in pending_restaurant_saves:
        prs = pending_restaurant_saves[user_id]
        step = prs.get("step")
        if step=="awaiting_location":
            location = text.strip()
            del pending_restaurant_saves[user_id]
            result = save_restaurant(prs["name"], location, prs.get("country","Singapore"))
            if result and result.startswith("_DUPLICATE_:"):
                pending_restaurant_saves[user_id] = {"step":"duplicate","existing":prs["name"],"name":prs["name"],"location":location,"country":prs.get("country","Singapore"),"tags":"","notes":""}
                await update.message.reply_text(f"*{prs['name']}* is already in your list. Update or save as new? (update / new)")
            else:
                await update.message.reply_text(format_restaurant_saved(prs["name"],location))
            return
        elif step=="awaiting_confirm":
            if lower in ["yes","y"]:
                n,loc,cty,tg,nt = prs["name"],prs.get("location",""),prs.get("country","Singapore"),prs.get("tags",""),prs.get("notes","")
                del pending_restaurant_saves[user_id]
                save_restaurant(n,loc,cty,tg,nt)
                await update.message.reply_text(format_restaurant_saved(n,loc,tg,nt))
            elif lower in ["no","n"]:
                del pending_restaurant_saves[user_id]
                await update.message.reply_text("OK, not saved.")
            else:
                tg = text.strip()
                n,loc,cty,nt = prs["name"],prs.get("location",""),prs.get("country","Singapore"),prs.get("notes","")
                del pending_restaurant_saves[user_id]
                save_restaurant(n,loc,cty,tg,nt)
                await update.message.reply_text(format_restaurant_saved(n,loc,tg,nt))
            return
        elif step=="awaiting_outlet":
            outlets = prs.get("outlets",[])
            picked = None
            if re.match(r"^\d+$",text.strip()):
                idx = int(text.strip())-1
                if 0<=idx<len(outlets): picked = outlets[idx]
            if not picked: picked = text.strip()
            n,cty,tg = prs["name"],prs.get("country","Singapore"),prs.get("tags","")
            del pending_restaurant_saves[user_id]
            save_restaurant(n,picked,cty,tg)
            await update.message.reply_text(format_restaurant_saved(n,picked,tg))
            return
        elif step=="duplicate":
            if lower in ["update","u"]:
                n,loc,cty,tg,nt = prs["name"],prs.get("location",""),prs.get("country","Singapore"),prs.get("tags",""),prs.get("notes","")
                del pending_restaurant_saves[user_id]
                save_restaurant(n,loc,cty,tg,nt,force_new=False)
                await update.message.reply_text(format_restaurant_saved(n,loc,tg,nt))
            elif lower in ["new","n"]:
                n,loc,cty,tg,nt = prs["name"],prs.get("location",""),prs.get("country","Singapore"),prs.get("tags",""),prs.get("notes","")
                del pending_restaurant_saves[user_id]
                save_restaurant(n,loc,cty,tg,nt,force_new=True)
                await update.message.reply_text(format_restaurant_saved(n,loc,tg,nt))
            else:
                await update.message.reply_text("Reply 'update' to update the existing, or 'new' to save separately.")
            return

    if user_id in pending_contact_saves:
        pcs = pending_contact_saves[user_id]
        if lower in ["update","u"]:
            del pending_contact_saves[user_id]
            await send_safe(update.message, update_field(pcs["data"]), parse_mode="Markdown")
        elif lower in ["new","n"]:
            del pending_contact_saves[user_id]
            await send_safe(update.message, save_contact(pcs["data"],force_new=True), parse_mode="Markdown")
        else:
            await update.message.reply_text(f"*{pcs['existing_name']}* already exists. Reply 'update' or 'new'.")
        return

    if user_id in todo_disambig_sessions:
        sess = todo_disambig_sessions[user_id]
        tasks = sess.get("tasks",[])
        action = sess.get("action")
        if re.match(r"^\d+$",text.strip()):
            idx = int(text.strip())-1
            if 0<=idx<len(tasks):
                task = tasks[idx]
                del todo_disambig_sessions[user_id]
                if action=="complete": reply = complete_todo(task,exact=True)
                elif action=="delete": reply = delete_todo(task,exact=True)
                elif action=="cancel_reminder":
                    entries = sess.get("entries",[])
                    rid = entries[idx].split(":")[0] if idx<len(entries) else ""
                    reply = cancel_reminder_by_keyword(rid,exact_id=True) if rid else "Couldn't find that reminder."
                else: reply = "Done."
                await update.message.reply_text(reply)
            else:
                await update.message.reply_text(f"Pick a number between 1 and {len(tasks)}.")
        elif lower=="all" and action=="cancel_reminder":
            entries = sess.get("entries",[])
            del todo_disambig_sessions[user_id]
            for e in entries: cancel_reminder_by_keyword(e.split(":")[0],exact_id=True)
            await update.message.reply_text(f"Cancelled {len(entries)} reminders.")
        else:
            await update.message.reply_text("Reply with the number of the item.")
        return

    if user_id in portfolio_delete_sessions:
        sess = portfolio_delete_sessions[user_id]
        if re.match(r"^\d+$",text.strip()):
            idx = int(text.strip())-1
            rows = sess.get("rows",[])
            if 0<=idx<len(rows):
                sheet_row, _ = rows[idx]
                del portfolio_delete_sessions[user_id]
                await update.message.reply_text(delete_portfolio_row(sheet_row))
            else:
                await update.message.reply_text(f"Pick a number between 1 and {len(rows)}.")
        else:
            del portfolio_delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
        return

    if user_id in delete_sessions:
        if re.match(r"^\d+$",text.strip()):
            idx = int(text.strip())-1
            expenses = delete_sessions[user_id].get("expenses",[])
            if 0<=idx<len(expenses):
                sheet_row, _ = expenses[idx]
                del delete_sessions[user_id]
                await update.message.reply_text(delete_expense_by_row(sheet_row))
            else:
                await update.message.reply_text(f"Pick a number between 1 and {len(expenses)}.")
        else:
            del delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
        return

    if user_id in confirm_sessions:
        sess = confirm_sessions[user_id]
        if lower in ["yes","y"]:
            action = sess.get("action")
            args = sess.get("args",[])
            del confirm_sessions[user_id]
            if action=="delete_contact": reply = delete_contact(*args)
            elif action=="delete_bill": reply = delete_bill(*args)
            elif action=="delete_restaurant": reply = delete_restaurant(*args)
            elif action=="delete_event": reply = delete_calendar_event(*args)
            elif action=="rename_category": reply = rename_category(*args)
            else: reply = "Done."
            await send_safe(update.message, reply, parse_mode="Markdown")
        elif lower in ["no","n","cancel"]:
            del confirm_sessions[user_id]
            await update.message.reply_text("Cancelled.")
        else:
            await update.message.reply_text(f"{sess.get('target','Confirm?')} (yes / no)")
        return

    if user_id in edit_sessions:
        reply = await handle_edit_session(user_id, text, update)
        if reply: await send_safe(update.message, reply, parse_mode="Markdown")
        return

    if user_id in receipt_confirm_sessions:
        reply = handle_receipt_confirm_session(user_id, text)
        if reply: await send_safe(update.message, reply, parse_mode="Markdown")
        return

    if user_id in expense_sessions:
        reply = handle_expense_session(user_id, text)
        if reply: await send_safe(update.message, reply, parse_mode="Markdown")
        return

    if user_id in meeting_sessions:
        sess = meeting_sessions[user_id]
        step = sess.get("step")
        if step=="get_name":
            sess["event_name"] = text.strip()
            sess["step"] = "collecting"
            sess["notes"] = []
            await update.message.reply_text(f"Got it, taking notes for {text.strip()}. Send everything over and say done when finished.")
            return
        if any(p in lower for p in MEETING_DONE_PHRASES):
            reply = await handle_meeting_session(user_id, update)
            if reply: await send_safe(update.message, reply, parse_mode="Markdown")
        elif step=="editing":
            reply = await handle_meeting_edit_session(user_id, text, update)
            if reply: await send_safe(update.message, reply, parse_mode="Markdown")
        else:
            sess["notes"].append(text)
        return

    # pending flight flow
    pf = overseas_state.get("_pending_flight")
    if pf:
        if overseas_state.get("_awaiting_return_flight"):
            overseas_state.pop("_awaiting_return_flight",None)
            ret_num = extract_flight_number(text)
            if ret_num:
                ret_data = lookup_flight(ret_num)
                if ret_data:
                    ret_dep = format_flight_time(ret_data["dep_time"])
                    ret_arr = format_flight_time(ret_data["arr_time"])
                    ret_data["flight"] = ret_num
                    pf["return_flight_data"] = ret_data
                    overseas_state["_pending_flight"] = pf
                    reply = f"Return flight: {ret_num} {ret_dep} → {ret_arr} (SIN)\n\nReply Y to confirm overseas mode, or N to cancel."
                else:
                    reply = f"Couldn't find {ret_num} on AviationStack. Reply Y to log just the departure, or try another flight number."
                try: await update.message.reply_text(reply, parse_mode="Markdown")
                except Exception: await update.message.reply_text(reply)
            else:
                await update.message.reply_text("Reply with a return flight number (e.g. OD805) or 'no' to log just the departure.")
            return

        if overseas_state.get("_awaiting_manual_trip"):
            overseas_state.pop("_awaiting_manual_trip",None)
            from datetime import datetime
            from config import TIMEZONE
            currency_match = re.search(r'\b([A-Z]{3})\b',text.upper())
            currency = currency_match.group(1) if currency_match else "SGD"
            if currency in ("THE","AND","FOR","BUT","ARE","YOU","SIN","SGD"): currency = "SGD"
            time_match = re.search(r'\b(\d{1,2}:\d{2})\b',text)
            dep_time_str = time_match.group(1) if time_match else None
            ret_dates = extract_flight_dates(text)
            ret_date_obj = ret_dates[-1] if ret_dates else None
            dest_match = re.search(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b',text)
            destination = dest_match.group(1) if dest_match else "Unknown"
            dep_date_str = pf.get("dep_date","")
            dep_time_full = None
            if dep_date_str and dep_time_str:
                try:
                    dep_dt = datetime.strptime(f"{dep_date_str} {dep_time_str}","%Y-%m-%d %H:%M")
                    dep_time_full = TIMEZONE.localize(dep_dt).isoformat()
                except Exception as e: print(f"Manual trip dep_time parse error: {e}")
            pf.update({"arr_city":destination,"arr_iata":"","dep_time":dep_time_full or dep_date_str,"arr_time":""})
            overseas_state["_pending_flight"] = pf
            ret_str = ret_date_obj.strftime("%d %b") if ret_date_obj else "not set"
            dep_display = f"{dep_date_str} {dep_time_str}" if dep_time_str else dep_date_str
            reply = (f"Got it ✈️\n{pf['flight_number']} → {destination} on {dep_display}\n"
                     f"Currency: {currency}\nReturn: {ret_str}\n\nReply Y to confirm — overseas mode will activate at departure time.")
            pf["manual_currency"] = currency
            if ret_date_obj: pf["manual_return_date"] = ret_date_obj.strftime("%d/%m/%Y")
            overseas_state["_pending_flight"] = pf
            try: await update.message.reply_text(reply)
            except Exception: await update.message.reply_text(reply)
            return

        if text.strip().upper()=="Y":
            from datetime import datetime, date
            from config import TIMEZONE
            overseas_state.pop("_pending_flight")
            info = get_dest_info_from_iata(pf.get("arr_iata",""),pf.get("arr_city",pf.get("arr_airport","")))
            dest = info.get("destination") or pf.get("arr_city") or pf.get("arr_airport","Unknown")
            curr = info.get("currency","SGD")
            dep_str = pf.get("dep_time","")
            dep_fmt = format_flight_time(dep_str)
            return_flight_data = pf.get("return_flight_data")
            scheduled = False
            if dep_str and _config._scheduler:
                try:
                    dep_dt = datetime.fromisoformat(dep_str.replace("Z","+00:00"))
                    dep_local = dep_dt.astimezone(TIMEZONE)
                    if dep_local > datetime.now(TIMEZONE):
                        job = _config._scheduler.add_job(activate_overseas_mode_scheduled,"date",run_date=dep_local,args=[dest,curr,return_flight_data])
                        overseas_state.update({"dep_job_id":job.id,"destination":dest,"currency":curr,
                            "dep_flight":pf.get("flight_number",""),"dep_time":pf.get("dep_time",""),
                            "dep_terminal":pf.get("dep_terminal",""),"dep_gate":pf.get("dep_gate",""),
                            "arr_terminal":pf.get("arr_terminal",""),"arr_gate":pf.get("arr_gate","")})
                        scheduled = True
                except Exception as e: print(f"Failed to schedule departure: {e}")
            if scheduled:
                ret_str = ""
                if return_flight_data:
                    ret_str = f"\nReturn: {return_flight_data.get('flight','')} {format_flight_time(return_flight_data.get('dep_time',''))} → {format_flight_time(return_flight_data.get('arr_time',''))}"
                reply = f"Got it ✈️ Overseas mode will activate at departure: {dep_fmt}\nDestination: {dest} ({curr}){ret_str}\nI'll send a confirmation when it kicks in."
            else:
                for d in [expense_sessions,receipt_confirm_sessions]: d.pop(user_id,None)
                session_timestamps.pop(user_id,None)
                dep_flight = pf.get("flight_number","")
                dep_time = pf.get("dep_time","")
                overseas_state.update({"active":True,"destination":dest,"currency":curr,
                    "currencies":[curr] if curr!="SGD" else [],"trip_destinations":[dest],
                    "trip_start":date.today().strftime("%d/%m/%Y"),"dep_flight":dep_flight,"dep_time":dep_time,
                    "dep_terminal":pf.get("dep_terminal",""),"dep_gate":pf.get("dep_gate",""),
                    "arr_terminal":pf.get("arr_terminal",""),"arr_gate":pf.get("arr_gate","")})
                save_trip(dest,curr,dep_flight,dep_time)
                reply = f"Overseas mode on ✈️\nDestination: {dest}\nCurrency: {curr}\nI'll log expenses in {curr} with SGD equivalent."
        elif text.strip().upper()=="N":
            overseas_state.pop("_pending_flight",None)
            reply = "Got it — what's your departure date/time, destination, and when are you back in SG?"
        else:
            overseas_state.pop("_pending_flight",None)
            reply = handle_overseas_request(text)
        if reply:
            try: await update.message.reply_text(reply,parse_mode="Markdown")
            except Exception: await update.message.reply_text(reply)
        return

    # -----------------------------------------------------------------------
    # Main routing chain
    # -----------------------------------------------------------------------
    reply = None

    if lower.startswith("save restaurant "):
        reply = _handle_restaurant_result(handle_save_restaurant(text),user_id)
    elif lower.startswith("save ") and not is_restaurant_save(text):
        result = save_contact(text[5:])
        if result.startswith("_DUPLICATE_:"):
            existing_name = result.split(":",1)[1]
            pending_contact_saves[user_id] = {"data":text[5:],"existing_name":existing_name}
            reply = f"*{existing_name}* already exists in your CRM.\nUpdate the existing contact or save as new? (update / new)"
        else:
            reply = result
    elif lower.startswith("find ") or lower.startswith("pull up "):
        reply = find_contact(text[5:] if lower.startswith("find ") else text[8:])
    elif lower.startswith("note "): reply = add_note(text[5:])
    elif lower.startswith("followup "): reply = set_followup(text[9:])
    elif lower.startswith("update ") and not re.match(r"set default card for .+ to .+",lower): reply = update_field(text[7:])
    elif (lower.startswith("delete ") and not lower.startswith("delete event") and not lower.startswith("delete expense")
          and not lower.startswith("delete bill") and not lower.startswith("delete restaurant") and not lower.startswith("delete last")):
        name = text[7:].strip()
        _, record = find_row(name)
        if not record:
            reply = f"No contact found for '{name}'"
        else:
            confirm_name = record.get("Name",name)
            confirm_sessions[user_id] = {"action":"delete_contact","args":[name],"target":f"Delete contact {confirm_name}?"}
            reply = f"Delete contact *{confirm_name}*? (yes / no)"
    elif lower.startswith("search "): reply = search_contacts(text[7:])
    elif lower.startswith("edit ") and not lower.startswith("edit last expense") and not lower.startswith("edit expense"):
        name = text[5:].strip()
        _, record = find_row(name)
        if not record:
            reply = f"❌ No contact found for '{name}'"
        else:
            edit_sessions[user_id] = {"name":record.get("Name"),"step":"choose_field"}
            reply = (f"Editing *{record.get('Name')}*. Which field?\n\n"
                     f"1. Alias\n2. Birthday\n3. Relationship\n4. Context\n5. Notes\n"
                     f"6. Follow up date\n7. Follow up notes\n8. Email\n9. Address\n\nType the field name or *cancel* to exit.")
    elif lower=="cancel":
        for d in [edit_sessions,excel_import_sessions,confirm_sessions,delete_sessions,receipt_confirm_sessions]: d.pop(user_id,None)
        session_timestamps.pop(user_id,None)
        reply = "Cancelled."
    elif lower=="reload profile":
        try: setup_em_profile(); reply = "✅ Profile reloaded successfully."
        except Exception as e: reply = f"⚠️ Couldn't reload profile: {str(e)}"
    elif lower in ["skip missed","dismiss missed","skip all missed"]: reply = "All missed follow-ups dismissed ✅"
    elif lower.startswith("skip ") and not lower.startswith("skip missed bills"): reply = f"Follow-up for {text[5:].strip()} dismissed ✅"
    elif lower in ["skip missed bills","dismiss missed bills"]: reply = "Missed bill reminders dismissed ✅"
    elif lower in ["yes","y","yep","yeah","yup"] and market_summary_pending.get(user_id):
        market_summary_pending.pop(user_id,None); reply = get_market_summary_now()
    elif re.match(r"set default card for .+ to .+",lower):
        m = re.match(r"set default card for (.+?) to (.+)",lower)
        reply = set_card_default_category(m.group(2).strip().title(),m.group(1).strip().title()) if m else "Try: 'set default card for FnB to Maybank'"
    elif lower=="list": reply = list_contacts()
    elif lower=="stats": reply = get_stats()
    elif lower=="followups": reply = upcoming_followups()
    elif lower=="overdue": reply = overdue_followups()
    elif lower=="birthdays": reply = upcoming_birthdays(30)
    elif lower=="soon": reply = upcoming_birthdays(7)
    elif lower.startswith("lastcontact "): reply = last_contact(text[12:])
    elif lower in ["referrals","all referrals","show referrals"]: reply = get_all_referrals()
    elif lower in ["top referrers","best referrers","who refers the most"]: reply = get_top_referrers()
    elif lower.startswith("referrals from ") or lower.startswith("who did "):
        if lower.startswith("referrals from "):
            name = text[15:].strip()
        else:
            m = re.search(r"who did (.+?) refer",lower)
            name = m.group(1).strip().title() if m else text[8:].strip()
        reply = get_referrals_by(name)
    elif "import" in lower and ("excel" in lower or "contacts" in lower or "spreadsheet" in lower):
        excel_import_sessions[user_id] = {"step":"awaiting_columns","column_order":[]}
        reply = "Sure! Tell me the column order in your Excel first — e.g. 'Name, Email, Date of Birth, Alias'"
    elif lower.startswith("search meetings ") or lower.startswith("find meeting "):
        query = text[16:] if lower.startswith("search meetings ") else text[13:]
        reply = search_meeting_notes(query.strip())
    elif lower=="cancel" and user_id in meeting_sessions:
        del meeting_sessions[user_id]; reply = "Recap cancelled."
    elif is_meeting_start(text):
        event_name = extract_event_name(text)
        meeting_sessions[user_id] = {"step":"collecting","event_name":event_name,"notes":[]}
        if event_name:
            reply = f"Got it, taking notes for {event_name}. Send everything over and say done when you're finished."
        else:
            meeting_sessions[user_id]["step"] = "get_name"; reply = "Sure, what's the event name?"
    elif lower in ["reminders","my reminders","list reminders"]: reply = list_reminders()
    elif is_cancel_reminder_request(text):
        keyword = text.lower().replace("cancel","").replace("delete","").replace("remove","").replace("reminder","").strip()
        cancelled = cancel_reminder_by_keyword(keyword) if keyword else []
        if not cancelled:
            reply = "Couldn't find a matching reminder to cancel."
        elif cancelled[0].startswith("_DISAMBIG_:"):
            entries = cancelled[0][len("_DISAMBIG_:"):].split("|")
            lines = ["Found multiple matching reminders — which one to cancel?"]
            for i,entry in enumerate(entries,1):
                parts = entry.split(":",1); lines.append(f"{i}. {parts[1] if len(parts)>1 else entry}")
            lines.append("\nReply with the number or 'all' to cancel all.")
            todo_disambig_sessions[user_id] = {"tasks":[e.split(":")[0] for e in entries],"action":"cancel_reminder","entries":entries}
            reply = "\n".join(lines)
        else:
            reply = f"Cancelled: {', '.join(cancelled)}"
    elif is_reschedule_request(text): reply = handle_reschedule(text,user_id)
    elif is_reminder_request(text): reply = handle_new_reminder(text)
    elif any(lower==p or lower.startswith(p) for p in ["what expense categories","list my categories","what categories","show categories","what are my expense categories","list categories","my expense categories","expense categories"]):
        reply = get_expense_categories()
    elif any(lower==p or lower.startswith(p) for p in ["what merchants","my merchants","merchant map","list merchants","show merchants","what merchants do we have","saved merchants","known merchants"]):
        reply = get_merchant_list()
    elif lower.startswith("delete merchant ") or lower.startswith("remove merchant ") or lower.startswith("forget merchant "):
        reply = delete_merchant(text.split(" ",2)[2].strip())
    elif lower in ["expense report","monthly report","spending report","expenses"]: reply = get_expense_report()
    elif lower in ["delete last expense","remove last expense"]: reply = delete_last_expense()
    elif lower in ["delete expense","remove expense","undo expense","undo last expense"]:
        recent = get_recent_expenses(5)
        if not recent: reply = "No expenses logged yet."
        else:
            delete_sessions[user_id] = {"step":"pick","expenses":recent}; reply = format_delete_list(recent)
    elif lower.startswith("delete expense ") or lower.startswith("remove expense "):
        query = re.sub(r"^(delete|remove) expense\s+","",lower).strip()
        results = search_expenses_by_merchant(query)
        if not results: reply = f"No expenses found matching '{query}'."
        elif len(results)==1: reply = delete_expense_by_row(results[0][0])
        else:
            delete_sessions[user_id] = {"step":"pick","expenses":results}; reply = format_delete_list(results)
    elif lower in ["last expense","show last expense","what did i log"]: reply = show_last_expense()
    elif lower in ["trip summary","trip spend","how much have i spent","trip expenses"]: reply = get_trip_summary()
    elif lower in ["trip history","my trips","past trips","trips"]: reply = format_trip_history()
    elif lower in ["current trip","active trip","am i overseas","overseas status"]:
        if overseas_state.get("active"):
            dest=overseas_state.get("destination","Unknown"); curr=overseas_state.get("currency","SGD")
            dep=overseas_state.get("dep_flight",""); ret_data=overseas_state.get("return_flight")
            ret_flight=ret_data.get("flight","") if isinstance(ret_data,dict) else ""
            reply = f"✈️ Active trip: {dest} ({curr})"
            if dep: reply += f"\nOutbound: {dep}"
            if ret_flight: reply += f"\nReturn: {ret_flight}"
        else:
            reply = "No active trip — you're in SG mode."
    elif lower.startswith("edit last expense ") or lower.startswith("edit expense "):
        edit_text = re.sub(r"^edit (?:last )?expense\s+","",text,flags=re.IGNORECASE).strip()
        reply = edit_last_expense(edit_text) if edit_text else show_last_expense()
    elif lower.startswith("rename category "):
        m = re.search(r"rename category (.+?) to (.+)",lower)
        if m:
            old_cat,new_cat = m.group(1).strip(),m.group(2).strip()
            confirm_sessions[user_id] = {"action":"rename_category","args":[old_cat,new_cat],"target":f"Rename {old_cat} to {new_cat}?"}
            reply = f"Rename category *{old_cat}* to *{new_cat}*? This will update all expense rows and Merchant Map. (yes / no)"
        else:
            reply = "Try: 'rename category FnB to Food'"
    elif lower.startswith("now in ") or lower.startswith("switched to ") or lower.startswith("arrived in "):
        if overseas_state.get("active"):
            dest_text = re.sub(r"^(now in|switched to|arrived in)\s+","",lower).strip().title()
            dest_info = get_dest_info_from_iata("",dest_text)
            new_curr = dest_info.get("currency",""); new_dest = dest_info.get("destination",dest_text)
            if new_curr and new_curr!="SGD":
                overseas_state["currency"] = new_curr; overseas_state["destination"] = new_dest
                if new_curr not in overseas_state["currencies"]: overseas_state["currencies"].append(new_curr)
                if new_dest not in overseas_state["trip_destinations"]: overseas_state["trip_destinations"].append(new_dest)
                get_fx_rate(new_curr)
                try:
                    ws = trips_sheet(); records = ws.get_all_records()
                    for i,row in enumerate(records,start=2):
                        if row.get("Status")=="active": ws.update_cell(i,2,new_dest); ws.update_cell(i,3,new_curr); break
                except Exception as e: print(f"Trips sheet update error: {e}")
                reply = f"Switched to {new_dest} — expenses will now be logged in {new_curr}."
            else:
                reply = handle_overseas_request(text)
        else:
            reply = handle_overseas_request(text)
    elif is_log_prefix_input(text):
        reply,needs_session,session_data = handle_expense_text(text[4:].strip(),user_id)
        if needs_session and session_data: expense_sessions[user_id] = session_data
    elif re.match(r"add return flight\s+([A-Z]{1,3}\d{2,4}[A-Z]?)",text.upper()):
        m = re.match(r"add return flight\s+([A-Z]{1,3}\d{2,4}[A-Z]?)",text.upper())
        ret_num = m.group(1); ret_data = lookup_flight(ret_num)
        if ret_data:
            ret_data["flight"] = ret_num; overseas_state["return_flight"] = ret_data
            reply = f"Return flight added: {ret_num} {format_flight_time(ret_data['dep_time'])} → {format_flight_time(ret_data['arr_time'])} (SIN) ✅"
        else:
            reply = f"Couldn't find {ret_num} on AviationStack. Try again closer to the flight date."
    elif lower in ["bills","my bills","list bills"]: reply = list_bills()
    elif lower.startswith("delete bill "):
        bill_name = text[12:].strip()
        confirm_sessions[user_id] = {"action":"delete_bill","args":[bill_name],"target":f"Delete bill {bill_name}?"}
        reply = f"Delete bill *{bill_name}*? (yes / no)"
    elif is_bill_request(text): reply = handle_new_bill(text)
    elif lower.startswith("delete restaurant ") or lower.startswith("remove restaurant "):
        rest_name = text.split(" ",2)[2].strip()
        confirm_sessions[user_id] = {"action":"delete_restaurant","args":[rest_name],"target":f"Delete restaurant {rest_name}?"}
        reply = f"Delete *{rest_name}* from your restaurant list? (yes / no)"
    elif is_restaurant_search(text): reply = handle_search_restaurants(text)
    elif is_restaurant_save(text): reply = _handle_restaurant_result(handle_save_restaurant(text),user_id)
    elif lower.startswith("search restaurants "): reply = search_restaurants(text[19:].strip())
    elif is_restaurant_suggestion_request(text): reply = get_similar_restaurants(text)
    elif is_restaurant_review_request(text):
        name = re.sub(r"reviews? for|review of|tell me about|how is|how's|what's|what is","",lower).strip().rstrip("?").strip()
        reply = get_restaurant_review(name) if name else "Which restaurant are you asking about?"
    elif lower in ["portfolio","my portfolio","holdings","portfolio performance"]: reply = get_portfolio_performance()
    elif lower in ["delete from portfolio","remove from portfolio","delete holding","remove holding","portfolio delete","clear holding"]:
        rows = get_portfolio_rows()
        if not rows: reply = "Nothing in your portfolio to remove."
        else:
            portfolio_delete_sessions[user_id] = {"step":"pick","rows":rows}; reply = format_portfolio_delete_list(rows)
    elif lower.startswith("delete portfolio ") or lower.startswith("remove portfolio "):
        query = re.sub(r"^(delete|remove) portfolio\s+","",lower).strip().upper()
        results = search_portfolio_by_ticker(query)
        if not results: reply = f"No holdings found matching '{query}'."
        elif len(results)==1: reply = delete_portfolio_row(results[0][0])
        else:
            portfolio_delete_sessions[user_id] = {"step":"pick","rows":results}; reply = format_portfolio_delete_list(results)
    elif lower in ["market summary","market today","how is the market","market update","weekly market summary","how are markets"]:
        reply = get_market_summary_now()
    elif is_stock_request(text):
        result = handle_stock_request(text)
        if result: reply = result
    elif lower.startswith("todo "): reply = add_todo(text[5:])
    elif lower.startswith("done "):
        result = complete_todo(text[5:])
        if result.startswith("_DISAMBIG_TODO_COMPLETE_:"):
            tasks = result.split(":",1)[1].split("|")
            todo_disambig_sessions[user_id] = {"tasks":tasks,"action":"complete"}
            reply = "\n".join(["Found multiple matching tasks — which one?"]+[f"{i+1}. {t}" for i,t in enumerate(tasks)]+["\nReply with the number."])
        else: reply = result
    elif lower.startswith("delete todo ") or lower.startswith("remove todo "):
        task_name = re.sub(r"^(delete|remove) todo\s+","",lower).strip()
        result = delete_todo(task_name)
        if result.startswith("_DISAMBIG_TODO_DELETE_:"):
            tasks = result.split(":",1)[1].split("|")
            todo_disambig_sessions[user_id] = {"tasks":tasks,"action":"delete"}
            reply = "\n".join(["Found multiple matching tasks — which one?"]+[f"{i+1}. {t}" for i,t in enumerate(tasks)]+["\nReply with the number."])
        else: reply = result
    elif lower=="todos": reply = list_todos()
    elif lower.startswith("add event") or lower.startswith("schedule ") or lower.startswith("create event"):
        reply = smart_add_event(text,user_id)
    elif lower=="events today": reply = get_events(1)
    elif lower=="events week": reply = get_events(7)
    elif lower.startswith("delete event "):
        event_name = text[13:].strip()
        confirm_sessions[user_id] = {"action":"delete_event","args":[event_name],"target":f"Delete event {event_name}?"}
        reply = f"Delete event *{event_name}*? (yes / no)"
    elif lower in ("em whats pending","em what's pending","em pending","whats pending"):
        reply = get_pending_backlog()
    elif lower=="em status":
        issues = []
        if not DRIVE_FOLDERS: issues.append("• Google Drive: not connected — check GOOGLE_CREDENTIALS in Railway")
        try:
            from sheets import spreadsheet; spreadsheet.worksheet("Expenses")
        except Exception as e: issues.append(f"• Sheets: connection error — {str(e)[:60]}")
        if not _config._scheduler or not _config._scheduler.running:
            issues.append("• Scheduler: not running — reminders and scheduled jobs are down, restart the bot")
        if not _config.em_profile or not _config.em_profile.get("version"):
            issues.append("• Profile: not loaded — reply 'reload profile' to retry")
        try: get_calendar("Personal")
        except Exception as e: issues.append(f"• iCloud Calendar: unreachable — check ICLOUD_USERNAME / ICLOUD_PASSWORD in Railway")
        if _anthropic_failure_count >= ANTHROPIC_FAILURE_THRESHOLD:
            issues.append("• Anthropic API: repeated failures detected — check API key or Anthropic status page")
        reply = "✅ Systems all green" if not issues else "⚠️ Issues detected:\n\n"+"\n".join(issues)
    elif lower=="help":
        reply = ("🤖 *Em — here's what I can do:*\n\n*CRM:*\nsave, find, note, followup, update, edit, delete, search, list, stats, followups, overdue, birthdays, soon, lastcontact\n"
                 "referrals, all referrals, top referrers, referrals from [name]\nimport excel — import contacts from a spreadsheet\n\n"
                 "*Calendar:*\nJust tell me naturally — 'schedule dinner tomorrow 7pm' or 'add event'\nevents today / events week / delete event\n\n"
                 "*To-Do:*\ntodo, done, todos\n\n*Other:*\nem status — check Em's health\n\nOr just chat — I'll figure it out 👍")
    elif (crm_action := detect_crm_natural_update(text)):
        action,name,field_or_referred,value = crm_action
        if action=="referral": reply = set_referral(name,field_or_referred)
        elif action=="update": reply = update_contact_field_natural(name,field_or_referred,value)
        elif action=="show_private": reply = find_contact(name,show_private=True)
    elif is_overseas_mode_request(text) or extract_flight_number(text):
        if not extract_flight_number(text) and user_id in conversation_histories:
            history_text = " ".join(m["content"] for m in conversation_histories[user_id][-10:] if m["role"]=="user")
            found_flights = extract_all_flight_numbers(history_text)
            reply = handle_overseas_request(" ".join(found_flights)+" "+text) if found_flights else handle_overseas_request(text)
        else:
            reply = handle_overseas_request(text)
    elif is_expense_input(text):
        reply,needs_session,session_data = handle_expense_text(text,user_id)
        if needs_session and session_data: expense_sessions[user_id] = session_data
    elif is_bare_merchant_input(text):
        reply,needs_session,session_data = handle_expense_text(text,user_id)
        if needs_session and session_data: expense_sessions[user_id] = session_data
    elif is_calendar_request(text):
        reply = smart_add_event(text,user_id)
    else:
        if user_id not in conversation_histories: conversation_histories[user_id] = []
        conversation_histories[user_id].append({"role":"user","content":text})
        if len(conversation_histories[user_id])>20: conversation_histories[user_id] = conversation_histories[user_id][-20:]
        overseas_key = (overseas_state.get("active"),overseas_state.get("destination"),overseas_state.get("currency"))
        if _config._system_prompt_cache is None or _config._system_prompt_overseas_key!=overseas_key:
            _config._system_prompt_cache = build_system_prompt()
            _config._system_prompt_overseas_key = overseas_key
        response = client.messages.create(model="claude-sonnet-4-6",max_tokens=1024,system=_config._system_prompt_cache,messages=conversation_histories[user_id])
        reply = response.content[0].text
        conversation_histories[user_id].append({"role":"assistant","content":reply})

    if not reply:
        try:
            fb = client.messages.create(model="claude-sonnet-4-6",max_tokens=300,
                system=("You are Em, a personal assistant bot. The user sent a message no handler recognised. "
                        "Acknowledge in one casual sentence, then suggest one concrete thing the developer could add. 2-3 sentences max. No bullet points."),
                messages=[{"role":"user","content":text}])
            reply = fb.content[0].text
        except Exception as e:
            print(f"Fallback Claude call failed: {e}")
            reply = "Not sure how to handle that one — might be worth adding a handler for it in the bot code."

    if reply:
        await send_safe(update.message, reply, parse_mode="Markdown")
