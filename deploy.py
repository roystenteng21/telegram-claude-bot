#!/usr/bin/env python3
"""
deploy.py — Em Bot deploy script
Usage: python3 ~/telegram-claude-bot/deploy.py "Session N" "commit message"
Place in ~/telegram-claude-bot/ alongside bot.py.
"""

import sys
import os
import subprocess
import json
import re
from datetime import date

SHEET_ID = os.getenv("SHEET_ID", "1uoLlnBrgogkWgVnirA4WtlZafwpprWk_epZy9NPLSQc")

# ANTHROPIC_API_KEY: read from env, fallback to credentials.json sibling file
def _get_anthropic_key():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if key:
        return key
    # Try loading from a local key file alongside deploy.py
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anthropic_key.txt")
    if os.path.exists(key_file):
        return open(key_file).read().strip()
    return ""


def run(cmd, cwd=None, allow_fail=False):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0 and not allow_fail:
        print(f"❌ Error: {' '.join(cmd)}\n{result.stderr}")
        sys.exit(1)
    return result.stdout.strip(), result.returncode


def get_commit_hash(repo):
    out, _ = run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, allow_fail=True)
    return out


def get_diff(repo):
    diff, _ = run(["git", "diff", "HEAD~1", "HEAD", "--", "bot.py"], cwd=repo, allow_fail=True)
    if not diff:
        diff, _ = run(["git", "show", "--stat", "HEAD"], cwd=repo, allow_fail=True)
    return diff[:6000] if diff else ""


def get_sheet_client():
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("⚠️  gspread not installed — pip install gspread google-auth")
        return None, None

    google_creds_env = os.getenv("GOOGLE_CREDENTIALS")
    if not google_creds_env:
        creds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
        if not os.path.exists(creds_file):
            print("⚠️  GOOGLE_CREDENTIALS not set and credentials.json not found — skipping Em Log.")
            return None, None
        creds_source = ("file", creds_file)
    else:
        creds_source = ("env", google_creds_env)

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        if creds_source[0] == "env":
            creds = Credentials.from_service_account_info(json.loads(creds_source[1]), scopes=scopes)
        else:
            creds = Credentials.from_service_account_file(creds_source[1], scopes=scopes)
        import gspread
        gc = gspread.authorize(creds)
        return gc, gc.open_by_key(SHEET_ID)
    except Exception as e:
        print(f"⚠️  Sheet connection failed: {e}")
        return None, None


def migrate_backlog_status(ws, all_values):
    """Idempotent: add Status column header + backfill missing Status cells."""
    in_backlog = False
    header_row_idx = None
    backlog_data_rows = []

    for i, row in enumerate(all_values):
        cell = str(row[0]) if row else ""
        if "BACKLOG" in cell and "SESSION" not in cell:
            in_backlog = True
            continue
        if "SESSION HISTORY" in cell:
            break
        if in_backlog:
            if row and row[0] == "Priority":
                header_row_idx = i + 1
                continue
            if header_row_idx and row and row[0] not in ("── BACKLOG (max 10) ──", "") and any(row):
                backlog_data_rows.append((i + 1, row))

    if not header_row_idx:
        return

    # Fix header col F
    try:
        header_row = all_values[header_row_idx - 1]
        if len(header_row) < 6 or header_row[5] != "Status":
            ws.update_cell(header_row_idx, 6, "Status")
            print("  ✅ Backlog: Status header added")
    except Exception as e:
        print(f"  ⚠️  Header fix failed: {e}")

    # Backfill missing Status
    migrated = 0
    for sheet_row, row in backlog_data_rows:
        status = row[5] if len(row) > 5 else ""
        if not status:
            try:
                ws.update_cell(sheet_row, 6, "🔲 Outstanding")
                migrated += 1
            except Exception as e:
                print(f"  ⚠️  Row {sheet_row} status failed: {e}")
    if migrated:
        print(f"  ✅ Backlog: {migrated} rows backfilled as Outstanding")


def read_backlog(all_values):
    """Parse backlog items from Em Log sheet values."""
    in_backlog = False
    header_passed = False
    items = []

    for i, row in enumerate(all_values):
        cell = str(row[0]) if row else ""
        if "BACKLOG" in cell and "SESSION" not in cell:
            in_backlog = True
            continue
        if "SESSION HISTORY" in cell:
            break
        if in_backlog:
            if row and row[0] == "Priority":
                header_passed = True
                continue
            if header_passed and row and row[0] not in ("── BACKLOG (max 10) ──", "") and any(row):
                items.append({
                    "sheet_row": i + 1,
                    "priority": row[0] if len(row) > 0 else "",
                    "item":     row[1] if len(row) > 1 else "",
                    "notes":    row[3] if len(row) > 3 else "",
                    "status":   row[5] if len(row) > 5 else "🔲 Outstanding",
                })
    return items


def mark_items_done(ws, sheet_rows):
    for row_num in sheet_rows:
        try:
            ws.update_cell(row_num, 6, "✅ Done")
        except Exception as e:
            print(f"  ⚠️  Mark done row {row_num} failed: {e}")


def append_session_row(ws, all_values, session_name, built, fixed, pending, commit):
    session_header_row = None
    for i, row in enumerate(all_values):
        if row and "SESSION HISTORY" in str(row[0]):
            session_header_row = i + 1
            break
    if not session_header_row:
        print("⚠️  SESSION HISTORY section not found — skipping row append.")
        return

    data_start = session_header_row + 2
    session_rows = [i + 1 for i, row in enumerate(all_values)
                    if i >= data_start and any(row)]
    while len(session_rows) >= 10:
        ws.delete_rows(session_rows[0])
        session_rows.pop(0)

    ws.append_row([date.today().strftime("%Y-%m-%d"), session_name, built, fixed, pending, commit])
    print(f"✅ Em Log updated — {session_name} logged.")


def analyse_with_haiku(session_name, diff, backlog_items):
    api_key = _get_anthropic_key()
    if not api_key:
        print("⚠️  No Anthropic API key found — skipping Haiku analysis.")
        print("   Fix: export ANTHROPIC_API_KEY=sk-... before running, or create ~/telegram-claude-bot/anthropic_key.txt")
        return None

    try:
        import urllib.request

        outstanding = [
            f"[{i}] {it['priority']} {it['item']}"
            for i, it in enumerate(backlog_items)
            if "Done" not in it.get("status", "")
        ]
        backlog_text = "\n".join(outstanding) if outstanding else "None"

        prompt = (
            f"You are analysing a git diff of Em Bot (a Telegram personal assistant bot) "
            f"to produce a detailed Em Log entry that serves as a handoff for the next session.\n\n"
            f"Session: {session_name}\n\n"
            f"BACKLOG (outstanding items, indexed):\n{backlog_text}\n\n"
            f"GIT DIFF (bot.py changes):\n{diff}\n\n"
            f"Produce a JSON object with exactly these fields:\n"
            f'"built": string — Detail every function or feature added or changed. '
            f"Name the functions, describe what each does, and note any behaviour changes. "
            f"Aim for 3-5 sentences. A future developer must know exactly what was shipped.\n"
            f'"fixed": string — For each bug fixed, name the exact issue and what the fix was '
            f"(e.g. check my expenses fell through to Claude — added to expense report elif). "
            f"One sentence per fix. If nothing fixed, write exactly: None\n"
            f'"fixed_indices": array of integers — indices from the BACKLOG list that this diff '
            f"fully resolves. Only include if the diff clearly addresses the item. Empty array if none.\n"
            f'"pending": string — List specific outstanding backlog items NOT addressed this session by name. '
            f"If nothing pending, write exactly: None\n\n"
            f"Return only the JSON object. No markdown fences, no explanation."
        )

        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())

        text = body["content"][0]["text"].strip()
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        return json.loads(text)

    except Exception as e:
        print(f"⚠️  Haiku analysis failed: {e}")
        return None


def main():
    if len(sys.argv) < 3:
        print('Usage: python3 deploy.py "Session N" "commit message"')
        sys.exit(1)

    session_name = sys.argv[1]
    commit_msg   = sys.argv[2]
    repo = os.path.dirname(os.path.abspath(__file__))

    if not os.path.exists(os.path.join(repo, "bot.py")):
        print(f"❌ bot.py not found in {repo}")
        sys.exit(1)

    # 1. Git commit + push
    run(["git", "add", "bot.py", "deploy.py"], cwd=repo)
    _, rc = run(["git", "commit", "-m", commit_msg], cwd=repo, allow_fail=True)
    if rc != 0:
        print("Nothing to commit — files unchanged.")
        committed = False
    else:
        run(["git", "push", "origin", "main"], cwd=repo)
        print("✅ Pushed — Railway will redeploy automatically.")
        committed = True

    commit_hash = get_commit_hash(repo)
    diff = get_diff(repo) if committed else ""

    # 2. Connect to sheet
    _, spreadsheet = get_sheet_client()
    if not spreadsheet:
        print("⚠️  Sheet unavailable — Em Log not updated.")
        return

    try:
        ws = spreadsheet.worksheet("Em Log")
        all_values = ws.get_all_values()
    except Exception as e:
        print(f"⚠️  Em Log open failed: {e}")
        return

    # 3. Migrate backlog Status column (idempotent)
    migrate_backlog_status(ws, all_values)
    all_values = ws.get_all_values()

    # 4. Read backlog
    backlog_items = read_backlog(all_values)
    outstanding = [it for it in backlog_items if "Done" not in it.get("status", "")]
    print(f"📋 Backlog: {len(backlog_items)} total, {len(outstanding)} outstanding")

    # 5. Haiku analysis
    analysis = analyse_with_haiku(session_name, diff, backlog_items) if diff else None

    if analysis:
        built         = analysis.get("built", f"See commit {commit_hash}.")
        fixed         = analysis.get("fixed", "None")
        pending       = analysis.get("pending", "None")
        fixed_indices = analysis.get("fixed_indices", [])
    else:
        built         = f"See commit {commit_hash}."
        fixed         = "None"
        pending       = "; ".join(it["item"][:60] for it in outstanding) if outstanding else "None"
        fixed_indices = []

    # 6. Mark resolved items Done
    if fixed_indices:
        rows_to_mark = []
        for idx in fixed_indices:
            if 0 <= idx < len(backlog_items):
                rows_to_mark.append(backlog_items[idx]["sheet_row"])
                print(f"  ✅ Marking done: {backlog_items[idx]['item'][:70]}")
        if rows_to_mark:
            mark_items_done(ws, rows_to_mark)
            all_values = ws.get_all_values()

    # 7. Append session row
    append_session_row(ws, all_values, session_name, built, fixed, pending, commit_hash or commit_msg)


if __name__ == "__main__":
    main()
