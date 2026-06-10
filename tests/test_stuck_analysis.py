"""Proof for stuck-analysis recovery (audit finding #6).

An analysis runs in a background thread with its progress held in an in-memory
dict. If the worker restarts mid-run the thread dies and the dict is gone, but
the session row would sit in 'analyzing' forever — the user stares at a spinner
that never resolves.

The fix marks runs 'analyzing' with a start time, and:
  - a boot sweep (db.fail_orphaned_analyses) flips leftover 'analyzing' rows to
    'failed';
  - /analysis_status, finding no in-memory progress, reports the run as failed
    (after a short grace window) so the page shows an error + retry.

Dependency-free:  python tests/test_stuck_analysis.py
"""
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_stuck.db")
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

import database as db          # noqa: E402
import app as appmod          # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _status_of(sid):
    return db.query("SELECT status FROM upload_sessions WHERE id=?", (sid,))[0]["status"]


def _mk_session(status, started_at=None):
    sid = db.execute(
        "INSERT INTO upload_sessions (user_id, org_name, status, analysis_started_at) VALUES (?,?,?,?)",
        (1, "Test Org", status, started_at)
    )
    return sid


# ── 1. Boot sweep flips orphaned 'analyzing' rows to 'failed' ────────────────
s_orphan = _mk_session("analyzing", (datetime.utcnow() - timedelta(hours=2)).isoformat())
n = db.fail_orphaned_analyses()
_check("boot sweep reports at least one cleaned run", n >= 1, detail=str(n))
_check("orphaned 'analyzing' row becomes 'failed'", _status_of(s_orphan) == "failed",
       detail=_status_of(s_orphan))

# ── Logged-in client in the same org (analysis_status checks ownership) ──────
appmod.app.config["TESTING"] = True
client = appmod.app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = 1
    sess["org_name"] = "Test Org"
    sess["model"] = "claude-sonnet-4-6"
    sess["is_admin"] = False
    sess["tier"] = "enterprise"
    sess["role"] = "admin"

# ── 2. A long-stale 'analyzing' run with no in-memory progress -> error ──────
s_stale = _mk_session("analyzing", (datetime.utcnow() - timedelta(hours=1)).isoformat())
r = client.get(f"/analysis_status/{s_stale}")
body = r.get_json()
_check("stale run reports error to the page", body.get("status") == "error", detail=str(body))
_check("stale run is persisted as 'failed'", _status_of(s_stale) == "failed", detail=_status_of(s_stale))

# ── 3. A freshly-started 'analyzing' run is given a grace window -> running ──
s_fresh = _mk_session("analyzing", datetime.utcnow().isoformat())
r = client.get(f"/analysis_status/{s_fresh}")
body = r.get_json()
_check("fresh run within grace still reports running", body.get("status") == "running", detail=str(body))
_check("fresh run is left as 'analyzing'", _status_of(s_fresh) == "analyzing", detail=_status_of(s_fresh))

# ── 4. A 'failed' run reports error ─────────────────────────────────────────
s_failed = _mk_session("failed", (datetime.utcnow() - timedelta(hours=1)).isoformat())
r = client.get(f"/analysis_status/{s_failed}")
_check("failed run reports error to the page", r.get_json().get("status") == "error", detail=str(r.get_json()))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll stuck-analysis tests passed.")
