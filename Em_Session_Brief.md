# Em Session Brief
_Paste this at the start of every dev session. Update after each deploy._

**Last updated: 02/05/2026 — Session 25**

---

## Architecture
- **State:** 20-module architecture (post-S22). bot.py is the active monolith until S22 modularisation completes.
- **Modules:** config · clients · state · sheets · helpers · crm · expenses · fx · reminders · cal · todos · meetings · bills · restaurants · stocks · trips · sessions · routing · infrastructure · bot.py
- **Import order:** config → clients → state → sheets → helpers → feature modules → sessions → routing → infrastructure → bot.py. No reverse imports. Feature modules never import each other except meetings→crm (one-way). routing.py is the sole omnimporter.
- **Repo:** roystenteng21/telegram-claude-bot
- **Runtime:** Railway. Deploy via deploy.py.

---

## Coding Standards (locked)
- **R1:** Route by cost — exact match → regex → keyword → cached lookup → live data → Claude. Never call Claude for routing decisions.
- **R2:** Single pass, immediate exit. Once a handler matches, execution stops.
- **R3:** One sheet read per request, cached in memory, invalidated only on write.
- **R4:** Haiku for classification/extraction/JSON under 200 tokens. Sonnet for reasoning/conversation/multi-step only.
- **R5:** Typing indicator before every external API call. Parallel calls use asyncio.gather().
- **R6:** Session messages never reach main routing chain. Sessions time out cleanly.
- **R7:** Cache hierarchy: in-memory → cached sheet read → live sheet read → external API.
- **R8:** Every branch sets reply or returns. Empty reply is a bug. Claude fallback for genuine unknown intent only.

---

## Rules
- **Ship Rule:** Nothing ships until fully wired, tested, and deployed in the same session.
- **Feasibility Rule:** Never suggest a solution without certainty it is feasible — applies to code, claims, and performance predictions alike.
- **No Silent Changes:** Any change to module boundaries, import structure, coding standards, or deploy flow must be logged to Dev Notes immediately.
- **Circular Import Zero Tolerance:** Never work around a circular import with lazy imports or importlib. Restructure instead.
- **Em Log Rule:** Every session must produce a complete Em Log entry. Built/Fixed/Pending must be specific.

---

## Session Protocol
- Explicit go-ahead required before building anything, no matter the size
- Upload only the module files relevant to the task (check Module Registry if unsure)
- If routing logic is touched, also upload routing.py
- During planning: be thorough. During building: no narration, execute and deliver.
- Always provide deploy command at end of session.

---

## Deploy Command Format
```
python3 ~/telegram-claude-bot/deploy.py "commit msg" "Session N" "built" "fixed" "pending"
```

---

## Last Session (S25 — 02/05/2026)
- **Built:** `log bug` command with priority inference; expense summary natural phrasing fix (keyword pattern check)
- **Fixed:** Session immediately expiring before user could reply yes/edit — `touch_session` now called on `receipt_confirm_sessions` creation in expenses.py
- **Pending:** `clear_done_backlog` threshold 3→2 — needs infrastructure.py upload next session
- **Commit:** boot

---

## Open Backlog (🔲 items only)
- 🟡 `clear_done_backlog` threshold 3→2 (infrastructure.py)

---

## Expense Categories
FnB · Transport · Entertainment · Personal · Family · Work · Shopping · Household · Travel

## Cards
Maybank (4002, default FnB) · Citi (1176, default General) · UOB (5372) · Amex (1008)

---
_Update "Last Session" and "Open Backlog" after every deploy._
