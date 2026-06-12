"""Summary sales sheets (no dates) must produce correct velocities.

13 June 2026: Cool Link's sales export is a SUMMARY report — no date column,
merged category/supplier cells, and its own "Avg Qty / Month" column. berthcast
ignored that column, assumed the file covered 12 months, and read AMMERLAND
EDAM at 608 KG/month when the sheet itself said 2,433 KG/month — a suggested
order 4x too small. The supplier column was ignored too ("Unknown" on the card
while the sheet names AMMERLAND).

Covers, in trust order:
  1. detect_avg_month_column — recognises stated-average columns, rejects money.
  2. Inventory agent uses the stated average directly, and infers the period
     from Qty ÷ Avg (no more silent 12-month assumption when evidence exists).
  3. Recommendation agent: velocity from the stated average; supplier filled
     down through merged cells and read off the sales sheet as a last resort.
  4. When there's truly nothing (no dates, no avg column): 12 months is
     assumed LOUDLY — data_notes flow to the results page banner.
  5. End-to-end dummy session: messy CSVs through the real ingestion and the
     real pipeline (deterministic stand-in for Claude), checked number by number.

Run: python tests/test_summary_sheet_velocity.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_summarysheet.db")
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
import agents.recommendation as rec_mod    # noqa: E402
from agents.shared import LEAD_TIME_BY_TYPE  # noqa: E402
from agents.orchestrator import run_pipeline  # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. detect_avg_month_column ───────────────────────────────────────────────
dam = shared.detect_avg_month_column
_check("recognises avg_qty___month (Cool Link's real header)",
       dam(["item_name", "qty_sold", "avg_qty___month"]) == "avg_qty___month")
_check("recognises average_monthly_qty", dam(["x", "average_monthly_qty"]) == "average_monthly_qty")
_check("recognises qty_per_month", dam(["x", "qty_per_month"]) == "qty_per_month")
_check("rejects money columns (monthly_amount)", dam(["monthly_amount"]) is None)
_check("rejects a bare month column", dam(["month", "qty"]) is None)
_check("rejects avg without a month token", dam(["avg_cost", "qty"]) is None)

inf = shared.infer_months_from_item_stats
_check("infers 3 months from qty/avg pairs",
       inf([{"total_qty": 7299.267, "avg_monthly_direct": 2433.089},
            {"total_qty": 6986.255, "avg_monthly_direct": 2328.751667}]) == (3, 2))
_check("junk rows don't break inference",
       inf([{"total_qty": 300, "avg_monthly_direct": 100},
            {"total_qty": 0, "avg_monthly_direct": 0},
            {"total_qty": "x", "avg_monthly_direct": "y"}]) == (3, 1))
_check("no usable pairs -> None", inf([{"total_qty": 5, "avg_monthly_direct": 0}]) is None)


# ── 2+3. The screenshot sheet, faithfully reproduced ─────────────────────────
db.init_db()
SID = 971
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "SummaryOrg", "complete", "all", "{}"))

# Sales summary exactly like the screenshot: merged category/supplier cells
# arrive as blanks on all but the first row of each group; CHEDDAR has a blank
# qty and a stated average of 0; no date column anywhere.
db.execute(f'CREATE TABLE sales_{SID} ('
           '"category" TEXT, "item_name" TEXT, "supplier" TEXT, "sales_pre_tax" TEXT, '
           '"qty_sold" TEXT, "unit" TEXT, "avg_qty___month" TEXT, "current_system_balance" TEXT)')
_SALES_ROWS = [
    ("CHEESE", "AMMERLAND EDAM",            "AMMERLAND", "62145.57",  "7299.267",  "KG",  "2433.089",    ""),
    ("",       "AMMERLAND GOUDA",           "",          "59496.74",  "6986.255",  "KG",  "2328.751667", ""),
    ("",       "AMMERLAND CHEDDAR",         "",          "",          "",          "KG",  "0",           ""),
    ("",       "AMMERLAND EMMENTHAL",       "",          "63897.48",  "6118.25",   "KG",  "1223.65",     ""),
    ("",       "AMMERLAND UNSALTED BUTTER", "",          "237647.28", "90541",     "PKT", "18108.2",     ""),
    ("",       "PAMPA EDAM",                "PAMPA",     "59641.17",  "7085.817",  "KG",  "2361.939",    ""),
    ("",       "PAMPA CHEDDAR",             "",          "111413.39", "11908.109", "KG",  "2381.6218",   ""),
    ("",       "PAMPA GOUDA",               "",          "72823.11",  "8435.348",  "KG",  "1687.0696",   ""),
]
for r in _SALES_ROWS:
    db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?,?,?,?,?,?)", r)

# Inventory uses LONGER names than the sales sheet (real Cool Link pattern).
db.execute(f'CREATE TABLE inventory_{SID} ('
           '"description" TEXT, "qty_on_hand" TEXT, "category" TEXT, "uom" TEXT)')
for r in [
    ("AMMERLAND EDAM CHEESE BLOCK",  "0",    "CHEESE", "KG"),   # the screenshot card
    ("AMMERLAND GOUDA CHEESE BLOCK", "5000", "CHEESE", "KG"),
    ("PAMPA CHEDDAR CHEESE BLOCK",   "1200", "CHEESE", "KG"),
]:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", r)

_cap = {"user": "", "system": ""}


def _rules_claude(model, system, user, max_tokens=4096):
    """Deterministic stand-in for Claude that follows the prompt's rules."""
    items = []
    for line in user.splitlines():
        if not line.startswith("Item: "):
            continue
        name = line.split("|")[0].replace("Item:", "").strip()
        stock = float(line.split("Stock:")[1].split("|")[0].strip() or 0)
        no_data = "no sales data" in line
        sold = 0.0
        if not no_data and "Total sold" in line:
            sold = float(line.split("Total sold")[1].split(":")[1].split("|")[0].strip())
        supply = None
        if "Months of supply:" in line:
            supply = float(line.split("Months of supply:")[1].split("|")[0].strip())
        if no_data:
            status = "DEAD" if stock == 0 else "HEALTHY"
        elif sold > 0 and stock == 0:
            status = "CRITICAL"
        elif supply is not None and supply < 1:
            status = "CRITICAL"
        elif supply is not None and supply <= 3:
            status = "LOW"
        elif sold == 0:
            status = "DEAD"
        else:
            status = "HEALTHY"
        items.append({"item": name, "category": "X", "stock": stock, "status": status,
                      "spoilage_risk": "NONE", "days_of_supply": (supply or 0) * 30,
                      "observation": "no sales data in upload" if no_data else "ok"})
    return json.dumps(items)


def _fake_inv_claude(model, system, user, max_tokens=4096):
    _cap["system"], _cap["user"] = system, user
    return _rules_claude(model, system, user, max_tokens)


inv_mod._call_claude = _fake_inv_claude
shared._call_claude = lambda *a, **k: "{}"  # auto-mapper: keyword fallback
log = []
res = inv_mod.run_inventory_agent(SID, "m", [], {}, progress_emit=log.append)

# The screenshot's own figures are mixed-period (some items Qty/Avg = 3, some
# = 5 — their ERP averages different windows per item); the median, 5, is the
# honest global label. Velocities are untouched by this: they come straight
# from each item's stated average.
_check("period inferred from the sheet's own figures (median ~5 months)",
       any("inferred" in l and "5 month" in l for l in log), detail=str(log[-8:]))
_check("no 12-month assumption note when evidence exists",
       not (res.get("data_notes") if isinstance(res, dict) else None))
_edam = next((l for l in _cap["user"].splitlines() if l.startswith("Item: AMMERLAND EDAM")), "")
_check("EDAM: real total carried with the inferred-period label",
       "Total sold (5mo): 7299" in _edam, detail=_edam[:160])
_gouda = next((l for l in _cap["user"].splitlines() if l.startswith("Item: AMMERLAND GOUDA")), "")
_check("GOUDA: months of supply = stock/stated avg = 5000/2328.75 = 2.1",
       "Months of supply: 2.1" in _gouda, detail=_gouda[:160])

# Recommendation agent on the EDAM card from the screenshot.
_rcap = {"user": ""}


def _fake_rec_claude(model, system, user, max_tokens=4096):
    _rcap["user"] = user
    return "[]"


rec_mod._call_claude = _fake_rec_claude
_REPORT = [
    {"item": "AMMERLAND EDAM CHEESE BLOCK", "category": "CHEESE", "stock": 0,
     "status": "CRITICAL", "spoilage_risk": "MEDIUM", "days_of_supply": 0, "observation": "t"},
    {"item": "AMMERLAND GOUDA CHEESE BLOCK", "category": "CHEESE", "stock": 5000,
     "status": "LOW", "spoilage_risk": "LOW", "days_of_supply": 63, "observation": "t"},
    {"item": "PAMPA CHEDDAR CHEESE BLOCK", "category": "CHEESE", "stock": 1200,
     "status": "LOW", "spoilage_risk": "LOW", "days_of_supply": 15, "observation": "t"},
]
rec_log = []
rec_mod.run_recommendation_agent(SID, "m", list(_REPORT), {}, progress_emit=rec_log.append)
_lines = _rcap["user"]

_check("EDAM velocity = the sheet's stated 2433.1 KG/month (was 608 before)",
       "Avg monthly sales: 2433.1 KG" in _lines,
       detail=[l for l in _lines.splitlines() if "Avg monthly" in l])
_exp_qty = round(2433.1 * (LEAD_TIME_BY_TYPE["other"] / 30 + 1.5))
_check(f"EDAM suggested order ~{_exp_qty} KG (4x the broken 2129)",
       f"Pre-computed suggested order quantity: {_exp_qty} KG" in _lines,
       detail=[l for l in _lines.splitlines() if "Pre-computed" in l])
_check("EDAM supplier read from the sales sheet (was Unknown)",
       "Supplier: AMMERLAND (" in _lines,
       detail=[l for l in _lines.splitlines() if l.startswith("Supplier:")][:1])
_check("merged supplier cell filled down (GOUDA row was blank -> AMMERLAND)",
       sum(1 for l in _lines.splitlines() if "Supplier: AMMERLAND (" in l) >= 2)
_check("second merge group also filled (PAMPA CHEDDAR blank row -> PAMPA)",
       "Supplier: PAMPA (" in _lines,
       detail=[l for l in _lines.splitlines() if l.startswith("Supplier:")])
_check("fill-down announced in progress log",
       any("merged cells filled down" in l for l in rec_log), detail=str(rec_log[-4:]))


# ── 4. Truly nothing to go on: 12 months assumed LOUDLY ──────────────────────
SID2 = 972
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID2, 1, "SummaryOrg", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{SID2} ("description" TEXT, "qty_on_hand" TEXT, "uom" TEXT)')
db.execute(f"INSERT INTO inventory_{SID2} VALUES (?,?,?)", ("WIDGET ALPHA", "10", "PCS"))
db.execute(f'CREATE TABLE sales_{SID2} ("item_name" TEXT, "qty_sold" TEXT)')
db.execute(f"INSERT INTO sales_{SID2} VALUES (?,?)", ("WIDGET ALPHA", "1200"))

log2 = []
res2 = inv_mod.run_inventory_agent(SID2, "m", [], {}, progress_emit=log2.append)
_check("12-month assumption emitted as a WARNING",
       any("assuming 12 months" in l for l in log2), detail=str(log2[-4:]))
_notes2 = res2.get("data_notes") if isinstance(res2, dict) else None
_check("data_notes carries the assumption for the results page",
       bool(_notes2) and "assumed 12 months" in _notes2[0], detail=str(_notes2))

# The note must reach the results page as a visible banner.
import app as appmod  # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json, data_notes) "
    "VALUES (?,?,?,?)",
    (SID2, json.dumps(res2.get("report") or []), "[]", json.dumps(_notes2 or [])))
client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"] = 1
    s["email"] = "u@summaryorg.com"
    s["org_name"] = "SummaryOrg"
    s["model"] = "m"
    s["is_admin"] = True
    s["tier"] = "enterprise"
    s["role"] = "admin"
r = client.get(f"/results/{SID2}")
body = r.data.decode("utf-8")
_check("results page renders 200", r.status_code == 200, detail=str(r.status_code))
_check("assumed-period banner shown on results page",
       "carry assumptions" in body and "assumed 12 months" in body)


# ── 5. End-to-end dummy session: messy CSVs -> real ingestion -> pipeline ────
SID3 = 973
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID3, 1, "SummaryOrg", "pending", "all", "{}"))

_tmpdir = tempfile.mkdtemp(prefix="berth_e2e_")
_inv_csv = os.path.join(_tmpdir, "inventory.csv")
_sal_csv = os.path.join(_tmpdir, "sales.csv")

with open(_inv_csv, "w", encoding="utf-8", newline="") as f:
    f.write("CL WAREHOUSE — STOCK BALANCE EXPORT,,,\n")           # title junk row
    f.write("Description,UOM,Qty On Hand,Location\n")
    f.write("AMMERLAND EDAM CHEESE BLOCK,KG,0,W1\n")              # OOS + has sales
    f.write("AMMERLAND EMMENTHAL CHEESE WHEEL,KG,0,W1\n")         # OOS + annotated sales
    f.write("AMMERLAND GOUDA CHEESE BLOCK,KG,5000,W1\n")          # stocked + sales
    f.write("OLD DISPLAY STAND,PCS,0,W2\n")                       # OOS, no sales anywhere
    f.write("SPARE SHELF BRACKET,PCS,40,W2\n")                    # stocked, no sales
with open(_sal_csv, "w", encoding="utf-8", newline="") as f:
    f.write("TOP SELLERS FINAL,,,,,,\n")                          # title junk row
    f.write("Category,Item Name,Supplier,Sales (Pre-Tax),Qty Sold,Unit,Avg Qty / Month\n")
    f.write("CHEESE,AMMERLAND EDAM,AMMERLAND,62145.57,7299.267,KG,2433.089\n")
    f.write(",AMMERLAND EMMENTHAL  ← out of stock,,63897.48,6118.25,KG,1223.65\n")
    f.write(",AMMERLAND GOUDA,,59496.74,6986.255,KG,2328.751667\n")

r_inv = db._csv_to_sqlite(_inv_csv, "inventory", SID3)
r_sal = db._csv_to_sqlite(_sal_csv, "sales", SID3)
_check("e2e: both files ingest", r_inv.get("ok") and r_sal.get("ok"),
       detail=f"{r_inv} / {r_sal}")


def _e2e_rec_claude(model, system, user, max_tokens=4096):
    recs = []
    for block in user.split("---"):
        lines = [l.strip() for l in block.strip().splitlines()]
        if not lines or not lines[0].startswith("Item:"):
            continue
        name = lines[0].replace("Item:", "").strip()
        qty_line = next((l for l in lines if l.startswith("Pre-computed suggested order quantity:")), "")
        qty = qty_line.split(":", 1)[1].strip() if qty_line else "Verify with team"
        sup_line = next((l for l in lines if l.startswith("Supplier:")), "Supplier: Unknown (")
        sup = sup_line.split(":", 1)[1].split("(")[0].strip()
        recs.append({"item": name, "supplier": sup, "supplier_type": "other",
                     "lead_time_days": None, "days_of_supply": 0,
                     "recommended_action": "REORDER", "suggested_quantity": qty,
                     "confidence": "HIGH", "consequence_if_acting": "a",
                     "consequence_if_not_acting": "b", "supplier_risk": "HIGH",
                     "mitigation": "m", "flags": [], "reason": "r"})
    return json.dumps(recs)


inv_mod._call_claude = _rules_claude
rec_mod._call_claude = _e2e_rec_claude
e2e_log = []
result = run_pipeline(SID3, "m", [], {}, emit=e2e_log.append, mark=lambda *a, **k: None)

_check("e2e: pipeline completes", "error" not in result, detail=str(result)[:200])
recs = {r["item"]: r for r in result.get("recommendations", []) if isinstance(r, dict)}
report = {r["item"]: r for r in result.get("inventory_report", []) if isinstance(r, dict)}

_check("e2e: out-of-stock EDAM is CRITICAL",
       report.get("AMMERLAND EDAM CHEESE BLOCK", {}).get("status") == "CRITICAL",
       detail=str(report.get("AMMERLAND EDAM CHEESE BLOCK")))
_check("e2e: annotated EMMENTHAL also CRITICAL (annotation didn't hide its sales)",
       report.get("AMMERLAND EMMENTHAL CHEESE WHEEL", {}).get("status") == "CRITICAL",
       detail=str(report.get("AMMERLAND EMMENTHAL CHEESE WHEEL")))
_check("e2e: EDAM gets a recommendation", "AMMERLAND EDAM CHEESE BLOCK" in recs)

_exp_edam = round(round(float("2433.089"), 1) * (LEAD_TIME_BY_TYPE["other"] / 30 + 1.5))
_edam_qty = str(recs.get("AMMERLAND EDAM CHEESE BLOCK", {}).get("suggested_quantity", ""))
_check(f"e2e: EDAM quantity sized from the stated average ({_exp_edam} KG)",
       str(_exp_edam) in _edam_qty, detail=_edam_qty)
_check("e2e: EDAM supplier is AMMERLAND, not Unknown",
       recs.get("AMMERLAND EDAM CHEESE BLOCK", {}).get("supplier") == "AMMERLAND",
       detail=str(recs.get("AMMERLAND EDAM CHEESE BLOCK", {}).get("supplier")))
_exp_emm = round(round(float("1223.65"), 1) * (LEAD_TIME_BY_TYPE["other"] / 30 + 1.5))
_emm_qty = str(recs.get("AMMERLAND EMMENTHAL CHEESE WHEEL", {}).get("suggested_quantity", ""))
_check(f"e2e: annotated EMMENTHAL gets a real quantity ({_exp_emm} KG), not 'verify'",
       str(_exp_emm) in _emm_qty, detail=_emm_qty)
_check("e2e: EMMENTHAL supplier filled down through the merged cell",
       recs.get("AMMERLAND EMMENTHAL CHEESE WHEEL", {}).get("supplier") == "AMMERLAND")
_check("e2e: no-sales display stand stays out of recommendations (DEAD)",
       "OLD DISPLAY STAND" not in recs
       and report.get("OLD DISPLAY STAND", {}).get("status") == "DEAD")
_check("e2e: stocked no-sales bracket is HEALTHY, not dead",
       report.get("SPARE SHELF BRACKET", {}).get("status") == "HEALTHY")
_check("e2e: no 12-month assumption note (period was inferred)",
       not result.get("data_notes"), detail=str(result.get("data_notes")))
_check("e2e: period inference visible in the run log",
       any("inferred" in l for l in e2e_log))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll summary-sheet velocity tests passed.")
