#!/usr/bin/env python3
"""
reauth.py — One-time OAuth2 setup for Em's personal Drive access.
Run this locally on your Mac whenever the OAuth token needs to be refreshed.

Usage: python3 ~/telegram-claude-bot/reauth.py
"""

import os
import json
import base64
import sys

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CLIENT_SECRETS = os.path.expanduser("~/telegram-claude-bot/client_secrets.json")
TOKEN_PATH = os.path.expanduser("~/telegram-claude-bot/token.json")


def main():
    if not os.path.exists(CLIENT_SECRETS):
        print(f"❌ client_secrets.json not found at {CLIENT_SECRETS}")
        print("   Download it from Google Cloud Console → APIs & Services → Credentials → Em Drive OAuth")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("Installing required packages...")
        os.system(f"{sys.executable} -m pip install google-auth-oauthlib google-auth-httplib2 --break-system-packages -q")
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

    creds = None

    # Try to load existing token and refresh it
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing token...")
            creds.refresh(Request())

    # Run full auth flow if needed
    if not creds or not creds.valid:
        print("\nOpening browser for Google login...")
        print("Log in with the Gmail account that owns Em Receipts folder.\n")
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, SCOPES)
        creds = flow.run_local_server(port=0)

    # Save token.json locally
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    print(f"✅ token.json saved to {TOKEN_PATH}")

    # Base64-encode for Railway env var
    with open(TOKEN_PATH, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    print("\n" + "="*60)
    print("Add this to Railway as GOOGLE_OAUTH_TOKEN:")
    print("="*60)
    print(encoded)
    print("="*60)
    print("\nSteps:")
    print("1. Copy the string above")
    print("2. Railway → Em project → Variables → Add GOOGLE_OAUTH_TOKEN")
    print("3. Redeploy Em")
    print("\nDone! Em will now upload receipts to your personal Drive.")


if __name__ == "__main__":
    main()
