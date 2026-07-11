"""Blocked-vs-crash split in /analysis_status (spec 2026-07-11).

A refused file (blocked) and a real crash used to reach the page as the same
red failure. The route must now (a) pass the ``blocked`` flag through, (b)
keep a blocked run's guidance message verbatim (it contains the fix), and
(c) replace raw crash text with a friendly generic — raw errors are for
logs and ALERT_EMAIL, never for staff.

Dependency-free:  python tests/test_analysis_status_blocked.py
"""
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_blocked.db")
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
import app as appmod           # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _mk_session(status, started_at=None):
    return db.execute(
        "INSERT INTO upload_sessions (user_id, org_name, status, analysis_started_at) VALUES (?,?,?,?)",
        (1, "Test Org", status, started_at)
    )


def _mem_entry(sid, **extra):
    entry = {"status": "running", "log": [], "started_at": time.time(),
             "agents": {}, "stats": {}, "current_agent": "inventory"}
    entry.update(extra)
    with appmod.analysis_progress_lock:
        appmod.analysis_progress[sid] = entry


appmod.app.config["TESTING"] = True
client = appmod.app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = 1
    sess["org_name"] = "Test Org"
    sess["model"] = "claude-sonnet-4-6"
    sess["is_admin"] = False
    sess["tier"] = "enterprise"
    sess["role"] = "admin"

GUIDANCE = ("We couldn't find both an item-name column and a current-stock "
            "column in your Inventory Report. Make sure one column lists "
            "product names and one lists how much is in stock now.")
RAW_CRASH = "No inventory rows. desc_col=None, qty_col=None, rows=0"

# ── 1. Blocked run: flag passes through, guidance kept verbatim ──────────────
s_blocked = _mk_session("failed")
_mem_entry(s_blocked, status="error", error=GUIDANCE, blocked=True,
           log=[{"t": 1.0, "msg": "Columns detected — item: desc, stock: qty",
                 "agent": "inventory"}])
body = client.get(f"/analysis_status/{s_blocked}").get_json()
_check("blocked run has blocked=true", body.get("blocked") is True, str(body))
_check("blocked run keeps its guidance verbatim", body.get("error") == GUIDANCE,
       str(body.get("error")))
_check("blocked run keeps its progress log", len(body.get("log") or []) == 1,
       str(body.get("log")))

# ── 2. Crash: raw text scrubbed, friendly generic shown ──────────────────────
# Agents also _emit the exception into the progress log right before returning
# it — the scrub must cover the WHOLE payload, not just the error field.
s_crash = _mk_session("failed")
_mem_entry(s_crash, status="error", error=RAW_CRASH, blocked=False,
           log=[{"t": 1.0, "msg": f"Inventory agent error: {RAW_CRASH}",
                 "agent": "inventory"}])
resp = client.get(f"/analysis_status/{s_crash}")
body = resp.get_json()
_check("crash has blocked=false", body.get("blocked") is False, str(body))
_check("crash error replaced by friendly generic",
       body.get("error") == appmod.CRASH_FRIENDLY_ERROR, str(body.get("error")))
_check("raw crash text absent from the whole payload (incl. log)",
       RAW_CRASH not in resp.get_data(as_text=True))

# ── 3. Missing blocked key (old-style entry) defaults to crash ───────────────
s_legacy = _mk_session("failed")
_mem_entry(s_legacy, status="error", error=RAW_CRASH)   # no 'blocked' key
body = client.get(f"/analysis_status/{s_legacy}").get_json()
_check("missing flag defaults to blocked=false", body.get("blocked") is False, str(body))
_check("missing flag still scrubs raw text",
       body.get("error") == appmod.CRASH_FRIENDLY_ERROR, str(body.get("error")))

# ── 4. Running entry untouched (scrub only applies to errors) ────────────────
s_running = _mk_session("analyzing", datetime.utcnow().isoformat())
_mem_entry(s_running)   # status='running', no error
body = client.get(f"/analysis_status/{s_running}").get_json()
_check("running entry not scrubbed", body.get("error") is None, str(body.get("error")))
_check("running entry carries blocked=false", body.get("blocked") is False, str(body))

# ── 5. DB fallback (worker died): friendly message, blocked=false ─────────────
s_dead = _mk_session("failed", (datetime.utcnow() - timedelta(hours=1)).isoformat())
body = client.get(f"/analysis_status/{s_dead}").get_json()
_check("worker-died fallback reports error", body.get("status") == "error", str(body))
_check("worker-died fallback has blocked=false", body.get("blocked") is False, str(body))
_check("worker-died fallback keeps its own friendly message",
       "run it again" in (body.get("error") or ""), str(body.get("error")))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll blocked-vs-crash payload tests passed.")
