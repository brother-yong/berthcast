"""Proof for the duplicate-analysis_results fix.

The dedup confirm step used `INSERT OR REPLACE INTO analysis_results`, but there
is no UNIQUE on session_id, so REPLACE never fired — every re-submit of the
dedup form (back button, double click) inserted a NEW row for the same session.
get_outcome_stats and update_supplier_scores both loop over ALL rows for an
org's complete sessions, so that one session's recommendations were counted
TWICE in the ROI/proof numbers and supplier scores.

The route now does DELETE-then-INSERT, keeping exactly one row per session.

This drives the REAL /dedup route so what we prove is what runs in production:
  - two dedup submits leave exactly ONE analysis_results row;
  - with one row, get_outcome_stats counts the session's recs once, not twice.

Throwaway DB + stubbed anthropic; no network.
Run: python tests/test_analysis_results_single_row.py
"""
import os
import sys
import json
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_single_row.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
os.environ.setdefault("SECRET_KEY", "test-secret-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db          # noqa: E402
import app as appmod          # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

db.init_db()
appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


PW = "single-row-pw-1"
ORG = "SingleRowOrg"

db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, email_verified, tier, role) "
    "VALUES (?,?,?,?,1,'enterprise','admin')",
    ("owner@t.example", generate_password_hash(PW), ORG, "claude-sonnet-4-6"),
)

# An uploading session owned by that org (so _verify_session_owner passes).
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (1, ORG, "uploading"),
)

c = appmod.app.test_client()
r = c.post("/login", data={"email": "owner@t.example", "password": PW}, follow_redirects=False)
assert r.status_code == 302, f"login failed: {r.status_code}"

groups = json.dumps([{"canonical": "White Bread 400g", "variants": ["WHT BRD 400G"]}])

# ── 1. Submit the dedup confirm form TWICE (simulates a back-button re-submit) ─
for _ in range(2):
    resp = c.post(f"/dedup/{sid}", data={"confirmed_groups": groups}, follow_redirects=False)
    assert resp.status_code in (302, 303), f"dedup POST unexpected status: {resp.status_code}"

rows = db.query("SELECT id FROM analysis_results WHERE session_id=?", (sid,))
_check("two dedup submits leave exactly ONE analysis_results row",
       len(rows) == 1, detail=f"got {len(rows)} rows")

# ── 2. Complete that session with real recs, then check the proof numbers ─────
# Mimic what run_analysis writes on completion.
recs = [
    {"item": "White Bread 400g", "supplier": "Local SG",
     "approved": True, "order_placed": True, "outcome_status": "stockout_avoided"},
    {"item": "Frozen Salmon 1kg", "supplier": "Import Other",
     "approved": True, "order_placed": True, "outcome_status": "stockout_avoided"},
]
db.execute(
    "UPDATE analysis_results SET inventory_report=?, recommendations_json=? WHERE session_id=?",
    ("[]", json.dumps(recs), sid),
)
db.execute("UPDATE upload_sessions SET status='complete' WHERE id=?", (sid,))

stats = db.get_outcome_stats(ORG)
_check("ROI proof counts 2 approved recs (not doubled to 4)",
       stats["total_approved"] == 2, detail=str(stats.get("total_approved")))
_check("ROI proof counts 2 stockouts avoided (not doubled to 4)",
       stats["stockout_avoided"] == 2, detail=str(stats.get("stockout_avoided")))
_check("ROI proof counts 2 orders placed (not doubled to 4)",
       stats["total_order_placed"] == 2, detail=str(stats.get("total_order_placed")))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll single-row analysis_results tests passed.")
