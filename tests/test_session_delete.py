"""Proof for the dashboard 'remove analysis' (delete-X) feature.

A user can delete an old analysis from the dashboard. This must remove ALL of its
data (the per-session tables, the saved results, outcome rows, the session row) and
nothing else, and it must never touch another organisation's analysis.

Drives the real /session/<id>/delete route through Flask's test client.
Run:  python tests/test_session_delete.py
"""
import os
import sys
import json
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_sessiondel_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
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

import database as db                                   # noqa: E402
import app as appmod                                    # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _session_exists(sid):
    return bool(db.query("SELECT id FROM upload_sessions WHERE id=?", (sid,)))


# ── Seed: two analyses for Cool Link, one for another org ────────────────────
db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
           ("buyer@coollink.com", generate_password_hash("x"), "Cool Link", "claude-sonnet-4-6", "enterprise"))
uid = db.query("SELECT id FROM users WHERE email=?", ("buyer@coollink.com",))[0]["id"]

A = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)", (uid, "Cool Link", "complete"))
B = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)", (uid, "Cool Link", "complete"))
# Another org's analysis — must be untouchable.
C = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)", (uid + 999, "Rival Foods", "complete"))

for sid in (A, B, C):
    db.execute("INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
               (sid, "[]", "[]"))
db.execute('CREATE TABLE "inventory_%d" (item TEXT)' % A)
db.execute('INSERT INTO "inventory_%d" (item) VALUES (?)' % A, ("Frozen Salmon",))
db.execute("INSERT INTO recommendation_outcomes (session_id, item) VALUES (?,?)", (A, "Frozen Salmon"))

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"]  = uid
    s["email"]    = "buyer@coollink.com"
    s["org_name"] = "Cool Link"
    s["model"]    = "claude-sonnet-4-6"
    s["is_admin"] = False
    s["tier"]     = "enterprise"
    s["role"]     = "admin"


# ── 1. Happy path: delete A ──────────────────────────────────────────────────
r = client.post(f"/session/{A}/delete")
_check("delete returns ok", r.status_code == 200 and r.get_json().get("ok") is True, detail=str(r.get_json()))
_check("session A row gone", not _session_exists(A))
_check("session A results gone", not db.query("SELECT id FROM analysis_results WHERE session_id=?", (A,)))
_check("session A outcomes gone", not db.query("SELECT id FROM recommendation_outcomes WHERE session_id=?", (A,)))
_check("session A per-session table dropped", not db.table_exists(f"inventory_{A}"))
_check("session B is untouched", _session_exists(B) and bool(db.query("SELECT id FROM analysis_results WHERE session_id=?", (B,))))


# ── 2. Org-scope: cannot delete another org's analysis ───────────────────────
r = client.post(f"/session/{C}/delete")
_check("deleting another org's analysis is refused", r.status_code in (403, 404), detail=str(r.status_code))
_check("the other org's analysis still exists", _session_exists(C))


# ── 3. Dashboard renders the delete control ──────────────────────────────────
r = client.get("/dashboard")
_check("dashboard renders 200", r.status_code == 200, detail=str(r.status_code))
_check("dashboard wires up deleteSession", b"deleteSession(" in r.data)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll session-delete tests passed.")
