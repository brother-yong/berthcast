"""End-to-end test for the admin 'set a new password' button.

Drives the real /admin route through Flask as an admin and proves:
  1. an admin can set a new password — the new one works, the old one stops
  2. a too-short password is rejected and the password is left unchanged

Throwaway temp DB + stubbed anthropic; no real data, no API calls. CSRF is
disabled for the test client only. Run: python tests/test_reset_password_route.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_reset_pw.db")
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

import database as db                                                     # noqa: E402
import app as appmod                                                      # noqa: E402
from werkzeug.security import generate_password_hash, check_password_hash # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
flask_app = appmod.app

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _hash_of(uid):
    return db.query("SELECT password_hash FROM users WHERE id=?", (uid,))[0]["password_hash"]


admin_id = db.query("SELECT id FROM users WHERE is_admin=1")[0]["id"]

db.execute("INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
           ("staff@example.com.sg", generate_password_hash("old-password-1"), "a regional food distributor", "claude-sonnet-4-6"))
user = db.query("SELECT id FROM users WHERE email=?", ("staff@example.com.sg",))[0]["id"]

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = admin_id
    s["email"]   = "admin@berthcast.com"
    s["org_name"] = "berthcast Admin"
    s["model"]   = "claude-sonnet-4-6"
    s["is_admin"] = True
    s["tier"]    = "enterprise"
    s["role"]    = "admin"

# 1. Happy path — set a new password.
r = client.post("/admin", data={"action": "set_password", "user_id": str(user),
                                "new_password": "new-strong-password-9"})
_check("set_password returns 200", r.status_code == 200, detail=str(r.status_code))
_check("the new password now works", check_password_hash(_hash_of(user), "new-strong-password-9"))
_check("the old password no longer works", not check_password_hash(_hash_of(user), "old-password-1"))

# 2. Safety — a too-short password is rejected and nothing changes.
hash_before = _hash_of(user)
r = client.post("/admin", data={"action": "set_password", "user_id": str(user),
                                "new_password": "short"})
_check("short password request still returns 200", r.status_code == 200, detail=str(r.status_code))
_check("password unchanged after a too-short attempt", _hash_of(user) == hash_before)
_check("the good password still works", check_password_hash(_hash_of(user), "new-strong-password-9"))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll reset-password route tests passed.")
