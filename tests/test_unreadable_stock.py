"""19 Jul 2026: blank / non-numeric / negative stock cells are missing data,
not real quantities.

A live smoke test with a deliberately dirty inventory export produced a
Purchase Order sheet showing "-5 on hand" and "current stock lasts ~-0.3 mo":
a blank stock cell coerced to the string "0", a non-numeric cell parsed to
the default 0.0, and a negative number taken at face value all fed the
deterministic status rule "total sold > 0 AND stock = 0 -> CRITICAL" (or a
negative months-of-supply), so data-entry garbage became a confident red
alert.

The fix classifies those cells as unreadable (stock unknown), never as a
quantity: LOW when the item has sales (worth checking), HEALTHY otherwise,
with an observation naming the bad cell, plus one capped results-page note.

Covers:
  A. agents.verifier.expected_status — stock=None branch (unreadable stock),
     ordered before the total_sold-is-None branch so None > 0 never raises.
  B. agents.inventory.run_inventory_agent — junk stock cells surface as
     'Stock: unreadable' in the prompt (never the raw negative number), the
     verifier corrects a wrongly-CRITICAL stub to LOW for junk cells while
     leaving a true zero-with-sales at CRITICAL, and exactly one data note
     lists the offending cells; a clean file adds no such note.

Run: python tests/test_unreadable_stock.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_unreadablestock.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                      # noqa: E402
import agents.shared as shared             # noqa: E402
import agents.inventory as inv_mod         # noqa: E402
from agents.verifier import expected_status  # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── Part A: verifier unit checks (pure functions, no DB) ─────────────────────
# Each call is wrapped so an exception counts as a failure with a clear label,
# rather than crashing the whole test file before Part B ever runs.
def _safe_expected_status(months_supply, lt_months, stock, total_sold):
    try:
        return expected_status(months_supply, lt_months, stock, total_sold), None
    except Exception as e:
        return None, e


_r1, _e1 = _safe_expected_status(None, None, None, 50)
_check("stock None + sales -> LOW", _e1 is None and _r1 == "LOW",
       detail=f"result={_r1!r} exc={_e1!r}")

_r2, _e2 = _safe_expected_status(None, None, None, 0)
_check("stock None + zero sold -> HEALTHY", _e2 is None and _r2 == "HEALTHY",
       detail=f"result={_r2!r} exc={_e2!r}")

_r3, _e3 = _safe_expected_status(None, None, None, None)
_check("stock None + no sales data -> HEALTHY", _e3 is None and _r3 == "HEALTHY",
       detail=f"result={_r3!r} exc={_e3!r}")

_r4, _e4 = _safe_expected_status(None, None, 0, 50)
_check("real zero + sales still CRITICAL", _e4 is None and _r4 == "CRITICAL",
       detail=f"result={_r4!r} exc={_e4!r}")


# ── Part B: agent flow ───────────────────────────────────────────────────────
db.init_db()
SID = 983

db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "UnreadableStockOrg", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{SID} ('
           '"description" TEXT, "qty_on_hand" TEXT, "category" TEXT, "uom" TEXT)')
_INV_ITEMS = [
    ("ALPHA MILK 1L",     "-5",   "DAIRY", "CTN"),
    ("BRAVO RICE 5KG",    "N/A",  "DRY",   "BAG"),
    ("CHARLIE OIL 1L",    "",     "DRY",   "BTL"),
    ("DELTA SAUCE 500G",  "100",  "DRY",   "BTL"),
    ("ECHO TEA 250G",     "0",    "DRY",   "PKT"),
]
for r in _INV_ITEMS:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", r)

db.execute(f'CREATE TABLE sales_{SID} '
           '("trans_date" TEXT, "item_description" TEXT, "qty" TEXT)')
_SALE_DATES = ("15/04/2026", "15/05/2026", "15/06/2026")
for (name, _stock, _cat, _uom) in _INV_ITEMS:
    for d in _SALE_DATES:
        db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?)", (d, name, "30"))

# Keyword fallback for the auto-mapper's LLM call — "{}" means no override,
# so the plain keyword guess (description/qty_on_hand/category/uom) is used.
shared._call_claude = lambda *a, **k: "{}"

_captured_prompts = []


def _all_critical_reply(model, system, user, max_tokens=4096):
    """Marks EVERY item CRITICAL — deliberately wrong for the junk-stock rows,
    so a pass proves the verifier (not the stub) corrects them. Also captures
    the user prompt so the test can inspect what Claude was actually shown."""
    _captured_prompts.append(user)
    items = []
    for line in user.split("\n"):
        if line.startswith("Item: "):
            name = line.split("|", 1)[0][len("Item: "):].strip()
            items.append(name)
    return json.dumps([
        {"item": name, "status": "CRITICAL", "days_of_supply": 0,
         "observation": "x", "category": "DRY", "spoilage_risk": "NONE"}
        for name in items
    ])


inv_mod._call_claude = _all_critical_reply
result = inv_mod.run_inventory_agent(SID, "m", [], {}, None)

_report = result.get("report") or []
_by_item = {r.get("item"): r for r in _report}

_check("run produced a report", bool(_report), detail=str(result))

_check("ALPHA (negative stock, has sales) corrected to LOW",
       _by_item.get("ALPHA MILK 1L", {}).get("status") == "LOW",
       detail=str(_by_item.get("ALPHA MILK 1L")))
_check("BRAVO (non-numeric stock, has sales) corrected to LOW",
       _by_item.get("BRAVO RICE 5KG", {}).get("status") == "LOW",
       detail=str(_by_item.get("BRAVO RICE 5KG")))
_check("CHARLIE (blank stock, has sales) corrected to LOW",
       _by_item.get("CHARLIE OIL 1L", {}).get("status") == "LOW",
       detail=str(_by_item.get("CHARLIE OIL 1L")))
_check("ECHO (true zero stock, has sales) stays CRITICAL — not weakened",
       _by_item.get("ECHO TEA 250G", {}).get("status") == "CRITICAL",
       detail=str(_by_item.get("ECHO TEA 250G")))

_check("captured at least one prompt", bool(_captured_prompts))
_all_prompts = "\n".join(_captured_prompts)
_check("prompt shows 'Stock: unreadable' for the junk-stock items",
       "Stock: unreadable" in _all_prompts, detail=_all_prompts[:2000])
_check("prompt never shows the raw negative figure 'Stock: -5'",
       "Stock: -5" not in _all_prompts, detail=_all_prompts[:2000])

_notes = result.get("data_notes") or []
_unreadable_notes = [n for n in _notes if "couldn't" in n and "read" in n]
_check("exactly one unreadable-stock data note",
       len(_unreadable_notes) == 1, detail=str(_notes))
_check("the note names an offending item and its raw cell text",
       bool(_unreadable_notes) and "-5" in _unreadable_notes[0],
       detail=str(_unreadable_notes))


# ── Clean file: no unreadable-stock note ─────────────────────────────────────
SID2 = 984
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID2, 1, "UnreadableStockCleanOrg", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{SID2} ('
           '"description" TEXT, "qty_on_hand" TEXT, "category" TEXT, "uom" TEXT)')
_CLEAN_ITEMS = [
    ("DELTA SAUCE 500G",  "100",  "DRY", "BTL"),
    ("ECHO TEA 250G",     "0",    "DRY", "PKT"),
]
for r in _CLEAN_ITEMS:
    db.execute(f"INSERT INTO inventory_{SID2} VALUES (?,?,?,?)", r)
db.execute(f'CREATE TABLE sales_{SID2} '
           '("trans_date" TEXT, "item_description" TEXT, "qty" TEXT)')
for (name, _stock, _cat, _uom) in _CLEAN_ITEMS:
    for d in _SALE_DATES:
        db.execute(f"INSERT INTO sales_{SID2} VALUES (?,?,?)", (d, name, "30"))

inv_mod._call_claude = _all_critical_reply
result2 = inv_mod.run_inventory_agent(SID2, "m", [], {}, None)
_notes2 = result2.get("data_notes") or []
_unreadable_notes2 = [n for n in _notes2 if "couldn't" in n and "read" in n]
_check("clean file (no junk stock cells) adds no unreadable-stock note",
       len(_unreadable_notes2) == 0, detail=str(_notes2))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll unreadable-stock tests passed.")
