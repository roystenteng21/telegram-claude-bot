import json
import re
import requests
import urllib.request
from datetime import date

import config
from config import (
    ALPHA_VANTAGE_API_KEY, MARKET_INDICES, MARKET_FLAGS_MAP,
    SGX_TICKER_MAP, HK_TICKER_MAP, YOUR_CHAT_ID,
)
from clients import client
from sheets import portfolio_sheet


# --- Ticker normalisation ---

def normalise_ticker(ticker):
    t = ticker.strip().lower()
    if t in SGX_TICKER_MAP:
        return SGX_TICKER_MAP[t]
    if t in HK_TICKER_MAP:
        return HK_TICKER_MAP[t]
    upper = ticker.strip().upper()
    if upper in HK_TICKER_MAP:
        return HK_TICKER_MAP[upper]
    if upper in SGX_TICKER_MAP:
        return SGX_TICKER_MAP[upper]
    if "." in upper:
        return upper
    if re.match(r"^\d{4}$", upper):
        return f"{upper}.HK"
    return upper


# --- Price fetch ---

def fetch_price(ticker):
    ticker = normalise_ticker(ticker)
    if ALPHA_VANTAGE_API_KEY:
        try:
            url = (
                f"https://www.alphavantage.co/query"
                f"?function=GLOBAL_QUOTE&symbol={ticker}&apikey={ALPHA_VANTAGE_API_KEY}"
            )
            resp = requests.get(url, timeout=10)
            data = resp.json()
            quote = data.get("Global Quote", {})
            if quote.get("05. price"):
                price = float(quote["05. price"])
                prev_close = float(quote["08. previous close"])
                change = float(quote["09. change"])
                change_pct = float(quote["10. change percent"].replace("%", ""))
                av_suffix = ticker.split(".")[-1] if "." in ticker else ""
                av_exchange_flags = {
                    "SI": "🇸🇬", "HK": "🇭🇰", "L": "🇬🇧", "AX": "🇦🇺",
                    "T": "🇯🇵", "NS": "🇮🇳", "BO": "🇮🇳", "SS": "🇨🇳", "SZ": "🇨🇳",
                }
                return {
                    "ticker": ticker, "name": ticker, "price": price,
                    "prev_close": prev_close, "change": change, "change_pct": change_pct,
                    "currency": "USD", "flag": av_exchange_flags.get(av_suffix, "🇺🇸"),
                }
        except Exception as e:
            print(f"Alpha Vantage error for {ticker}: {e}")

    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        result = data["chart"]["result"][0]
        meta = result["meta"]
        price = meta.get("regularMarketPrice", 0)
        prev_close = meta.get("chartPreviousClose", 0)
        currency = meta.get("currency", "USD")
        name = meta.get("longName") or meta.get("shortName") or ticker
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0
        market_state = meta.get("marketState", "")
        exchange = meta.get("exchange", "")
        full_exchange = meta.get("fullExchangeName", "")
        closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]
        week52_low = min(closes) if closes else None
        week52_high = max(closes) if closes else None
        suffix = ticker.split(".")[-1] if "." in ticker else ""
        exchange_flags = {
            "SI": "🇸🇬", "HK": "🇭🇰", "L": "🇬🇧", "AX": "🇦🇺",
            "T": "🇯🇵", "NS": "🇮🇳", "BO": "🇮🇳", "SS": "🇨🇳", "SZ": "🇨🇳",
        }
        flag = exchange_flags.get(suffix, "🇺🇸")
        return {
            "ticker": ticker, "name": name, "price": price, "prev_close": prev_close,
            "change": change, "change_pct": change_pct, "currency": currency,
            "market_state": market_state, "exchange": exchange,
            "fullExchangeName": full_exchange,
            "week52_low": week52_low, "week52_high": week52_high, "flag": flag,
        }
    except Exception as e:
        print(f"Yahoo Finance fallback error for {ticker}: {e}")
        return None


def fetch_weekly_change(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1wk&range=1mo"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]
        opens = [o for o in result["indicators"]["quote"][0].get("open", []) if o is not None]
        if len(closes) >= 1 and len(opens) >= 1:
            week_open = opens[-1]
            week_close = closes[-1]
            if week_open:
                return (week_close - week_open) / week_open * 100
    except Exception as e:
        print(f"fetch_weekly_change error for {ticker}: {e}")
    return None


# --- Source labels ---

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

def get_source_label(source_name):
    lower = source_name.lower()
    for key, label in SOURCE_LABELS.items():
        if key in lower:
            return label
    for obs in _OBSCURE_SOURCES:
        if obs in lower:
            return None
    return None


def _fetch_rss_headlines_for_stock(ticker, name):
    import xml.etree.ElementTree as ET
    from concurrent.futures import ThreadPoolExecutor, as_completed
    gn_query = f"{name} stock".replace(" ", "+")
    yf_query = f"{ticker} stock".replace(" ", "+")
    rss_sources = [
        f"https://news.google.com/rss/search?q={gn_query}&hl=en&gl=US&ceid=US:en",
        f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
    ]
    headlines = []
    sources = []
    seen = set()

    def fetch_one(url):
        try:
            resp = requests.get(url, timeout=3)
            root = ET.fromstring(resp.content)
            results = []
            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                source = item.findtext("source", "") or title.split(" - ")[-1].strip()
                title = title.split(" - ")[0].strip()
                if title:
                    results.append((title, source))
            return results
        except Exception as e:
            print(f"RSS fetch error {url}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(fetch_one, url) for url in rss_sources]
        for f in as_completed(futures):
            for title, source in f.result():
                if title not in seen and len(headlines) < 4:
                    seen.add(title)
                    headlines.append(title)
                    sources.append(source)
    return headlines[:3], sources[:3]


def _generate_price_movement_summary(data):
    price = data.get("price", 0)
    change_pct = data.get("change_pct", 0)
    week52_low = data.get("week52_low")
    week52_high = data.get("week52_high")
    currency = data.get("currency", "")
    name = data.get("name", data.get("ticker", ""))
    direction = "up" if change_pct >= 0 else "down"
    sentences = [f"{name} is {direction} {abs(change_pct):.2f}% today, currently at {currency} {price:.2f}."]
    if week52_low and week52_high:
        position = (price - week52_low) / (week52_high - week52_low) * 100 if week52_high != week52_low else 50
        if position >= 75:
            range_desc = "trading near its 52-week high"
        elif position <= 25:
            range_desc = "trading near its 52-week low"
        else:
            range_desc = "trading in the middle of its 52-week range"
        sentences.append(f"It is {range_desc} ({currency} {week52_low:.2f} – {currency} {week52_high:.2f}).")
    return " ".join(sentences)


def fetch_stock_summary(ticker, name, price_data=None):
    headlines, sources = _fetch_rss_headlines_for_stock(ticker, name)
    labelled = [(h, get_source_label(s)) for h, s in zip(headlines, sources)]
    usable = [(h, l) for h, l in labelled if l]
    if not usable and price_data:
        return _generate_price_movement_summary(price_data), []
    if not usable:
        return None, []
    source_context = "; ".join(f"{h[:60]} [{l}]" for h, l in usable[:3])
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=120,
            messages=[{"role": "user", "content": (
                f"Headlines about {name} ({ticker}):\n{source_context}\n\n"
                "Write 2-3 short factual sentences as one paragraph. "
                "First sentence: where the stock sits in its 52-week range (near high, near low, or mid-range). "
                "Remaining sentences: recent business developments or earnings from the headlines — factual only. "
                "No timing advice, no buy/sell signals, no speculation. "
                "End with a single [SourceName] tag for the most relevant source."
            )}]
        )
        return resp.content[0].text.strip(), [l for _, l in usable[:2]]
    except Exception as e:
        print(f"Stock summary Claude error: {e}")
        if price_data:
            return _generate_price_movement_summary(price_data), []
        return None, []


def format_price(data, summary=None):
    if not data:
        return None
    flag = data.get("flag", "🌐")
    name = data.get("name", data["ticker"])
    ticker = data["ticker"]
    currency = data.get("currency", "")
    price = data.get("price", 0)
    change_pct = data.get("change_pct", 0)
    arrow = "▲" if change_pct >= 0 else "▼"
    market_state = data.get("market_state", "REGULAR")
    state_label = {"PRE": " (pre)", "POST": " (post)", "CLOSED": " (closed)", "REGULAR": ""}.get(market_state, "")
    week52_low = data.get("week52_low")
    week52_high = data.get("week52_high")
    range_line = f"52-week range: {currency} {week52_low:.2f} – {currency} {week52_high:.2f}" if week52_low and week52_high else ""
    if summary is None:
        summary, _ = fetch_stock_summary(ticker, name, price_data=data)
    lines = [f"{flag} {name} ({ticker})"]
    lines.append(f"{currency} {price:.2f} {arrow} {abs(change_pct):.2f}%{state_label}")
    if range_line:
        lines.append(range_line)
    lines.append(f"\n{summary if summary else _generate_price_movement_summary(data)}")
    return "\n".join(lines)


# --- Portfolio ---

def log_portfolio_buy(ticker, quantity, price, buy_date=None):
    sheet = portfolio_sheet()
    today = buy_date or date.today().strftime("%d/%m/%Y")
    sheet.append_row([ticker.upper(), str(quantity), str(price), today, ""])

def get_portfolio_holdings():
    try:
        sheet = portfolio_sheet()
        records = sheet.get_all_records()
        holdings = {}
        for r in records:
            ticker = r.get("Stock", "").upper()
            qty = float(r.get("Quantity", 0))
            price = float(r.get("Buy Price", 0))
            if ticker not in holdings:
                holdings[ticker] = {"total_qty": 0, "total_cost": 0}
            holdings[ticker]["total_qty"] += qty
            holdings[ticker]["total_cost"] += qty * price
        return {t: {"qty": h["total_qty"], "avg_cost": h["total_cost"] / h["total_qty"]}
                for t, h in holdings.items() if h["total_qty"] > 0}
    except Exception as e:
        print(f"Error getting portfolio: {e}")
        return {}

def get_portfolio_performance():
    holdings = get_portfolio_holdings()
    if not holdings:
        return "No holdings logged yet."
    lines = ["Portfolio:\n"]
    total_cost = 0
    total_value = 0
    for ticker, h in holdings.items():
        data = fetch_price(ticker)
        avg = h["avg_cost"]
        qty = h["qty"]
        cost = avg * qty
        if data:
            current = data["price"]
            value = current * qty
            pnl = value - cost
            pnl_pct = (pnl / cost * 100) if cost else 0
            sign = "+" if pnl >= 0 else ""
            flag = "✅" if pnl >= 0 else "⚠️"
            lines.append(
                f"{flag} {ticker}: {qty:.0f} shares @ avg {data['currency']} {avg:.2f} "
                f"| now {data['currency']} {current:.2f} | {sign}{pnl_pct:.1f}% ({sign}{data['currency']} {pnl:.2f})"
            )
            total_cost += cost
            total_value += value
        else:
            lines.append(f"• {ticker}: {qty:.0f} shares @ avg {avg:.2f} (price unavailable)")
            total_cost += cost
    if total_cost > 0:
        total_pnl = total_value - total_cost
        total_pct = (total_pnl / total_cost * 100)
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\nTotal P&L: {sign}{total_pct:.1f}% ({sign}${total_pnl:.2f})")
    return "\n".join(lines)

def get_portfolio_rows():
    try:
        ws = portfolio_sheet()
        records = ws.get_all_records()
        return [(i + 2, r) for i, r in enumerate(records)]
    except Exception as e:
        print(f"get_portfolio_rows error: {e}")
        return []

def format_portfolio_delete_list(rows):
    if not rows:
        return "No holdings in portfolio."
    lines = ["Which holding do you want to remove?\n"]
    for i, (_, r) in enumerate(rows, 1):
        ticker = r.get("Stock", "?")
        qty = r.get("Quantity", "?")
        price = r.get("Buy Price", "?")
        buy_date = r.get("Buy Date", "")
        line = f"{i}. {ticker} — {qty} shares @ ${price}"
        if buy_date:
            line += f" (bought {buy_date})"
        lines.append(line)
    lines.append("\nReply with a number to remove.")
    return "\n".join(lines)

def search_portfolio_by_ticker(query):
    try:
        ws = portfolio_sheet()
        records = ws.get_all_records()
        return [(i + 2, r) for i, r in enumerate(records)
                if query.upper() in r.get("Stock", "").upper()]
    except Exception as e:
        print(f"search_portfolio_by_ticker error: {e}")
        return []

def delete_portfolio_row(sheet_row):
    try:
        ws = portfolio_sheet()
        all_values = ws.get_all_values()
        if sheet_row < 2 or sheet_row > len(all_values):
            return "Couldn't find that holding."
        row = all_values[sheet_row - 1]
        ticker = row[0] if row else "?"
        ws.delete_rows(sheet_row)
        return f"Removed {ticker} from portfolio ✅"
    except Exception as e:
        return f"Error removing holding: {str(e)}"


# --- Price alerts ---

def set_price_alert(ticker, condition, alert_price):
    config.price_alerts[ticker.upper()] = {
        "condition": condition, "price": alert_price, "active": True
    }

async def check_price_alerts(app):
    for ticker, alert in list(config.price_alerts.items()):
        if not alert.get("active"):
            continue
        try:
            data = fetch_price(ticker)
            if not data:
                continue
            current = data["price"]
            condition = alert["condition"]
            target = alert["price"]
            triggered = (condition == "below" and current <= target) or (condition == "above" and current >= target)
            if triggered:
                await app.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🔔 Price alert: {ticker} is {condition} ${target:.2f} — currently ${current:.2f}"
                )
                config.price_alerts[ticker]["active"] = False
        except Exception as e:
            print(f"check_price_alerts error for {ticker}: {e}")


# --- Stock detection ---

def is_stock_request(text):
    lower = text.lower()
    triggers = [
        "price of", "stock price", "share price", "how is", "how's",
        "check ", "what's ", "what is ", "how much is",
        "alert me if", "alert me when",
        "bought shares", "shares of", "shares at", "shares @",
        "portfolio", "holdings", "market summary"
    ]
    if any(t in lower for t in triggers):
        ticker_patterns = re.findall(r'\b([A-Z]{1,5}\.?(?:SI|HK|L|T|NS|BO|SS|SZ)?)\b|\b(\^[A-Z]+)\b', text.upper())
        ticker_candidates = [p[0] or p[1] for p in ticker_patterns if p[0] or p[1]]
        if ticker_candidates or any(t in lower for t in ["portfolio", "holdings", "market summary"]):
            return True
    return False


def handle_stock_request(text):
    try:
        lower = text.lower()
        prompt = (
            f"Parse this stock/finance request and return ONLY JSON:\n"
            f"Request: '{text}'\n\n"
            f"JSON fields:\n"
            f"- intent: one of: price_check, set_alert, portfolio_add, portfolio_view, stock_suggest, market_summary\n"
            f"- ticker: string (stock ticker, normalised, or empty)\n"
            f"- alert_condition: 'above' or 'below'\n"
            f"- alert_price: number\n"
            f"- quantity: number\n"
            f"- price: number\n"
            f"- criteria: string (for stock_suggest)\n\n"
            f"Return ONLY the JSON."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
        parsed = json.loads(raw)
        intent = parsed.get("intent", "price_check")
        ticker = parsed.get("ticker", "").upper()

        if intent == "price_check" and ticker:
            data = fetch_price(ticker)
            if data:
                if "OTC" in data.get("fullExchangeName", "").upper() or "Pink" in data.get("fullExchangeName", ""):
                    print(f"OTC warning for {ticker}")
                name = data.get("name", ticker)
                headlines, sources = _fetch_rss_headlines_for_stock(ticker, name)
                labelled = [(h, get_source_label(s)) for h, s in zip(headlines, sources)]
                usable = [(h, l) for h, l in labelled if l]
                if usable:
                    source_context = "; ".join(f"{h[:60]} [{l}]" for h, l in usable[:3])
                    try:
                        summary_resp = client.messages.create(
                            model="claude-haiku-4-5-20251001", max_tokens=120,
                            messages=[{"role": "user", "content": (
                                f"Headlines about {name} ({ticker}):\n{source_context}\n\n"
                                "Write 2-3 short factual sentences as one paragraph. "
                                "First sentence: 52-week range position. "
                                "Remaining: business developments from headlines. "
                                "No speculation. End with a single [SourceName] tag."
                            )}]
                        )
                        summary = summary_resp.content[0].text.strip()
                    except Exception:
                        summary = _generate_price_movement_summary(data)
                else:
                    summary = _generate_price_movement_summary(data)
                return format_price(data, summary=summary)
            if ALPHA_VANTAGE_API_KEY:
                return f"Couldn't fetch {ticker} — Alpha Vantage and Yahoo Finance both failed. The ticker may be wrong, or try again in a moment."
            return f"Couldn't fetch {ticker}. Check the ticker and try again."

        elif intent == "set_alert" and ticker:
            condition = parsed.get("alert_condition", "below")
            alert_price = parsed.get("alert_price", 0)
            if not alert_price:
                return "What price should I alert you at? Try: 'alert me if AAPL drops below $180'"
            set_price_alert(ticker, condition, alert_price)
            return f"Alert set — I'll let you know when {ticker} goes {condition} ${alert_price:.2f}."

        elif intent == "portfolio_add" and ticker:
            qty = parsed.get("quantity", 0)
            price = parsed.get("price", 0)
            if not qty or not price:
                return "I need the quantity and price. Try: 'bought 100 AAPL at $180'"
            log_portfolio_buy(ticker, qty, price)
            return f"Logged — {qty:.0f} {ticker} @ ${price:.2f}."

        elif intent == "portfolio_view":
            return get_portfolio_performance()

        elif intent == "market_summary":
            return "Pulling the latest market data, give me a sec..."

        else:
            if ticker:
                data = fetch_price(ticker)
                if data:
                    return format_price(data)
                return f"Couldn't find data for {ticker} — check the ticker and try again."
            return "Couldn't work out what stock you're asking about. Try 'price of AAPL' or 'what's DBS at'."

    except Exception as e:
        print(f"handle_stock_request error: {e}")
        return "Something went wrong with that stock request — try again in a moment."


# --- Market Summary ---

_HEADLINE_REJECT = [
    "will it", "will they", "should you", "best stocks", "stocks to watch",
    "to buy and watch", "to watch:", "opening:", "opening bell", "preview:",
    "what to expect", "top picks", "analyst picks", "should i", "is it time",
    "here's what", "what you need", "everything you need",
]

def fetch_market_rss_headlines(market_name):
    import xml.etree.ElementTree as ET
    try:
        queries = {
            "US": ["US stock market today", "Wall Street S&P 500", "Federal Reserve economy"],
            "China": ["China stock market", "Shanghai economy", "China trade economy"],
            "India": ["India Nifty stock market", "RBI India economy", "BSE Sensex"],
        }
        key = next((k for k in queries if k in market_name), None)
        if not key:
            return []
        headlines = []
        for q_text in queries[key]:
            if len(headlines) >= 3:
                break
            q = q_text.replace(" ", "+")
            url = f"https://news.google.com/rss/search?q={q}&hl=en-SG&gl=SG&ceid=SG:en"
            try:
                resp = requests.get(url, timeout=5)
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    if len(headlines) >= 3:
                        break
                    title = item.findtext("title", "").split(" - ")[0].strip()
                    if not title or title in headlines:
                        continue
                    if any(pattern in title.lower() for pattern in _HEADLINE_REJECT):
                        continue
                    headlines.append(title)
            except Exception as e:
                print(f"RSS fetch error for {market_name} query '{q_text}': {e}")
        return headlines[:3]
    except Exception as e:
        print(f"fetch_market_rss_headlines error: {e}")
        return []


def get_market_summary_now():
    try:
        market_data_blocks = []
        all_headlines = []

        for market, indices in MARKET_INDICES.items():
            flag = MARKET_FLAGS_MAP.get(market, "🌐")
            ticker, index_name = list(indices.items())[0]
            weekly_pct = fetch_weekly_change(ticker)
            if weekly_pct is None:
                price_data = fetch_price(ticker)
                weekly_pct = price_data["change_pct"] if price_data else 0
                arrow = "▲" if weekly_pct >= 0 else "▼"
                pct_str = f"Day {arrow} {abs(weekly_pct):.1f}%"
            else:
                arrow = "▲" if weekly_pct >= 0 else "▼"
                pct_str = f"Week {arrow} {abs(weekly_pct):.1f}%"

            headlines = fetch_market_rss_headlines(market)
            all_headlines.extend(headlines)
            market_data_blocks.append({
                "market": market, "flag": flag, "index_name": index_name,
                "pct_str": pct_str, "weekly_pct": weekly_pct, "headlines": headlines,
            })

        data_summary = ""
        for b in market_data_blocks:
            data_summary += f"\n{b['flag']} {b['market']} — {b['index_name']} {b['pct_str']}\n"
            if b["headlines"]:
                data_summary += "Headlines: " + "; ".join(b["headlines"]) + "\n"

        try:
            claude_resp = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=500,
                messages=[{"role": "user", "content": (
                    f"Here is this week's market data:\n{data_summary}\n\n"
                    "For each market (US, China, India), write exactly 3 bullet points. "
                    "Each bullet = 1 short informative sentence grounded in the headlines. "
                    "No speculation. Then write a 2-3 sentence overall summary. "
                    "Format exactly as:\n"
                    "US_BULLETS:\n• ...\n• ...\n• ...\n"
                    "CHINA_BULLETS:\n• ...\n• ...\n• ...\n"
                    "INDIA_BULLETS:\n• ...\n• ...\n• ...\n"
                    "OVERALL:\n[2-3 sentences]"
                )}]
            )
            raw = claude_resp.content[0].text.strip()
        except Exception as e:
            print(f"Market summary Claude error: {e}")
            raw = ""

        def extract_section(text, key):
            import re as _re
            pattern = _re.compile(_re.escape(key) + r"(.*?)(?=\n[A-Z_]+:|$)", _re.DOTALL | _re.IGNORECASE)
            m = pattern.search(text)
            if not m:
                return []
            chunk = m.group(1)
            return [l.strip() for l in chunk.split("\n") if l.strip().startswith("•")][:3]

        def extract_overall(text):
            import re as _re
            pattern = _re.compile(r"OVERALL:(.*?)$", _re.DOTALL | _re.IGNORECASE)
            m = pattern.search(text)
            return m.group(1).strip() if m else ""

        us_bullets = extract_section(raw, "US_BULLETS:")
        china_bullets = extract_section(raw, "CHINA_BULLETS:")
        india_bullets = extract_section(raw, "INDIA_BULLETS:")
        overall = extract_overall(raw)
        bullet_map = {"US": us_bullets, "China": china_bullets, "India": india_bullets}

        lines = [f"📊 *Market Summary* — {date.today().strftime('%d %b %Y')}\n"]
        for b in market_data_blocks:
            header = f"{b['flag']} *{b['market']} — {b['index_name']} ({b['pct_str']})*"
            bullets = bullet_map.get(b["market"], [])
            section = header
            for bullet in (bullets or ["• Market data unavailable"]):
                section += f"\n{bullet}"
            lines.append(section)

        if overall:
            lines.append(f"\n{overall}")

        return "\n\n".join(lines)

    except Exception as e:
        return f"❌ Couldn't pull market data right now ({type(e).__name__}: {str(e)[:80]})"


async def send_weekly_market_summary(app):
    try:
        summary = get_market_summary_now()
        await app.bot.send_message(chat_id=YOUR_CHAT_ID, text=summary, parse_mode="Markdown")
        # Record last sent date
        from sheets import get_sheet
        sheet = get_sheet("Settings")
        if sheet:
            records = sheet.get_all_records()
            for i, r in enumerate(records):
                if r.get("Key") == "market_summary_last_sent":
                    sheet.update_cell(i + 2, 2, date.today().isoformat())
                    return
            sheet.append_row(["market_summary_last_sent", date.today().isoformat()])
    except Exception as e:
        print(f"send_weekly_market_summary error: {e}")


def suggest_stocks(criteria):
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=400,
            messages=[{"role": "user", "content": (
                f"Suggest 3 stocks based on: {criteria}. "
                "For each: ticker, name, exchange, and one sentence on why it fits. "
                "Only suggest real, actively traded stocks. "
                "Format: 🔹 TICKER (Name, Exchange) — reason"
            )}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return "Couldn't generate stock suggestions right now."
