"""End-to-end check on the reusable dummy-data fixture (fixtures/*.csv).

This is the regression test for "does berthcast still read a normal upload
correctly". It ingests the four fixture CSVs through the REAL excel_to_sqlite
and runs the REAL pipeline (orchestrator -> inventory agent -> recommendation
agent). Only the Claude calls are stubbed — and the stubs are faithful: the
inventory stub applies the documented status thresholds to the numbers the real
code computed, and the recommendation stub echoes back the real pre-computed
order quantity (exactly what Claude is told to do). So everything under test —
column detection, date/period reading, velocity, months-of-supply, dead-SKU and
missing-sales handling, split-row merging, total-row dropping, name-drift
matching, money parsing, lead-time-aware thresholds, quantity sizing — is the
production code path.

The fixture plants ~12 items whose numbers make one verdict each unambiguous.
If a future change breaks one of those branches, the matching check here fails.

Run: python tests/test_dummy_data_fixture.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")
sys.path.insert(0, ROOT)
sys.path.insert(0, FIX)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_dummyfix.db")
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
from quantity import parse_quantity        # noqa: E402
import generate_dummy_data                 # noqa: E402  (fixtures/ is on sys.path)

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── Make sure the CSVs exist (regenerate if a checkout is missing them) ───────
_csvs = ["inventory.csv", "sales.csv", "suppliers.csv", "purchase_orders.csv"]
if not all(os.path.exists(os.path.join(FIX, c)) for c in _csvs):
    generate_dummy_data.build()

# ── Ingest the fixtures through the real converter ───────────────────────────
db.init_db()
SID = 7100
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "DummyCo", "complete", "all", "{}"))
# Pre-seed the column map so the run is deterministic without an LLM proposal.
db.execute("UPDATE upload_sessions SET column_map_json=? WHERE id=?",
           (json.dumps({"description": "item_description", "stock": "qty_on_hand",
                        "category": "category", "uom": "uom"}), SID))

for slot, fname in (("inventory", "inventory.csv"), ("sales", "sales.csv"),
                    ("suppliers", "suppliers.csv"), ("purchase_orders", "purchase_orders.csv")):
    res = db.excel_to_sqlite(os.path.join(FIX, fname), slot, SID)
    _check(f"ingest {fname}", res.get("ok"), detail=str(res))

_inv_rows = db.query(f"SELECT COUNT(*) AS n FROM inventory_{SID}")[0]["n"]
_check("inventory ingested as >120 rows", _inv_rows > 120, detail=str(_inv_rows))
_check("sales ingested", db.query(f"SELECT COUNT(*) AS n FROM sales_{SID}")[0]["n"] > 300)


# ── Faithful Claude stubs ────────────────────────────────────────────────────
_inv_prompt = {"user": ""}


def _fields(line):
    """Split an 'Item: ... | Key: val | ...' line into {key: value}."""
    out = {}
    for part in line.split(" | "):
        if ": " in part:
            k, v = part.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def _num(s, default=0.0):
    try:
        return float(str(s).split()[0].replace(",", ""))
    except (ValueError, IndexError):
        return default


def _status_for(line):
    """Apply the inventory agent's documented thresholds to one prompt line."""
    f = _fields(line)
    stock = _num(f.get("Stock", "0"))
    sold_key = next((k for k in f if k.startswith("Total sold")), None)
    sold_val = f.get(sold_key, "")
    if "no sales data" in sold_val:
        return "HEALTHY" if stock > 0 else "DEAD"
    if _num(sold_val) == 0:
        return "DEAD"
    ms = _num(f["Months of supply"]) if "Months of supply" in f else 999.0
    if "Lead time" in f:
        # "112d (3.7mo)" -> 3.7
        lt = _num(f["Lead time"].split("(")[-1].replace("mo)", ""))
        return "CRITICAL" if ms < lt else ("LOW" if ms <= lt + 2 else "HEALTHY")
    return "CRITICAL" if ms < 1 else ("LOW" if ms <= 3 else "HEALTHY")


def _fake_inv_claude(model, system, user, max_tokens=4096):
    _inv_prompt["user"] = user
    items = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            f = _fields(line)
            items.append({
                "item": f.get("Item", ""),
                "category": f.get("Category", ""),
                "stock": _num(f.get("Stock", "0")),
                "status": _status_for(line),
                "spoilage_risk": "NONE",
                "days_of_supply": 0,
                "observation": "test",
            })
    return json.dumps(items)


def _fake_rec_claude(model, system, user, max_tokens=4096):
    """Echo back the pre-computed order quantity, like Claude is instructed to."""
    recs = []
    for block in user.split("---"):
        iname = qty = None
        for line in block.splitlines():
            if line.startswith("Item: "):
                iname = line[len("Item: "):].strip()
            elif line.startswith("Pre-computed suggested order quantity: "):
                qty = line.split(": ", 1)[1].strip()
        if iname:
            recs.append({
                "item": iname, "supplier": "", "supplier_type": "other",
                "lead_time_days": None, "days_of_supply": None,
                "recommended_action": "REORDER", "suggested_quantity": qty,
                "confidence": "HIGH", "consequence_if_acting": "x",
                "consequence_if_not_acting": "y", "supplier_risk": "None",
                "mitigation": "", "flags": [], "reason": "test",
            })
    return json.dumps(recs)


inv_mod._call_claude = _fake_inv_claude
rec_mod._call_claude = _fake_rec_claude
shared._call_claude = lambda *a, **k: "[]"   # safety net: nothing hits the network

# ── Run the real pipeline ────────────────────────────────────────────────────
result = orch_mod.run_pipeline(SID, "claude-sonnet-4-6", [], {},
                               emit=lambda *a, **k: None, mark=lambda *a, **k: None)

_check("pipeline returned a report (no block)", "inventory_report" in result,
       detail=str(result.get("error")))
report = result.get("inventory_report", [])
recs = [r for r in result.get("recommendations", []) if isinstance(r, dict) and not r.get("error")]
status = {r["item"]: r.get("status") for r in report if isinstance(r, dict)}
rec_by = {r["item"]: r for r in recs}
prompt = _inv_prompt["user"]


def _line(prefix):
    return next((l for l in prompt.splitlines() if l.startswith(prefix)), "")


# ── Clean file => no caveats, real period read ───────────────────────────────
_check("clean upload produces no data-quality caveats", result.get("data_notes") == [],
       detail=str(result.get("data_notes")))
_check("sales period read from the dates (3 months, not the 12-month fallback)",
       "data covers 3 months" in prompt)
_check("whole catalogue classified (>100 items)", len(report) > 100, detail=str(len(report)))


# ── Planted verdicts ─────────────────────────────────────────────────────────
_check("out-of-stock item with real sales -> CRITICAL",
       status.get("COWHEAD UHT MILK FULL CREAM 1L") == "CRITICAL",
       detail=str(status.get("COWHEAD UHT MILK FULL CREAM 1L")))
_check("name-drift sales (annotated) still credited (900 sold)",
       "Total sold (3mo): 900" in _line("Item: COWHEAD"),
       detail=_line("Item: COWHEAD")[:160])
_check("lead time resolved from PO+supplier (import, 112d)",
       "Lead time: 112d" in _line("Item: COWHEAD"),
       detail=_line("Item: COWHEAD")[:160])

_check("tight cover (1.25 months) -> LOW",
       status.get("EMBORG MATURE CHEDDAR BLOCK 200G") == "LOW",
       detail=str(status.get("EMBORG MATURE CHEDDAR BLOCK 200G")))
_check("well-stocked mover -> HEALTHY",
       status.get("SUNRICE PREMIUM JASMINE RICE 5KG") == "HEALTHY")

_check("item with sales but zero sold -> DEAD",
       status.get("RETIRED PRALINE SPREAD 250G") == "DEAD")
_check("stocked item absent from sales -> HEALTHY, never DEAD",
       status.get("NEW LAUNCH OAT MILK BARISTA 1L") == "HEALTHY")
_check("zero-stock item absent from sales -> DEAD",
       status.get("DISCONTINUED USB GADGET") == "DEAD")

# Split rows merged into one, stock summed (100 + 150 = 250).
_anchor = [r for r in report if r.get("item") == "ANCHOR PROFESSIONAL UNSALTED BUTTER 5KG"]
_check("item split across warehouse rows is merged to one", len(_anchor) == 1,
       detail=f"{len(_anchor)} rows")
_check("merged stock is summed (250)", _anchor and _num(_anchor[0].get("stock")) == 250,
       detail=str(_anchor[0].get("stock") if _anchor else None))

# Total/subtotal line dropped, never analysed as a product.
_check("'Grand Total' summary line dropped", "Grand Total" not in status)

# Money with a thousands separator parsed (3 x 1,240.00 = 3720).
_check("thousands-separated money parsed (revenue 3720)",
       "Revenue: 3720" in _line("Item: MILO"), detail=_line("Item: MILO")[:160])


# ── Recommendations: who gets one, who doesn't, and a sane quantity ──────────
_cow = rec_by.get("COWHEAD UHT MILK FULL CREAM 1L")
_check("critical item produces a reorder", bool(_cow))
_cow_qty = parse_quantity(_cow.get("suggested_quantity")) if _cow else None
_check("reorder quantity is a sane number (~1570, lead-time sized)",
       _cow_qty is not None and 1000 < _cow_qty < 2200, detail=str(_cow_qty))
_emborg = rec_by.get("EMBORG MATURE CHEDDAR BLOCK 200G")
_check("low item also produces a reorder", bool(_emborg))
_check("low item's quantity is a real number",
       _emborg and parse_quantity(_emborg.get("suggested_quantity")) is not None)
_check("healthy item gets NO reorder",
       "SUNRICE PREMIUM JASMINE RICE 5KG" not in rec_by)
_check("dead SKU never reaches recommendations",
       "RETIRED PRALINE SPREAD 250G" not in rec_by)


if _FAILED:
    print("\nSOME DUMMY-DATA CHECKS FAILED")
    sys.exit(1)
print(f"\nAll dummy-data fixture checks passed "
      f"({len(report)} items classified, {len(recs)} reorders).")
