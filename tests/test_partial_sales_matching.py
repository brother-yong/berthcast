"""Regression tests for the 12 June "no recommendations" failure.

Cool Link ran their full inventory (1,346 items, 731 zero-stock) with a sales
report covering a handful of SKUs and got ZERO recommendations, despite a
clearly out-of-stock item with real sales. Three compounding causes, all
reproduced against their real files:

  1. Name drift: their staff type annotations ("<- out of stock") into the
     sales sheet's item-name column, so exact-name matching loses the sales
     data for exactly the items that matter most.
  2. Absence-as-death: any item missing from the (partial) sales file showed
     "Total sold: 0" and the prompt rule marked it DEAD; DEAD items are
     silently stripped from recommendations.
  3. Silent truncation: 800-item batches with max_tokens=16000 cannot fit in
     the reply; the JSON repair quietly kept only the front of each batch.

Run: python tests/test_partial_sales_matching.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_partialsales.db")
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
import agents.orchestrator as orch_mod     # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. SalesNameIndex: name matching survives real-world drift ───────────────
SNI = shared.SalesNameIndex

_claimed = {shared.normalise_match_key(n) for n in (
    "COWHEAD UHT MILK FULL CREAM 1 LTR",
    "COWHEAD UHT MILK LOW FAT 1 LTR",
    "EMBORG CHEDDAR 200G",
    "MILK",  # tiny generic name — must never steal prefix matches
)}

_sales = {
    # staff annotation appended in the sales sheet (the Cool Link killer)
    "COWHEAD UHT MILK FULL CREAM 1 LTR  ← out of stock": {"total_qty": 840, "total_revenue": 1200},
    # case + spacing drift only
    "embOrg  cheddar   200g": {"total_qty": 55},
    # junk rows that land in the name column of real exports
    "28846700.07": {"total_qty": 1},
    "Amount ($)": {"total_qty": 2},
}
idx = SNI(_sales, claimed_keys=_claimed)

hit = idx.get("COWHEAD UHT MILK FULL CREAM 1 LTR")
_check("annotated sales name matches its inventory item",
       hit is not None and hit.get("total_qty") == 840, detail=str(hit))
_check("annotation never bleeds onto the sibling flavour",
       idx.get("COWHEAD UHT MILK LOW FAT 1 LTR") is None)
_check("case/spacing drift matches", (idx.get("EMBORG CHEDDAR 200G") or {}).get("total_qty") == 55)
_check("junk numeric rows match nothing", idx.get("MILK") is None)

# Two annotated rows for the SAME item are summed, not lost.
idx2 = SNI({
    "EMBORG CHEDDAR 200G (low stock)": {"total_qty": 10},
    "EMBORG CHEDDAR 200G - check": {"total_qty": 5},
}, claimed_keys=_claimed)
_check("multiple annotated rows of one item are summed",
       (idx2.get("EMBORG CHEDDAR 200G") or {}).get("total_qty") == 15)

# A sales name that exactly equals ANOTHER inventory item is never folded
# into a shorter item's bucket.
_claimed3 = {shared.normalise_match_key(n) for n in ("UHT MILK 1 LTR", "UHT MILK 1 LTR PROMO")}
idx3 = SNI({"UHT MILK 1 LTR PROMO": {"total_qty": 99}}, claimed_keys=_claimed3)
_check("sales row belonging to a longer sibling item stays with that item",
       idx3.get("UHT MILK 1 LTR") is None and (idx3.get("UHT MILK 1 LTR PROMO") or {}).get("total_qty") == 99)

# Truncated sales name resolves only when unambiguous.
idx4 = SNI({"EMBORG CHEDDAR 2": {"total_qty": 7}}, claimed_keys=_claimed)
_check("truncated sales name resolves to its unique item",
       (idx4.get("EMBORG CHEDDAR 200G") or {}).get("total_qty") == 7)
idx5 = SNI({"COWHEAD UHT MILK": {"total_qty": 7}}, claimed_keys=_claimed)
_check("ambiguous truncation matches nothing (never double-counts)",
       idx5.get("COWHEAD UHT MILK FULL CREAM 1 LTR") is None
       and idx5.get("COWHEAD UHT MILK LOW FAT 1 LTR") is None)

# BOTH drifts at once: truncated name + trailing annotation. The full string
# matches nothing, so the index must trim the annotation and retry.
idx_both = SNI({"COWHEAD UHT MILK FULL CREAM ← out of stock": {"total_qty": 11}},
               claimed_keys=_claimed)
_check("truncation + annotation combined still resolves",
       (idx_both.get("COWHEAD UHT MILK FULL CREAM 1 LTR") or {}).get("total_qty") == 11)

# alias_map (dedup confirm) still folds variants onto the canonical name.
idx6 = SNI({"CHED 200G": {"total_qty": 3}},
           alias_map={"ched 200g": "EMBORG CHEDDAR 200G"},
           claimed_keys=_claimed)
_check("alias_map variants fold onto the canonical item",
       (idx6.get("EMBORG CHEDDAR 200G") or {}).get("total_qty") == 3)


# ── 2. Inventory agent: partial sales file handled honestly ──────────────────
db.init_db()
SID = 951
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "PartialOrg", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{SID} ('
           '"description" TEXT, "qty_on_hand" TEXT, "category" TEXT, "uom" TEXT)')
for row in [
    ("COWHEAD UHT MILK FULL CREAM 1 LTR", "0",   "DAIRY", "CTN"),  # OOS, has sales (annotated)
    ("EMBORG CHEDDAR 200G",               "120", "DAIRY", "CTN"),  # stocked, has sales
    ("OLD DUSTY GADGET",                  "0",   "MISC",  "PCS"),  # OOS, truly absent from sales
    ("WAREHOUSE FILLER ITEM",             "50",  "MISC",  "PCS"),  # stocked, absent from sales
]:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", row)
db.execute(f'CREATE TABLE sales_{SID} ("item_name" TEXT, "qty_sold" TEXT, "date" TEXT)')
for row in [
    ("COWHEAD UHT MILK FULL CREAM 1 LTR  ← out of stock", "120", "2026-03-05"),
    ("COWHEAD UHT MILK FULL CREAM 1 LTR  ← out of stock", "120", "2026-04-05"),
    ("EMBORG CHEDDAR 200G", "60", "2026-04-10"),
]:
    db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?)", row)

_captured = {"user": "", "system": "", "max_tokens": None}


def _fake_inv_claude(model, system, user, max_tokens=4096):
    _captured["system"] = system
    _captured["user"] = user
    _captured["max_tokens"] = max_tokens
    items = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            items.append({"item": line.split("|")[0].replace("Item:", "").strip(),
                          "category": "X", "stock": 0, "status": "CRITICAL",
                          "spoilage_risk": "NONE", "days_of_supply": 0, "observation": "t"})
    return json.dumps(items)


inv_mod._call_claude = _fake_inv_claude
log = []
res = inv_mod.run_inventory_agent(SID, "claude-sonnet-4-6", [], {}, progress_emit=log.append)
prompt = _captured["user"]

_cow_line = next((l for l in prompt.splitlines() if l.startswith("Item: COWHEAD")), "")
_check("annotated item's sales are found (240 sold, not 0)",
       "Total sold" in _cow_line and "240" in _cow_line, detail=_cow_line[:140])
_dusty_line = next((l for l in prompt.splitlines() if l.startswith("Item: OLD DUSTY")), "")
_check("item absent from sales says 'no sales data', never a fake 0",
       "no sales data" in _dusty_line and ": 0" not in _dusty_line.split("Total sold")[-1][:8],
       detail=_dusty_line[:140])
_filler_line = next((l for l in prompt.splitlines() if l.startswith("Item: WAREHOUSE")), "")
_check("stocked item absent from sales also marked 'no sales data'",
       "no sales data" in _filler_line, detail=_filler_line[:140])

_sysp = _captured["system"]
_check("prompt: absence of sales data is NOT proof an item is dead",
       "NOT proof" in _sysp or "not proof" in _sysp)
_check("prompt: DEAD now requires sales evidence (old blanket rule gone)",
       "total sold = 0 AND no reason to believe" not in _sysp)
_check("prompt: stocked no-data items stay out of the DEAD bucket",
       "no sales data" in _sysp)
_check("inventory batches sized to fit the reply window (<= 300)",
       getattr(inv_mod, "_INV_BATCH", 999) <= 300,
       detail=str(getattr(inv_mod, "_INV_BATCH", "missing")))
_check("inventory max_tokens raised to 64000", _captured["max_tokens"] == 64000,
       detail=str(_captured["max_tokens"]))
_check("coverage emitted to the progress log",
       any("matched" in l.lower() and "sales" in l.lower() for l in log), detail=str(log[-6:]))


# ── 3. Recommendation agent: drifted names keep their velocity + qty ─────────
_rec_captured = {"user": "", "max_tokens": None}


def _fake_rec_claude(model, system, user, max_tokens=4096):
    _rec_captured["user"] = user
    _rec_captured["max_tokens"] = max_tokens
    return "[]"


rec_mod._call_claude = _fake_rec_claude
_REPORT = [
    {"item": "COWHEAD UHT MILK FULL CREAM 1 LTR", "category": "DAIRY", "stock": 0,
     "status": "CRITICAL", "spoilage_risk": "LOW", "days_of_supply": 0, "observation": "t"},
]
rec_mod.run_recommendation_agent(SID, "claude-sonnet-4-6", list(_REPORT), {}, None)
_rline = _rec_captured["user"]
_check("rec agent finds avg monthly sales despite the annotated name",
       "Avg monthly sales: 0 " not in _rline and "Avg monthly sales: 0\n" not in _rline,
       detail=[l for l in _rline.splitlines() if "Avg monthly" in l][:1])
_check("rec agent computes a real suggested quantity (not 'insufficient sales data')",
       "insufficient sales data" not in _rline)
_check("recommendation max_tokens raised to 64000", _rec_captured["max_tokens"] == 64000,
       detail=str(_rec_captured["max_tokens"]))


# ── 4. Orchestrator surfaces truncated (partial) reports ─────────────────────
_orig_inv, _orig_rec = orch_mod.run_inventory_agent, orch_mod.run_recommendation_agent
try:
    orch_mod.run_inventory_agent = lambda *a, **k: {"report": list(_REPORT), "partial": True}
    orch_mod.run_recommendation_agent = lambda *a, **k: []
    _olog = []
    orch_mod.run_pipeline(SID, "m", [], {}, emit=_olog.append, mark=lambda *a, **k: None)
    _check("truncated inventory reply is announced, not silent",
           any("cut short" in l.lower() or "incomplete" in l.lower() or "truncat" in l.lower()
               for l in _olog), detail=str(_olog))
finally:
    orch_mod.run_inventory_agent, orch_mod.run_recommendation_agent = _orig_inv, _orig_rec


# ── 5. Column mapping runs automatically (no user confirm step) ──────────────
SID_AUTO = 952
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_AUTO, 1, "PartialOrg", "complete", "all", "{}"))
# Two stock-ish columns: keyword detection alone picks "qty"; the LLM proposal
# (stubbed) correctly picks current_system_balance — and must be auto-applied.
db.execute(f'CREATE TABLE inventory_{SID_AUTO} ('
           '"item_name" TEXT, "qty" TEXT, "current_system_balance" TEXT, "category" TEXT, "uom" TEXT)')
db.execute(f"INSERT INTO inventory_{SID_AUTO} VALUES (?,?,?,?,?)",
           ("CHEDDAR", "1", "500", "CHEESE", "KG"))
db.execute(f'CREATE TABLE sales_{SID_AUTO} ("item_name" TEXT, "qty" TEXT, "date" TEXT)')

shared._call_claude = lambda model, system, user, max_tokens=4096: json.dumps(
    {"description": "item_name", "stock": "current_system_balance",
     "category": "category", "uom": "uom"})

_captured["user"] = ""
inv_mod.run_inventory_agent(SID_AUTO, "claude-sonnet-4-6", [], {}, None)
_check("auto-mapping applied without any confirm step (stock read from balance col)",
       "Stock: 500" in _captured["user"], detail=_captured["user"][:160])
_saved = db.query("SELECT column_map_json FROM upload_sessions WHERE id=?", (SID_AUTO,))[0]["column_map_json"]
_saved_map = json.loads(_saved) if _saved else {}
_check("auto-mapping saved for audit", _saved_map.get("stock") == "current_system_balance",
       detail=str(_saved_map))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll partial-sales matching tests passed.")
