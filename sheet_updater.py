#!/usr/bin/env python3
"""
sheet_updater.py — Em Bot sheet writer
Reads GOOGLE_CREDENTIALS from credentials.json in the same folder.
Usage: python3 sheet_updater.py
"""

import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID = "1uoLlnBrgogkWgVnirA4WtlZafwpprWk_epZy9NPLSQc"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDS_PATH = os.path.join(SCRIPT_DIR, "credentials.json")


def get_service():
    with open(CREDS_PATH) as f:
        raw = json.load(f)
    # Support both raw service account JSON and Railway-style wrapped JSON
    if "type" not in raw and "GOOGLE_CREDENTIALS" in raw:
        raw = json.loads(raw["GOOGLE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(raw, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


def find_row(service, sheet_name, col_index, value):
    """Return 1-based row number where column col_index matches value, or None."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A:A"
    ).execute()
    rows = result.get("values", [])
    for i, row in enumerate(rows):
        if row and row[0] == value:
            return i + 1
    return None


def update_row(service, sheet_name, row_num, values):
    """Update an entire row starting at column A."""
    col_end = chr(ord("A") + len(values) - 1)
    range_notation = f"{sheet_name}!A{row_num}:{col_end}{row_num}"
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=range_notation,
        valueInputOption="RAW",
        body={"values": [values]}
    ).execute()
    print(f"  ✅ Updated row {row_num}: {values[0]}")


def append_row(service, sheet_name, values):
    """Append a new row to the sheet."""
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]}
    ).execute()
    print(f"  ✅ Appended: {values[0]}")


def upsert_dev_notes_row(service, section, content, updated="2026-04-30"):
    """Update Dev Notes row by Section key, or append if not found."""
    sheet_name = "Dev Notes"
    row = find_row(service, sheet_name, 0, section)
    values = [section, content, updated]
    if row:
        update_row(service, sheet_name, row, values)
    else:
        append_row(service, sheet_name, values)


def main():
    print("Connecting to Google Sheets...")
    service = get_service()
    print("Connected.\n")

    print("Updating Dev Notes...")

    upsert_dev_notes_row(
        service,
        section="Deploy Flow",
        content="Save bot.py to ~/telegram-claude-bot/ → cd ~/telegram-claude-bot → git add bot.py → git commit -m 'message' → git push origin main → Railway auto-deploys. bot.py and deploy.py both live in ~/telegram-claude-bot/.",
    )

    upsert_dev_notes_row(
        service,
        section="Handoff Rule",
        content="Handoff must be accurate and complete. Cover: last deployed commit, bot.py status, any mid-session context still hanging, and outstanding work in priority order. Drive handoff doc only for oversized payloads (60+ findings). Standard sessions: handoff inline in chat only.",
    )

    print("\nDone.")


if __name__ == "__main__":
    main()
