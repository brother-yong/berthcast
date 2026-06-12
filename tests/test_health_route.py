"""Proof for the health-check endpoint (audit finding #7, hardened 12 June 2026).

Render's health monitor needs an unauthenticated URL to tell whether the app is
alive and its database is reachable. /health returns 200 when the DB answers and
503 when it doesn't, so Render can restart a wedged worker.

12 June 2026 hardening: the old probe ran SELECT 1, which touches no stored
data pages — when the Render disk wedged after a plan change, /health kept
saying 200 while every login hung forever on a real table read, so Render
never restarted the zombie. The probe now reads the users table in a side
thread with a bounded wait: a hung read answers 503 instead of hanging, a
stuck probe is abandoned (at most ONE leaked thread, single-flight), and the
endpoint recovers on its own once the disk serves reads again.

Dependency-free:  python tests/test_health_route.py
"""
import os
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_health.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
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

appmod.app.config["TESTING"] = True
client = appmod.app.test_client()

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


_orig_query = appmod.db.query

# ── Healthy: DB answers ─────────────────────────────────────────────────────
r = client.get("/health")
_check("healthy check returns 200", r.status_code == 200, detail=str(r.status_code))
_check("healthy check reports ok", r.get_json() == {"status": "ok"}, detail=str(r.get_json()))
_check("health endpoint needs no login (no redirect)", r.status_code != 302)

# ── The probe must read a REAL table (SELECT 1 was the 12 June blind spot) ──
_captured = []


def _spy_query(sql, params=()):
    _captured.append(sql)
    return _orig_query(sql, params)


appmod.db.query = _spy_query
try:
    r = client.get("/health")
finally:
    appmod.db.query = _orig_query
_check("probe reads the users table, not SELECT 1",
       any("users" in s.lower() for s in _captured), detail=str(_captured))

# ── Unhealthy: DB query raises ──────────────────────────────────────────────


def _broken_query(*a, **k):
    raise RuntimeError("database is unreachable")


appmod.db.query = _broken_query
try:
    r = client.get("/health")
    _check("unhealthy check returns 503", r.status_code == 503, detail=str(r.status_code))
    _check("unhealthy check reports error", r.get_json() == {"status": "error"}, detail=str(r.get_json()))
finally:
    appmod.db.query = _orig_query

# Recovered
r = client.get("/health")
_check("health recovers once the DB is back", r.status_code == 200, detail=str(r.status_code))

# ── Wedged disk: the read hangs forever (the 12 June failure mode) ──────────
_release = threading.Event()
_probe_calls = []


def _hung_query(sql, params=()):
    _probe_calls.append(sql)
    _release.wait()          # simulates a read stuck inside the OS
    return _orig_query(sql, params)


_orig_timeout = appmod.HEALTH_DB_PROBE_TIMEOUT_S
appmod.HEALTH_DB_PROBE_TIMEOUT_S = 0.3
appmod.db.query = _hung_query
try:
    t0 = time.time()
    r1 = client.get("/health")
    took1 = time.time() - t0
    _check("hung DB read -> 503 within the bounded wait (never hangs the poll)",
           r1.status_code == 503 and took1 < 2.0,
           detail=f"status={r1.status_code} took={took1:.2f}s")

    t0 = time.time()
    r2 = client.get("/health")
    took2 = time.time() - t0
    _check("while the probe is stuck, the next poll answers 503 instantly",
           r2.status_code == 503 and took2 < 0.25,
           detail=f"status={r2.status_code} took={took2:.2f}s")
    _check("single-flight: the stuck probe is not duplicated per poll",
           len(_probe_calls) == 1, detail=f"probe calls={len(_probe_calls)}")
finally:
    _release.set()           # un-wedge: let the abandoned thread finish
    time.sleep(0.3)          # it clears the in-flight slot on its way out
    appmod.db.query = _orig_query
    appmod.HEALTH_DB_PROBE_TIMEOUT_S = _orig_timeout

r = client.get("/health")
_check("health recovers on its own after the disk serves reads again",
       r.status_code == 200, detail=str(r.status_code))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll health-route tests passed.")
