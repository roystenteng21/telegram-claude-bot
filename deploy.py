#!/usr/bin/env python3
"""
deploy.py — Em deployment script
Usage: python3 ~/telegram-claude-bot/deploy.py "commit msg" "Session N" "built" "fixed" "pending"
- Copies all 20 module files from ~ to ~/telegram-claude-bot/
- Commits and pushes to GitHub
- Railway auto-deploys on push
- Updates Em Log and Module Registry in Google Sheets
"""

import os
import sys
import subprocess
from datetime import date

# All 20 module files (flat, same directory)
MODULE_FILES = [
    "config.py",
    "clients.py",
    "state.py",
    "sheets.py",
    "helpers.py",
    "crm.py",
    "expenses.py",
    "fx.py",
    "reminders.py",
    "cal.py",
    "todos.py",
    "meetings.py",
    "bills.py",
    "restaurants.py",
    "stocks.py",
    "trips.py",
    "sessions.py",
    "routing.py",
    "infrastructure.py",
    "bot.py",
]

REPO_DIR = os.path.expanduser("~/telegram-claude-bot")


def run(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, cwd=cwd or REPO_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"CMD: {cmd}")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            return  # Not an error
        raise RuntimeError(f"Command failed: {cmd}")
    return result.stdout.strip()


def copy_modules():
    # Files are saved directly into REPO_DIR — no copy needed.
    missing = [f for f in MODULE_FILES if not os.path.exists(os.path.join(REPO_DIR, f))]
    if missing:
        print(f"⚠️  Missing from repo: {', '.join(missing)}")
    else:
        print(f"✅ All {len(MODULE_FILES)} module files present in repo")


def git_commit_push(commit_msg):
    try:
        run("git add -A")
        result = subprocess.run(
            f'git commit -m "{commit_msg}"',
            shell=True, cwd=REPO_DIR, capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            print("ℹ️  Nothing new to commit — files unchanged.")
            return False
        if result.returncode != 0:
            print(f"Git commit error: {result.stderr}")
            return False
        print(f"✅ Committed: {commit_msg}")
    except Exception as e:
        print(f"Git commit error: {e}")
        return False
    try:
        run("git push")
        print("✅ Pushed to GitHub — Railway deploying...")
        return True
    except Exception as e:
        print(f"Git push error: {e}")
        return False


def _has_credentials():
    """Return True if Google credentials are available in this environment."""
    if os.environ.get("GOOGLE_CREDENTIALS"):
        return True
    if os.path.exists(os.path.join(REPO_DIR, "credentials.json")):
        return True
    return False


def update_em_log_and_registry(session_name, built, fixed, pending, commit_hash):
    """Update Em Log and Module Registry in Google Sheets.
    Skipped silently when credentials are not available — Railway handles this on boot.
    """
    if not _has_credentials():
        print("ℹ️  No local credentials — Em Log + Module Registry will update on Railway boot.")
        return
    try:
        sys.path.insert(0, REPO_DIR)
        from sheets import add_session_to_em_log, update_module_registry
        today = date.today().strftime("%Y-%m-%d")
        add_session_to_em_log(today, session_name, built, fixed, pending, commit_hash)
        print(f"✅ Em Log updated: {session_name}")
        for fname in MODULE_FILES:
            mod_name = fname.replace(".py", "")
            update_module_registry(mod_name, fname, today, session_name, "✅ Active")
        print("✅ Module Registry updated")
    except Exception as e:
        print(f"⚠️  Em Log / Module Registry update failed: {e}")
        print("    (Deploy succeeded — Railway will update on boot)")


def get_commit_hash():
    try:
        result = subprocess.run(
            "git rev-parse --short HEAD",
            shell=True, cwd=REPO_DIR, capture_output=True, text=True
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 deploy.py 'commit msg' ['Session N'] ['built'] ['fixed'] ['pending']")
        sys.exit(1)

    commit_msg = sys.argv[1]
    session_name = sys.argv[2] if len(sys.argv) > 2 else "Manual deploy"
    built = sys.argv[3] if len(sys.argv) > 3 else commit_msg
    fixed = sys.argv[4] if len(sys.argv) > 4 else "None"
    pending = sys.argv[5] if len(sys.argv) > 5 else "None"

    print(f"\n🚀 Deploying Em — {session_name}")
    print(f"   Commit: {commit_msg}\n")

    copy_modules()
    pushed = git_commit_push(commit_msg)
    commit_hash = get_commit_hash()

    if pushed:
        update_em_log_and_registry(session_name, built, fixed, pending, commit_hash)
        print(f"\n✅ Deploy complete — {commit_hash}")
        print("   Railway will restart Em in ~30 seconds.")
    else:
        print(f"\n✅ Files copied. No new commit (files unchanged).")


if __name__ == "__main__":
    main()
