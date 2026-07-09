"""Proof for the DB wedge watchdog (added 6 Jul 2026).

Root cause of the recurring login freeze: when the Render disk stalls,
sqlite3.connect() blocks inside an uninterruptible OS wait that database.py's
timeout=30 does NOT cover. Every DB-touching request piles up and the single
worker freezes until a human restarts it. gunicorn never times it out because
its accept loop keeps the worker heartbeat alive.

The watchdog probes its own DB open on a dedicated thread. _probe_wedged returns
True ONLY when that open never finishes within the deadline (a true hang); a
normal error still finishes, so ordinary lock contention never triggers a
restart. When it returns True the watchdog calls os._exit(1) and gunicorn
respawns a fresh worker. This exercises the decision — the part that would
otherwise kill the site by mistake — without ever calling os._exit.

Dependency-free:  python tests/test_db_watchdog.py
"""
import os
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_watchdog.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)          # so importing app does NOT start the real watchdog
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

# Stub the anthropic SDK — only constructed at import, never called here.
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


_orig_query = appmod.db.query

# ── Healthy DB: probe finishes fast, worker is NOT wedged ───────────────────
t0 = time.time()
wedged = appmod._probe_wedged(2.0)
_check("healthy DB is not flagged wedged", wedged is False)
_check("healthy probe returns well within the deadline", (time.time() - t0) < 1.0,
       detail=f"took={time.time() - t0:.2f}s")

# ── Erroring DB (e.g. a lock that raises): NOT a wedge, must not restart ─────
def _broken_query(*a, **k):
    raise RuntimeError("database is locked")


appmod.db.query = _broken_query
try:
    wedged = appmod._probe_wedged(1.0)
    _check("a DB error is NOT treated as a wedge (no needless restart)", wedged is False)
finally:
    appmod.db.query = _orig_query

# ── Wedged DB: the open hangs forever (the real failure mode) ───────────────
_release = threading.Event()


def _hung_query(*a, **k):
    _release.wait()             # simulates sqlite3.connect() stuck inside the OS
    return _orig_query(*a, **k)


appmod.db.query = _hung_query
try:
    t0 = time.time()
    wedged = appmod._probe_wedged(0.3)
    took = time.time() - t0
    _check("a hung DB open IS flagged wedged (would trigger self-restart)", wedged is True)
    _check("wedge is detected at the deadline, not indefinitely", took < 1.0,
           detail=f"took={took:.2f}s")
finally:
    _release.set()              # un-wedge: let the abandoned probe thread finish
    time.sleep(0.1)
    appmod.db.query = _orig_query


# ── Start logic: once, inside the worker, never under TESTING ───────────────
# The 9 Jul 2026 freeze did not self-heal because the watchdog was started at
# import time and never ran in the forked worker. It now starts on first request
# via _ensure_watchdog. Stub the loop so the test never spawns the real thing.
_started = []
appmod._db_watchdog = lambda: _started.append(1)
appmod._watchdog_started = False

appmod.app.config["TESTING"] = True
appmod._ensure_watchdog()
_check("TESTING blocks the watchdog (no self-killing thread in tests)",
       appmod._watchdog_started is False and _started == [])

appmod.app.config["TESTING"] = False
try:
    appmod._ensure_watchdog()
    appmod._ensure_watchdog()          # second call must be a no-op
    _check("watchdog marks itself started when not testing", appmod._watchdog_started is True)
    time.sleep(0.1)                    # let the (stubbed) target run
    _check("watchdog starts exactly once", len(_started) == 1, detail=f"starts={len(_started)}")
finally:
    appmod.app.config["TESTING"] = True


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll DB-watchdog tests passed.")
