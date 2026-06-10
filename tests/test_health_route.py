"""Proof for the health-check endpoint (audit finding #7).

Render's health monitor needs an unauthenticated URL to tell whether the app is
alive and its database is reachable. /health returns 200 when the DB answers and
503 when it doesn't, so Render can restart a wedged worker.

Dependency-free:  python tests/test_health_route.py
"""
import os
import sys
import tempfile
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


# ── Healthy: DB answers ─────────────────────────────────────────────────────
r = client.get("/health")
_check("healthy check returns 200", r.status_code == 200, detail=str(r.status_code))
_check("healthy check reports ok", r.get_json() == {"status": "ok"}, detail=str(r.get_json()))
_check("health endpoint needs no login (no redirect)", r.status_code != 302)

# ── Unhealthy: DB query raises ──────────────────────────────────────────────
_orig_query = appmod.db.query


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


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll health-route tests passed.")
