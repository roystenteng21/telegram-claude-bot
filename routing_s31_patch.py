# routing.py — S31 Patch Instructions
# Apply these 8 changes to routing.py in your local repo.
# All changes are surgical — no structural changes to routing logic.

# ── CHANGE 1: clients import (line ~8) ────────────────────────────────────────
# OLD:
from clients import client, drive_service
# NEW:
from clients import client, drive_service, personal_drive_service


# ── CHANGE 2: helpers import (line ~13) ───────────────────────────────────────
# OLD:
from helpers import send_safe, looks_like_new_intent, format_date
# NEW:
from helpers import send_safe, looks_like_new_intent, format_date, alert_error


# ── CHANGE 3: today_str format in photo handler ────────────────────────────────
# Find this line in the photo handler block (~line 195):
# OLD:
        today_str = today_obj.strftime("%Y-%m")
# NEW:
        today_str = today_obj.strftime("%d-%m-%Y")


# ── CHANGE 4: Receipt upload block ────────────────────────────────────────────
# Replace the entire try/except upload block (starts with "try:" after today_str):
# OLD:
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
# NEW:
        if state.OAUTH_DRIVE_OK:
            try:
                from googleapiclient.http import MediaIoBaseUpload
                receipts_root = state.DRIVE_FOLDERS.get("receipts", "")
                if receipts_root:
                    month_folder_id = get_or_create_drive_folder(month_folder_name, receipts_root)
                    temp_name = f"{today_str}-receipt-{photo.file_id[:8]}.jpg"
                    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
                    file_meta = {"name": temp_name, "parents": [month_folder_id]}
                    uploaded = personal_drive_service.files().create(
                        body=file_meta, media_body=media, fields="id,webViewLink"
                    ).execute()
                    drive_file_id = uploaded.get("id", "")
                    receipt_link = uploaded.get("webViewLink", "")
            except Exception as e:
                print(f"Receipt upload error: {e}")
                await update.message.reply_text("⚠️ Receipt couldn't be saved to Drive — expense will still be logged.")
                await alert_error("receipt_upload", str(e))
        else:
            print("Receipt upload skipped — OAUTH_DRIVE_OK is False")


# ── CHANGE 5: Receipt rename function ─────────────────────────────────────────
# Inside rename_receipt_in_drive(), replace drive_service with personal_drive_service:
# OLD:
                drive_service.files().update(
                    fileId=drive_file_id, body={"name": new_name}, supportsAllDrives=True
                ).execute()
# NEW:
                personal_drive_service.files().update(
                    fileId=drive_file_id, body={"name": new_name}
                ).execute()


# ── CHANGE 6: Vision parse path — add force_confirm=True ──────────────────────
# In the "if not caption:" block, find the handle_expense_text call:
# OLD:
                    reply, needs_session, session_data = handle_expense_text(synthesized, user_id, receipt_link=receipt_link)
# NEW:
                    reply, needs_session, session_data = handle_expense_text(synthesized, user_id, receipt_link=receipt_link, force_confirm=True)


# ── CHANGE 7: Caption photo path — add force_confirm=True ─────────────────────
# In the "if is_expense_input(caption):" block:
# OLD:
            reply, needs_session, session_data = handle_expense_text(caption, user_id, receipt_link=receipt_link)
# NEW:
            reply, needs_session, session_data = handle_expense_text(caption, user_id, receipt_link=receipt_link, force_confirm=True)


# ── CHANGE 8: Edit last expense routing ───────────────────────────────────────
# In the primary routing chain:
# OLD:
    elif lower.startswith("edit last expense ") or lower.startswith("edit expense "):
# NEW:
    elif lower.startswith("edit last expense") or lower.startswith("edit expense "):
