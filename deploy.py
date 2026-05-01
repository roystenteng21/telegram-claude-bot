#!/usr/bin/env python3
"""
deploy.py — Em deployment script
Usage: python3 ~/telegram-claude-bot/deploy.py "commit msg" "Session N" "built" "fixed" "pending"
- Copies all 20 module files from ~ to ~/telegram-claude-bot/
- Writes session_meta.json to repo (Railway reads this on boot to update Em Log + Module Registry)
- Commits and pushes to GitHub
- Railway auto-deploys on push
"""

import os
import sys
import json
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
SESSION_META_PATH = os.path.join(REPO_DIR, "session_meta.json")


def run(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, cwd=cwd or REPO_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"CMD: {cmd}")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            return
        raise RuntimeError(f"Command failed: {cmd}")
    return result.stdout.strip()


def copy_modules():
    missing = [f for f in MODULE_FILES if not os.path.exists(os.path.join(REPO_DIR, f))]
    if missing:
        print(f"⚠️  Missing from repo: {', '.join(missing)}")
    else:
        print(f"✅ All {len(MODULE_FILES)} module files present in repo")


def _read_deploy_count():
    """Read current deploy_count from session_meta.json, defaulting to 0."""
    try:
        if os.path.exists(SESSION_META_PATH):
            with open(SESSION_META_PATH) as f:
                data = json.load(f)
            return data.get("deploy_count", 0)
    except Exception:
        pass
    return 0


def write_session_meta(session_name, built, fixed, pending):
    """Write session_meta.json to repo. Railway reads this on boot."""
    deploy_count = _read_deploy_count() + 1
    meta = {
        "date": date.today().strftime("%Y-%m-%d"),
        "session": session_name,
        "built": built,
        "fixed": fixed,
        "pending": pending,
        "deploy_count": deploy_count,
    }
    with open(SESSION_META_PATH, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"✅ session_meta.json written (deploy #{deploy_count})")
    return deploy_count


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
    write_session_meta(session_name, built, fixed, pending)
    pushed = git_commit_push(commit_msg)
    commit_hash = get_commit_hash()

    if pushed:
        print(f"\n✅ Deploy complete — {commit_hash}")
        print("   Railway will restart Em in ~30 seconds.")
        print("   Em Log + Module Registry will update automatically on boot.")
    else:
        print(f"\n✅ Files copied. No new commit (files unchanged).")


if __name__ == "__main__":
    main()
