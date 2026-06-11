"""Regression tests for per-org caps on the Claude-calling endpoints (Tier 1B).

/analyse, /api/chat and /dedup/stream each cost real API money per hit and had
no rate limit — a buggy frontend retry loop or a rogue script could run up an
unbounded bill before anyone noticed. The caps reuse rate_limit.hit() (the
same machinery already throttling sign-up/reset/contact), keyed per org.

Run: python tests/test_org_api_caps.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_apicaps.db")
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

SID = 601
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "CapOrg", "uploading", "all", "{}"))

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"] = 1
    s["email"] = "u@caporg.com"
    s["org_name"] = "CapOrg"
    s["model"] = "claude-sonnet-4-6"
    s["is_admin"] = True
    s["tier"] = "enterprise"
    s["role"] = "admin"


# ── 1. Org scoping: one org's hits never count against another ───────────────
rate_limit._hits.clear()
for _ in range(5):
    rate_limit.hit("chat:OrgA", 5, 3600)
_check("OrgA at its limit", rate_limit.hit("chat:OrgA", 5, 3600) is True)
_check("OrgB unaffected by OrgA's usage", rate_limit.hit("chat:OrgB", 5, 3600) is False)

# ── 2. /api/chat returns 429 once the org cap is hit ─────────────────────────
# Limit forced to 0 so the very first request exceeds it — this proves the cap
# fires BEFORE any DB write or Claude call (no stubbing of chat internals
# needed: reaching them would blow up on the dummy API key).
rate_limit._hits.clear()
_orig_chat = appmod.ORG_CHAT_PER_HOUR
appmod.ORG_CHAT_PER_HOUR = 0
r = client.post("/api/chat", json={"message": "hello"})
appmod.ORG_CHAT_PER_HOUR = _orig_chat
_check("chat over cap -> 429", r.status_code == 429, detail=str(r.status_code))
_check("chat 429 has a plain-language message",
       "wait" in (r.get_json() or {}).get("error", "").lower(), detail=str(r.get_json()))

# ── 3. /analyse redirects with a flash once the org cap is hit ───────────────
rate_limit._hits.clear()
_orig_an = appmod.ORG_ANALYSES_PER_DAY
appmod.ORG_ANALYSES_PER_DAY = 0
r = client.get(f"/analyse/{SID}", follow_redirects=False)
appmod.ORG_ANALYSES_PER_DAY = _orig_an
_check("analyse over cap -> redirect (no thread spawned)", r.status_code == 302,
       detail=str(r.status_code))
_check("analyse redirects to dashboard", "/dashboard" in r.headers.get("Location", ""),
       detail=r.headers.get("Location", ""))
_row = db.query("SELECT status FROM upload_sessions WHERE id=?", (SID,))
_check("session status untouched (still 'uploading')",
       _row and _row[0]["status"] == "uploading", detail=str(_row))

# ── 4. /dedup/stream returns 429 once the org cap is hit ─────────────────────
rate_limit._hits.clear()
_orig_dd = appmod.ORG_DEDUP_RUNS_PER_DAY
appmod.ORG_DEDUP_RUNS_PER_DAY = 0
r = client.get(f"/dedup/stream/{SID}")
appmod.ORG_DEDUP_RUNS_PER_DAY = _orig_dd
_check("dedup over cap -> 429", r.status_code == 429, detail=str(r.status_code))

# ── 5. Under the cap, /analyse proceeds past the limiter ─────────────────────
# Real limits are generous (10/day); with the daily allowance fresh, the
# request must NOT be turned away by the limiter. (It will start a background
# run with the stubbed anthropic client; we only assert it wasn't 429/redirected
# by the cap — the progress page renders 200.)
rate_limit._hits.clear()
r = client.get(f"/analyse/{SID}", follow_redirects=False)
_check("fresh allowance: analyse not blocked by the cap",
       r.status_code == 200, detail=str(r.status_code))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll per-org API cap tests passed.")
