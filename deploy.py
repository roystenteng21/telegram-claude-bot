#!/usr/bin/env python3
"""
deploy.py — Em deployment script
Usage: python3 ~/telegram-claude-bot/deploy.py "commit msg" "Session N" "built" "fixed" "pending"
- Auto-detects all module .py files in repo (excludes non-module files)
- Writes session_meta.json to repo (Railway reads this on boot to update Em Log + Module Registry)
- Commits and pushes to GitHub
- Railway auto-deploys on push
"""

import os
import sys
import json
import subprocess
from datetime import date

REPO_DIR = os.path.expanduser("~/telegram-claude-bot")
SESSION_META_PATH = os.path.join(REPO_DIR, "session_meta.json")

# Files that live in the repo but are not Em modules
NON_MODULE_FILES = {
    "deploy.py",
    "reauth.py",
    "session_meta.json",
    "requirements.txt",
    "Procfile",
    "runtime.txt",
    ".env",
}


def get_module_files():
    """Auto-detect all .py module files in the repo directory."""
    files = []
    for f in sorted(os.listdir(REPO_DIR)):
        if f.endswith(".py") and f not in NON_MODULE_FILES and not f.startswith("."):
            files.append(f)
    return files


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


def check_modules():
    """Detect and report all module files. Abort if none found."""
    modules = get_module_files()
    if not modules:
        print("❌ No module files found in repo — aborting.")
        sys.exit(1)
    print(f"✅ {len(modules)} module files detected: {', '.join(modules)}")
    return modules


def _read_deploy_count():
    try:
        if os.path.exists(SESSION_META_PATH):
            with open(SESSION_META_PATH) as f:
                data = json.load(f)
            return data.get("deploy_count", 0)
    except Exception:
        pass
    return 0


def write_session_meta(session_name, built, fixed, pending, modules):
    deploy_count = _read_deploy_count() + 1
    meta = {
        "date": date.today().strftime("%Y-%m-%d"),
        "session": session_name,
        "built": built,
        "fixed": fixed,
        "pending": pending,
        "deploy_count": deploy_count,
        "module_count": len(modules),
        "modules": modules,
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

    modules = check_modules()
    write_session_meta(session_name, built, fixed, pending, modules)
    pushed = git_commit_push(commit_msg)
    commit_hash = get_commit_hash()

    if pushed:
        print(f"\n✅ Deploy complete — {commit_hash}")
        print("   Railway will restart Em in ~30 seconds.")
        print("   Em Log + Module Registry will update automatically on boot.")
    else:
        print(f"\n✅ Files ready. No new commit (files unchanged).")


if __name__ == "__main__":
    main()
