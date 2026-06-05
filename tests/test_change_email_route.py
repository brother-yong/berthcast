"""End-to-end test for the admin 'change email' button.

Drives the real /admin route through Flask's test client, logged in as an
admin, and proves:
  1. an admin can change an account's email (and it gets normalised)
  2. you cannot change one account to an email another account already uses
  3. a malformed email is rejected and nothing is saved

Uses a throwaway temp DB and a stubbed anthropic client, so it touches no real
data and makes no API calls. CSRF is disabled for the test client only (CSRF is
still enforced in the real app). Run: python tests/test_change_email_route.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_change_email.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
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


def _email_of(uid):
    return db.query("SELECT email FROM users WHERE id=?", (uid,))[0]["email"]


# An admin already exists (created at import by _ensure_admin). Use it.
admin_id = db.query("SELECT id FROM users WHERE is_admin=1")[0]["id"]

# Two ordinary accounts: the one we created with a wrong email, and another.
db.execute("INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
           ("staff@coollink.com", generate_password_hash("x"), "Cool Link", "claude-sonnet-4-6"))
db.execute("INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
           ("other@coollink.com.sg", generate_password_hash("x"), "Cool Link", "claude-sonnet-4-6"))
userA = db.query("SELECT id FROM users WHERE email=?", ("staff@coollink.com",))[0]["id"]
userB = db.query("SELECT id FROM users WHERE email=?", ("other@coollink.com.sg",))[0]["id"]

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = admin_id
    s["email"]   = "admin@berthcast.com"
    s["org_name"] = "berthcast Admin"
    s["model"]   = "claude-sonnet-4-6"
    s["is_admin"] = True
    s["tier"]    = "enterprise"
    s["role"]    = "admin"

# 1. Happy path — fix the wrong email; it should also be trimmed + lowercased.
r = client.post("/admin", data={"action": "change_email", "user_id": str(userA),
                                "new_email": "  Staff.Real@CoolLink.Com.SG "})
_check("change_email returns 200", r.status_code == 200, detail=str(r.status_code))
_check("userA email updated and normalised",
       _email_of(userA) == "staff.real@coollink.com.sg", detail=_email_of(userA))

# 2. Safety — cannot take over another account's email.
r = client.post("/admin", data={"action": "change_email", "user_id": str(userA),
                                "new_email": "other@coollink.com.sg"})
_check("duplicate email request still returns 200", r.status_code == 200, detail=str(r.status_code))
_check("userA email unchanged after duplicate attempt",
       _email_of(userA) == "staff.real@coollink.com.sg", detail=_email_of(userA))
_check("userB email untouched", _email_of(userB) == "other@coollink.com.sg", detail=_email_of(userB))

# 3. Safety — malformed email is not saved.
r = client.post("/admin", data={"action": "change_email", "user_id": str(userA),
                                "new_email": "not-an-email"})
_check("malformed email not saved",
       _email_of(userA) == "staff.real@coollink.com.sg", detail=_email_of(userA))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll change-email route tests passed.")
