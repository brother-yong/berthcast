"""Server-side session revocation via users.session_version.

A signed session cookie carries the user's session_version stamped at login.
login_required re-reads the live version each request; if it no longer matches
(the user was removed, demoted, or had their password reset) the cookie stops
working immediately instead of staying valid for its 30-day life.

Covers the two findings the 30-day "remember me" cookie widened:
  HIGH   — a removed/demoted user keeps access until the cookie expires.
  MEDIUM — a password reset doesn't kill other live sessions.

Throwaway temp DB + stubbed anthropic; CSRF disabled for the test client only.
Run: python tests/test_session_revocation.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_session_revocation.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
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
flask_app = appmod.app

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _mkuser(email, password):
    return db.execute(
        "INSERT INTO users (email, password_hash, org_name, model, role) VALUES (?,?,?,?,?)",
        (email, generate_password_hash(password), "a regional food distributor",
         "claude-sonnet-4-6", "admin"))


def _login(email, password):
    c = flask_app.test_client()
    r = c.post("/login", data={"email": email, "password": password})
    return c, r


def _protected_ok(client):
    """True if the client can still reach a login-only page (not bounced to login)."""
    r = client.get("/guide", follow_redirects=False)
    return r.status_code == 200


def _bounced_to_login(client):
    r = client.get("/guide", follow_redirects=False)
    return r.status_code == 302 and "/login" in r.headers.get("Location", "")


# 1) Baseline: a fresh login can reach a protected page.
u1 = _mkuser("staff1@example.com.sg", "correct-horse-9")
c1, r1 = _login("staff1@example.com.sg", "correct-horse-9")
_check("fresh login redirects", r1.status_code == 302, detail=str(r1.status_code))
_check("fresh login reaches protected page", _protected_ok(c1))

# 2) HIGH: bump the version (as an admin remove/demote would) -> cookie dies.
db.execute("UPDATE users SET session_version = session_version + 1 WHERE id=?", (u1,))
_check("revoked session is bounced to login after a version bump", _bounced_to_login(c1))

# 3) Deleted user: the signed cookie stops working immediately.
u2 = _mkuser("staff2@example.com.sg", "correct-horse-9")
c2, _ = _login("staff2@example.com.sg", "correct-horse-9")
_check("deleted-user baseline works first", _protected_ok(c2))
db.execute("DELETE FROM users WHERE id=?", (u2,))
_check("deleted user's cookie is bounced to login", _bounced_to_login(c2))

# 4) Self password change keeps THIS device, kicks the OTHERS.
u3 = _mkuser("staff3@example.com.sg", "correct-horse-9")
c_here, _  = _login("staff3@example.com.sg", "correct-horse-9")
c_other, _ = _login("staff3@example.com.sg", "correct-horse-9")
_check("both devices start signed in",
       _protected_ok(c_here) and _protected_ok(c_other))
r = c_here.post("/settings", data={
    "action": "change_password",
    "current_password": "correct-horse-9",
    "new_password": "brand-new-secret-1",
    "confirm_password": "brand-new-secret-1",
})
_check("this device stays signed in after changing its own password", _protected_ok(c_here))
_check("other device is kicked after the password change", _bounced_to_login(c_other))

# 5) MEDIUM: the forgot-password reset flow kills an existing live session.
u4 = _mkuser("staff4@example.com.sg", "correct-horse-9")
c_live, _ = _login("staff4@example.com.sg", "correct-horse-9")
_check("reset baseline works first", _protected_ok(c_live))
raw_token = "reset-token-abc-123"
db.execute("INSERT INTO password_reset_tokens (user_id, token) VALUES (?,?)",
           (u4, appmod._hash_token(raw_token)))
c_anon = flask_app.test_client()  # a reset is done while logged OUT
c_anon.post(f"/reset-password/{raw_token}",
            data={"password": "reset-fresh-pass-1", "password2": "reset-fresh-pass-1"})
_check("password reset kills the previously live session", _bounced_to_login(c_live))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll session-revocation tests passed.")
