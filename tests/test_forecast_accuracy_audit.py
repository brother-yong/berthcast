"""Forecast-accuracy audit fixes (2 Jul 2026) — six silent-wrong-number bugs.

Each one made a velocity, lead time or item silently wrong or missing:

  1. Supplier/lead-time lookup was exact-name only. Sales matching got
     drift-tolerance after the 12-June incident; the PO->supplier maps never
     did, so case/spacing drift between files lost an item's lead time and its
     thresholds/order sizing degraded to defaults. Fixed: NameKeyedDict — .get()
     falls back to a normalise_match_key match.
  2. Recommendation agent read velocity from EITHER the sheet's Avg/Month
     column OR qty totals — never both. An item with a blank avg cell but real
     qty history got "Verify with team" while the inventory agent sized the
     same item fine. Fixed: one query carries both, per-item trust order.
  3. Month counting read DISTINCT raw date values LIMIT 5000 — a
     datetime-stamped export blew the limit, months undercounted, every
     velocity inflated. Fixed: DISTINCT substr(date,1,10) collapses time-of-day.
  4. detect_avg_month_column matched "per" inside "period" — a "month_period"
     column would be read as a stated monthly average (fabricated velocity).
     Fixed: "per" must be its own word.
  5. _num_sql read accounting negatives "(50)" as 0 — credit notes silently
     vanished from SUM totals. Fixed: parens become a minus sign, matching
     Python's _to_num.
  6. The top-N scope filter compared lowercased names exactly — punctuation
     drift silently dropped a top seller from a scoped run (fallback only fired
     when ZERO items matched). Fixed: alias-then-normalise on both sides.

Dependency-free: run with `python tests/test_forecast_accuracy_audit.py`.
Exits non-zero on the first failed assertion.
"""
import json
import os
import sqlite3
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="berth_accuracy_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                        # noqa: E402
import agents.shared as shared               # noqa: E402
import agents.inventory as inv_mod           # noqa: E402
import agents.recommendation as rec_mod      # noqa: E402
from agents.shared import (                  # noqa: E402
    NameKeyedDict,
    _num_sql,
    _resolve_item_suppliers,
    count_sales_months,
    detect_avg_month_column,
)

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


db.init_db()


# ── 1. Supplier/lead-time lookup tolerates name drift ─────────────────────────
d = NameKeyedDict({"White Bread 400g": 1, "  OCEAN-IMPORT CO.  ": 2})
_check("exact key still wins", d.get("White Bread 400g") == 1)
_check("case drift resolves", d.get("WHITE BREAD 400G") == 1)
_check("spacing/punctuation drift resolves", d.get("Ocean Import Co") == 2)
_check("miss returns default", d.get("NOPE ITEM", "x") == "x")
_check("all-punctuation key can't false-match", d.get("---", "x") == "x")
_check("iteration/len unchanged (no mirror entries)", len(d) == 2)

db.execute('CREATE TABLE suppliers_31 ("supplier_name" TEXT, '
           '"supplier_type" TEXT, "_session_id" TEXT)')
db.execute("INSERT INTO suppliers_31 VALUES (?,?,?)",
           ("Ocean Import Co", "Import", "31"))
db.execute('CREATE TABLE purchase_orders_31 ("inventory_desc" TEXT, '
           '"supplier_name" TEXT, "_session_id" TEXT)')
# PO file shouts in caps; the inventory file uses title case.
db.execute("INSERT INTO purchase_orders_31 VALUES (?,?,?)",
           ("COOKING OIL 5L", "Ocean Import Co", "31"))
db.execute("INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
           "VALUES (?,?,?,?,?,?)", (31, 1, "AuditCo", "complete", "all", "{}"))

_config = db.get_company_config("AuditCo")
sup_map, lt_map, type_map = _resolve_item_suppliers(31, "AuditCo", _config, {})
_check("drifted item name still finds its supplier",
       sup_map.get("Cooking Oil 5l") == "Ocean Import Co", detail=str(dict(sup_map)))
_lt = lt_map.get("Cooking Oil 5L") or {}
_check("drifted item name still finds its 112-day import lead time",
       _lt.get("lead_time_days") == 112, detail=str(_lt))
_check("supplier-type map tolerates drift too",
       type_map.get("OCEAN IMPORT CO") == "import")


# ── 2. Rec agent: blank Avg/Month cell falls back to qty totals ───────────────
SID = 32
db.execute("INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
           "VALUES (?,?,?,?,?,?)", (SID, 1, "AuditCo", "complete", "all", "{}"))
db.execute(f'CREATE TABLE sales_{SID} ("item_name" TEXT, "qty_sold" TEXT, '
           '"avg_qty___month" TEXT)')
for r in [
    ("ALPHA WIDGET PRO", "300", "100"),   # stated avg; ratio says 3 months
    ("BETA WIDGET PRO",  "90",  ""),      # blank avg cell, real qty history
    ("GAMMA WIDGET PRO", "600", "200"),   # stated avg; ratio says 3 months
]:
    db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?)", r)

_rcap = {"user": ""}


def _cap_rec_claude(model, system, user, max_tokens=4096):
    _rcap["user"] = user
    return "[]"


rec_mod._call_claude = _cap_rec_claude
_REPORT = [
    {"item": "ALPHA WIDGET PRO", "category": "X", "stock": 10, "status": "CRITICAL",
     "spoilage_risk": "NONE", "days_of_supply": 3, "observation": "t"},
    {"item": "BETA WIDGET PRO", "category": "X", "stock": 5, "status": "CRITICAL",
     "spoilage_risk": "NONE", "days_of_supply": 5, "observation": "t"},
]
rec_mod.run_recommendation_agent(SID, "m", list(_REPORT), {})
_lines = _rcap["user"]
_alpha = next((b for b in _lines.split("---") if "ALPHA WIDGET PRO" in b), "")
_beta  = next((b for b in _lines.split("---") if "BETA WIDGET PRO" in b), "")
_check("stated avg still used when present (ALPHA: 100/month)",
       "Avg monthly sales: 100" in _alpha, detail=_alpha[:200])
# Period inferred from the sheet's own Qty/Avg ratios (3 months) -> 90/3 = 30.
_check("blank avg cell falls back to totals (BETA: 90 over 3 inferred months = 30)",
       "Avg monthly sales: 30" in _beta, detail=_beta[:200])
_check("BETA gets a sized order, not 'insufficient sales data'",
       "Pre-computed suggested order quantity: 105 units" in _beta, detail=_beta[:300])


# ── 3. Datetime-stamped exports can't undercount months ───────────────────────
conn = sqlite3.connect(":memory:")
conn.execute('CREATE TABLE s ("invoice_date" TEXT)')
rows = [(f"2025-{m:02d}-{1 + i % 28:02d} 00:{i // 60:02d}:{i % 60:02d}",)
        for m in range(1, 13) for i in range(500)]  # 6000 distinct datetimes
conn.executemany("INSERT INTO s VALUES (?)", rows)

_old = [r[0] for r in conn.execute(
    'SELECT DISTINCT "invoice_date" AS d FROM s LIMIT 5000')]
_old_count = count_sales_months(_old)
_check("old raw-DISTINCT query really undercounts (proves the bug existed)",
       _old_count and _old_count[0] < 12, detail=str(_old_count))

_new = [r[0] for r in conn.execute(
    'SELECT DISTINCT substr("invoice_date", 1, 10) AS d FROM s LIMIT 5000')]
_new_count = count_sales_months(_new)
_check("substr(1,10) query counts all 12 months",
       _new_count and _new_count[0] == 12, detail=str(_new_count))
conn.close()

_check("date-only values unaffected by the trim (3 months)",
       count_sales_months(["15/04/2026", "01/05/2026", "20/06/2026"])[0] == 3)
_check("textual datetime still parses after trim ('15-Jun-26 ')",
       count_sales_months(["15-Jun-26 ", "03-Jul-26 "])[0] == 2)


# ── 4. "period" is not "per month" ────────────────────────────────────────────
_check("month_period is NOT a stated average",
       detect_avg_month_column(["item", "month_period", "qty"]) is None)
_check("sales_period_month is NOT a stated average",
       detect_avg_month_column(["sales_period_month"]) is None)
_check("qty_per_month still detected",
       detect_avg_month_column(["x", "qty_per_month"]) == "qty_per_month")
_check("avg_qty___month still detected",
       detect_avg_month_column(["avg_qty___month"]) == "avg_qty___month")


# ── 5. Accounting negatives reach SQL sums ────────────────────────────────────
conn = sqlite3.connect(":memory:")
conn.execute('CREATE TABLE t ("qty" TEXT)')
for v in ["100", "(50)", "1,200", "S$(30)", "junk"]:
    conn.execute("INSERT INTO t VALUES (?)", (v,))
total = conn.execute(f'SELECT SUM({_num_sql("qty")}) FROM t').fetchone()[0]
_check("credit notes subtract: 100 - 50 + 1200 - 30 + 0 = 1220", total == 1220.0,
       detail=str(total))
conn.close()


# ── 6. Scope filter survives name drift ───────────────────────────────────────
SID2 = 33
db.execute("INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
           "VALUES (?,?,?,?,?,?)", (SID2, 1, "AuditCo", "complete", "2", "{}"))
db.execute(f'CREATE TABLE inventory_{SID2} ("description" TEXT, "qty_on_hand" TEXT, '
           '"uom" TEXT)')
for r in [
    ("AMMERLAND EDAM CHEESE", "5",  "KG"),   # sales sheet spells it with drift
    ("PLAIN BUN",             "10", "PCS"),  # exact match
    ("CHEAP STRAW",           "99", "PCS"),  # real item, outside top 2
]:
    db.execute(f"INSERT INTO inventory_{SID2} VALUES (?,?,?)", r)
db.execute(f'CREATE TABLE sales_{SID2} ("item_name" TEXT, "qty" TEXT, "net_amount" TEXT)')
for r in [
    ("Ammerland-Edam  Cheese.", "50", "1000"),  # top seller, drifted spelling
    ("PLAIN BUN",               "40", "500"),
    ("CHEAP STRAW",             "5",  "10"),
]:
    db.execute(f"INSERT INTO sales_{SID2} VALUES (?,?,?)", r)

_icap = {"user": ""}


def _cap_inv_claude(model, system, user, max_tokens=4096):
    _icap["user"] = user
    items = [{"item": l.split("|")[0].replace("Item:", "").strip(), "category": "X",
              "stock": 0, "status": "HEALTHY", "spoilage_risk": "NONE",
              "days_of_supply": 0, "observation": "t"}
             for l in user.splitlines() if l.startswith("Item: ")]
    return json.dumps(items)


inv_mod._call_claude = _cap_inv_claude
shared._call_claude = lambda *a, **k: "{}"  # column mapper: keyword fallback
res = inv_mod.run_inventory_agent(SID2, "m", [], {})
_check("scoped run completes", isinstance(res, dict) and "report" in res,
       detail=str(res)[:200])
_check("drifted top seller kept in a scoped run (was silently dropped)",
       "Item: AMMERLAND EDAM CHEESE" in _icap["user"], detail=_icap["user"][:300])
_check("exact-named item still kept", "Item: PLAIN BUN" in _icap["user"])
_check("scope still filters (item outside top-2 excluded)",
       "CHEAP STRAW" not in _icap["user"])


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll forecast-accuracy audit tests passed.")
