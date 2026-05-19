#!/usr/bin/env python3
"""
final_patches.py — Apply L7 (stocks) and L9 (crm). Run from ~/telegram-claude-bot.
"""
import os, subprocess

REPO = os.path.expanduser("~/telegram-claude-bot")

def patch(filename, old, new, label):
    path = os.path.join(REPO, filename)
    with open(path, 'r') as f:
        content = f.read()
    if old not in content:
        print(f"❌ {label}: not found in {filename}")
        return False
    with open(path, 'w') as f:
        f.write(content.replace(old, new))
    print(f"✅ {label}")
    return True

# L7: suggest_stocks try/except
patch("stocks.py",
    '    resp = client.messages.create(\n        model="claude-sonnet-4-6",\n        max_tokens=600,\n        messages=[{"role": "user", "content": prompt}]\n    )\n    return resp.content[0].text.strip()',
    '    try:\n        resp = client.messages.create(\n            model="claude-sonnet-4-6",\n            max_tokens=600,\n            messages=[{"role": "user", "content": prompt}]\n        )\n        return resp.content[0].text.strip()\n    except Exception as e:\n        print(f"suggest_stocks Claude error: {e}")\n        return "⚠️ Couldn\'t generate stock suggestions right now — try again in a moment."',
    "L7: suggest_stocks try/except")

# L9: remove global state from send_birthday_reminders
patch("crm.py",
    'async def send_birthday_reminders(app):\n    import asyncio\n    from config import YOUR_CHAT_ID\n    global state\n    try:',
    'async def send_birthday_reminders(app):\n    import asyncio\n    from config import YOUR_CHAT_ID\n    try:',
    "L9: remove redundant global state")

# Syntax check
for f in ["stocks.py", "crm.py"]:
    r = subprocess.run(["python3", "-m", "py_compile", os.path.join(REPO, f)], capture_output=True, text=True)
    print(f"  {'✅' if r.returncode == 0 else '❌'} {f}")
