"""Regression tests for the stream-lane guards (the 11 June 2026 freeze).

The outage: gunicorn ran 1 worker x 4 threads. The two SSE endpoints
(/api/chat, /dedup/stream) each hold a thread for a whole Claude stream, and
the dedup REVIEW page popped its cache and re-ran the entire normalisation
agent synchronously on a request thread on any refresh. A few refreshes /
locked phones consumed every thread and the whole site (including /health and
login) hung for 85 minutes with nothing in the logs.

Fixes under test:
  - dedup review never blocks a request thread: cache is read (not consumed),
    a miss redirects to the loading page instead of running the agent inline;
  - /dedup/stream serves a cached result instantly on reconnect — no second
    Claude call, no burn of the daily rate cap;
  - both streams claim a lane from _stream_lanes (capped below gunicorn's
    --threads) and return a plain 503 when lanes run out, so plain pages
    always have free threads — the site can no longer fully freeze;
  - lanes are released on every path (response close, early returns, errors);
  - streaming Anthropic clients carry explicit timeouts (SDK default of
    10 minutes would pin a thread that long on a stalled call).

Run: python tests/test_stream_lanes.py
"""
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_streamlanes.db")
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

import database as db        # noqa: E402
import rate_limit            # noqa: E402
import app as appmod         # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


db.init_db()
appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True

SID = 801
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "LaneOrg", "uploading", "all", "{}"))
# The dedup stream reads item names from this table; without it the generator
# short-circuits before ever reaching the Anthropic client.
db.execute(f'CREATE TABLE inventory_{SID} ("description" TEXT, "qty" TEXT)')
for n in ("EDAM CHEESE 1KG", "EDAM CHSE 1 KG", "GOUDA WHEEL 4KG"):
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?)", (n, "5"))

SID2 = 802
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID2, 1, "LaneOrg", "uploading", "all", "{}"))

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"] = 1
    s["email"] = "u@laneorg.com"
    s["org_name"] = "LaneOrg"
    s["model"] = "claude-sonnet-4-6"
    s["is_admin"] = True
    s["tier"] = "enterprise"
    s["role"] = "admin"


def _lanes_free():
    """Count free lanes by draining and restoring the semaphore."""
    n = 0
    while appmod._stream_lanes.acquire(blocking=False):
        n += 1
    for _ in range(n):
        appmod._stream_lanes.release()
    return n


def _drain_lanes():
    n = 0
    while appmod._stream_lanes.acquire(blocking=False):
        n += 1
    return n


def _refill_lanes(n):
    for _ in range(n):
        appmod._stream_lanes.release()


class _CapturingAnthropic:
    """Stands in for anthropic.Anthropic inside app.py. Captures constructor
    kwargs (to assert the timeout is wired) and raises on use, which drives
    each stream's error path without any real API call."""
    last_kwargs = None

    def __init__(self, **kw):
        _CapturingAnthropic.last_kwargs = kw

    class _Msgs:
        def stream(self, **kw):
            raise RuntimeError("stub: tests never call the real API")

        def create(self, **kw):
            raise RuntimeError("stub: tests never call the real API")

    @property
    def messages(self):
        return self._Msgs()


_orig_anthropic = appmod._anthropic.Anthropic
appmod._anthropic.Anthropic = _CapturingAnthropic
_LANES_AT_START = _lanes_free()


# ── 1. The refresh bomb is dead ──────────────────────────────────────────────
appmod.normalization_cache.pop(SID, None)
r = client.get(f"/dedup/{SID}", follow_redirects=False)
_check("review without cache -> redirect to loading page (never blocks a lane)",
       r.status_code == 302 and f"/dedup/loading/{SID}" in r.headers.get("Location", ""),
       detail=f"{r.status_code} {r.headers.get('Location', '')}")

# ── 2. Dedup stream: timeout wired, error cached, lane returned ──────────────
rate_limit._hits.clear()
_CapturingAnthropic.last_kwargs = None
r = client.get(f"/dedup/stream/{SID}")
body = r.get_data(as_text=True)
r.close()
_check("dedup stream ran (status + error events)", "'" not in body and "status" in body and "error" in body,
       detail=body[:200])
_check("dedup Anthropic client carries the explicit timeout",
       (_CapturingAnthropic.last_kwargs or {}).get("timeout") == appmod.DEDUP_STREAM_TIMEOUT_S,
       detail=str(_CapturingAnthropic.last_kwargs))
_check("failed scan still caches a result (review won't loop)",
       appmod.normalization_cache.get(SID) is not None)
_check("lane returned after dedup stream closed", _lanes_free() == _LANES_AT_START,
       detail=f"free={_lanes_free()} expected={_LANES_AT_START}")

# ── 3. Review renders from cache and the cache survives refreshes ────────────
r1 = client.get(f"/dedup/{SID}", follow_redirects=False)
r2 = client.get(f"/dedup/{SID}", follow_redirects=False)
_check("review renders from cache", r1.status_code == 200, detail=str(r1.status_code))
_check("refresh renders again — cache NOT consumed (this WAS the outage)",
       r2.status_code == 200 and appmod.normalization_cache.get(SID) is not None,
       detail=str(r2.status_code))

# ── 4. Stream reconnect is free: cached, no Claude call, no cap burn ─────────
rate_limit._hits.clear()
_CapturingAnthropic.last_kwargs = None
r = client.get(f"/dedup/stream/{SID}")
body = r.get_data(as_text=True)
r.close()
_check("reconnect serves cached done instantly", "done" in body, detail=body[:200])
_check("reconnect never touches Claude", _CapturingAnthropic.last_kwargs is None)
_check("reconnect burns no daily allowance",
       not any(k.startswith("dedup:") for k in rate_limit._hits),
       detail=str(list(rate_limit._hits.keys())))

# ── 5. Chat stream: timeout wired, lane returned, errors surfaced ────────────
_CapturingAnthropic.last_kwargs = None
r = client.post("/api/chat", json={"message": "what should I order?"})
body = r.get_data(as_text=True)
r.close()
_check("chat stream responds (conversation id + error event from stub)",
       r.status_code == 200 and "conversation_id" in body and "error" in body,
       detail=f"{r.status_code} {body[:200]}")
_check("chat Anthropic client carries the explicit timeout",
       (_CapturingAnthropic.last_kwargs or {}).get("timeout") == appmod.CHAT_STREAM_TIMEOUT_S,
       detail=str(_CapturingAnthropic.last_kwargs))
_check("lane returned after chat stream closed", _lanes_free() == _LANES_AT_START,
       detail=f"free={_lanes_free()}")

# ── 6. Lanes full: streams get 503, the rest of the site stays alive ─────────
_n = _drain_lanes()
try:
    _conv_before = db.query("SELECT COUNT(*) AS n FROM chat_conversations")[0]["n"]
    r = client.post("/api/chat", json={"message": "hello"})
    _conv_after = db.query("SELECT COUNT(*) AS n FROM chat_conversations")[0]["n"]
    _check("chat over lane cap -> 503 with plain message",
           r.status_code == 503 and "busy" in (r.get_json() or {}).get("error", ""),
           detail=f"{r.status_code} {r.get_json()}")
    _check("refused chat writes nothing (no orphan conversation)",
           _conv_after == _conv_before, detail=f"{_conv_before}->{_conv_after}")

    rate_limit._hits.clear()
    r = client.get(f"/dedup/stream/{SID2}")
    _check("dedup stream over lane cap -> 503", r.status_code == 503, detail=str(r.status_code))
    _check("busy rejection burns no daily allowance",
           not any(k.startswith("dedup:") for k in rate_limit._hits))

    r = client.get("/health")
    _check("/health still answers with every lane taken (Render monitoring stays truthful)",
           r.status_code == 200, detail=str(r.status_code))
    r = client.get("/dashboard")
    _check("dashboard still answers with every lane taken (site never fully freezes)",
           r.status_code == 200, detail=str(r.status_code))
finally:
    _refill_lanes(_n)

# ── 7. Early-return paths never leak a lane ──────────────────────────────────
r = client.post("/api/chat", json={"conversation_id": 999999, "message": "hi"})
_check("unknown conversation -> 404, no lane lost",
       r.status_code == 404 and _lanes_free() == _LANES_AT_START,
       detail=f"{r.status_code} free={_lanes_free()}")

rate_limit._hits.clear()
appmod.normalization_cache.pop(SID2, None)
_orig_cap = appmod.ORG_DEDUP_RUNS_PER_DAY
appmod.ORG_DEDUP_RUNS_PER_DAY = 0
r = client.get(f"/dedup/stream/{SID2}")
appmod.ORG_DEDUP_RUNS_PER_DAY = _orig_cap
_check("dedup daily cap -> 429, no lane lost",
       r.status_code == 429 and _lanes_free() == _LANES_AT_START,
       detail=f"{r.status_code} free={_lanes_free()}")

# ── 8. Config drift guard: stream cap must stay below gunicorn threads ───────
with open(os.path.join(ROOT, "render.yaml"), encoding="utf-8") as f:
    _yaml = f.read()
_m = re.search(r"--threads\s+(\d+)", _yaml)
_threads = int(_m.group(1)) if _m else 0
_check("render.yaml --threads exceeds MAX_CONCURRENT_STREAMS (pages keep free lanes)",
       _threads > appmod.MAX_CONCURRENT_STREAMS,
       detail=f"threads={_threads} cap={appmod.MAX_CONCURRENT_STREAMS}")

# ── 9. The cap derives from the LIVE thread count, not an assumption ─────────
# 12 June 2026: the Render dashboard's start command overrode render.yaml and
# still ran 4 threads — a cap of 8 protected nothing. The app now reads
# --threads off gunicorn's own command line (workers inherit the master argv).
_check("--threads 12 -> 8 lanes (4 always free for pages)",
       appmod._lanes_for_threads(12) == 8, detail=str(appmod._lanes_for_threads(12)))
_check("--threads 4 -> 1 lane (the dashboard-drift case, site stays alive)",
       appmod._lanes_for_threads(4) == 1, detail=str(appmod._lanes_for_threads(4)))
_check("--threads 6 -> 2 lanes", appmod._lanes_for_threads(6) == 2)
_check("lanes never drop below 1", appmod._lanes_for_threads(1) == 1)
_check("lanes never exceed 8 however many threads",
       appmod._lanes_for_threads(64) == 8)
_check("argv '--threads 12' parsed",
       appmod._gunicorn_threads(["gunicorn", "app:app", "--threads", "12"]) == 12)
_check("argv '--threads=4' parsed",
       appmod._gunicorn_threads(["gunicorn", "--threads=4"]) == 4)
_check("no --threads in argv -> None (dev/test default of 8 applies)",
       appmod._gunicorn_threads(["python", "tests/x.py"]) is None)
_check("junk --threads value -> None, never a crash at import",
       appmod._gunicorn_threads(["gunicorn", "--threads", "lots"]) is None)


appmod._anthropic.Anthropic = _orig_anthropic

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll stream-lane tests passed.")
