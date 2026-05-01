import os
import json
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, SHEET_ID

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Lazy singletons — initialized on first access, not at import time.
# This allows test_em.py to import feature modules without needing credentials.
_gc = None
_spreadsheet = None
_drive_service = None
_creds = None
_client = None


def _has_google_credentials():
    """Return True if Google credentials are available in this environment."""
    if os.getenv("GOOGLE_CREDENTIALS"):
        return True
    if os.path.exists("credentials.json"):
        return True
    return False

def _get_creds():
    global _creds
    if _creds is None:
        from google.oauth2.service_account import Credentials
        google_creds_env = os.getenv("GOOGLE_CREDENTIALS")
        if google_creds_env:
            google_creds = json.loads(google_creds_env)
            _creds = Credentials.from_service_account_info(google_creds, scopes=SCOPES)
        else:
            _creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return _creds


class _LazyGC:
    def __getattr__(self, name):
        global _gc
        if _gc is None:
            import gspread
            _gc = gspread.authorize(_get_creds())
        return getattr(_gc, name)

    def open_by_key(self, key):
        global _gc
        if _gc is None:
            import gspread
            _gc = gspread.authorize(_get_creds())
        return _gc.open_by_key(key)


class _LazySpreadsheet:
    def _get(self):
        global _spreadsheet
        if _spreadsheet is None:
            if not _has_google_credentials():
                return None
            _spreadsheet = gc.open_by_key(SHEET_ID)
        return _spreadsheet

    def __getattr__(self, name):
        s = self._get()
        if s is None:
            return None
        return getattr(s, name)

    def worksheet(self, *args, **kwargs):
        s = self._get()
        if s is None:
            return None
        return s.worksheet(*args, **kwargs)

    def worksheets(self, *args, **kwargs):
        s = self._get()
        if s is None:
            return []
        return s.worksheets(*args, **kwargs)

    def add_worksheet(self, *args, **kwargs):
        s = self._get()
        if s is None:
            return None
        return s.add_worksheet(*args, **kwargs)


class _LazyDrive:
    def _get(self):
        global _drive_service
        if _drive_service is None:
            from googleapiclient.discovery import build
            _drive_service = build("drive", "v3", credentials=_get_creds())
        return _drive_service

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def files(self):
        return self._get().files()


class _LazyClient:
    def _get(self):
        global _client
        if _client is None:
            _client = Anthropic(api_key=ANTHROPIC_API_KEY)
        return _client

    def __getattr__(self, name):
        return getattr(self._get(), name)

    @property
    def messages(self):
        return self._get().messages


# Module-level names — lazy proxies, connections deferred until first use
gc = _LazyGC()
spreadsheet = _LazySpreadsheet()
drive_service = _LazyDrive()
client = _LazyClient()
