"""Regression tests for the Cool Link "all healthy but no stock" incident.

A staff member uploaded a hand-made cheese sheet whose columns were:
  Category | Item Name | Supplier | Sales (Pre-Tax) | Qty Sold | Unit |
  Avg Qty / Month | Current System balance   (the balance column was EMPTY)

Two bugs combined:
  1. Stock-column detection took the FIRST fuzzy match in sheet order, so
     "Qty Sold" was read as current stock. Stock == total sold makes
     months-of-supply identical for every item -> the whole catalogue
     reported HEALTHY even though the real balance column was blank.
  2. Nothing checked whether the stock column had any values at all, so the
     run proceeded silently instead of telling the user the file had no
     stock data.

Fixes under test:
  - agents/shared._pick_stock_column: tiered detection, movement/rate/money
    words ("sold", "avg", "value", ...) can never be picked as stock.
  - agents/inventory: hard stop with a plain-language error when the chosen
    stock column is empty on every row.

Run: python tests/test_stock_column_detection.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_stockcol.db")
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
from agents.shared import _pick_stock_column   # noqa: E402
import agents.inventory as inv_mod         # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. Unit: column picking ──────────────────────────────────────────────────
# The Cool Link sheet, exactly as _sanitize_name stores it. Column order
# matters: qty_sold comes BEFORE current_system_balance, which is what made
# first-match-wins pick the wrong one.
COOLLINK_COLS = ["category", "item_name", "supplier", "sales_pre_tax",
                 "qty_sold", "unit", "avg_qty_month", "current_system_balance"]

_check("Cool Link sheet: stock = current_system_balance, never qty_sold",
       _pick_stock_column(COOLLINK_COLS) == "current_system_balance",
       detail=str(_pick_stock_column(COOLLINK_COLS)))

_check("legacy export still picks qty_on_hand",
       _pick_stock_column(["description", "qty_on_hand", "category", "uom"]) == "qty_on_hand")

_check("plain qty still works",
       _pick_stock_column(["item_name", "qty", "category"]) == "qty")

_check("a sheet with ONLY sold/avg quantity columns yields no stock column",
       _pick_stock_column(["item_name", "qty_sold", "avg_qty_month"]) is None)

_check("stock_value (money) is not mistaken for stock units",
       _pick_stock_column(["item_name", "stock_value", "closing_stock"]) == "closing_stock")

_check("allocated_qty is not mistaken for stock",
       _pick_stock_column(["item_name", "allocated_qty", "balance"]) == "balance")


# ── 2. Integration fixtures: her file as a session ───────────────────────────
db.init_db()

SID_EMPTY, SID_FILLED = 77, 78
for sid in (SID_EMPTY, SID_FILLED):
    db.execute(
        "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
        "VALUES (?,?,?,?,?,?)",
        (sid, 1, "TestCo", "complete", "all", "{}"),
    )

_SHEET_COLS = ('"category" TEXT, "item_name" TEXT, "supplier" TEXT, '
               '"sales_pre_tax" TEXT, "qty_sold" TEXT, "unit" TEXT, '
               '"avg_qty_month" TEXT, "current_system_balance" TEXT')

# Rows mirror the screenshot; merged cells mean category/supplier are blank
# on continuation rows. Balance is empty everywhere for session 77.
_ROWS = [
    ("CHEESE", "AMMERLAND EDAM",    "AMMERLAND", "62145.57", "7299.267", "KG", "2433.089", ""),
    ("",       "AMMERLAND GOUDA",   "",          "59496.74", "6986.255", "KG", "2328.75",  ""),
    ("",       "AMMERLAND CHEDDAR", "",          "",         "",         "KG", "0",        ""),
    ("",       "PAMPA EDAM",        "PAMPA",     "59641.17", "7085.817", "KG", "2361.939", ""),
]

for sid, balance_vals in ((SID_EMPTY, ["", "", "", ""]),
                          (SID_FILLED, ["0", "150", "0", "920.5"])):
    db.execute(f'CREATE TABLE inventory_{sid} ({_SHEET_COLS})')
    db.execute(f'CREATE TABLE sales_{sid} ({_SHEET_COLS})')
    for row, bal in zip(_ROWS, balance_vals):
        vals = row[:-1] + (bal,)
        db.execute(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?,?,?,?,?)", vals)
        db.execute(f"INSERT INTO sales_{sid} VALUES (?,?,?,?,?,?,?,?)", vals)

# Capture what (if anything) gets sent to Claude.
_calls = []


def _fake_call_claude(model, system, user, max_tokens=4096):
    _calls.append({"system": system, "user": user})
    # Echo a minimal valid health report for every item in the prompt.
    items = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            name = line.split("|")[0].replace("Item:", "").strip()
            items.append({"item": name, "category": "CHEESE", "stock": 0,
                          "status": "CRITICAL", "spoilage_risk": "MEDIUM",
                          "days_of_supply": 0, "observation": "test"})
    return json.dumps(items)


inv_mod._call_claude = _fake_call_claude

_progress = []


def _emit_capture(msg):
    _progress.append(msg)


# ── 3. Empty balance column: hard stop, Claude never called ──────────────────
res = inv_mod.run_inventory_agent(SID_EMPTY, "claude-sonnet-4-6", [], {}, _emit_capture)
_check("empty stock column stops the run with an error", "error" in res, detail=str(res)[:120])
_check("error names the column and says it is empty",
       "current_system_balance" in res.get("error", "") and "empty" in res.get("error", ""))
_check("Claude was never called for the empty file", len(_calls) == 0)
_check("progress log says which columns were detected",
       any("stock: current_system_balance" in m for m in _progress))

# ── 4. Filled balance column: runs, and stock comes from the balance ─────────
_calls.clear()
_progress.clear()
res2 = inv_mod.run_inventory_agent(SID_FILLED, "claude-sonnet-4-6", [], {}, _emit_capture)
_check("filled file analyses successfully", "report" in res2, detail=str(res2)[:120])
_check("Claude called exactly once", len(_calls) == 1)

prompt = _calls[0]["user"] if _calls else ""
_check("stock read from balance column (GOUDA shows 150)",
       "AMMERLAND GOUDA" in prompt and "Stock: 150" in prompt)
_check("stock NOT read from qty_sold (no 7299 stock anywhere)",
       "Stock: 7299" not in prompt)
_check("velocity still computed from qty_sold (months of supply present)",
       "Months of supply:" in prompt)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll stock-column detection tests passed.")
