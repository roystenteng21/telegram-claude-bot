"""
handlers.py — Session handler logic extracted from routing.py.
Owns all multi-step session processing. Called by routing.py.
Layer 5.5 — imports from feature modules only, never from routing.py.
"""
import state
from helpers import send_safe
from crm import save_contact, update_field, delete_contact, find_row
from expenses import (
    handle_expense_text, handle_expense_session, handle_receipt_confirm_session,
    delete_expense_by_row, format_delete_list, search_expenses_by_merchant,
    rename_category
)
from restaurants import (
    save_restaurant, format_restaurant_saved, delete_restaurant,
    handle_save_restaurant, handle_search_restaurants
)
from stocks import (
    delete_portfolio_row, format_portfolio_delete_list, search_portfolio_by_ticker
)
from bills import delete_bill
from cal import (
    delete_calendar_event, write_calendar_event, apply_calendar_edit,
    find_upcoming_events, format_calendar_confirm
)
from todos import complete_todo
from meetings import handle_meeting_session
from sessions import touch_session


# ---------------------------------------------------------------------------
# Excel import session
# ---------------------------------------------------------------------------

async def handle_excel_import_session(user_id, text, lower, update, parse_excel_column_order, handle_excel_import):
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
        return True
    return False


# ---------------------------------------------------------------------------
# Calendar confirm session
# ---------------------------------------------------------------------------

async def handle_calendar_confirm_session(user_id, text, lower, update):
    touch_session(user_id)
    cs = state.calendar_confirm_sessions[user_id]

    # Multiple delete disambiguation
    if cs.get("step") == "pick_delete":
        matches = cs.get("delete_matches", [])
        if text.strip().isdigit():
            idx = int(text.strip()) - 1
            if 0 <= idx < len(matches):
                _, summary, _ = matches[idx]
                del state.calendar_confirm_sessions[user_id]
                state.session_timestamps.pop(user_id, None)
                reply = delete_calendar_event(summary)
                await send_safe(update.message, reply, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"Pick a number between 1 and {len(matches)}.")
        elif lower in ["cancel", "nvm", "nevermind"]:
            del state.calendar_confirm_sessions[user_id]
            state.session_timestamps.pop(user_id, None)
            await update.message.reply_text("Cancelled.")
        else:
            await update.message.reply_text("Reply with a number or 'cancel'.")
        return True

    # Add confirm session
    if lower in ["yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay"]:
        del state.calendar_confirm_sessions[user_id]
        state.session_timestamps.pop(user_id, None)
        try:
            reply = write_calendar_event(cs["parsed"])
        except Exception as e:
            reply = f"⚠️ Couldn't save to iCloud Calendar: {type(e).__name__}: {str(e)[:100]}"
        await send_safe(update.message, reply, parse_mode="Markdown")
    else:
        # Inline edit
        try:
            reply = apply_calendar_edit(user_id, text)
        except Exception as e:
            state.calendar_confirm_sessions.pop(user_id, None)
            reply = f"⚠️ Edit failed: {type(e).__name__}: {str(e)[:100]}"
        await send_safe(update.message, reply, parse_mode="Markdown")
    return True


# ---------------------------------------------------------------------------
# Restaurant save session
# ---------------------------------------------------------------------------

async def handle_restaurant_save_session(user_id, text, lower, update):
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
        return True

    elif step == "awaiting_confirm":
        words = lower.split(None, 1)
        first_word = words[0] if words else ""
        rest = words[1] if len(words) > 1 else ""
        if first_word in ["yes", "y"]:
            merged_tags = ", ".join(filter(None, [prs.get("tags", ""), rest.strip()]))
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
        return True

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
        return True

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
        return True

    return False


# ---------------------------------------------------------------------------
# Contact save session
# ---------------------------------------------------------------------------

async def handle_contact_save_session(user_id, text, lower, update):
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
    return True


# ---------------------------------------------------------------------------
# Todo disambiguation session
# ---------------------------------------------------------------------------

async def handle_todo_disambig_session(user_id, text, lower, update):
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
            elif action == "delete":
                sheet = todo_sheet()
                records = sheet.get_all_records()
                for i, r in enumerate(records):
                    if r.get("Task", "") == task_name:
                        sheet.delete_rows(i + 2)
                        await update.message.reply_text(f"Deleted — {task_name} ✅")
                        return True
        else:
            await update.message.reply_text("Invalid number. Try again or 'cancel'.")
    elif lower == "cancel":
        del state.todo_disambig_sessions[user_id]
        await update.message.reply_text("Cancelled.")
    else:
        await update.message.reply_text("Reply with a number or 'cancel'.")
    return True


# ---------------------------------------------------------------------------
# Delete session (expenses)
# ---------------------------------------------------------------------------

async def handle_delete_session(user_id, text, lower, update):
    session = state.delete_sessions[user_id]
    step = session.get("step")

    if lower in ["cancel", "nevermind", "never mind", "nvm"]:
        del state.delete_sessions[user_id]
        await update.message.reply_text("Cancelled.")
        return True

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
            return True
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
            return True
        elif lower in ["not there", "not here", "none of these", "not in the list"]:
            await update.message.reply_text("Reply 'search [merchant name]' to find it.")
            return True
        else:
            await update.message.reply_text("Reply with a number, 'search [merchant]', or 'cancel'.")
            return True

    return False


# ---------------------------------------------------------------------------
# Confirm session (generic yes/no)
# ---------------------------------------------------------------------------

async def handle_confirm_session(user_id, text, lower, update):
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
            reply = delete_calendar_event(args[0])
        else:
            reply = "Done."
        await send_safe(update.message, reply, parse_mode="Markdown")
    elif lower in ["no", "n", "cancel", "nope", "nah"]:
        del state.confirm_sessions[user_id]
        state.session_timestamps.pop(user_id, None)
        await update.message.reply_text("Cancelled.")
    else:
        await update.message.reply_text(f"{cs.get('target', 'Confirm?')} (yes / no)")
    return True


# ---------------------------------------------------------------------------
# Portfolio delete session
# ---------------------------------------------------------------------------

async def handle_portfolio_delete_session(user_id, text, lower, update):
    touch_session(user_id)
    pd_session = state.portfolio_delete_sessions[user_id]

    if lower in ["cancel", "nevermind", "nvm"]:
        del state.portfolio_delete_sessions[user_id]
        await update.message.reply_text("Cancelled.")
        return True

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
        return True
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
        return True
    else:
        await update.message.reply_text("Reply with a number, 'search [ticker]', or 'cancel'.")
        return True


# ---------------------------------------------------------------------------
# CRM edit session
# ---------------------------------------------------------------------------

async def handle_crm_edit_session(user_id, text, lower, update):
    session = state.edit_sessions[user_id]
    step = session["step"]
    fields = ["alias", "birthday", "relationship", "context", "notes",
              "follow up date", "follow up notes", "email", "address"]

    if step == "choose_field":
        field = text.lower().strip()
        if field == "cancel":
            del state.edit_sessions[user_id]
            await update.message.reply_text("Cancelled.")
            return True
        if field not in fields:
            await update.message.reply_text(
                f"Pick a field to edit:\n1. Alias\n2. Birthday\n3. Relationship\n4. Context\n"
                f"5. Notes\n6. Follow up date\n7. Follow up notes\n8. Email\n9. Address\n\n"
                f"Or type *cancel* to exit.",
                parse_mode="Markdown"
            )
            return True
        session["field"] = field
        session["step"] = "enter_value"
        await update.message.reply_text(f"Enter the new value for *{field.title()}*:", parse_mode="Markdown")

    elif step == "enter_value":
        field = session["field"]
        name = session["name"]
        result = update_field(f"{name}, {field}, {text}")
        del state.edit_sessions[user_id]
        await update.message.reply_text(result, parse_mode="Markdown")

    return True


# ---------------------------------------------------------------------------
# Reconciliation session
# ---------------------------------------------------------------------------

async def handle_recon_session(user_id, text, lower, update):
    touch_session(user_id)
    rs = state.recon_sessions[user_id]
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
    return True


# ---------------------------------------------------------------------------
# Restaurant save result helper (used by routing.py primary chain)
# ---------------------------------------------------------------------------

def handle_restaurant_save_result(result, user_id):
    """Parse restaurant save signal strings and set up pending session."""
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
