"""Supplier accuracy history must be real, not item-name string luck.

get_supplier_accuracy used to filter recommendation_outcomes with
`ro.item LIKE '%<supplier name>%'` — matching PRODUCT names against a SUPPLIER
name. A product name virtually never contains its supplier's name, so the
"Past recs: N approved/dismissed" prompt enrichment returned zero forever.
The fix adds a supplier column to the table, records it at write time, and
filters on it exactly. Legacy rows (NULL supplier) stay excluded.

Run: python tests/test_supplier_accuracy.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_supacc.db")
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

import database as db  # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


db.init_db()
SID_A, SID_B = 991, 992
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_A, 1, "OrgA", "complete", "all", "{}"))
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_B, 2, "OrgB", "complete", "all", "{}"))

# All names invented: NORDVIK / PADIMAS / BROOKVALE / TIDEWORTH.
db.save_recommendation_outcome(
    session_id=SID_A, item="BROOKVALE UHT MILK 1L", action_recommended="REORDER",
    predicted_loss_no_act=0, predicted_cost_act=0, net_benefit=0,
    confidence="HIGH", supplier="NORDVIK DAIRY")
db.save_recommendation_outcome(
    session_id=SID_A, item="BROOKVALE BUTTER 250G", action_recommended="REORDER",
    predicted_loss_no_act=0, predicted_cost_act=0, net_benefit=0,
    confidence="MEDIUM", supplier="NORDVIK DAIRY")
db.save_recommendation_outcome(
    session_id=SID_A, item="TIDEWORTH COCONUT WATER 330ML", action_recommended="HOLD",
    predicted_loss_no_act=0, predicted_cost_act=0, net_benefit=0,
    confidence="LOW", supplier="PADIMAS TRADING")
db.save_recommendation_outcome(
    session_id=SID_B, item="BROOKVALE UHT MILK 1L", action_recommended="REORDER",
    predicted_loss_no_act=0, predicted_cost_act=0, net_benefit=0,
    confidence="HIGH", supplier="NORDVIK DAIRY")

# INSERT OR IGNORE must not silently drop any of the four rows.
_n = db.query("SELECT COUNT(*) AS n FROM recommendation_outcomes "
              "WHERE session_id IN (?,?)", (SID_A, SID_B))[0]["n"]
_check("all four outcome rows were written (INSERT OR IGNORE skipped none)",
       _n == 4, detail=str(_n))

db.execute("UPDATE recommendation_outcomes SET user_action='approved' "
           "WHERE session_id=? AND item=?", (SID_A, "BROOKVALE UHT MILK 1L"))

acc = db.get_supplier_accuracy("OrgA", "NORDVIK DAIRY")
_check("OrgA/NORDVIK sees its two outcomes", acc["total_recs"] == 2, detail=str(acc))
_check("OrgA/NORDVIK approved tally = 1", acc["approved"] == 1, detail=str(acc))
_check("OrgA/PADIMAS sees its one outcome",
       db.get_supplier_accuracy("OrgA", "PADIMAS TRADING")["total_recs"] == 1)
_check("org isolation: OrgB never sees OrgA's PADIMAS history",
       db.get_supplier_accuracy("OrgB", "PADIMAS TRADING")["total_recs"] == 0)

# Legacy rows never recorded a supplier — they must stay excluded, not
# accidentally match anything.
db.execute("INSERT INTO recommendation_outcomes (session_id, item, action_recommended) "
           "VALUES (?,?,?)", (SID_A, "LEGACY ITEM NO SUPPLIER", "REORDER"))
_check("legacy NULL-supplier rows never match",
       db.get_supplier_accuracy("OrgA", "NORDVIK DAIRY")["total_recs"] == 2)

# The old bug matched ITEM names against the supplier string: "MILK" appears
# in an item name but is no supplier — it must count zero now.
_check("item-name LIKE matching is gone ('MILK' matches no supplier)",
       db.get_supplier_accuracy("OrgA", "MILK")["total_recs"] == 0)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll supplier-accuracy tests passed.")
