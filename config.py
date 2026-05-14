import os
import pytz
from dotenv import load_dotenv

load_dotenv()

# --- Credentials ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
ICLOUD_USERNAME = os.getenv("ICLOUD_USERNAME")
ICLOUD_PASSWORD = os.getenv("ICLOUD_PASSWORD")
AVIATIONSTACK_API_KEY = os.getenv("AVIATIONSTACK_API_KEY", "")
EXCHANGE_RATE_API_KEY = os.getenv("EXCHANGE_RATE_API_KEY", "")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID"))
RAILWAY_DEPLOYMENT_ID = os.getenv("RAILWAY_DEPLOYMENT_ID", "")

ANTHROPIC_FAILURE_THRESHOLD = 3

# --- Timezone ---
TIMEZONE = pytz.timezone("Asia/Kuala_Lumpur")

# --- Expense ---
EXPENSE_CATEGORIES = ["FnB", "Entertainment", "Personal", "Family", "Work", "Transport", "Shopping", "Travel"]
EXPENSE_CARDS = ["Citi", "Maybank", "Amex", "UOB"]  # fallback only

CARDS_SCHEMA = ["Card Name", "Last 4", "Default Category", "Notes"]
INITIAL_CARDS = [
    ["Maybank", "4002", "FnB", ""],
    ["Citi", "1176", "General", ""],
    ["UOB", "5372", "", ""],
    ["Amex", "1008", "", ""],
]

EXPENSE_CATEGORY_EMOJI = {
    "FnB": "🍽️",
    "Transport": "🚗",
    "Entertainment": "🎬",
    "Personal": "🪞",
    "Family": "👨‍👩‍👧",
    "Work": "💼",
    "Shopping": "🛍️",
    "Household": "🏠",
    "Travel": "✈️",
}

EXPENSE_MERCHANT_OVERRIDES = {
    "FnB": [
        (["coffee", "starbucks", "kopitiam", "ya kun", "toast box", "kopi", "cafe", "espresso", "latte"], "☕"),
        (["ramen", "ichiran", "ippudo", "japanese", "sushi", "yakitori", "donburi", "izakaya"], "🍜"),
        (["mcdonald", "burger king", "wendy", "burger", "kfc", "popeyes", "fast food"], "🍔"),
        (["bar", "beer", "wine", "drinks", "cocktail", "pub", "taproom", "brewery"], "🍺"),
    ],
    "Transport": [
        (["flight", "airline", "air asia", "scoot", "singapore airlines", "cathay", "emirates", "jetstar"], "✈️"),
        (["grab", "gojek", "taxi", "uber", "ryde", "tada"], "🚕"),
        (["mrt", "bus", "transit", "ez-link", "train"], "🚌"),
    ],
    "Entertainment": [
        (["netflix", "disney", "hbo", "prime video", "apple tv", "streaming", "hulu", "mewatch"], "📺"),
        (["spotify", "apple music", "tidal", "deezer", "music"], "🎵"),
        (["cinema", "cathay", "gv", "shaw", "golden village", "movie"], "🎥"),
        (["steam", "playstation", "xbox", "nintendo", "game"], "🎮"),
    ],
    "Shopping": [
        (["guardian", "watsons", "unity", "pharmacy", "watson"], "💊"),
        (["ntuc", "giant", "cold storage", "fairprice", "supermarket", "grocery", "market"], "🛒"),
    ],
}

SGD_HIGH_AMOUNT_THRESHOLD = 5000.0

LOCATION_CONTEXT_WORDS = {
    "japan", "korea", "thailand", "malaysia", "indonesia", "vietnam", "philippines",
    "australia", "china", "hong kong", "taiwan", "india", "uk", "england", "france",
    "germany", "italy", "spain", "usa", "america", "canada", "dubai", "uae",
    "tokyo", "osaka", "seoul", "bangkok", "kuala lumpur", "kl", "jakarta", "bali",
    "sydney", "melbourne", "beijing", "shanghai", "guangzhou", "shenzhen",
    "taipei", "mumbai", "delhi", "london", "paris", "berlin", "rome", "barcelona",
    "new york", "los angeles", "chicago", "toronto", "vancouver",
    "hkg", "nrt", "icn", "bkk", "kul", "cgk", "sin", "syd", "pek", "pvg",
    "tpe", "bom", "del", "lhr", "cdg", "txl", "fco", "jfk", "lax", "yyz",
}

QUESTION_WORDS = {"what", "how", "why", "when", "which", "who", "where", "list", "show", "tell", "is", "are", "do", "does", "can", "could", "would", "should"}
COMMAND_PREFIXES = ["delete", "remove", "undo", "edit", "rename", "show", "list", "what", "how"]

EDIT_FIELD_SYNONYMS = {
    "merchant": "Merchant", "shop": "Merchant", "store": "Merchant", "place": "Merchant",
    "amount": "Amount", "price": "Amount", "total": "Amount", "cost": "Amount",
    "currency": "Currency",
    "category": "Category", "cat": "Category",
    "card": "Card", "payment": "Card",
    "notes": "Notes", "note": "Notes",
    "sgd": "SGD Amount",
}

# --- Sessions ---
SESSION_TIMEOUT_MINUTES = 5
SESSION_TIMEOUT_MESSAGES = {
    "expense": "Session timed out — nothing was logged. Start again when ready.",
    "delete": "Session timed out — nothing was deleted.",
    "portfolio_delete": "Session timed out — nothing was removed.",
    "confirm": "Session timed out — nothing was changed.",
    "receipt_confirm": "Session timed out — nothing was logged. Send the receipt again when ready.",
    "edit": "Session timed out — nothing was changed.",
    "meeting": "Meeting recap session timed out.",
}

# --- Stocks ---
MARKET_INDICES = {
    "US": {"^GSPC": "S&P 500"},
    "China": {"000001.SS": "Shanghai"},
    "India": {"^NSEI": "Nifty 50"},
}

MARKET_FLAGS_MAP = {
    "US": "🇺🇸",
    "China": "🇨🇳",
    "India": "🇮🇳",
}

SGX_TICKER_MAP = {
    "dbs": "D05.SI", "d05": "D05.SI",
    "ocbc": "O39.SI", "o39": "O39.SI",
    "uob": "U11.SI", "u11": "U11.SI",
    "singtel": "Z74.SI", "z74": "Z74.SI",
    "capitaland": "9CI.SI", "capitaland investment": "9CI.SI", "9ci": "9CI.SI", "cli": "9CI.SI",
    "keppel": "BN4.SI", "bn4": "BN4.SI",
    "wilmar": "F34.SI", "f34": "F34.SI",
    "sia": "C6L.SI", "singapore airlines": "C6L.SI", "c6l": "C6L.SI",
    "jardine": "J36.SI",
    "thai bev": "Y92.SI", "thaibev": "Y92.SI",
}

HK_TICKER_MAP = {
    "tencent": "0700.HK", "0700": "0700.HK", "TENCENT": "0700.HK",
    "alibaba": "9988.HK", "9988": "9988.HK", "ALIBABA": "9988.HK",
    "meituan": "3690.HK", "3690": "3690.HK", "MEITUAN": "3690.HK",
    "hsbc": "0005.HK", "0005": "0005.HK", "HSBC": "0005.HK",
    "aia": "1299.HK", "1299": "1299.HK", "AIA": "1299.HK",
    "byd": "1211.HK", "1211": "1211.HK", "BYD": "1211.HK",
    "xiaomi": "1810.HK", "1810": "1810.HK", "XIAOMI": "1810.HK",
    "jd": "9618.HK", "9618": "9618.HK", "JD": "9618.HK",
    "netease": "9999.HK", "9999": "9999.HK", "NETEASE": "9999.HK",
    "cnooc": "0883.HK", "0883": "0883.HK", "CNOOC": "0883.HK",
}

SOURCE_LABELS = {
    "reuters": "Reuters", "bloomberg": "Bloomberg",
    "financial times": "FT", "ft.com": "FT",
    "cnbc": "CNBC", "straits times": "Straits Times",
    "business times": "Business Times", "nikkei": "Nikkei",
    "wall street journal": "WSJ", "wsj": "WSJ",
    "yahoo finance": "Yahoo Finance", "marketwatch": "MarketWatch",
    "seeking alpha": "Seeking Alpha", "channel news asia": "CNA",
    "cna": "CNA", "barrons": "Barron's", "fortune": "Fortune",
    "investopedia": "Investopedia", "motley fool": "Motley Fool",
    "benzinga": "Benzinga", "zacks": "Zacks",
}

_OBSCURE_SOURCES = {"guruFocus", "forex.com", "indmoney", "gotrade", "traders union",
                    "cliftonlarsonallen", "simply wall st", "stockanalysis"}

_HEADLINE_REJECT = [
    "will it", "will they", "should you", "best stocks", "stocks to watch",
    "to buy and watch", "to watch:", "opening:", "opening bell", "preview:",
    "what to expect", "top picks", "analyst picks", "should i", "is it time",
    "here's what", "what you need", "everything you need",
]

# --- Meetings ---
MEETING_START_PHRASES = [
    "meeting recap", "taking notes", "log this meeting", "meeting notes",
    "recap for", "notes for", "log meeting", "start recap", "new recap",
    "networking recap", "presentation recap"
]

MEETING_DONE_PHRASES = [
    "done", "that's it", "thats it", "save that", "save it",
    "finish", "finished", "end recap", "process this", "that's all", "thats all"
]

# --- Bills ---
BILL_REMINDER_GREETINGS = [
    "Heads up", "Just a nudge", "Quick reminder", "Hey", "FYI"
]

# --- Dates ---
DATE_FORMATS = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"]

# --- Em Log / Dev Notes ---
DEV_NOTES_CONTENT = [
    ["Section", "Content", "Last Updated"],
    ["Coding Standard — R1", "Route by cost: exact match → regex → keyword → cached lookup → live data → Claude. Never call Claude for routing decisions.", "2026-04-26"],
    ["Coding Standard — R2", "Single pass, immediate exit. Once a handler matches, execution stops. No fallback re-runs detectors. Loops exit on first match.", "2026-04-26"],
    ["Coding Standard — R3", "One sheet read per request, at the latest possible moment. Cached in memory, invalidated only on write. No sheet read during routing.", "2026-04-26"],
    ["Coding Standard — R4", "Haiku for classification/extraction/JSON under 200 tokens. Sonnet for reasoning/conversation/multi-step only. Set min max_tokens.", "2026-04-26"],
    ["Coding Standard — R5", "Typing indicator before every external API call. Parallel calls use asyncio.gather(). Nothing blocks the event loop.", "2026-04-26"],
    ["Coding Standard — R6", "Session messages never reach main routing chain. Session data is flat dict with explicit fields. Sessions time out cleanly.", "2026-04-26"],
    ["Coding Standard — R7", "Cache hierarchy: in-memory → cached sheet read → live sheet read → external API. Each layer reached only if above misses.", "2026-04-26"],
    ["Coding Standard — R8", "Every branch sets reply or returns. Empty reply is a bug. Claude fallback for genuine unknown intent only.", "2026-04-26"],
    ["Architecture — Routing", "Primary elif chain runs once. No else block re-runs detectors. Session handlers exit before reaching main router.", "2026-04-26"],
    ["Architecture — Caches", "_merchant_cache, _card_names_cache, _system_prompt_cache. Invalidate at write site only. Warm card cache on startup.", "2026-04-26"],
    ["Architecture — Models", "Haiku: is_calendar_request, parse_expense_text_v2, classification. Sonnet: conversation fallback, reasoning, market narrative.", "2026-04-26"],
    ["Architecture — Sheets", "setup_sheets() uses single worksheets() call. No repeated API reads in setup. Em Log and Dev Notes never read in routing.", "2026-04-26"],
    ["Architecture — Module Layers", "Import order (no reverse imports ever): config → clients → state → sheets → helpers → feature modules → sessions → routing → infrastructure → bot.py. Feature modules never import each other except meetings→crm (one-way). routing.py is the only file that imports everything.", "2026-05-01"],
    ["Architecture — Module Registry", "Module Registry tab in this sheet is the live index of all modules. Columns: Module · File · Layer · Key Functions · Imports From · Last Changed · Session · Status. Updated automatically by deploy.py on every deploy. Claude reads this tab at session start to know which file to request for any given task.", "2026-05-01"],
    ["Rule — Ship Rule", "Nothing ships until fully wired, tested, and deployed in the same session. Plan-only sessions that produce dead files are banned.", "2026-04-30"],
    ["Rule — Em Log Documentation", "Every session MUST produce a complete Em Log entry before closing. Built, Fixed, and Pending fields must be specific — not 'various fixes'. Any architectural decision, new pattern, or deviation from standards must be documented in Dev Notes in the same session it is made. Future Claude instances rely on this as the sole source of truth.", "2026-05-01"],
    ["Rule — No Silent Changes", "Any change to module boundaries, import structure, coding standards, or deploy flow must be logged to Dev Notes immediately. Never leave a session with undocumented architectural changes. The sheet is the memory — if it's not in the sheet, it didn't happen.", "2026-05-01"],
    ["Rule — Circular Import Zero Tolerance", "Imports flow strictly downward through layers. If adding an import would create a cycle, restructure — move the shared function to a lower layer or pass it as a parameter. Never work around a circular import with lazy imports or importlib.", "2026-05-01"],
    ["Handoff Rule", "Start of every session: (1) Read the Em Session Brief uploaded by Roysten — this is the sole source of truth. Do not read the Em Management Sheet or Google Drive. (2) Identify the relevant module(s) from the brief's architecture section. (3) Ask Roysten to upload only those module file(s). Never request files beyond what the task requires. Never start building without completing steps 1–2 and receiving Roysten's explicit go-ahead. Session is not closed and no brief is generated until Roysten confirms the deploy succeeded.", "2026-05-02"],
    ["Handoff — What To Upload", "Pre-modularisation: upload bot.py. Post-modularisation: upload only the module file(s) relevant to the session task. Claude identifies the right module(s) from the Module Registry. If a session touches routing logic, also upload routing.py.", "2026-05-01"],
    ["Handoff — Mid-Session Context", "If a session ends mid-build (not deployed): log exactly what was completed, what is half-built, and what the next step is in the Em Log Pending field. The next session picks up from that exact point — no re-explaining required.", "2026-05-01"],
    ["Deploy Flow — Pre-Modularisation", "Download bot.py from Claude chat → save to ~/telegram-claude-bot/bot.py → python ~/telegram-claude-bot/deploy.py 'commit msg' 'Session N' 'built' 'fixed' 'pending' → Railway auto-deploys, Em Log + Module Registry auto-updated.", "2026-05-01"],
    ["Deploy Flow — Post-Modularisation", "Download changed module file(s) from Claude chat → save to ~/telegram-claude-bot/ → python ~/telegram-claude-bot/deploy.py 'commit msg' 'Session N' 'built' 'fixed' 'pending' → deploy.py copies all 20 module files, commits, pushes → Railway auto-deploys, Em Log + Module Registry auto-updated.", "2026-05-01"],
    ["Modularisation — Status", "S22 — DONE. 20 modules deployed. Import layer hierarchy defined and locked. Module Registry tab populated.", "2026-05-01"],
    ["Modularisation — Files", "config.py · clients.py · state.py · sheets.py · helpers.py · crm.py · expenses.py · fx.py · reminders.py · cal.py · todos.py · meetings.py · bills.py · restaurants.py · stocks.py · trips.py · sessions.py · routing.py · infrastructure.py · bot.py", "2026-05-01"],
    ["Modularisation — Cross-Feature Rule", "Only permitted cross-feature import: meetings.py → crm.find_row (one-way). All other cross-feature calls go through routing.py dispatch. No feature module calls another feature module's handler functions.", "2026-05-01"],
    ["Roadmap — Session 9", "DONE. Trips schema migration + AviationStack strip (−307 lines).", "2026-04-30"],
    ["Roadmap — Session 10", "DONE. deploy.py built. Repo: roystenteng21/telegram-claude-bot. Token: 90-day ghp_ (no daily reset).", "2026-04-30"],
    ["Roadmap — Sessions 11–21", "DONE. All reliability, efficiency, and completeness work completed. All backlog items resolved. Full audit passed S22a.", "2026-05-01"],
    ["Roadmap — Session 22", "DONE. Modularisation complete. Split bot.py into 20 modules. deploy.py updated. All 91 tests pass.", "2026-05-01"],
]

EM_LOG_HEADERS_BACKLOG = ["Priority", "Item", "Stage", "Notes", "Added", "Status"]
EM_LOG_HEADERS_SESSION = ["Date", "Session", "Built", "Fixed", "Pending", "Commit"]
