#!/usr/bin/env python3
"""
deploy.py — Em Bot deploy script
Usage: python3 ~/deploy.py ~/telegram-claude-bot/bot.py "commit message"
"""

import sys
import os
import shutil
import subprocess

def run(cmd, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running {' '.join(cmd)}:\n{result.stderr}")
        sys.exit(1)
    return result.stdout.strip()

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 ~/deploy.py <path/to/bot.py> \"commit message\"")
        sys.exit(1)

    src = os.path.expanduser(sys.argv[1])
    message = sys.argv[2]
    repo = os.path.expanduser("~/telegram-claude-bot")
    dest = os.path.join(repo, "bot.py")

    if not os.path.exists(src):
        print(f"Error: {src} not found")
        sys.exit(1)

    if not os.path.exists(repo):
        print(f"Error: repo not found at {repo}")
        sys.exit(1)

    shutil.copy2(src, dest)
    print(f"Copied {src} → {dest}")

    run(["git", "add", "bot.py"], cwd=repo)
    run(["git", "commit", "-m", message], cwd=repo)
    out = run(["git", "push", "origin", "main"], cwd=repo)
    print(out)
    print("✅ Pushed — Railway will redeploy automatically.")

if __name__ == "__main__":
    main()
