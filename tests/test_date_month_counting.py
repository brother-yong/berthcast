"""Regression tests for robust sales-date month counting (client-#2 audit, Tier 1A).

months-of-data is the denominator of ALL velocity math (avg monthly sales =
total sold / months). The old path counted months with SQLite's strftime,
which only understands ISO dates — a file with 15/06/2026 (Singapore's own
standard!) or Excel serial dates silently miscounted, shifting every health
label with no warning.

Fix under test: agents/shared.count_sales_months — Python-side parsing of
ISO, D/M/Y vs M/D/Y (decided once per column), 15-Jun-26 styles, and Excel
serials; returns None (callers fall back) when fewer than half the values
parse, because a velocity denominator must never be guessed from junk.

Run: python tests/test_date_month_counting.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_datemonths.db")
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

import database as db                          # noqa: E402
from agents.shared import count_sales_months   # noqa: E402
import agents.inventory as inv_mod             # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. Unit: each format counts months correctly ─────────────────────────────
r = count_sales_months(["2026-01-05", "2026-01-20", "2026-02-10", "2026-03-01"])
_check("ISO dates: 3 months", r and r[0] == 3, detail=str(r))
_check("ISO label", r and r[1] == "ISO (YYYY-MM-DD)", detail=str(r))

r = count_sales_months(["15/06/2026", "01/06/2026", "02/07/2026"])
_check("DD/MM/YYYY (day>12 evidence): 2 months", r and r[0] == 2, detail=str(r))
_check("DD/MM label", r and r[1] == "DD/MM/YYYY", detail=str(r))

r = count_sales_months(["06/15/2026", "06/01/2026", "07/02/2026"])
_check("MM/DD/YYYY (second token>12 evidence): 2 months", r and r[0] == 2, detail=str(r))
_check("MM/DD label", r and r[1] == "MM/DD/YYYY", detail=str(r))

r = count_sales_months(["05/06/2026", "05/07/2026"])
_check("ambiguous numeric defaults to day-first (SEA): months 6 and 7",
       r and r[0] == 2 and r[1] == "DD/MM/YYYY", detail=str(r))

r = count_sales_months(["15/06/26", "15.07.26", "15-08-26"])
_check("2-digit years + dot/dash separators: 3 months", r and r[0] == 3, detail=str(r))

r = count_sales_months(["2026/06/15", "2026/07/01"])
_check("year-first with slashes: 2 months", r and r[0] == 2, detail=str(r))

r = count_sales_months(["15-Jun-26", "3 March 2026", "01 jun 2026"])
_check("textual month names: Jun + Mar = 2 months", r and r[0] == 2, detail=str(r))

# Excel serials: 45292 = 2024-01-01, 45323 = 2024-02-01 (epoch 1899-12-30).
r = count_sales_months(["45292", "45300", "45323"])
_check("Excel serials: Jan + Feb 2024 = 2 months", r and r[0] == 2, detail=str(r))
_check("Excel serial label", r and r[1] == "Excel serial dates", detail=str(r))

r = count_sales_months(["2026-06-01", "15/07/2026"])
_check("mixed formats flagged in label", r and "mixed" in r[1], detail=str(r))

# ── 2. Unit: junk never becomes a denominator ────────────────────────────────
_check("mostly junk returns None (no guessing)",
       count_sales_months(["cheese", "n/a", "??", "2026-01-05"]) is None)
_check("empty input returns None", count_sales_months([]) is None)
_check("all blanks returns None", count_sales_months(["", None, "  "]) is None)
_check("plain year 2026 is not a date", count_sales_months(["2026", "2025"]) is None)
_check("8-digit numbers are not serials", count_sales_months(["20260615"]) is None)
r = count_sales_months(["2026-01-05", "2026-02-05", "junk", "junk2"])
_check("exactly half parsed is accepted", r and r[0] == 2, detail=str(r))
_check("single month floors at 1",
       count_sales_months(["2026-06-01", "2026-06-30"])[0] == 1)


# ── 3. Integration: the agent's prompt reflects the parsed months ────────────
db.init_db()
_calls = []


def _fake_call_claude(model, system, user, max_tokens=4096):
    _calls.append(user)
    items = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            name = line.split("|")[0].replace("Item:", "").strip()
            items.append({"item": name, "category": "X", "stock": 0,
                          "status": "CRITICAL", "spoilage_risk": "LOW",
                          "days_of_supply": 0, "observation": "t"})
    return json.dumps(items)


inv_mod._call_claude = _fake_call_claude
_progress = []


def _emit_capture(msg):
    _progress.append(msg)


def _make_session(sid, dates):
    db.execute(
        "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
        "VALUES (?,?,?,?,?,?)", (sid, 1, "DateCo", "complete", "all", "{}"))
    db.execute(f'CREATE TABLE inventory_{sid} ("item_name" TEXT, "category" TEXT, '
               '"current_stock" TEXT, "uom" TEXT)')
    db.execute(f'CREATE TABLE sales_{sid} ("item_name" TEXT, "qty" TEXT, "date" TEXT)')
    db.execute(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?)", ("EDAM", "CHEESE", "300", "KG"))
    for d in dates:
        db.execute(f"INSERT INTO sales_{sid} VALUES (?,?,?)", ("EDAM", "100", d))


# (a) DD/MM/YYYY spanning 3 months — the format that used to silently break.
_make_session(501, ["05/04/2026", "15/04/2026", "01/05/2026", "20/06/2026"])
_calls.clear()
_progress.clear()
res = inv_mod.run_inventory_agent(501, "claude-sonnet-4-6", [], {}, _emit_capture)
prompt = _calls[0] if _calls else ""
_check("DD/MM/YYYY file: agent sees 3 months of data", "(3mo)" in prompt,
       detail=prompt[:200])
_check("progress names the detected format",
       any("dates read as DD/MM/YYYY" in m for m in _progress), detail=str(_progress))

# (b) ISO dates — regression: same months as the old SQL path counted.
_make_session(502, ["2026-01-05", "2026-02-10"])
_calls.clear()
res = inv_mod.run_inventory_agent(502, "claude-sonnet-4-6", [], {}, None)
prompt = _calls[0] if _calls else ""
_check("ISO file unchanged: 2 months", "(2mo)" in prompt, detail=prompt[:200])

# (c) Unreadable dates — loud fallback to 12, never a quiet wrong number.
# (13 June: wording changed when the Qty÷Avg inference layer was added — the
# assumption is now also carried to the results page via data_notes.)
_make_session(503, ["next tuesday", "soon", "Q3"])
_calls.clear()
_progress.clear()
res = inv_mod.run_inventory_agent(503, "claude-sonnet-4-6", [], {}, _emit_capture)
prompt = _calls[0] if _calls else ""
_check("junk dates fall back to 12 months", "(12mo)" in prompt, detail=prompt[:200])
_check("fallback is announced in progress",
       any("assuming 12 months" in m for m in _progress),
       detail=str(_progress))
_check("fallback carried to the results page as a data note",
       bool(res.get("data_notes")) and "assumed 12 months" in res["data_notes"][0],
       detail=str(res.get("data_notes")))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll date month-counting tests passed.")
