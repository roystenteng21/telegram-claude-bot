"""
restaurants.py — Restaurant save, search, suggest, review handlers.

Imports from: config, clients, sheets
"""

import re
import json
import requests

from config import YOUR_CHAT_ID
from clients import client
from sheets import restaurants_sheet


# ---------------------------------------------------------------------------
# Parse / Extract
# ---------------------------------------------------------------------------

def parse_restaurant_save(text):
    """Parse a restaurant save request — name, location, tags, notes."""
    prompt = (
        f"Extract restaurant details from: '{text}'\n\n"
        f"Return ONLY a JSON object with:\n"
        f"- name: string (restaurant name)\n"
        f"- location: string (address or area, e.g. 'Teck Lim Road' or 'Shibuya, Tokyo')\n"
        f"- country: string (country, default 'Singapore' if not mentioned)\n"
        f"- tags: string (comma-separated tags like 'date night, japanese, omakase' — only if mentioned, else empty)\n"
        f"- notes: string (any notes like 'need reservation', 'cash only' — only if mentioned, else empty)\n\n"
        f"Return ONLY the JSON."
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def lookup_restaurant_from_maps(url):
    """Extract restaurant name and address from a Google Maps URL via Claude."""
    prompt = (
        f"Given this Google Maps URL: {url}\n\n"
        f"Extract the restaurant/place name and address from the URL itself (don't browse it).\n"
        f"Return ONLY a JSON object with:\n"
        f"- name: string (place name if visible in URL, else empty)\n"
        f"- location: string (area or address if visible, else empty)\n"
        f"- country: string (country if determinable from URL, else 'Singapore')\n\n"
        f"Return ONLY the JSON."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        if not result.get("location"):
            result["_needs_location"] = True
        return result
    except Exception as e:
        print(f"lookup_restaurant_from_maps error: {e}")
        return {"name": "", "location": "", "country": "Singapore", "_needs_location": True}


def infer_restaurant_location(name, country="Singapore"):
    """Use Claude to infer location and outlets for a restaurant name."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"For the restaurant '{name}' in {country}, do the following:\n"
                    "1. Does it have multiple outlets? (yes/no)\n"
                    "2. If yes, list up to 4 outlets as area names only (e.g. Dempsey Hill, Jewel Changi).\n"
                    "3. If no, give the single area/neighbourhood (e.g. Dempsey Hill, Tanjong Pagar).\n\n"
                    "Return ONLY a JSON object with:\n"
                    "- multiple_outlets: boolean\n"
                    "- outlets: list of area strings (1 item if single, up to 4 if multiple)\n"
                    "Return ONLY the JSON."
                )
            }]
        )
        raw = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"infer_restaurant_location error: {e}")
        return {"multiple_outlets": False, "outlets": []}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def save_restaurant(name, location, country="Singapore", tags="", notes="", force_new=False):
    """Save a restaurant to the Restaurants sheet. Checks for duplicates unless force_new."""
    try:
        if not force_new:
            try:
                sheet = restaurants_sheet()
                records = sheet.get_all_records()
                for r in records:
                    if r.get("Name", "").lower() == name.lower():
                        return "_DUPLICATE_:" + name
            except Exception as e:
                print(f"save_restaurant duplicate check error: {e}")
        sheet = restaurants_sheet()
        sheet.append_row([name, location, country, tags, notes])
        return None  # Success
    except Exception as e:
        print(f"save_restaurant error: {e}")
        return f"_ERROR_:{str(e)}"


def delete_restaurant(name):
    """Delete a restaurant by name."""
    try:
        sheet = restaurants_sheet()
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if name.lower() in r.get("Name", "").lower():
                sheet.delete_rows(i + 2)
                return f"Removed {r.get('Name')} from your list."
        return f"No restaurant found matching '{name}'."
    except Exception as e:
        return f"Error: {str(e)}"


def format_restaurant_saved(name, location, tags="", notes="", country="Singapore"):
    """Format the restaurant saved confirmation."""
    lines = ["Saved!"]
    lines.append(f"🏪 {name}")
    loc_str = f"{location}, {country}" if country and country != "Singapore" else location
    lines.append(f"📍 {loc_str}")
    if tags:
        lines.append(f"🏷 {tags}")
    if notes:
        lines.append(f"📝 {notes}")
    return "\n".join(lines)


def search_restaurants(query):
    """Search restaurants by cuisine, location, or tag."""
    try:
        sheet = restaurants_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No restaurants saved yet."

        query_lower = query.lower()
        results = []
        for r in records:
            searchable = " ".join(str(v).lower() for v in r.values())
            if query_lower in searchable:
                results.append(r)

        if not results:
            return f"No restaurants found matching '{query}'."

        lines = [f"{len(results)} restaurant(s) found:\n"]
        for r in results:
            line = f"🏪 {r.get('Name', '')} — 📍 {r.get('Location', '')}"
            if r.get("Tags"):
                line += f" ({r.get('Tags')})"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching restaurants: {str(e)}"


def list_restaurants(country_filter=None):
    """List all saved restaurants, optionally filtered by country."""
    try:
        sheet = restaurants_sheet()
        records = sheet.get_all_records()
        if not records:
            return "No restaurants saved yet."

        if country_filter:
            records = [r for r in records if country_filter.lower() in r.get("Country", "").lower()]

        if not records:
            return f"No restaurants saved for {country_filter}."

        lines = [f"{len(records)} saved restaurant(s):\n"]
        for r in records:
            line = f"🏪 {r.get('Name', '')} — 📍 {r.get('Location', '')}"
            if r.get("Tags"):
                line += f" ({r.get('Tags')})"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing restaurants: {str(e)}"


# ---------------------------------------------------------------------------
# Review & Suggestions
# ---------------------------------------------------------------------------

def get_restaurant_review(name):
    """Fetch RSS headlines and generate formatted review with emoji, 2 bullets, and plain text summary."""
    try:
        import xml.etree.ElementTree as ET
        headlines = []
        queries = [f"{name} restaurant review Singapore", f"{name} Singapore food"]
        for q_text in queries:
            if len(headlines) >= 4:
                break
            q = q_text.replace(" ", "+")
            url = f"https://news.google.com/rss/search?q={q}&hl=en-SG&gl=SG&ceid=SG:en"
            try:
                resp = requests.get(url, timeout=5)
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    if len(headlines) >= 4:
                        break
                    title = item.findtext("title", "").split(" - ")[0].strip()
                    if title and title not in headlines:
                        headlines.append(title)
            except Exception as e:
                print(f"Restaurant review RSS error for {name}: {e}")
                continue

        if not headlines:
            return f"Couldn't find recent reviews for {name} — try searching online for the latest."

        area = ""
        try:
            ws = restaurants_sheet()
            records = ws.get_all_records()
            for r in records:
                if name.lower() in r.get("Name", "").lower():
                    area = r.get("Location", "").strip()
                    break
        except Exception as e:
            print(f"Restaurant area lookup error: {e}")

        try:
            location_hint = f"The restaurant's saved address is: {area}. " if area else ""
            area_resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=20,
                messages=[{
                    "role": "user",
                    "content": (
                        f"{location_hint}What neighbourhood or area is '{name}' in Singapore known to be in? "
                        "Reply with just the neighbourhood name (e.g. Dempsey Hill, Tanjong Pagar, Orchard). "
                        "If unknown, reply 'Singapore'."
                    )
                }]
            )
            area = area_resp.content[0].text.strip()
        except Exception:
            area = area or ""

        headline_text = "\n".join(f"- {h}" for h in headlines)
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Here are headlines about the restaurant '{name}':\n{headline_text}\n\n"
                        "Write a review in this exact format:\n"
                        "EMOJI: [one contextual emoji for the restaurant vibe/cuisine — varied, creative, not always 🍽]\n"
                        "BULLET1: [one sentence — a highlight or strength, grounded in the headlines]\n"
                        "BULLET2: [one sentence — a caveat, limitation, or honest note]\n"
                        "SUMMARY: [exactly 2 short sentences — overall impression. Plain, honest, no hedging about sources.]\n"
                    )
                }]
            )
            raw = resp.content[0].text.strip()

            def get_field(text, key):
                if key not in text:
                    return ""
                line = text.split(key)[-1].split("\n")[0].strip().lstrip(":").strip()
                return line

            emoji = get_field(raw, "EMOJI:")
            bullet1 = get_field(raw, "BULLET1:")
            bullet2 = get_field(raw, "BULLET2:")
            summary = get_field(raw, "SUMMARY:")

            if not emoji:
                emoji = "🍽"

            area_str = f" ({area})" if area else ""
            result = f"{emoji} *{name}*{area_str}\n"
            if bullet1:
                result += f"\n• {bullet1}\n"
            if bullet2:
                result += f"\n• {bullet2}"
            if summary:
                result += f"\n\n{summary}"

            return result
        except Exception as e:
            print(f"Restaurant review Claude error: {e}")
            return f"Found some mentions of {name} but couldn't summarise them right now."
    except Exception as e:
        return f"Couldn't fetch reviews for {name} right now."


def get_similar_restaurants(text):
    """Suggest similar restaurants based on cuisine/vibe — grounded in RSS results."""
    try:
        import xml.etree.ElementTree as ET
        lower = text.lower()
        ref_name = None
        for trigger in ["similar to", "like ", "anything like", "places like",
                        "restaurants like", "something like", "alternatives to", "similar places to"]:
            if trigger in lower:
                idx = lower.index(trigger) + len(trigger)
                ref_name = text[idx:].strip().rstrip("?").strip()
                break

        context = ""
        if ref_name:
            try:
                ws = restaurants_sheet()
                records = ws.get_all_records()
                for r in records:
                    if ref_name.lower() in r.get("Name", "").lower():
                        tags = r.get("Tags", "")
                        context = f"'{r['Name']}' is tagged as: {tags}." if tags else ""
                        break
            except Exception as e:
                print(f"Similar restaurant sheet lookup error: {e}")

        real_names = []
        try:
            query = f"best restaurants similar to {ref_name or text} Singapore".replace(" ", "+")
            url = f"https://news.google.com/rss/search?q={query}&hl=en-SG&gl=SG&ceid=SG:en"
            resp = requests.get(url, timeout=3)
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:5]:
                title = item.findtext("title", "").split(" - ")[0].strip()
                if title:
                    real_names.append(title)
        except Exception as e:
            print(f"Similar restaurants RSS error: {e}")

        real_context = f"These headlines may help identify real options: {'; '.join(real_names[:3])}" if real_names else ""

        prompt = (
            f"Suggest up to 3 restaurants in Singapore similar to '{ref_name or text}'. "
            f"{context} {real_context} "
            "Only suggest real restaurants you are fully confident exist and can be found on Google Maps in Singapore right now. "
            "If you cannot confidently name any, reply with exactly: NONE "
            "For each suggestion: name, area, and one sentence on why it's similar. "
            "Format each as: 🍽 [Name] — [Area] — [Why similar]\n---"
        )
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        result = resp.content[0].text.strip()
        if result.upper().startswith("NONE"):
            return "Nothing comes to mind right now."
        return result
    except Exception as e:
        return "Couldn't generate suggestions right now — try again in a moment."


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def is_restaurant_save(text):
    """Detect restaurant save intent."""
    lower = text.lower()
    triggers = [
        "save restaurant", "add restaurant", "save this place", "add this place",
        "save this restaurant", "try this restaurant", "this restaurant to try",
        "want to try", "add to my list", "save to my list", "maps.google",
        "goo.gl/maps", "maps.app.goo", "restaurant to try", "place to try",
        "log this restaurant", "note this restaurant", "remember this place",
        "remember this restaurant"
    ]
    return any(t in lower for t in triggers)


def is_restaurant_search(text):
    """Detect restaurant search intent."""
    lower = text.lower()
    triggers = ["find a restaurant", "search restaurants", "any restaurants",
                "restaurant recommendations", "where to eat", "restaurants in",
                "show my restaurants", "my restaurant list", "saved restaurants"]
    return any(t in lower for t in triggers)


def is_restaurant_suggestion_request(text):
    """Detect similar restaurant suggestion request."""
    lower = text.lower()
    triggers = ["similar to", "like ", "anything like", "places like",
                "restaurants like", "something like", "alternatives to", "similar places"]
    return any(t in lower for t in triggers)


def is_restaurant_review_request(text):
    """Detect restaurant review request intent."""
    lower = text.lower()
    if any(t in lower for t in ["reviews for", "review of", "reviews of"]):
        return True
    how_match = re.search(r"how(?:'s| is)\s+(.+?)(?:\?|$)", lower)
    if how_match:
        subject = how_match.group(1).strip()
        person_signals = ["he", "she", "they", "him", "her", "doing", "feeling", "been"]
        if any(p in subject.split() for p in person_signals):
            return False
        food_signals = ["restaurant", "place", "cafe", "ramen", "sushi", "bbq", "bar",
                        "bistro", "hawker", "eatery", "kitchen", "grill"]
        if any(f in subject for f in food_signals):
            return True
        if len(subject.split()) <= 3 and not any(p in subject for p in person_signals):
            return True
    restaurant_context = ["restaurant", "place", "cafe", "bar", "eatery"]
    if any(t in lower for t in ["is it good", "is it worth", "worth going", "worth visiting", "any good"]):
        if any(c in lower for c in restaurant_context):
            return True
    return False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_save_restaurant(text, force_new=False):
    """Handle saving a restaurant from text or maps link."""
    try:
        if "maps.google" in text or "goo.gl/maps" in text or "maps.app.goo" in text:
            words = text.split()
            url = next((w for w in words if "map" in w.lower() or "goo.gl" in w.lower()), "")
            parsed = lookup_restaurant_from_maps(url)
            name = parsed.get("name", "")
            location = parsed.get("location", "")
            country = parsed.get("country", "Singapore")
            tags = ""
            notes = ""

            if not name:
                return "I got the link but couldn't extract the name. Try: 'save Burnt Ends, Teck Lim Road, tag: date night'"

            if parsed.get("_needs_location") or not location:
                return f"_NEEDS_LOCATION_:{name}:{country}"
        else:
            parsed = parse_restaurant_save(text)
            name = parsed.get("name", "")
            location = parsed.get("location", "")
            country = parsed.get("country", "Singapore")
            tags = parsed.get("tags", "")
            notes = parsed.get("notes", "")

        if not name:
            return "What's the restaurant name? Try: 'save Burnt Ends'"

        if location:
            result = save_restaurant(name, location, country, tags, notes, force_new=force_new)
            if result and result.startswith("_DUPLICATE_:"):
                existing = result.split(":", 1)[1]
                return f"_DUPLICATE_RESTAURANT_:{existing}:{name}:{location}:{country}:{tags}:{notes}"
            return format_restaurant_saved(name, location, tags, notes)

        location_data = infer_restaurant_location(name, country)
        outlets = location_data.get("outlets", [])
        multiple = location_data.get("multiple_outlets", False)
        return f"_INFER_LOCATION_:{name}:{country}:{tags}:{int(multiple)}:" + "|".join(outlets)

    except Exception as e:
        return f"Couldn't save that restaurant: {str(e)}"


def handle_search_restaurants(text):
    """Handle a restaurant search request."""
    try:
        lower = text.lower()
        if "my restaurant list" in lower or "show my restaurants" in lower or "saved restaurants" in lower:
            return list_restaurants()

        for trigger in ["restaurants in", "find a restaurant", "any restaurants",
                        "where to eat", "restaurant recommendations", "search restaurants"]:
            if trigger in lower:
                query = lower.replace(trigger, "").strip()
                if query:
                    return search_restaurants(query)
                else:
                    return list_restaurants()

        return search_restaurants(text)
    except Exception as e:
        return f"Error: {str(e)}"
