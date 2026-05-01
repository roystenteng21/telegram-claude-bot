#!/usr/bin/env python3
"""
test_em.py — Em test suite (post-modularisation)
Run: python3 ~/telegram-claude-bot/test_em.py
All 91 tests must pass before deploy.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Import from modules (not bot.py) ──────────────────────────────────────────
from expenses import (
    is_expense_input, is_log_prefix_input, is_bare_merchant_input,
    fuzzy_match_card, fuzzy_match_category, parse_multi_field_edit,
    get_merchant_emoji,
)
from reminders import (
    is_reminder_request, is_reschedule_request, is_cancel_reminder_request,
)
from stocks import is_stock_request, normalise_ticker
from cal import is_calendar_request
from meetings import is_meeting_start, is_meeting_done
from bills import is_bill_request
from restaurants import (
    is_restaurant_save, is_restaurant_search,
    is_restaurant_review_request, is_restaurant_suggestion_request,
)
from trips import is_overseas_mode_request, extract_flight_number
from crm import detect_crm_natural_update
from helpers import looks_like_new_intent, format_date, calculate_age


# ── Test harness ──────────────────────────────────────────────────────────────

passed = 0
failed = 0
errors = []


def check(name, result, expected):
    global passed, failed
    if result == expected:
        passed += 1
    else:
        failed += 1
        errors.append(f"FAIL [{name}]: got {result!r}, expected {expected!r}")


def section(title):
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print('─' * 50)


# ── is_expense_input ──────────────────────────────────────────────────────────
section("is_expense_input")
check("spent trigger", is_expense_input("spent $12 on lunch"), True)
check("paid trigger", is_expense_input("paid $5 for coffee"), True)
check("dollar sign", is_expense_input("Starbucks $4.50"), True)
check("bought trigger", is_expense_input("bought groceries for $30"), True)
check("flight number guard", is_expense_input("TR450 departs 9am"), False)
check("shares guard", is_expense_input("bought 10 shares of AAPL"), False)
check("delete expense guard", is_expense_input("delete expense Starbucks"), False)
check("expense categories guard", is_expense_input("expense categories"), False)
check("merchant map guard", is_expense_input("merchant map"), False)
check("edit expense guard", is_expense_input("edit last expense"), False)

# ── is_log_prefix_input ───────────────────────────────────────────────────────
section("is_log_prefix_input")
check("log prefix", is_log_prefix_input("log Starbucks $5"), True)
check("log prefix 2", is_log_prefix_input("log lunch $12"), True)
check("no log", is_log_prefix_input("logged $5"), False)
check("too short", is_log_prefix_input("log "), False)

# ── is_reminder_request ───────────────────────────────────────────────────────
section("is_reminder_request")
check("remind me", is_reminder_request("remind me to call James tomorrow"), True)
check("set a reminder", is_reminder_request("set a reminder for 3pm"), True)
check("dont let me forget", is_reminder_request("don't let me forget to buy milk"), True)
check("alert me when", is_reminder_request("alert me when market opens"), True)
check("ping me to", is_reminder_request("ping me to check emails at 9am"), True)
check("not reminder", is_reminder_request("what's the weather today"), False)
check("expense not reminder", is_reminder_request("spent $5 at Starbucks"), False)

# ── is_cancel_reminder_request ────────────────────────────────────────────────
section("is_cancel_reminder_request")
check("cancel reminder", is_cancel_reminder_request("cancel reminder call James"), True)
check("delete reminder", is_cancel_reminder_request("delete reminder"), True)
check("remove reminder", is_cancel_reminder_request("remove my reminder"), True)
check("not cancel", is_cancel_reminder_request("cancel expense"), False)

# ── is_reschedule_request ─────────────────────────────────────────────────────
section("is_reschedule_request")
check("remind me again", is_reschedule_request("remind me again in 2 hours"), True)
check("snooze", is_reschedule_request("snooze"), True)
check("again in", is_reschedule_request("push it to tomorrow"), True)
check("not reschedule", is_reschedule_request("remind me to call James"), False)

# ── is_stock_request ──────────────────────────────────────────────────────────
section("is_stock_request")
check("price of", is_stock_request("price of AAPL"), True)
check("pull up", is_stock_request("pull up DBS"), True)
check("portfolio", is_stock_request("my portfolio"), True)
check("market summary", is_stock_request("market summary"), True)
check("shares bought", is_stock_request("bought 100 shares of AAPL"), True)
check("check stock", is_stock_request("check AAPL"), True)
check("not stock — reminder", is_stock_request("remind me to check reminders"), False)
check("not stock — bill", is_stock_request("my credit card bill"), False)
check("not stock — bought coffee", is_stock_request("bought coffee $5"), False)

# ── is_overseas_mode_request ──────────────────────────────────────────────────
section("is_overseas_mode_request")
check("flying to", is_overseas_mode_request("flying to Bangkok tomorrow"), True)
check("im in tokyo", is_overseas_mode_request("i'm in tokyo"), True)
check("overseas", is_overseas_mode_request("going overseas next week"), True)
check("back home", is_overseas_mode_request("back home now"), True)
check("flight number", is_overseas_mode_request("TR450 departs 9am"), True)
check("not overseas — meeting", is_overseas_mode_request("i'm in a meeting"), False)
check("not overseas — generic im in", is_overseas_mode_request("i'm in the office"), False)

# ── extract_flight_number ─────────────────────────────────────────────────────
section("extract_flight_number")
check("TR450", extract_flight_number("TR450"), "TR450")
check("SQ321", extract_flight_number("flying SQ321 tomorrow"), "SQ321")
check("MH370", extract_flight_number("MH370"), "MH370")
check("no flight", extract_flight_number("going to bangkok"), None)
check("lowercase", extract_flight_number("tr450"), "TR450")

# ── is_bill_request ───────────────────────────────────────────────────────────
section("is_bill_request")
check("bill is due", is_bill_request("my citi bill is due on the 15th"), True)
check("due on the", is_bill_request("due on the 20th each month"), True)
check("add a bill", is_bill_request("add a bill for Netflix"), True)
check("not bill", is_bill_request("remind me to pay Netflix"), False)

# ── is_restaurant_save ────────────────────────────────────────────────────────
section("is_restaurant_save")
check("save restaurant", is_restaurant_save("save restaurant Burnt Ends"), True)
check("want to try", is_restaurant_save("want to try Jiro"), True)
check("maps link", is_restaurant_save("maps.google.com/place/ichiran"), True)
check("add to my list", is_restaurant_save("add to my list Sushi Saito"), True)
check("not save", is_restaurant_save("search restaurants in Tanjong Pagar"), False)

# ── is_restaurant_review_request ──────────────────────────────────────────────
section("is_restaurant_review_request")
check("review of", is_restaurant_review_request("review of Burnt Ends"), True)
check("how is restaurant", is_restaurant_review_request("how is Ichiran"), True)
check("any good", is_restaurant_review_request("is it any good? the restaurant"), True)
check("not review — person", is_restaurant_review_request("how is James doing"), False)

# ── is_restaurant_suggestion_request ─────────────────────────────────────────
section("is_restaurant_suggestion_request")
check("similar to", is_restaurant_suggestion_request("anything similar to Ichiran"), True)
check("suggest restaurant", is_restaurant_suggestion_request("suggest restaurant ramen"), True)
check("places like", is_restaurant_suggestion_request("places like Nando's"), True)
check("not suggestion", is_restaurant_suggestion_request("save restaurant Ichiran"), False)

# ── is_meeting_start / is_meeting_done ───────────────────────────────────────
section("is_meeting_start / is_meeting_done")
check("meeting recap", is_meeting_start("meeting recap for client call"), True)
check("taking notes", is_meeting_start("taking notes"), True)
check("not meeting", is_meeting_start("remind me of meeting at 3pm"), False)
check("done", is_meeting_done("done"), True)
check("thats it", is_meeting_done("thats it"), True)
check("not done", is_meeting_done("not done yet"), False)

# ── is_calendar_request ───────────────────────────────────────────────────────
section("is_calendar_request")
check("schedule", is_calendar_request("schedule dinner tomorrow 7pm"), True)
check("add event", is_calendar_request("add event team standup Monday 9am"), True)
check("not calendar — reminder", is_calendar_request("remind me tomorrow"), False)
check("not calendar — expense", is_calendar_request("spent $50 on dinner"), False)
check("no time anchor — no trigger", is_calendar_request("James is coming over"), False)

# ── detect_crm_natural_update ─────────────────────────────────────────────────
section("detect_crm_natural_update")
r1 = detect_crm_natural_update("James's email is james@gmail.com")
check("email update", r1, ("update", "James", "email", "james@gmail.com"))

r2 = detect_crm_natural_update("Sarah referred Tom")
check("referral", r2, ("referral", "Sarah", "Tom", None))

r3 = detect_crm_natural_update("update John's address to 123 Orchard Road")
check("address update", r3, ("update", "John", "address", "123 orchard road"))

r4 = detect_crm_natural_update("what's the weather today")
check("no match", r4, None)

# ── normalise_ticker ──────────────────────────────────────────────────────────
section("normalise_ticker")
check("dbs", normalise_ticker("dbs"), "D05.SI")
check("tencent", normalise_ticker("tencent"), "0700.HK")
check("AAPL passthrough", normalise_ticker("AAPL"), "AAPL")
check("4-digit HK", normalise_ticker("0700"), "0700.HK")
check("already suffixed", normalise_ticker("D05.SI"), "D05.SI")

# ── fuzzy_match_card / fuzzy_match_category ───────────────────────────────────
section("fuzzy_match_card / fuzzy_match_category")
card, exact = fuzzy_match_card("maybank")
check("card exact", card, "Maybank")
card2, exact2 = fuzzy_match_card("may")
check("card prefix", card2, "Maybank")
cat, exact = fuzzy_match_category("food")
check("category synonym", cat, "FnB")
cat2, exact2 = fuzzy_match_category("FnB")
check("category exact", cat2, "FnB")

# ── parse_multi_field_edit ────────────────────────────────────────────────────
section("parse_multi_field_edit")
edits = parse_multi_field_edit("merchant Starbucks category FnB card Maybank")
check("merchant", edits.get("merchant"), "Starbucks")
check("category", edits.get("category"), "FnB")
check("card", edits.get("card"), "Maybank")

edits2 = parse_multi_field_edit("amount 25.50 currency JPY")
check("amount", edits2.get("amount"), "25.50")
check("currency", edits2.get("currency"), "JPY")

# ── looks_like_new_intent ─────────────────────────────────────────────────────
section("looks_like_new_intent")
check("yes is session reply", looks_like_new_intent("yes"), False)
check("digit is session reply", looks_like_new_intent("2"), False)
check("remind me is intent", looks_like_new_intent("remind me to call James"), True)
check("spent is intent", looks_like_new_intent("spent $50 at Starbucks"), True)
check("short phrase not intent", looks_like_new_intent("ok done"), False)

# ── format_date / calculate_age ───────────────────────────────────────────────
section("format_date / calculate_age")
check("format_date", format_date("21/07/1994"), "21 Jul 1994")
check("format_date bad", format_date("bad"), "bad")
age = calculate_age("21/07/1994")
check("age is numeric string", age.isdigit(), True)
check("age empty on bad", calculate_age("bad"), "")

# ── Summary ───────────────────────────────────────────────────────────────────
total = passed + failed
print(f"\n{'═' * 50}")
print(f"  Results: {passed}/{total} passed", end="")
if failed:
    print(f"  ({failed} FAILED)")
    print()
    for e in errors:
        print(f"  {e}")
else:
    print("  ✅ All passed")
print('═' * 50)

sys.exit(0 if failed == 0 else 1)
