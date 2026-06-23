"""Proof for the operator-granted trial model.

Access is granted, never self-served. The operator stamps `users.trial_ends_at`
on an account; until that date the account has full access, after it the
money/value actions (analysis, upload, chat, export) soft-lock while past
results stay readable. A NULL date = a permanent account.

This drives the REAL routes (login, /analyse, /api/chat, /dashboard) so what we
prove is what runs in production:
  - login stamps the trial date into the session;
  - an EXPIRED trial is blocked from running analyses (page) and chat (JSON);
  - a FUTURE trial sails past the gate (hits ownership/404, not the trial 403);
  - the countdown banner shows the right state for active / expired / permanent.

Throwaway DB + stubbed anthropic; no network. Run: python tests/test_trial_gate.py
"""
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_trialgate.db")
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


PW = "trial-password-1"
today = datetime.utcnow().date()
EXPIRED = (today - timedelta(days=1)).isoformat()
FUTURE = (today + timedelta(days=10)).isoformat()
FUTURE_DMY = (today + timedelta(days=10)).strftime("%d/%m/%Y")
EXPIRED_DMY = (today - timedelta(days=1)).strftime("%d/%m/%Y")


def _make(email, org, trial):
    db.execute(
        "INSERT INTO users (email, password_hash, org_name, model, email_verified, tier, role, trial_ends_at) "
        "VALUES (?,?,?,?,1,'enterprise','admin',?)",
        (email, generate_password_hash(PW), org, "claude-sonnet-4-6", trial),
    )


_make("expired@t.example", "ExpiredOrg", EXPIRED)
_make("future@t.example", "FutureOrg", FUTURE)
_make("perm@t.example", "PermOrg", None)


def _login(email):
    c = appmod.app.test_client()
    r = c.post("/login", data={"email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 302, f"login failed for {email}: {r.status_code}"
    return c


# ── 1. Login stamps the trial date into the session ──────────────────────────
c_exp = _login("expired@t.example")
with c_exp.session_transaction() as s:
    _check("login stamps trial_ends_at into session", s.get("trial_ends_at") == EXPIRED,
           detail=str(s.get("trial_ends_at")))

# ── 2. Expired trial: running an analysis (page route) is blocked ────────────
r = c_exp.get("/analyse/999999", follow_redirects=True)
body = r.get_data(as_text=True)
_check("expired trial blocked from /analyse (redirected to dashboard)",
       "trial has ended" in body.lower())

# ── 3. Expired trial: chat (JSON route) returns 403 with a clear message ─────
r = c_exp.post("/api/chat", json={"message": "hello"})
j = r.get_json() or {}
_check("expired trial -> /api/chat 403", r.status_code == 403, detail=str(r.status_code))
_check("expired trial -> chat ok:false + message",
       j.get("ok") is False and "trial has ended" in j.get("error", "").lower(), detail=str(j))

# ── 4. Expired trial can STILL read past results (dashboard) — soft lock ──────
r = c_exp.get("/dashboard")
_check("expired trial can still load the dashboard (read-only access)", r.status_code == 200,
       detail=str(r.status_code))
_check("expired dashboard shows the 'trial ended' banner",
       f"Your free trial ended on {EXPIRED_DMY}" in r.get_data(as_text=True))

# ── 5. Future trial: NOT blocked by the gate (passes through to ownership/404)
c_fut = _login("future@t.example")
r = c_fut.get("/analyse/999999", follow_redirects=False)
_check("future trial passes the gate (404 ownership, not a trial redirect)",
       r.status_code == 404, detail=str(r.status_code))

# ── 6. Future trial: countdown banner shows the date + days left ─────────────
r = c_fut.get("/dashboard")
body = r.get_data(as_text=True)
_check("future trial shows countdown banner with the date",
       f"Your free trial ends on {FUTURE_DMY}" in body)
_check("future trial banner shows days left", "10 days left" in body, detail="expected '10 days left'")

# ── 7. Permanent account: no banner, full access ─────────────────────────────
c_perm = _login("perm@t.example")
r = c_perm.get("/dashboard")
body = r.get_data(as_text=True)
_check("permanent account shows NO trial banner", "Your free trial" not in body)
r = c_perm.get("/analyse/999999", follow_redirects=False)
_check("permanent account passes the gate (404 ownership, not blocked)", r.status_code == 404,
       detail=str(r.status_code))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll trial-gate tests passed.")
