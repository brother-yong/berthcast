"""Bulk outcome writer: one transaction for a whole run's outcome stubs.

The recommendation agent used to write one outcome row per rec, each in its
own SQLite connection+transaction — hundreds of serial open/commit/close
cycles per analysis. save_recommendation_outcomes_bulk takes the whole run's
rows in one executemany, carrying the supplier column plan 004 added.

Run: python tests/test_outcomes_bulk.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_outcomesbulk.db")
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
SID = 995
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "BulkOrg", "complete", "all", "{}"))


def _row(item, supplier):
    return {
        "session_id": SID,
        "item": item,
        "action_recommended": "REORDER",
        "predicted_loss_no_act": 0,
        "predicted_cost_act": 0,
        "net_benefit": 0,
        "confidence": "HIGH",
        "supplier": supplier,
    }


# All names invented: BROOKVALE / NORDVIK / PADIMAS.
db.save_recommendation_outcomes_bulk([
    _row("BROOKVALE UHT MILK 1L", "NORDVIK DAIRY"),
    _row("BROOKVALE BUTTER 250G", "NORDVIK DAIRY"),
    _row("TIDEWORTH COCONUT WATER 330ML", "PADIMAS TRADING"),
])
_n = db.query("SELECT COUNT(*) AS n FROM recommendation_outcomes")[0]["n"]
_check("bulk call inserts all 3 rows", _n == 3, detail=str(_n))

db.save_recommendation_outcomes_bulk([])
_n2 = db.query("SELECT COUNT(*) AS n FROM recommendation_outcomes")[0]["n"]
_check("empty list is a no-op (no error, count unchanged)", _n2 == 3, detail=str(_n2))

_sup = {r["item"]: r["supplier"] for r in db.query(
    "SELECT item, supplier FROM recommendation_outcomes WHERE session_id=?", (SID,))}
_check("supplier values carried into the rows",
       _sup.get("BROOKVALE UHT MILK 1L") == "NORDVIK DAIRY"
       and _sup.get("BROOKVALE BUTTER 250G") == "NORDVIK DAIRY"
       and _sup.get("TIDEWORTH COCONUT WATER 330ML") == "PADIMAS TRADING",
       detail=str(_sup))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll bulk-outcome tests passed.")
