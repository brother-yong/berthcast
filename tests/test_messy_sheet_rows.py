"""Regression tests for messy real-world sheet content (adversarial probe fixes).

An adversarial probe of the upload pipeline found three silent-wrong-answer
classes that real ERP exports and hand-made sheets trigger:

  1. TOTAL / subtotal summary rows inside the data were analysed as products,
     double-counting stock and polluting the report.
  2. The same item split across rows (per-warehouse / per-batch) was judged
     per-row against the item's FULL sales velocity, making one healthy
     100+400 split read as two near-critical items.
  3. Currency-prefixed stock ("S$1,200") parsed to 0, so months-of-supply
     came out 0.0 and the item false-alarmed as CRITICAL.

Fixes under test:
  - agents/shared._to_num: tolerant cell-to-number parsing (commas, currency
    prefixes, accounting negatives, trailing unit words) that never fishes
    digits out of the middle of text.
  - agents/shared._num_sql: SQL-side casts strip currency prefixes too.
  - agents/inventory: total rows dropped (with brand-name safety — TOTAL is
    a real dairy brand), duplicate item rows summed when units agree.

Throwaway DB + stubbed anthropic; no network. Run:
    python tests/test_messy_sheet_rows.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_messyrows.db")
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
from agents.shared import _to_num, _num_sql, _looks_numeric   # noqa: E402
import agents.inventory as inv_mod             # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. _to_num: tolerant but never digit-fishing ─────────────────────────────
_check("plain number", _to_num("120") == 120.0)
_check("thousands separator 1,200", _to_num("1,200") == 1200.0)
_check("decimal 1,200.50", _to_num("1,200.50") == 1200.5)
_check("currency S$1,200", _to_num("S$1,200") == 1200.0)
_check("currency US$500", _to_num("US$500") == 500.0)
_check("currency RM5", _to_num("RM5") == 5.0)
_check("accounting negative (50)", _to_num("(50)") == -50.0)
_check("currency inside parens (S$50)", _to_num("(S$50)") == -50.0)
_check("trailing unit 120 KG", _to_num("120 KG") == 120.0)
_check("minus sign -3", _to_num("-3") == -3.0)
_check("NIL falls to default 0", _to_num("NIL") == 0.0)
_check("empty cell falls to default 0", _to_num("") == 0.0)
_check("None falls to default 0", _to_num(None) == 0.0)
_check("item code ABC123 NOT digit-fished", _to_num("ABC123") == 0.0)
_check("ambiguous 12-34 NOT guessed", _to_num("12-34") == 0.0)
_check("version-like 1.2.3 NOT guessed", _to_num("1.2.3") == 0.0)
_check("default=None distinguishes junk from zero",
       _to_num("NIL", default=None) is None and _to_num("0", default=None) == 0.0)
_check("xlsx scientific notation 1.23e5", _to_num("1.23e5") == 123000.0)
_check("xlsx scientific notation 1.234567E6", _to_num("1.234567E6") == 1234567.0)

# ── 2. _looks_numeric upgraded: currency columns count as numeric ────────────
_check("currency column looks numeric", _looks_numeric(["S$1,200", "S$900", ""]))
_check("name column still not numeric", not _looks_numeric(["EDAM", "GOUDA"]))

# ── 3. _num_sql: SQL-side sums tolerate currency too ─────────────────────────
db.init_db()
db.execute('CREATE TABLE t_numsql ("v" TEXT)')
for v in ("S$1,200", "1,000", "$50"):
    db.execute("INSERT INTO t_numsql VALUES (?)", (v,))
_total = db.query(f"SELECT SUM({_num_sql('v')}) AS s FROM t_numsql")[0]["s"]
_check("SQL sum of S$1,200 + 1,000 + $50 = 2250", _total == 2250.0, detail=str(_total))


# ── Fixtures: agent runs with stubbed Claude ─────────────────────────────────
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


def _new_session(sid):
    db.execute(
        "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
        "VALUES (?,?,?,?,?,?)", (sid, 1, "TestCo", "complete", "all", "{}"))


_INV_COLS = '"item_name" TEXT, "category" TEXT, "current_stock" TEXT, "uom" TEXT'
_SAL_COLS = '"item_name" TEXT, "qty" TEXT, "date" TEXT'


# ── 4. Total/subtotal rows dropped; TOTAL-brand product survives ─────────────
SID = 301
_new_session(SID)
db.execute(f'CREATE TABLE inventory_{SID} ({_INV_COLS})')
db.execute(f'CREATE TABLE sales_{SID} ({_SAL_COLS})')
for row in [("EDAM", "CHEESE", "120", "KG"),
            ("GOUDA", "CHEESE", "150", "KG"),
            ("TOTAL CHEESE", "", "270", ""),          # summary line: drop
            ("Grand Total", "", "270", ""),           # bare total: drop
            ("Total:", "", "270", ""),                # bare total w/ colon: drop
            ("TOTAL GREEK YOGHURT 500G", "DAIRY", "80", "KG")]:  # real brand: KEEP
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", row)

_calls.clear()
_progress.clear()
res = inv_mod.run_inventory_agent(SID, "claude-sonnet-4-6", [], {}, _emit_capture)
prompt = _calls[0] if _calls else ""
_check("run succeeds", "report" in res, detail=str(res)[:120])
_check("'TOTAL CHEESE' summary row dropped", "Item: TOTAL CHEESE" not in prompt)
_check("'Grand Total' row dropped", "Grand Total" not in prompt)
_check("'Total:' row dropped", "Item: Total" not in prompt or "Item: Total:" not in prompt)
_check("TOTAL-brand yoghurt (has unit) KEPT", "TOTAL GREEK YOGHURT 500G" in prompt)
_check("real items still present", "Item: EDAM" in prompt and "Item: GOUDA" in prompt)
_check("progress says how many were skipped",
       any("Skipped 3 total/subtotal" in m for m in _progress), detail=str(_progress))

# ── 5. Duplicate item rows: summed when units agree, kept apart otherwise ────
SID = 302
_new_session(SID)
db.execute(f'CREATE TABLE inventory_{SID} ({_INV_COLS})')
db.execute(f'CREATE TABLE sales_{SID} ({_SAL_COLS})')
for row in [("EDAM", "CHEESE", "100", "KG"),
            ("EDAM", "CHEESE", "400", "KG"),     # same item+unit: merge -> 500
            ("BUTTER", "DAIRY", "10", "KG"),
            ("BUTTER", "DAIRY", "5", "CTN"),     # unit conflict: keep separate
            ("", "MISC", "7", "KG"),
            ("", "MISC", "9", "KG")]:            # blank names: never merged
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", row)
db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?)", ("EDAM", "1200", "2026-01-05"))

_calls.clear()
_progress.clear()
res = inv_mod.run_inventory_agent(SID, "claude-sonnet-4-6", [], {}, _emit_capture)
prompt = _calls[0] if _calls else ""
edam_lines = [ln for ln in prompt.splitlines() if ln.startswith("Item: EDAM")]
butter_lines = [ln for ln in prompt.splitlines() if ln.startswith("Item: BUTTER")]
unknown_lines = [ln for ln in prompt.splitlines() if ln.startswith("Item: Unknown")]
_check("EDAM split rows merged into ONE line", len(edam_lines) == 1, detail=str(edam_lines))
_check("merged EDAM stock is the SUM (500)",
       edam_lines and "Stock: 500" in edam_lines[0], detail=str(edam_lines))
_check("months of supply uses combined stock (500/1200 over 1mo = 0.4)",
       edam_lines and "Months of supply: 0.4" in edam_lines[0], detail=str(edam_lines))
_check("BUTTER unit conflict (KG vs CTN) stays as 2 rows", len(butter_lines) == 2)
_check("blank-name rows NOT merged", len(unknown_lines) == 2)
_check("progress says how many were combined",
       any("Combined 1 duplicate" in m for m in _progress), detail=str(_progress))

# ── 6. Name variants (alias groups) merge into one canonical line ────────────
SID = 303
_new_session(SID)
db.execute(f'CREATE TABLE inventory_{SID} ({_INV_COLS})')
db.execute(f'CREATE TABLE sales_{SID} ({_SAL_COLS})')
for row in [("EDAM 1KG PACK", "CHEESE", "100", "KG"),
            ("EDAM (1KG)", "CHEESE", "400", "KG")]:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", row)
groups = [{"canonical": "EDAM", "variants": ["EDAM 1KG PACK", "EDAM (1KG)"]}]

_calls.clear()
res = inv_mod.run_inventory_agent(SID, "claude-sonnet-4-6", groups, {}, None)
prompt = _calls[0] if _calls else ""
edam_lines = [ln for ln in prompt.splitlines() if ln.startswith("Item: EDAM")]
_check("alias variants merge into one canonical line",
       len(edam_lines) == 1, detail=str(edam_lines))
_check("alias-merged stock is the sum",
       edam_lines and "Stock: 500" in edam_lines[0], detail=str(edam_lines))

# ── 6b. Million-unit merge survives formatting (no scientific notation) ──────
SID = 305
_new_session(SID)
db.execute(f'CREATE TABLE inventory_{SID} ({_INV_COLS})')
db.execute(f'CREATE TABLE sales_{SID} ({_SAL_COLS})')
for row in [("STRAW PCS", "PACKAGING", "900000", "PCS"),
            ("STRAW PCS", "PACKAGING", "600000", "PCS")]:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", row)

_calls.clear()
res = inv_mod.run_inventory_agent(SID, "claude-sonnet-4-6", [], {}, None)
prompt = _calls[0] if _calls else ""
straw_line = next((ln for ln in prompt.splitlines() if ln.startswith("Item: STRAW")), "")
_check("1.5M merged stock shows as plain 1500000, not 1.5e+06",
       "Stock: 1500000" in straw_line, detail=straw_line)

# ── 7. Currency stock: months-of-supply now computed correctly ───────────────
SID = 304
_new_session(SID)
db.execute(f'CREATE TABLE inventory_{SID} ({_INV_COLS})')
db.execute(f'CREATE TABLE sales_{SID} ({_SAL_COLS})')
db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)",
           ("CURRENCY ITEM", "CHEESE", "S$1,200", "KG"))
db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?)", ("CURRENCY ITEM", "600", "2026-01-05"))

_calls.clear()
res = inv_mod.run_inventory_agent(SID, "claude-sonnet-4-6", [], {}, None)
prompt = _calls[0] if _calls else ""
cur_line = next((ln for ln in prompt.splitlines() if ln.startswith("Item: CURRENCY")), "")
_check("currency stock yields correct months of supply (1200/600 = 2.0)",
       "Months of supply: 2.0" in cur_line, detail=cur_line)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll messy-sheet-row tests passed.")
