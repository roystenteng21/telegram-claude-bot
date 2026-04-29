import json
import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from anthropic import Anthropic

from config import SCOPES, SHEET_ID, ANTHROPIC_API_KEY

# --- Google Credentials ---
google_creds_env = os.getenv("GOOGLE_CREDENTIALS")
if google_creds_env:
    google_creds = json.loads(google_creds_env)
    creds = Credentials.from_service_account_info(google_creds, scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)

gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SHEET_ID)
drive_service = build("drive", "v3", credentials=creds)

# --- Anthropic Client ---
client = Anthropic(api_key=ANTHROPIC_API_KEY)
