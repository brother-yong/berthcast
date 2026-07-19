"""The quantity hallucination-guard must survive item-name echo drift.

The recommendation agent stores its Python-computed order quantity in
qty_basis_by_item, then looks it up by the item name the model ECHOED back.
If the model drifts the spacing/casing/punctuation of the name, an exact-match
lookup misses, sanitize_suggested_quantity silently never runs, and the model's
raw suggested_quantity goes straight to the printed PO sheet — the exact
scenario the guardrail exists to prevent. Both maps must be keyed with
normalise_match_key so drift can never break the match.

Run: python tests/test_qty_guard_keying.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_qtyguard.db")
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
import agents.recommendation as rec_mod    # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


db.init_db()
SID = 981
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "QtyGuardOrg", "complete", "all", "{}"))

# Inventory: one out-of-stock item with a UOM the guard should carry through.
db.execute(f'CREATE TABLE inventory_{SID} ('
           '"description" TEXT, "qty_on_hand" TEXT, "category" TEXT, "uom" TEXT)')
db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)",
           ("BROOKVALE UHT MILK 1L", "0", "DAIRY", "CTN"))

# Sales: a clear ~100/month average so the Python-side quantity exists.
db.execute(f'CREATE TABLE sales_{SID} ('
           '"item_name" TEXT, "qty_sold" TEXT, "avg_qty___month" TEXT)')
db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?)",
           ("BROOKVALE UHT MILK 1L", "1200", "100"))

_REPORT = [
    {"item": "BROOKVALE UHT MILK 1L", "status": "CRITICAL", "days_of_supply": 0,
     "observation": "o", "category": "DAIRY", "spoilage_risk": "NONE"},
]


def _drifted_claude(model, system, user, max_tokens=4096):
    """The model echoes the item name with drifted case/punctuation and a
    hallucinated quantity — the guard must still catch it."""
    return ('[{"item": "Brookvale U.H.T. Milk 1L", "recommended_action": "ORDER", '
            '"suggested_quantity": "99999", "confidence": "HIGH", "reason": "x", '
            '"supplier": "Unknown"}]')


rec_mod._call_claude = _drifted_claude
log = []
recs = rec_mod.run_recommendation_agent(SID, "m", list(_REPORT), {}, progress_emit=log.append)

rec = next((r for r in recs if isinstance(r, dict) and "item" in r), {})
_qty = str(rec.get("suggested_quantity", ""))
_check("hallucinated 99999 never reaches the PO sheet (sanitizer ran despite drift)",
       _qty != "99999" and not _qty.startswith("99999"), detail=_qty)
_check("correction flagged (_quantity_corrected is True)",
       rec.get("_quantity_corrected") is True, detail=str(rec.get("_quantity_corrected")))
_avg = rec.get("avg_monthly_sales")
_check("quantity basis attached despite drift (avg_monthly_sales > 0)",
       isinstance(_avg, (int, float)) and _avg > 0, detail=str(_avg))
_check("UOM lookup survived the drift (uom_label carries CTN)",
       "CTN" in str(rec.get("uom_label", "")), detail=str(rec.get("uom_label")))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll quantity-guard keying tests passed.")
