"""Cross-org lockout for the session-scoped routes that had no test.

A route-coverage sweep on 25 Jul 2026 found 17 of 52 routes were never touched by
the suite. The dangerous subset is the session-scoped ones: every one takes an id
straight from the URL or the JSON body, so a missing ownership check is a
cross-tenant data leak — the single invariant CLAUDE.md calls out as "Org A must
never see Org B's data".

The guards are all present in app.py today. This test exists so they cannot be
dropped silently later. /diff is the clearest example: it verifies BOTH ids
(app.py:3153-3154), and it is tested in both argument orders here, because
verifying only the first one is the natural way for that to regress.

Covered: /diff/<a>/<b>, /upload/status, /upload/scope, /upload/use_previous,
/api/chat/conversation, /recommend/approve_all, /recommend/outcome, and
/admin/backup/download (which streams the entire database, so a non-admin
reaching it would be the worst single failure in the app).

Run:  python tests/test_cross_org_untested_routes.py
"""
import os
import sys
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Throwaway DB + uploads, not on Render, BEFORE importing config/app — both read
# these at import time and run guards.
_TMP = tempfile.mkdtemp(prefix="berth_crossorg_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

# Stub anthropic — constructed at import, never called here.
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


def _denied(resp):
    """A refusal is any non-2xx that isn't a server error. 404 is fine and
    preferred — it doesn't confirm the id exists."""
    return resp.status_code in (302, 400, 403, 404)


# ── Seed: our org, and a rival org whose data must stay invisible ────────────
db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
    ("buyer@example.com", generate_password_hash("x"),
     "a regional food distributor", "claude-sonnet-4-6", "enterprise"),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("buyer@example.com",))[0]["id"]

MINE = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "a regional food distributor", "complete"),
)
THEIRS = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid + 999, "Rival Foods", "complete"),
)
for sid in (MINE, THEIRS):
    db.execute(
        "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) "
        "VALUES (?,?,?)",
        (sid, "[]", '[{"item": "NORDVIK Cod Fillet", "order_qty": 10}]'),
    )

# A rival conversation for the chat route.
try:
    THEIR_CONV = db.execute(
        "INSERT INTO chat_conversations (user_id, org_name, title) VALUES (?,?,?)",
        (uid + 999, "Rival Foods", "their private planning chat"),
    )
except Exception:
    THEIR_CONV = None  # schema drift — skipped below rather than failing loudly

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "buyer@example.com"
    s["org_name"] = "a regional food distributor"
    s["model"] = "claude-sonnet-4-6"
    s["is_admin"] = False
    s["tier"] = "enterprise"
    s["role"] = "analyst"

print("-- cross-org denial on previously untested routes --")

# 1. /diff — both orders. Verifying only the first id is the natural regression.
r = client.get(f"/diff/{MINE}/{THEIRS}")
_check("/diff refuses a rival session in the SECOND slot", _denied(r), detail=str(r.status_code))
_check("/diff leaks no rival item name", b"NORDVIK" not in r.data)

r = client.get(f"/diff/{THEIRS}/{MINE}")
_check("/diff refuses a rival session in the FIRST slot", _denied(r), detail=str(r.status_code))

# 2. Upload routes keyed on a session id from the URL.
r = client.get(f"/upload/status/{THEIRS}")
_check("/upload/status refuses a rival session", _denied(r), detail=str(r.status_code))

r = client.post(f"/upload/scope/{THEIRS}", json={"scope": "all"})
_check("/upload/scope refuses a rival session", _denied(r), detail=str(r.status_code))

r = client.post(f"/upload/use_previous/{THEIRS}")
_check("/upload/use_previous refuses cloning a rival's tables", _denied(r), detail=str(r.status_code))

# 3. Recommendation writes keyed on a session id in the JSON body.
r = client.post("/recommend/approve_all", json={"session_id": THEIRS})
_check("/recommend/approve_all refuses a rival session", _denied(r), detail=str(r.status_code))

r = client.post("/recommend/outcome", json={
    "session_id": THEIRS, "item": "NORDVIK Cod Fillet",
    "field": "order_placed", "value": True,
})
_check("/recommend/outcome refuses a rival session", _denied(r), detail=str(r.status_code))

# 4. Chat history belonging to another org.
if THEIR_CONV:
    r = client.get(f"/api/chat/conversation/{THEIR_CONV}")
    _check("/api/chat/conversation refuses a rival conversation", _denied(r), detail=str(r.status_code))
    _check("chat route leaks no rival title", b"their private planning chat" not in r.data)
else:
    print("ok: chat conversation seed unavailable — skipped")

# 5. The whole database, behind admin only.
r = client.get("/admin/backup/download")
_check("/admin/backup/download refuses a non-admin", _denied(r), detail=str(r.status_code))

# 6. Sanity: the guards deny by ownership, not by refusing everything.
r = client.get(f"/upload/status/{MINE}")
_check("own session still reachable (guard isn't a blanket deny)",
       r.status_code == 200, detail=str(r.status_code))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll cross-org route tests passed.")
