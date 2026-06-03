import re
import io
import json
import asyncio
from datetime import date, datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes

import state
from config import (
    EXPENSE_CATEGORIES, EXPENSE_CARDS, YOUR_CHAT_ID, TIMEZONE,
    AVIATIONSTACK_API_KEY, TEXT_SHORTCUTS
)
from clients import client, drive_service
from sheets import get_sheet, get_pending_backlog, append_bug_to_backlog
from helpers import send_safe, looks_like_new_intent, format_date
from crm import (
    find_row, find_all_rows, save_contact, find_contact, add_note, set_followup,
    update_field, update_contact_field_natural, delete_contact, search_contacts,
    list_contacts, get_stats, upcoming_followups, overdue_followups,
    upcoming_birthdays, last_contact, set_referral, get_referrals_by,
    get_all_referrals, get_top_referrers, parse_excel_column_order,
    handle_excel_import, check_birthday_acknowledgement, detect_crm_natural_update,
    send_followup_reminders, send_birthday_reminders, send_birthday_followups,
    persist_birthday_pending, load_birthday_pending_from_sheet,
    auto_clear_birthday_pending, mark_birthday_not_sent
)
from expenses import (
    get_cards_live, get_card_names_live, set_card_default_category,
    get_merchant_memory, parse_expense_text, handle_expense_text,
    handle_expense_session, handle_receipt_confirm_session,
    get_expense_categories, get_merchant_list, delete_merchant,
    get_expense_report, delete_last_expense, get_recent_expenses,
    format_delete_list, search_expenses_by_merchant, delete_expense_by_row,
    show_last_expense, get_trip_summary, edit_last_expense, rename_category,
    is_expense_input, is_log_prefix_input, is_bare_merchant_input
)
from fx import (
    get_fx_rate, refresh_fx_rates, persist_fx_rates_to_sheet,
    load_fx_rates_from_sheet
)
from reminders import (
    list_reminders, is_reminder_request, is_reschedule_request,
    is_cancel_reminder_request, handle_new_reminder, handle_reschedule,
    cancel_reminder_by_keyword, check_and_fire_reminders
)
from cal import (
    get_events, get_events_for_date, get_events_for_date_range,
    delete_calendar_event, is_calendar_request,
    smart_add_event, edit_calendar_event, apply_calendar_edit,
    find_upcoming_events, _fmt_event_row, complete_smart_add
)
from todos import add_todo, complete_todo, delete_todo, list_todos
from meetings import (
    is_meeting_start, extract_event_name, handle_meeting_session,
    search_meeting_notes
)
from bills import (
    list_bills, delete_bill, is_bill_request, handle_new_bill,
    send_bill_reminders
)
from restaurants import (
    save_restaurant, format_restaurant_saved, delete_restaurant,
    is_restaurant_save, is_restaurant_search, is_restaurant_review_request,
    is_restaurant_suggestion_request, get_restaurant_review, get_similar_restaurants,
    handle_save_restaurant, handle_search_restaurants
)
from stocks import (
    get_portfolio_rows, format_portfolio_delete_list, delete_portfolio_row,
    search_portfolio_by_ticker, get_portfolio_performance, is_stock_request,
    handle_stock_request, get_market_summary_now, send_weekly_market_summary,
    check_price_alerts, handle_statement_upload, persist_price_alerts_to_sheet,
    load_price_alerts_from_sheet
)
from trips import (
    save_trip, close_trip, get_active_trip, restore_overseas_from_trips,
    format_trip_history, extract_flight_number, _parse_trip_dates,
    _get_currency_for_dest, is_overseas_mode_request, deactivate_overseas_mode,
    activate_overseas_mode_scheduled, deactivate_and_notify, handle_overseas_request,
    persist_trip_setup, load_trip_setup_from_sheet
)
from sessions import (
    touch_session, is_session_expired, check_session_timeouts,
    get_active_session_label, persist_sessions_to_sheet, load_sessions_from_sheet
)
from infrastructure import (
    run_infrastructure_setup, send_startup_message, save_em_profile,
    setup_em_profile, track_anthropic_call, notify_anthropic_down,
    get_or_create_drive_folder, RECEIPTS_FOLDER_ID
)


def build_system_prompt():
    profile_notes = ""
    if state.em_profile:
        forbidden = ", ".join(state.em_profile.get("forbidden_phrases", []))
        profile_notes = f"\nForbidden phrases (never use): {forbidden}" if forbidden else ""
    overseas_context = ""
    if state.overseas_state.get("active"):
        dest = state.overseas_state.get("destination", "")
        curr = state.overseas_state.get("currency", "SGD")
        trip_start = state.overseas_state.get("trip_start", "")
        currencies = state.overseas_state.get("currencies", [])
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
    today_str = date.today().strftime("%A, %d %b %Y")
    return (
        f"## Today\nToday is {today_str}.\n\n"
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
    ) + overseas_context + expense_context


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != YOUR_CHAT_ID:
        return
    try:
        await _handle_message_inner(update, context, user_id)
    except Exception as e:
        import traceback
        err_type = type(e).__name__
        err_msg = str(e)[:120]
        tb_lines = traceback.format_exc().splitlines()
        location = next((l.strip() for l in reversed(tb_lines) if "routing.py" in l or "bot.py" in l), "")
        detail = f"{err_type}: {err_msg}"
        if location:
            detail += f"\n({location})"
        print(f"UNHANDLED handle_message error: {traceback.format_exc()}")
        try:
            await update.message.reply_text(f"❌ Something went wrong: {detail}")
        except Exception:
            pass


async def _handle_message_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    # ── Document handler ──────────────────────────────────────────────────────
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
                auto_cols = [str(c).strip().replace('\xa0', '') for c in first_row if c] if first_row else []
            except Exception as e:
                print(f"Excel header auto-detect error: {e}")
                auto_cols = []
            if auto_cols:
                col_str = ", ".join(auto_cols)
                await update.message.reply_text(f"Detected columns: {col_str}\nImporting now...")
                await handle_excel_import(file_bytes, auto_cols, update)
            elif user_id in state.excel_import_sessions and state.excel_import_sessions[user_id].get("step") == "awaiting_file":
                col_order = state.excel_import_sessions[user_id].get("column_order", [])
                del state.excel_import_sessions[user_id]
                await handle_excel_import(file_bytes, col_order, update)
            else:
                state.excel_import_sessions[user_id] = {"step": "awaiting_columns", "file_bytes": file_bytes}
                await update.message.reply_text(
                    "Got the file but couldn't read its headers. Tell me the column order — "
                    "e.g. 'Name, Alias, Email, Date of Birth'"
                )
        else:
            await update.message.reply_text("I can only import .xlsx or .xls files for CRM.")
        return

    # ── Photo handler ─────────────────────────────────────────────────────────
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
            receipts_root = state.DRIVE_FOLDERS.get("receipts", "")
            if receipts_root:
                month_folder_id = get_or_create_drive_folder(month_folder_name, receipts_root)
                temp_name = f"{today_str}-receipt-{photo.file_id[:8]}.jpg"
                media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
                file_meta = {"name": temp_name, "parents": [month_folder_id]}
                uploaded = drive_service.files().create(
                    body=file_meta, media_body=media, fields="id,webViewLink",
                    supportsAllDrives=True
                ).execute()
                drive_file_id = uploaded.get("id", "")
                receipt_link = uploaded.get("webViewLink", "")
        except Exception as e:
            print(f"Receipt upload error: {e}")

        def rename_receipt_in_drive(merchant_name):
            if not drive_file_id or not merchant_name:
                return
            try:
                safe_merchant = re.sub(r"[^a-zA-Z0-9\-_]", "", merchant_name.replace(" ", "-").lower())
                new_name = f"{today_str}-{safe_merchant}.jpg"
                drive_service.files().update(
                    fileId=drive_file_id, body={"name": new_name}, supportsAllDrives=True
                ).execute()
            except Exception as e:
                print(f"Receipt rename error: {e}")

        if not caption:
            await update.message.reply_text("Got the receipt 🧾 Reading it now...")
            try:
                import base64
                img_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
                curr = state.overseas_state.get("currency", "SGD") if state.overseas_state.get("active") else "SGD"
                vision_resp = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=300,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                            {
                                "type": "text",
                                "text": (
                                    f"This is a receipt. Extract: merchant name, total amount, and currency (default {curr} if not shown). "
                                    "For the merchant name, copy it exactly as printed at the very top of the receipt — including any numbers or prefixes (e.g. '108 Matcha Saro', not 'Matcha Saro'). "
                                    "Do not shorten, capitalise, or summarise — copy the exact name character by character. "
                                    "For amount, use the final total (Net Total, Total, Grand Total — not subtotal). "
                                    "Reply ONLY in this format with no other text: MERCHANT | AMOUNT | CURRENCY\n"
                                    "Example: 108 Matcha Saro | 85.70 | MYR"
                                )
                            }
                        ]
                    }]
                )
                vision_text = vision_resp.content[0].text.strip()
                parts = [p.strip() for p in vision_text.split("|")]
                if len(parts) == 3:
                    merchant_v, amount_v, currency_v = parts
                    rename_receipt_in_drive(merchant_v)
                    synthesized = f"{merchant_v} {amount_v} {currency_v}"
                    reply, needs_session, session_data = handle_expense_text(synthesized, user_id, receipt_link=receipt_link)
                    if needs_session and session_data:
                        state.expense_sessions[user_id] = session_data
                    if reply:
                        await send_safe(update.message, reply, parse_mode="Markdown")
                else:
                    await update.message.reply_text(f"Read the receipt but got an unexpected format from vision — got: '{vision_text}'\nTry adding a caption like '45.50 Ichiran'.")
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
                state.expense_sessions[user_id] = session_data
            if reply:
                await send_safe(update.message, reply, parse_mode="Markdown")
        else:
            await update.message.reply_text("Got the receipt but couldn't read that as an expense. Try a caption like '1400 Ichiran' or 'spent 45 at Uniqlo'.")
        return

    text = update.message.text.strip()
    # Text shortcut expansion — first word only, word-boundary replacement
    _words = text.split()
    if _words and _words[0].lower() in TEXT_SHORTCUTS:
        _words[0] = TEXT_SHORTCUTS[_words[0].lower()]
        text = " ".join(_words)
    lower = text.lower()

    # ── DND intercept ─────────────────────────────────────────────────────────
    if state.em_profile.get("dnd_active") and lower not in ("dnd off", "dnd on"):
        held = state.em_profile.get("dnd_held_messages", [])
        held.append({"text": text, "time": datetime.now(TIMEZONE).strftime("%H:%M")})
        state.em_profile["dnd_held_messages"] = held
        save_em_profile()
        return

    # ── Birthday acknowledgement ───────────────────────────────────────────────
    bday_handled, bday_reply = check_birthday_acknowledgement(text)
    if bday_handled:
        if bday_reply:
            await update.message.reply_text(bday_reply)
        return

    # ── Excel import column declaration ───────────────────────────────────────
    if user_id in state.excel_import_sessions:
        session = state.excel_import_sessions[user_id]
        if session.get("step") == "awaiting_columns":
            cols = parse_excel_column_order(text)
            if cols:
                if session.get("file_bytes"):
                    file_bytes = session["file_bytes"]
                    del state.excel_import_sessions[user_id]
                    await update.message.reply_text(f"Got it — columns: {', '.join(cols)}. Importing now...")
                    await handle_excel_import(file_bytes, cols, update)
                else:
                    session["column_order"] = cols
                    session["step"] = "awaiting_file"
                    await update.message.reply_text(f"Got it — columns: {', '.join(cols)}. Now send the Excel file.")
            else:
                await update.message.reply_text("Couldn't parse that. Try: 'Name, Email, Date of Birth, Alias'")
            return

    # ── Session timeouts ──────────────────────────────────────────────────────
    if any(user_id in s for s in [state.expense_sessions, state.delete_sessions, state.portfolio_delete_sessions,
                                    state.confirm_sessions, state.receipt_confirm_sessions, state.edit_sessions,
                                    state.meeting_sessions, state.pending_restaurant_saves,
                                    state.calendar_confirm_sessions]):
        if is_session_expired(user_id):
            await check_session_timeouts(user_id, update)
            return

    # ── Session interrupt guard ───────────────────────────────────────────────
    active_label = get_active_session_label(user_id)
    if active_label and looks_like_new_intent(text):
        if user_id in state.interrupted_sessions:
            pending = state.interrupted_sessions[user_id]
            if text.strip().lower() in ["yes", "y"]:
                for d in [state.receipt_confirm_sessions, state.expense_sessions, state.meeting_sessions,
                          state.edit_sessions, state.confirm_sessions, state.delete_sessions,
                          state.portfolio_delete_sessions, state.excel_import_sessions,
                          state.pending_restaurant_saves, state.pending_contact_saves,
                          state.todo_disambig_sessions, state.calendar_confirm_sessions]:
                    d.pop(user_id, None)
                state.session_timestamps.pop(user_id, None)
                del state.interrupted_sessions[user_id]
                update.message.text = pending["pending_text"]
                text = pending["pending_text"]
                lower = text.lower()
            elif text.strip().lower() in ["no", "n"]:
                del state.interrupted_sessions[user_id]
                await update.message.reply_text(f"Got it — continuing with your {pending['label']}.")
                return
            else:
                await update.message.reply_text(f"Reply yes to switch, or no to continue with your {pending['label']}.")
                return
        else:
            if user_id not in state.interrupted_sessions:
                state.interrupted_sessions[user_id] = {"label": active_label, "pending_text": text}
            await update.message.reply_text(
                f"You're mid-{active_label} — did you mean to do something else?\n"
                f"Reply yes to switch, or no to continue."
            )
            return

    # ── Pending restaurant saves ──────────────────────────────────────────────
    if user_id in state.pending_restaurant_saves:
        prs = state.pending_restaurant_saves[user_id]
        step = prs.get("step")
        if step == "awaiting_location":
            location = text.strip()
            del state.pending_restaurant_saves[user_id]
            result = save_restaurant(prs["name"], location, prs.get("country", "Singapore"))
            if result and result.startswith("_DUPLICATE_:"):
                state.pending_restaurant_saves[user_id] = {
                    "step": "duplicate", "existing": prs["name"],
                    "name": prs["name"], "location": location,
                    "country": prs.get("country", "Singapore"), "tags": "", "notes": ""
                }
                await update.message.reply_text(f"*{prs['name']}* is already in your list. Update or save as new? (update / new)")
            else:
                await update.message.reply_text(format_restaurant_saved(prs["name"], location))
            return
        elif step == "awaiting_confirm":
            extra_tags = ""
            words = lower.split(None, 1)
            first_word = words[0] if words else ""
            rest = words[1] if len(words) > 1 else ""
            if first_word in ["yes", "y"]:
                extra_tags = rest.strip()
                merged_tags = ", ".join(filter(None, [prs.get("tags", ""), extra_tags]))
                location = prs.get("location", "")
                del state.pending_restaurant_saves[user_id]
                result = save_restaurant(prs["name"], location, prs.get("country", "Singapore"), merged_tags)
                if result and result.startswith("_DUPLICATE_:"):
                    state.pending_restaurant_saves[user_id] = {
                        "step": "duplicate", "existing": prs["name"],
                        "name": prs["name"], "location": location,
                        "country": prs.get("country", "Singapore"), "tags": merged_tags, "notes": ""
                    }
                    await update.message.reply_text(f"*{prs['name']}* is already in your list. Update or save as new? (update / new)")
                else:
                    await update.message.reply_text(format_restaurant_saved(prs["name"], location, merged_tags))
            elif first_word in ["no", "n"]:
                del state.pending_restaurant_saves[user_id]
                await update.message.reply_text(f"Got it — what's the correct location for {prs['name']}?")
                state.pending_restaurant_saves[user_id] = {"name": prs["name"], "country": prs.get("country", "Singapore"), "step": "awaiting_location"}
            else:
                await update.message.reply_text("Reply yes to confirm or no to correct the location.")
            return
        elif step == "awaiting_outlet":
            outlets = prs.get("outlets", [])
            words = lower.split(None, 1)
            first_word = words[0].strip().rstrip(",")
            rest = words[1].strip() if len(words) > 1 else ""
            try:
                idx = int(first_word) - 1
                if 0 <= idx < len(outlets):
                    location = outlets[idx]
                    merged_tags = ", ".join(filter(None, [prs.get("tags", ""), rest]))
                    del state.pending_restaurant_saves[user_id]
                    result = save_restaurant(prs["name"], location, prs.get("country", "Singapore"), merged_tags)
                    if result and result.startswith("_DUPLICATE_:"):
                        state.pending_restaurant_saves[user_id] = {
                            "step": "duplicate", "existing": prs["name"],
                            "name": prs["name"], "location": location,
                            "country": prs.get("country", "Singapore"), "tags": merged_tags, "notes": ""
                        }
                        await update.message.reply_text(f"*{prs['name']}* is already in your list. Update or save as new? (update / new)")
                    else:
                        await update.message.reply_text(format_restaurant_saved(prs["name"], location, merged_tags))
                else:
                    await update.message.reply_text(f"Pick a number between 1 and {len(outlets)}.")
            except ValueError:
                await update.message.reply_text(f"Reply with the number of the outlet (1–{len(outlets)}).")
            return
        elif step == "duplicate":
            if lower in ["new", "save new"]:
                del state.pending_restaurant_saves[user_id]
                save_restaurant(prs["name"], prs["location"], prs.get("country", "Singapore"),
                                prs.get("tags", ""), prs.get("notes", ""), force_new=True)
                await update.message.reply_text(format_restaurant_saved(prs["name"], prs["location"], prs.get("tags", "")))
            elif lower in ["update", "update existing"]:
                del state.pending_restaurant_saves[user_id]
                await update.message.reply_text(f"Use 'edit restaurant {prs['existing']}' to update it.")
            elif lower in ["skip", "s", "cancel", "no", "n", "nope", "nah"]:
                del state.pending_restaurant_saves[user_id]
                await update.message.reply_text("Skipped.")
            else:
                await update.message.reply_text("Reply 'update', 'new', or 'skip'.")
            return

    # ── Pending contact saves ─────────────────────────────────────────────────
    if user_id in state.pending_contact_saves:
        pcs = state.pending_contact_saves[user_id]
        if lower in ["new", "save new", "save as new"]:
            del state.pending_contact_saves[user_id]
            reply = save_contact(pcs["data"], force_new=True)
            await send_safe(update.message, reply, parse_mode="Markdown")
        elif lower in ["update", "update existing"]:
            del state.pending_contact_saves[user_id]
            reply = f"Opening {pcs['existing_name']} for editing — use 'edit {pcs['existing_name']}' to update fields."
            await send_safe(update.message, reply, parse_mode="Markdown")
        elif lower in ["skip", "s", "cancel", "no", "n", "nope", "nah"]:
            del state.pending_contact_saves[user_id]
            await update.message.reply_text("Skipped.")
        else:
            await update.message.reply_text(f"*{pcs['existing_name']}* already exists. Reply 'update', 'new', or 'skip'.")
        return

    # ── Todo disambiguation ───────────────────────────────────────────────────
    if user_id in state.todo_disambig_sessions:
        td = state.todo_disambig_sessions[user_id]
        tasks = td.get("tasks", [])
        action = td.get("action", "complete")
        if text.strip().isdigit():
            idx = int(text.strip()) - 1
            if 0 <= idx < len(tasks):
                task_name = tasks[idx]
                del state.todo_disambig_sessions[user_id]
                from sheets import todo_sheet
                if action == "complete":
                    result = complete_todo(task_name)
                    await send_safe(update.message, result, parse_mode="MarkdownV2")
                    return
                elif action == "delete":
                    sheet = todo_sheet()
                    records = sheet.get_all_records()
                    for i, r in enumerate(records):
                        if r.get("Task", "") == task_name:
                            sheet.delete_rows(i + 2)
                            await update.message.reply_text(f"Deleted — {task_name} ✅")
                            return
            else:
                await update.message.reply_text("Invalid number. Try again or 'cancel'.")
        elif lower == "cancel":
            del state.todo_disambig_sessions[user_id]
            await update.message.reply_text("Cancelled.")
        else:
            await update.message.reply_text("Reply with a number or 'cancel'.")
        return

    # ── Active session handlers ───────────────────────────────────────────────
    if user_id in state.receipt_confirm_sessions:
        touch_session(user_id)
        await handle_receipt_confirm_session(user_id, text, update)
        return
    if user_id in state.meeting_sessions:
        touch_session(user_id)
        await handle_meeting_session(user_id, text, update)
        return
    if user_id in state.expense_sessions:
        touch_session(user_id)
        await handle_expense_session(user_id, text, update)
        return

    # ── Delete session ────────────────────────────────────────────────────────
    if user_id in state.delete_sessions:
        session = state.delete_sessions[user_id]
        step = session.get("step")
        if lower in ["cancel", "nevermind", "never mind", "nvm"]:
            del state.delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return
        if step == "pick":
            if text.strip().isdigit():
                idx = int(text.strip()) - 1
                expenses = session.get("expenses", [])
                if 0 <= idx < len(expenses):
                    sheet_row, _ = expenses[idx]
                    reply = delete_expense_by_row(sheet_row)
                    del state.delete_sessions[user_id]
                    await send_safe(update.message, reply, parse_mode="Markdown")
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
                    del state.delete_sessions[user_id]
                    await send_safe(update.message, reply, parse_mode="Markdown")
                else:
                    state.delete_sessions[user_id] = {"step": "pick", "expenses": results}
                    await update.message.reply_text(format_delete_list(results))
                return
            elif lower in ["not there", "not here", "none of these", "not in the list"]:
                await update.message.reply_text("Reply 'search [merchant name]' to find it.")
                return
            else:
                await update.message.reply_text("Reply with a number, 'search [merchant]', or 'cancel'.")
                return

    # ── Confirm session ───────────────────────────────────────────────────────
    if user_id in state.confirm_sessions:
        touch_session(user_id)
        cs = state.confirm_sessions[user_id]
        action = cs.get("action")
        args = cs.get("args", [])
        if lower in ["yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay"]:
            del state.confirm_sessions[user_id]
            state.session_timestamps.pop(user_id, None)
            if action == "delete_contact":
                reply = delete_contact(args[0])
            elif action == "rename_category":
                reply = rename_category(args[0], args[1])
            elif action == "delete_bill":
                reply = delete_bill(args[0])
            elif action == "delete_restaurant":
                reply = delete_restaurant(args[0])
            elif action == "delete_event":
                reply = await delete_calendar_event(args[0])
            else:
                reply = "Done."
            await send_safe(update.message, reply, parse_mode="Markdown")
        elif lower in ["no", "n", "cancel", "nope", "nah"]:
            del state.confirm_sessions[user_id]
            state.session_timestamps.pop(user_id, None)
            await update.message.reply_text("Cancelled.")
        else:
            await update.message.reply_text(f"{cs.get('target', 'Confirm?')} (yes / no)")
        return

    # ── Portfolio delete session ──────────────────────────────────────────────
    if user_id in state.portfolio_delete_sessions:
        touch_session(user_id)
        pd_session = state.portfolio_delete_sessions[user_id]
        if lower in ["cancel", "nevermind", "nvm"]:
            del state.portfolio_delete_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return
        if text.strip().isdigit():
            idx = int(text.strip()) - 1
            rows = pd_session.get("rows", [])
            if 0 <= idx < len(rows):
                sheet_row, _ = rows[idx]
                del state.portfolio_delete_sessions[user_id]
                result = delete_portfolio_row(sheet_row)
                await update.message.reply_text(result)
            else:
                await update.message.reply_text("Invalid number. Try again or 'cancel'.")
            return
        elif lower.startswith("search "):
            query = text[7:].strip()
            results = search_portfolio_by_ticker(query)
            if not results:
                await update.message.reply_text(f"No holdings found matching '{query}'.")
            elif len(results) == 1:
                sheet_row, _ = results[0]
                del state.portfolio_delete_sessions[user_id]
                result = delete_portfolio_row(sheet_row)
                await update.message.reply_text(result)
            else:
                state.portfolio_delete_sessions[user_id] = {"step": "pick", "rows": results}
                await update.message.reply_text(format_portfolio_delete_list(results))
            return
        else:
            await update.message.reply_text("Reply with a number, 'search [ticker]', or 'cancel'.")
            return

    # ── CRM edit session ──────────────────────────────────────────────────────
    if user_id in state.edit_sessions:
        session = state.edit_sessions[user_id]
        step = session["step"]
        fields = ["alias", "birthday", "relationship", "context", "notes",
                  "follow up date", "follow up notes", "email", "address"]
        if step == "choose_field":
            field = text.lower().strip()
            if field == "cancel":
                del state.edit_sessions[user_id]
                await update.message.reply_text("Cancelled.")
                return
            if field not in fields:
                await update.message.reply_text(
                    f"Pick a field to edit:\n1. Alias\n2. Birthday\n3. Relationship\n4. Context\n"
                    f"5. Notes\n6. Follow up date\n7. Follow up notes\n8. Email\n9. Address\n\n"
                    f"Or type *cancel* to exit.",
                    parse_mode="Markdown"
                )
                return
            session["field"] = field
            session["step"] = "enter_value"
            await update.message.reply_text(f"Enter the new value for *{field.title()}*:", parse_mode="Markdown")
        elif step == "enter_value":
            field = session["field"]
            name = session["name"]
            result = update_field(f"{name}, {field}, {text}")
            del state.edit_sessions[user_id]
            await update.message.reply_text(result, parse_mode="Markdown")
        return

    # ── Reconciliation session ────────────────────────────────────────────────
    if user_id in state.recon_sessions:
        touch_session(user_id)
        rs = state.recon_sessions[user_id]
        step = rs.get("step")
        unmatched = rs.get("unmatched", [])
        idx = rs.get("index", 0)
        if lower in ["done", "skip all", "close"]:
            del state.recon_sessions[user_id]
            await update.message.reply_text("Reconciliation session closed ✅")
        elif lower.startswith("log "):
            log_text = text[4:].strip()
            reply_str, needs_session, session_data = handle_expense_text(log_text, user_id)
            if needs_session and session_data:
                state.expense_sessions[user_id] = session_data
            del state.recon_sessions[user_id]
            await send_safe(update.message, reply_str, parse_mode="Markdown")
        elif lower in ["skip", "next", "s"]:
            rs["index"] = idx + 1
            if rs["index"] < len(unmatched):
                await update.message.reply_text(
                    f"Next unmatched ({rs['index']+1}/{len(unmatched)}):\n{unmatched[rs['index']]}\n\nReply 'log [expense]' to log, 'skip' for next, or 'done' to close."
                )
            else:
                del state.recon_sessions[user_id]
                await update.message.reply_text("All unmatched items reviewed ✅")
        else:
            if idx < len(unmatched):
                await update.message.reply_text(
                    f"Unmatched item ({idx+1}/{len(unmatched)}):\n{unmatched[idx]}\n\nReply 'log [expense]' to log it, 'skip' for next, or 'done' to close."
                )
            else:
                del state.recon_sessions[user_id]
                await update.message.reply_text("All unmatched items reviewed ✅")
        return

    # ── Calendar confirm session (edit field prompt + multi-match picker) ────────
    if user_id in state.calendar_confirm_sessions:
        touch_session(user_id)
        cs = state.calendar_confirm_sessions[user_id]
        step = cs.get("step")

        if lower in ["cancel", "nevermind", "nvm"]:
            del state.calendar_confirm_sessions[user_id]
            state.session_timestamps.pop(user_id, None)
            await update.message.reply_text("Cancelled.")
            return

        if step == "overlap_confirm":
            parsed = cs.get("parsed", {})
            del state.calendar_confirm_sessions[user_id]
            state.session_timestamps.pop(user_id, None)
            if lower in ["yes", "y"]:
                reply = await complete_smart_add(parsed, user_id)
            else:
                title = parsed.get("title", "event")
                reply = f"Cancelled — *{title}* not added."
            await send_safe(update.message, reply, parse_mode="Markdown")
            return

        if step == "awaiting_edit_field":
            # User replied with field to edit — use stored matched event if available
            matched_meta = cs.get("matched_meta")
            matched_summary = cs.get("matched_summary", "")
            matched_dtstart = cs.get("matched_dtstart")
            matched_dtend = cs.get("matched_dtend")
            del state.calendar_confirm_sessions[user_id]
            state.session_timestamps.pop(user_id, None)
            if matched_meta:
                # Reconstruct edit command and apply directly using stored event
                event_query = cs.get("event_query", "")
                reconstructed = f"edit cal {event_query} {text.strip()}"
                reply = await edit_calendar_event(reconstructed, user_id)
            else:
                event_query = cs.get("event_query", "")
                cal_filter = cs.get("cal_filter", "")
                cal_part = f" {cal_filter}" if cal_filter else ""
                reconstructed = f"edit cal {event_query}{cal_part} {text.strip()}"
                reply = await edit_calendar_event(reconstructed, user_id)
            await send_safe(update.message, reply, parse_mode="Markdown")
            return

        if step == "pick_edit":
            awaiting_field = cs.get("awaiting_field", False)
            if text.strip().isdigit():
                idx = int(text.strip()) - 1
                matches = cs.get("edit_matches", [])
                if 0 <= idx < len(matches):
                    meta, summary, dtstart, dtend = matches[idx]
                    if awaiting_field:
                        # No change specified yet — prompt for field now
                        state.calendar_confirm_sessions[user_id] = {
                            "step": "awaiting_edit_field",
                            "event_query": cs.get("event_query", ""),
                            "cal_filter": "",
                            "matched_meta": meta,
                            "matched_summary": summary,
                            "matched_dtstart": dtstart,
                            "matched_dtend": dtend,
                        }
                        await update.message.reply_text(f"What would you like to edit for *{summary}*?\n(time / date / calendar / location / title)", parse_mode="Markdown")
                    else:
                        field = cs.get("field")
                        value = cs.get("value")
                        date_val = cs.get("date_val")
                        time_range_val = cs.get("time_range_val")
                        del state.calendar_confirm_sessions[user_id]
                        state.session_timestamps.pop(user_id, None)
                        from cal import _apply_event_edit
                        reply = await _apply_event_edit(meta, summary, field, value, dtstart, dtend, date_val=date_val, time_range_val=time_range_val)
                        await send_safe(update.message, reply, parse_mode="Markdown")
                else:
                    await update.message.reply_text("Invalid number — pick from the list or 'cancel'.")
            elif cs.get("capped") and lower == "more":
                event_query = cs.get("event_query", "")
                del state.calendar_confirm_sessions[user_id]
                state.session_timestamps.pop(user_id, None)
                reply = await edit_calendar_event(f"edit cal {event_query} expand", user_id)
                await send_safe(update.message, reply, parse_mode="Markdown")
            else:
                await update.message.reply_text("Reply with a number to pick an event, or 'cancel'.")
            return

    # ── Trip setup session (awaiting missing fields only) ─────────────────────
    if state.overseas_state.get("_trip_setup"):
        ts = state.overseas_state["_trip_setup"]
        t = text.strip()

        # Cancel escape
        if t.lower() in ("cancel", "nevermind", "nvm", "stop", "abort"):
            state.overseas_state.pop("_trip_setup", None)
            persist_trip_setup()
            await update.message.reply_text("Trip setup cancelled.")
            return

        # Silent termination on new intent — fall through to main routing
        if looks_like_new_intent(text):
            state.overseas_state.pop("_trip_setup", None)
            persist_trip_setup()
            # Fall through — do not return

        elif ts.get("step") == "awaiting_missing":
            # One follow-up to supply missing destination and/or dates
            result = handle_overseas_request(text, _partial=ts)
            if result:
                await send_safe(update.message, result, parse_mode="Markdown")
            return

    # ─────────────────────────────────────────────────────────────────────────
    # PRIMARY ROUTING CHAIN — single pass, immediate exit on first match
    # ─────────────────────────────────────────────────────────────────────────
    reply = None

    # Save restaurant (must check before generic "save " to handle maps links)
    if lower.startswith("save restaurant "):
        result = handle_save_restaurant(text)
        reply = _handle_restaurant_save_result(result, user_id)

    elif lower.startswith("save ") and not is_restaurant_save(text):
        result = save_contact(text[5:])
        if result.startswith("_DUPLICATE_:"):
            existing_name = result.split(":", 1)[1]
            state.pending_contact_saves[user_id] = {"data": text[5:], "existing_name": existing_name}
            reply = f"*{existing_name}* already exists in your CRM.\nUpdate the existing contact or save as new? (update / new)"
        else:
            reply = result

    elif lower.startswith("find ") or lower.startswith("pull up "):
        name = text[5:] if lower.startswith("find ") else text[8:]
        name_lower = name.strip().lower()
        # Route pull up to correct handler before defaulting to CRM
        if name_lower in ["todo list", "todos", "my todos", "my todo list", "tasks", "my tasks"]:
            reply = list_todos()
        elif name_lower in ["bills", "my bills", "bill list"]:
            reply = list_bills()
        elif name_lower in ["reminders", "my reminders", "reminder list"]:
            reply = list_reminders()
        elif name_lower in ["followups", "follow ups", "my followups", "my follow ups"]:
            reply = upcoming_followups()
        elif name_lower in ["contacts", "my contacts"]:
            reply = list_contacts()
        else:
            reply = find_contact(name)

    elif lower.startswith("note "):
        reply = add_note(text[5:])

    elif lower.startswith("followup "):
        reply = set_followup(text[9:])

    elif lower.startswith("update "):
        reply = update_field(text[7:])

    elif (lower.startswith("delete ") and not lower.startswith("delete event")
          and not lower.startswith("delete expense") and not lower.startswith("delete bill")
          and not lower.startswith("delete restaurant") and not lower.startswith("delete last")
          and not lower.startswith("delete todo") and not lower.startswith("remove todo")
          and not lower.startswith("delete cal ")):
        name = text[7:].strip()
        _, record = find_row(name)
        if not record:
            reply = f"No contact found for '{name}'"
        else:
            confirm_name = record.get("Name", name)
            state.confirm_sessions[user_id] = {"action": "delete_contact", "args": [name], "target": f"Delete contact {confirm_name}?"}
            reply = f"Delete contact *{confirm_name}*? (yes / no)"

    elif lower.startswith("search "):
        reply = search_contacts(text[7:])

    elif (lower.startswith("edit ") and not lower.startswith("edit last expense")
          and not lower.startswith("edit expense")
          and not lower.startswith("edit cal ")
          and not lower.startswith("edit event ")
          and lower != "edit cal"
          and lower != "edit event"):
        name = text[5:].strip()
        _, record = find_row(name)
        if not record:
            # R12: no CRM contact — fall through to calendar edit
            reply = await edit_calendar_event(text, user_id)
        else:
            state.edit_sessions[user_id] = {"name": record.get("Name"), "step": "choose_field"}
            reply = (
                f"Editing *{record.get('Name')}*. Which field?\n\n"
                f"1. Alias\n2. Birthday\n3. Relationship\n4. Context\n5. Notes\n"
                f"6. Follow up date\n7. Follow up notes\n8. Email\n9. Address\n\n"
                f"Type the field name or *cancel* to exit."
            )

    elif lower == "cancel":
        for d in [state.edit_sessions, state.excel_import_sessions, state.confirm_sessions,
                  state.delete_sessions, state.receipt_confirm_sessions]:
            d.pop(user_id, None)
        state.session_timestamps.pop(user_id, None)
        reply = "Cancelled."

    elif lower == "dnd on":
        state.em_profile["dnd_active"] = True
        state.em_profile["dnd_held_messages"] = []
        save_em_profile()
        reply = "DND on — holding all messages until you turn it off."

    elif lower == "dnd off":
        state.em_profile["dnd_active"] = False
        held = state.em_profile.get("dnd_held_messages", [])
        state.em_profile["dnd_held_messages"] = []
        save_em_profile()
        if held:
            lines = [f"DND off. {len(held)} message(s) held while you were away:\n"]
            for m in held:
                lines.append(f"[{m['time']}] {m['text']}")
            reply = "\n".join(lines)
        else:
            reply = "DND off."

    elif lower == "reload profile":
        try:
            setup_em_profile()
            reply = "✅ Profile reloaded successfully."
        except Exception as e:
            reply = f"⚠️ Couldn't reload profile: {str(e)}"

    elif lower in ["skip missed", "dismiss missed", "skip all missed"]:
        reply = "All missed follow-ups dismissed ✅"
    elif lower.startswith("skip ") and not lower.startswith("skip missed bills"):
        name = text[5:].strip()
        reply = f"Follow-up for {name} dismissed ✅"
    elif lower in ["skip missed bills", "dismiss missed bills"]:
        reply = "Missed bill reminders dismissed ✅"

    elif lower in ["yes", "y", "yep", "yeah", "yup", "sure"] and state.market_summary_pending.get(user_id):
        state.market_summary_pending.pop(user_id, None)
        reply = await get_market_summary_now()

    elif re.match(r"set default card for .+ to .+", lower):
        m = re.match(r"set default card for (.+?) to (.+)", lower)
        if m:
            category_name = m.group(1).strip().title()
            card_name = m.group(2).strip().title()
            reply = set_card_default_category(card_name, category_name)
        else:
            reply = "Try: 'set default card for FnB to Maybank'"

    elif lower == "list":
        reply = list_contacts()
    elif lower == "stats":
        reply = get_stats()
    elif lower in ["followups", "show my followups", "show followups", "pending followups",
                   "what followups do i have", "my followups", "list followups",
                   "upcoming followups", "show my follow ups", "what are my followups"]:
        reply = upcoming_followups()
    elif lower == "overdue":
        reply = overdue_followups()
    elif lower == "birthdays":
        reply = upcoming_birthdays(30)
    elif lower == "soon":
        reply = upcoming_birthdays(7)
    elif lower.startswith("lastcontact "):
        reply = last_contact(text[12:])

    elif lower in ["referrals", "all referrals", "show referrals"]:
        reply = get_all_referrals()
    elif lower in ["top referrers", "best referrers", "who refers the most"]:
        reply = get_top_referrers()
    elif lower.startswith("referrals from ") or lower.startswith("who did "):
        if lower.startswith("referrals from "):
            name = text[15:].strip()
        else:
            m = re.search(r"who did (.+?) refer", lower)
            name = m.group(1).strip().title() if m else text[8:].strip()
        reply = get_referrals_by(name)

    elif "import" in lower and ("excel" in lower or "contacts" in lower or "spreadsheet" in lower):
        state.excel_import_sessions[user_id] = {"step": "awaiting_columns", "column_order": []}
        reply = "Sure! Tell me the column order in your Excel first — e.g. 'Name, Email, Date of Birth, Alias'"

    elif lower.startswith("search meetings ") or lower.startswith("find meeting "):
        query = text[16:] if lower.startswith("search meetings ") else text[13:]
        reply = search_meeting_notes(query.strip())
    elif lower == "cancel" and user_id in state.meeting_sessions:
        del state.meeting_sessions[user_id]
        reply = "Recap cancelled."
    elif is_meeting_start(text):
        event_name = extract_event_name(text)
        state.meeting_sessions[user_id] = {"step": "collecting", "event_name": event_name, "notes": []}
        if event_name:
            reply = f"Got it, taking notes for {event_name}. Send everything over and say done when you're finished."
        else:
            reply = "Sure, what's the event name?"
            state.meeting_sessions[user_id]["step"] = "get_name"

    elif lower in ["reminders", "my reminders", "list reminders"]:
        reply = list_reminders()
    elif is_cancel_reminder_request(text):
        keyword = text.lower().replace("cancel", "").replace("delete", "").replace("remove", "").replace("reminder", "").strip()
        cancelled = cancel_reminder_by_keyword(keyword) if keyword else []
        if not cancelled:
            reply = "Couldn't find a matching reminder to cancel."
        elif cancelled[0].startswith("_DISAMBIG_:"):
            entries = cancelled[0][len("_DISAMBIG_:"):].split("|")
            lines = ["Found multiple matching reminders — which one to cancel?"]
            for i, entry in enumerate(entries, 1):
                parts = entry.split(":", 1)
                msg = parts[1] if len(parts) > 1 else entry
                lines.append(f"{i}. {msg}")
            lines.append("\nReply with the number or 'all' to cancel all.")
            state.todo_disambig_sessions[user_id] = {"tasks": [e.split(":")[0] for e in entries], "action": "cancel_reminder", "entries": entries}
            reply = "\n".join(lines)
        else:
            reply = f"Cancelled: {', '.join(cancelled)}"
    elif is_reschedule_request(text):
        reply = handle_reschedule(text, user_id)
    elif is_reminder_request(text):
        reply = handle_new_reminder(text)

    elif any(lower == p or lower.startswith(p) for p in [
        "what expense categories", "list my categories", "what categories",
        "show categories", "what are my expense categories", "list categories",
        "my expense categories", "expense categories"
    ]):
        reply = get_expense_categories()
    elif any(lower == p or lower.startswith(p) for p in [
        "what merchants", "my merchants", "merchant map", "list merchants",
        "show merchants", "what merchants do we have", "saved merchants", "known merchants"
    ]):
        reply = get_merchant_list()
    elif lower.startswith("delete merchant ") or lower.startswith("remove merchant ") or lower.startswith("forget merchant "):
        merchant_name = text.split(" ", 2)[2].strip()
        reply = delete_merchant(merchant_name)
    elif lower.startswith("log bug "):
        description = text[8:].strip()
        reply = append_bug_to_backlog(description)
    elif (lower in ["expense report", "monthly report", "spending report", "expenses",
                   "monthly summary", "monthly spend", "this month", "expense summary"]
          or (re.search(r"\b(expense|spend|spent|spending)\b", lower) and re.search(r"\b(month|monthly|this month)\b", lower))):
        reply = get_expense_report()
    elif lower in ["delete last expense", "remove last expense"]:
        reply = delete_last_expense()
    elif lower in ["delete expense", "remove expense", "undo expense", "undo last expense"]:
        recent = get_recent_expenses(5)
        if not recent:
            reply = "No expenses logged yet."
        else:
            state.delete_sessions[user_id] = {"step": "pick", "expenses": recent}
            reply = format_delete_list(recent)
    elif lower.startswith("delete expense ") or lower.startswith("remove expense "):
        query = re.sub(r"^(delete|remove) expense\s+", "", lower).strip()
        results = search_expenses_by_merchant(query)
        if not results:
            reply = f"No expenses found matching '{query}'."
        elif len(results) == 1:
            sheet_row, _ = results[0]
            reply = delete_expense_by_row(sheet_row)
        else:
            state.delete_sessions[user_id] = {"step": "pick", "expenses": results}
            reply = format_delete_list(results)
    elif lower in ["last expense", "show last expense", "what did i log"]:
        reply = show_last_expense()
    elif lower in ["trip summary", "trip spend", "how much have i spent", "trip expenses"]:
        reply = get_trip_summary()
    elif lower in ["trip history", "my trips", "past trips", "trips"]:
        reply = format_trip_history()
    elif lower == "close trip":
        if state.overseas_state.get("active"):
            deactivate_overseas_mode()
            reply = "Trip closed. Back to SG mode. 🏠"
        else:
            closed = close_trip()
            reply = "Trip closed ✅" if closed else "No active trip to close."
    elif lower in ["current trip", "active trip", "am i overseas", "overseas status",
                   "am i in overseas mode", "is overseas mode on", "overseas mode status",
                   "am i in overseas", "what's my overseas status"]:
        if state.overseas_state.get("active"):
            dest = state.overseas_state.get("destination", "Unknown")
            curr = state.overseas_state.get("currency", "SGD")
            trip_start = state.overseas_state.get("trip_start", "")
            reply = f"✈️ Active trip: {dest} ({curr})"
            if trip_start:
                reply += f"\nStarted: {trip_start}"
        else:
            reply = "No active trip — you're in SG mode."
    elif lower in ["show me my bills", "show my bills", "show me my reminders",
                   "show my reminders", "show me my tasks", "show my tasks",
                   "show me my todos", "show my todos", "show me my todo list"]:
        if "bill" in lower:
            reply = list_bills()
        elif "reminder" in lower:
            reply = list_reminders()
        else:
            reply = list_todos()
    elif lower.startswith("edit last expense ") or lower.startswith("edit expense "):
        edit_text = re.sub(r"^edit (?:last )?expense\s+", "", text, flags=re.IGNORECASE).strip()
        if edit_text:
            reply = edit_last_expense(edit_text)
        else:
            reply = show_last_expense()
    elif lower.startswith("rename category "):
        m = re.search(r"rename category (.+?) to (.+)", lower)
        if m:
            old_cat = m.group(1).strip()
            new_cat = m.group(2).strip()
            state.confirm_sessions[user_id] = {"action": "rename_category", "args": [old_cat, new_cat], "target": f"Rename {old_cat} to {new_cat}?"}
            reply = f"Rename category *{old_cat}* to *{new_cat}*? This will update all expense rows and Merchant Map. (yes / no)"
        else:
            reply = "Try: 'rename category FnB to Food'"
    elif (lower.startswith("now in ") or lower.startswith("switched to ") or lower.startswith("arrived in ")
            or re.search(r"\bi'?m in\b|\bi am in\b", lower) or re.search(r"\bcoming back\b|\bheading back\b|\bon my way back\b", lower)) \
            and not is_bill_request(text):
        if state.overseas_state.get("active"):
            dest_text = re.sub(r"^(now in|switched to|arrived in)\s+", "", lower).strip().title()
            new_curr = _get_currency_for_dest(dest_text)
            new_dest = dest_text
            if new_curr and new_curr != "SGD":
                state.overseas_state["currency"] = new_curr
                state.overseas_state["destination"] = new_dest
                if new_curr not in state.overseas_state["currencies"]:
                    state.overseas_state["currencies"].append(new_curr)
                if new_dest not in state.overseas_state["trip_destinations"]:
                    state.overseas_state["trip_destinations"].append(new_dest)
                asyncio.create_task(asyncio.to_thread(get_fx_rate, new_curr))
                try:
                    from sheets import trips_sheet
                    ws = trips_sheet()
                    records = ws.get_all_records()
                    for i, row in enumerate(records, start=2):
                        if row.get("Status") == "active":
                            ws.update_cell(i, 2, new_dest)
                            ws.update_cell(i, 3, new_curr)
                            break
                except Exception as e:
                    print(f"Trips sheet update error: {e}")
                reply = f"Switched to {new_dest} — expenses will now be logged in {new_curr}."
            else:
                reply = handle_overseas_request(text)
        else:
            reply = handle_overseas_request(text)
    elif is_log_prefix_input(text):
        log_text = text[4:].strip()
        reply, needs_session, session_data = handle_expense_text(log_text, user_id)
        if needs_session and session_data:
            state.expense_sessions[user_id] = session_data

    elif lower in ["bills", "my bills", "list bills", "bills due", "what bills do i have",
                   "upcoming bills", "show bills"]:
        reply = list_bills()
    elif lower.startswith("delete bill "):
        bill_name = text[12:].strip()
        state.confirm_sessions[user_id] = {"action": "delete_bill", "args": [bill_name], "target": f"Delete bill {bill_name}?"}
        reply = f"Delete bill *{bill_name}*? (yes / no)"
    elif is_bill_request(text):
        reply = handle_new_bill(text)

    elif lower.startswith("delete restaurant ") or lower.startswith("remove restaurant "):
        rest_name = text.split(" ", 2)[2].strip()
        state.confirm_sessions[user_id] = {"action": "delete_restaurant", "args": [rest_name], "target": f"Delete restaurant {rest_name}?"}
        reply = f"Delete *{rest_name}* from your restaurant list? (yes / no)"
    elif is_restaurant_search(text):
        reply = handle_search_restaurants(text)
    elif is_restaurant_save(text):
        result = handle_save_restaurant(text)
        reply = _handle_restaurant_save_result(result, user_id)
    elif lower.startswith("search restaurants "):
        from restaurants import search_restaurants
        reply = search_restaurants(text[19:].strip())
    elif is_restaurant_suggestion_request(text):
        reply = await get_similar_restaurants(text)
    elif is_restaurant_review_request(text):
        name = re.sub(r"reviews? for|review of|tell me about|how is|how's|what's|what is", "", lower).strip().rstrip("?").strip()
        reply = await get_restaurant_review(name) if name else "Which restaurant are you asking about?"

    elif lower in ["portfolio", "my portfolio", "holdings", "portfolio performance",
                   "how is my portfolio", "how are my stocks", "portfolio today", "check portfolio"]:
        reply = await get_portfolio_performance()
    elif lower in ["delete from portfolio", "remove from portfolio", "delete holding", "remove holding",
                   "portfolio delete", "clear holding"]:
        rows = get_portfolio_rows()
        if not rows:
            reply = "Nothing in your portfolio to remove."
        else:
            state.portfolio_delete_sessions[user_id] = {"step": "pick", "rows": rows}
            reply = format_portfolio_delete_list(rows)
    elif lower.startswith("delete portfolio ") or lower.startswith("remove portfolio "):
        query = re.sub(r"^(delete|remove) portfolio\s+", "", lower).strip().upper()
        results = search_portfolio_by_ticker(query)
        if not results:
            reply = f"No holdings found matching '{query}'."
        elif len(results) == 1:
            sheet_row, _ = results[0]
            reply = delete_portfolio_row(sheet_row)
        else:
            state.portfolio_delete_sessions[user_id] = {"step": "pick", "rows": results}
            reply = format_portfolio_delete_list(results)
    elif lower in ["market summary", "market today", "how is the market", "market update",
                   "weekly market summary", "how are markets"]:
        reply = await get_market_summary_now()
    elif is_stock_request(text):
        result = await handle_stock_request(text)
        if result:
            reply = result

    elif any(lower.startswith(p) for p in [
        "todo ", "add task ", "new task ", "create todo ",
        "add todo ", "add to my list ", "add to my todo "
    ]):
        for p in ["todo ", "add task ", "new task ", "create todo ", "add todo ", "add to my list ", "add to my todo "]:
            if lower.startswith(p):
                reply = add_todo(text[len(p):])
                break
    elif lower.startswith("done "):
        result = complete_todo(text[5:])
        if result.startswith("_DISAMBIG_TODO_COMPLETE_:"):
            tasks = result.split(":", 1)[1].split("|")
            state.todo_disambig_sessions[user_id] = {"tasks": tasks, "action": "complete"}
            lines = ["Found multiple matching tasks — which one?"]
            for i, t in enumerate(tasks, 1):
                lines.append(f"{i}. {t}")
            lines.append("\nReply with the number.")
            reply = "\n".join(lines)
        else:
            await send_safe(update.message, result, parse_mode="MarkdownV2")
            return
    elif lower.startswith("delete todo ") or lower.startswith("remove todo "):
        task_name = re.sub(r"^(delete|remove) todo\s+", "", lower).strip()
        result = delete_todo(task_name)
        if result.startswith("_DISAMBIG_TODO_DELETE_:"):
            tasks = result.split(":", 1)[1].split("|")
            state.todo_disambig_sessions[user_id] = {"tasks": tasks, "action": "delete"}
            lines = ["Found multiple matching tasks — which one?"]
            for i, t in enumerate(tasks, 1):
                lines.append(f"{i}. {t}")
            lines.append("\nReply with the number.")
            reply = "\n".join(lines)
        else:
            reply = result
    elif lower in ["todos", "my todos", "todo list", "my todo list", "show todos",
                   "show my todos", "list todos", "what's on my list", "my tasks",
                   "show my tasks", "list my tasks", "pending tasks"]:
        reply = list_todos()

    elif lower in ("add cal", "cal", "add event", "create event"):
        reply = "What's the event? Give me a title, date and time — e.g. cal Dentist 5 Jun 10am"
    elif lower in ("edit cal", "edit event"):
        reply = "Which event do you want to edit?"
    elif (lower.startswith("add event") or lower.startswith("schedule ")
          or lower.startswith("create event") or lower.startswith("cal ")
          or lower.startswith("add cal ")):
        reply = await smart_add_event(text, user_id)
    elif lower.startswith("edit cal ") or lower.startswith("edit event "):
        reply = await edit_calendar_event(text, user_id)
    elif (lower.startswith("remove cal ") or lower.startswith("remove event ")
          or lower.startswith("cal delete ") or lower.startswith("delete cal ")
          or lower.startswith("del cal ") or lower.startswith("del event ")):
        raw_name = re.sub(r"^(remove cal|remove event|cal delete|delete cal|del cal|del event)\s+", "", text, flags=re.IGNORECASE).strip()
        # Strip trailing temporal words before title search
        raw_name = re.sub(r"\b(today|tomorrow|yesterday|next week|this week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}\s+\w+|\d{1,2}/\d{1,2})\b", "", raw_name, flags=re.IGNORECASE).strip()
        matches, err, _ = await find_upcoming_events(raw_name)
        if err:
            reply = err
        elif not matches:
            reply = f"❌ No upcoming event found matching '{raw_name}'"
        else:
            meta, summary, dtstart, dtend = matches[0]
            meta["summary"] = summary
            meta["dtstart"] = dtstart
            row = _fmt_event_row(summary, meta.get("cal_name", ""), dtstart, dtend)
            state.confirm_sessions[user_id] = {"action": "delete_event", "args": [meta], "target": row}
            touch_session(user_id)
            reply = f"Found *{summary}* — {row.split('|', 1)[1].strip() if '|' in row else ''}\nDelete this event? (yes / no)"
    elif lower.startswith("edit ") and "calendar_last_added" and user_id in state.calendar_last_added and not lower.startswith("edit last expense") and not lower.startswith("edit expense"):
        # Post-write edit — only fires if last action was a calendar add
        reply = await apply_calendar_edit(user_id, text)
    elif lower == "events today":
        reply = await get_events(1)
    elif lower == "events week":
        reply = await get_events(7)
    elif lower in ("events next week", "next week events", "what's on next week", "whats on next week"):
        # Monday to Sunday of next week
        today = date.today()
        days_until_monday = (7 - today.weekday()) % 7 or 7
        next_monday = today + timedelta(days=days_until_monday)
        next_sunday = next_monday + timedelta(days=6)
        reply = await get_events_for_date_range(next_monday, next_sunday)
    elif re.match(r"events on .+", lower) or re.match(r"events (for|this) .+", lower):
        date_text = re.sub(r"^events (on|for|this)\s+", "", lower).strip()
        anchors = {k.lower(): v for k, v in {
            "today": __import__('datetime').date.today().strftime("%d %b %Y"),
            "tomorrow": (__import__('datetime').date.today() + __import__('datetime').timedelta(days=1)).strftime("%d %b %Y"),
        }.items()}
        if date_text in anchors:
            resolved = anchors[date_text]
        else:
            from datetime import date as _date
            resolved = None
            for fmt in ["%d %b %Y", "%d %b", "%d/%m/%Y", "%d-%m-%Y"]:
                try:
                    parsed_d = __import__('datetime').datetime.strptime(date_text, fmt)
                    if fmt == "%d %b":
                        parsed_d = parsed_d.replace(year=_date.today().year)
                    resolved = parsed_d.strftime("%d %b %Y")
                    break
                except ValueError:
                    continue
            if not resolved:
                try:
                    from cal import _build_date_anchor_block
                    date_block = _build_date_anchor_block()
                    resp = await asyncio.to_thread(
                        lambda: client.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=20,
                            messages=[{"role": "user", "content":
                                f"{date_block}\nResolve this date reference to DD MMM YYYY format only, no other text: '{date_text}'"}]
                        )
                    )
                    resolved = resp.content[0].text.strip()
                except Exception:
                    resolved = None
        if resolved:
            reply = await get_events_for_date(resolved)
        else:
            reply = f"Couldn't understand that date — try 'events on 23 May' or 'events today'."
    elif lower.startswith("delete event ") or lower.startswith("del event "):
        raw_name = re.sub(r"^(delete event|del event)\s+", "", text, flags=re.IGNORECASE).strip()
        # Strip trailing temporal words before title search
        raw_name = re.sub(r"\b(today|tomorrow|yesterday|next week|this week|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}\s+\w+|\d{1,2}/\d{1,2})\b", "", raw_name, flags=re.IGNORECASE).strip()
        matches, err, _ = await find_upcoming_events(raw_name)
        if err:
            reply = err
        elif not matches:
            reply = f"❌ No upcoming event found matching '{raw_name}'"
        else:
            meta, summary, dtstart, dtend = matches[0]
            meta["summary"] = summary
            meta["dtstart"] = dtstart
            row = _fmt_event_row(summary, meta.get("cal_name", ""), dtstart, dtend)
            state.confirm_sessions[user_id] = {"action": "delete_event", "args": [meta], "target": row}
            touch_session(user_id)
            reply = f"Found *{summary}* — {row.split('|', 1)[1].strip() if '|' in row else ''}\nDelete this event? (yes / no)"

    elif lower in ("em whats pending", "em what's pending", "em pending", "whats pending"):
        reply = get_pending_backlog()

    elif lower == "em status":
        issues = []
        if not state.DRIVE_FOLDERS:
            issues.append("• Google Drive: not connected — check GOOGLE_CREDENTIALS in Railway")
        try:
            from clients import spreadsheet
            spreadsheet.worksheet("Expenses")
        except Exception as e:
            issues.append(f"• Sheets: connection error — {str(e)[:60]}")
        if not state._scheduler or not state._scheduler.running:
            issues.append("• Scheduler: not running — reminders and scheduled jobs are down, restart the bot")
        if not state.em_profile or not state.em_profile.get("version"):
            issues.append("• Profile: not loaded — reply 'reload profile' to retry")
        try:
            await get_events(1)
        except Exception:
            issues.append("• Google Calendar: unreachable — check GOOGLE_CREDENTIALS in Railway")
        from config import ANTHROPIC_FAILURE_THRESHOLD
        if state._anthropic_failure_count >= ANTHROPIC_FAILURE_THRESHOLD:
            issues.append("• Anthropic API: repeated failures detected — check API key or Anthropic status page")
        reply = "✅ Systems all green" if not issues else "⚠️ Issues detected:\n\n" + "\n".join(issues)

    elif lower == "help":
        reply = (
            "🤖 *Em — here's what I can do:*\n\n"
            "*CRM:*\n"
            "save, find, note, followup, update, edit, delete, search, list, stats, followups, overdue, birthdays, soon, lastcontact\n"
            "referrals, all referrals, top referrers, referrals from [name]\n"
            "import excel — import contacts from a spreadsheet\n\n"
            "*Calendar:*\n"
            "Just tell me naturally — 'schedule dinner tomorrow 7pm' or 'add event'\n"
            "events today / events week / delete event\n\n"
            "*To-Do:*\n"
            "todo [task] / add task [task] / new task [task]\n"
            "done [task] — mark complete\n"
            "todos — list all\n\n"
            "*Expenses:*\n"
            "log [merchant] [amount] — or just send a receipt photo\n"
            "last expense / edit last expense / delete last expense\n"
            "monthly summary / expense categories\n\n"
            "*Reminders:*\n"
            "remind me to [task] at [time] — or 'don't let me forget to [task]'\n"
            "reminders — list pending\n"
            "cancel reminder [keyword]\n\n"
            "*Bills:*\n"
            "add bill / bills due / delete bill\n\n"
            "*Stocks:*\n"
            "how is [ticker] doing / price of [ticker] / check [ticker]\n"
            "my portfolio / add to portfolio / market summary\n"
            "alert me if [ticker] hits [price]\n\n"
            "*Restaurants:*\n"
            "save restaurant [name] / restaurants / review [name]\n"
            "suggest restaurant [cuisine/area]\n\n"
            "*Trips & Overseas:*\n"
            "now in [country] — activate overseas mode\n"
            "flying [flight number] — log a flight\n"
            "trip summary / close trip\n\n"
            "*Meeting Recap:*\n"
            "meeting recap — then send your notes\n\n"
            "*Other:*\n"
            "em status — check Em's health\n"
            "dnd on / dnd off — do not disturb\n\n"
            "Or just chat — I'll figure it out 👍"
        )

    # Natural language CRM updates
    elif (crm_action := detect_crm_natural_update(text)) and not is_bill_request(text) and not is_overseas_mode_request(text):
        action, name, field_or_referred, value = crm_action
        if action == "referral":
            reply = set_referral(name, field_or_referred)
        elif action == "update":
            reply = update_contact_field_natural(name, field_or_referred, value)
        elif action == "show_private":
            reply = find_contact(name, show_private=True)

    # Overseas / trip setup
    elif is_overseas_mode_request(text):
        reply = handle_overseas_request(text)

    # Expense
    elif is_expense_input(text):
        reply, needs_session, session_data = handle_expense_text(text, user_id)
        if needs_session and session_data:
            state.expense_sessions[user_id] = session_data
    elif is_bare_merchant_input(text):
        reply, needs_session, session_data = handle_expense_text(text, user_id)
        if needs_session and session_data:
            state.expense_sessions[user_id] = session_data

    # Calendar queries (read — what's on, do I have anything)
    elif any(t in lower for t in [
        "what's on today", "whats on today", "what is on today",
        "what's on tomorrow", "whats on tomorrow",
        "do i have anything today", "do i have anything tomorrow",
        "any events today", "any events tomorrow",
        "what have i got today", "what have i got tomorrow",
        "what do i have today", "what do i have tomorrow",
        "calendar today", "calendar tomorrow",
        "show my calendar", "what's happening today", "what's happening tomorrow",
    ]):
        days = 7 if "week" in lower else 1
        reply = await get_events(days)

    # Calendar (create)
    elif await is_calendar_request(text):
        reply = await smart_add_event(text, user_id)

    # Claude conversation fallback
    else:
        if user_id not in state.conversation_histories:
            state.conversation_histories[user_id] = []
        state.conversation_histories[user_id].append({"role": "user", "content": text})
        if len(state.conversation_histories[user_id]) > 20:
            state.conversation_histories[user_id] = state.conversation_histories[user_id][-20:]
        overseas_key = (
            date.today().isoformat(),
            state.overseas_state.get("active"),
            state.overseas_state.get("destination"),
            state.overseas_state.get("currency"),
        )
        if state._system_prompt_cache is None or state._system_prompt_overseas_key != overseas_key:
            state._system_prompt_cache = build_system_prompt()
            state._system_prompt_overseas_key = overseas_key
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=state._system_prompt_cache,
            messages=state.conversation_histories[user_id]
        )
        reply = response.content[0].text
        state.conversation_histories[user_id].append({"role": "assistant", "content": reply})

    if not reply:
        reply = "Not sure what you mean — try rephrasing, or say 'help' to see what I can do."
    if reply:
        await send_safe(update.message, reply, parse_mode="Markdown")


def _handle_restaurant_save_result(result, user_id):
    """Helper to parse restaurant save signals and set up pending session."""
    if not result:
        return None
    if result.startswith("_NEEDS_LOCATION_:"):
        parts = result.split(":", 2)
        name = parts[1] if len(parts) > 1 else ""
        country = parts[2] if len(parts) > 2 else "Singapore"
        state.pending_restaurant_saves[user_id] = {"name": name, "country": country, "step": "awaiting_location"}
        return f"⚠️ Couldn't read that Maps link — what's the location for {name}?\n(e.g. 313 Orchard Road, Singapore)"
    if result.startswith("_INFER_LOCATION_:"):
        parts = result.split(":", 5)
        name = parts[1] if len(parts) > 1 else ""
        country = parts[2] if len(parts) > 2 else "Singapore"
        tags = parts[3] if len(parts) > 3 else ""
        multiple = parts[4] == "1" if len(parts) > 4 else False
        outlets = parts[5].split("|") if len(parts) > 5 and parts[5] else []
        if multiple and len(outlets) > 1:
            state.pending_restaurant_saves[user_id] = {
                "step": "awaiting_outlet", "name": name, "country": country,
                "tags": tags, "outlets": outlets
            }
            outlet_list = "\n".join(f"{i+1}. {o}" for i, o in enumerate(outlets))
            return f"Found a few {name} outlets — which one, and any tags?\n{outlet_list}"
        else:
            location = outlets[0] if outlets else ""
            state.pending_restaurant_saves[user_id] = {
                "step": "awaiting_confirm", "name": name, "country": country,
                "tags": tags, "location": location
            }
            return f"Got it — I have {name} at {location}. Is that right, and any tags? (yes / no, add tags or skip)"
    if result.startswith("_DUPLICATE_RESTAURANT_:"):
        parts = result.split(":", 5)
        state.pending_restaurant_saves[user_id] = {
            "step": "duplicate", "existing": parts[1], "name": parts[2],
            "location": parts[3], "country": parts[4],
            "tags": parts[5] if len(parts) > 5 else "", "notes": ""
        }
        return f"*{parts[1]}* is already in your list.\nUpdate existing or save as new? (update / new)"
    return result


async def check_missed_items_on_startup(app):
    try:
        from reminders import reminders_sheet, get_next_recurrence
        from crm import _get_crm_records
        from sheets import bills_sheet

        today = date.today()
        yesterday = today - timedelta(days=1)
        missed_followups = []
        missed_bills = []
        missed_reminders = []

        try:
            records = _get_crm_records()
            for r in records:
                fu_date_str = r.get("Follow Up Date", "")
                if fu_date_str:
                    for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"]:
                        try:
                            fu_date = datetime.strptime(fu_date_str, fmt).date()
                            if fu_date < today:
                                missed_followups.append({
                                    "name": r.get("Name", "?"),
                                    "date": fu_date_str,
                                    "notes": r.get("Follow Up Notes", "")
                                })
                            break
                        except ValueError:
                            continue
        except Exception as e:
            print(f"Missed followups check error: {e}")

        try:
            ws = bills_sheet()
            records = ws.get_all_records()
            for r in records:
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
                if due_date and due_date == yesterday:
                    missed_bills.append(r.get("Name", "?"))
        except Exception as e:
            print(f"Missed bills check error: {e}")

        try:
            ws = reminders_sheet()
            records = ws.get_all_records()
            now = datetime.now(TIMEZONE)
            for i, r in enumerate(records, start=2):
                if r.get("Status") != "pending":
                    continue
                scheduled_str = r.get("Scheduled Time", "")
                if not scheduled_str:
                    continue
                try:
                    scheduled = TIMEZONE.localize(datetime.strptime(scheduled_str, "%Y-%m-%d %H:%M"))
                    if scheduled < now:
                        missed_reminders.append({
                            "message": r.get("Message", ""),
                            "row": i,
                            "recurrence": r.get("Recurrence", "once")
                        })
                except Exception:
                    continue
        except Exception as e:
            print(f"Missed reminders check error: {e}")

        if missed_followups:
            lines = ["⚠️ Missed follow-ups while offline:"]
            for f in missed_followups:
                lines.append(f"• {f['name']} — was due {f['date']}")
            lines.append("\nReply 'followups' for details, 'skip missed' to dismiss all, or 'skip [name]' for one.")
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text="\n".join(lines))

        if missed_bills:
            lines = ["⚠️ Missed bill reminder(s) while offline:"]
            for b in missed_bills:
                lines.append(f"• {b}")
            lines.append("\nReply 'skip missed bills' to dismiss.")
            await app.bot.send_message(chat_id=YOUR_CHAT_ID, text="\n".join(lines))

        for rem in missed_reminders:
            try:
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🔔 Reminder (missed while offline): {rem['message']}"
                )
                ws = reminders_sheet()
                if rem["recurrence"] != "once":
                    next_time = get_next_recurrence(
                        datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                        rem["recurrence"]
                    )
                    if next_time:
                        ws.update_cell(rem["row"], 3, next_time)
                else:
                    ws.update_cell(rem["row"], 5, "sent")
            except Exception as e:
                print(f"Missed reminder fire error: {e}")

        try:
            if today.weekday() == 0:
                already_sent = False
                try:
                    sheet = get_sheet("Settings")
                    if sheet:
                        records = sheet.get_all_records()
                        for r in records:
                            if r.get("Key") == "market_summary_last_sent":
                                already_sent = r.get("Value", "") == today.isoformat()
                                break
                except Exception as e:
                    print(f"market_summary_last_sent check error: {e}")
                if not already_sent:
                    await app.bot.send_message(
                        chat_id=YOUR_CHAT_ID,
                        text="Missed the Monday market summary while offline — want me to send it now? (yes / no)"
                    )
                    state.market_summary_pending[YOUR_CHAT_ID] = True
        except Exception as e:
            print(f"Missed market summary check error: {e}")

    except Exception as e:
        print(f"check_missed_items_on_startup error: {e}")
