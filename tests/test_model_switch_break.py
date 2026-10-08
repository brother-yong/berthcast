"""Switching an account's model on /admin must reach that account's live session,
and /admin must only store a model the app can drive.

The 8 Oct 2026 update leaves existing accounts on their stored model (shown on
/admin as "<id> (old)") and expects the operator to move them with the per-user
dropdown. That dropdown posts action=change_model, which updates users.model.
Chat, the duplicate check, analysis and invites read the model from the login
cookie (session["model"]), so before the fix a user who was already signed in
kept running the old model until they signed out (up to 30 days with "keep me
signed in"), while /admin showed the new one. The fix refreshes the cookie copy
in login_required, which already reads the account row on every request.

change_model also stored whatever string was posted: a typo or a model the app
can't drive (thinking rules differ per model) made every call 400, and a
missing field 500'd. Only config.AVAILABLE_MODELS is accepted now, plus the
account's current stored value so an "(old)" option can still be kept.

Invented brands only. Run: python tests/test_model_switch_break.py
"""
import os
import sys
import tempfile
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_model_switch_break_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
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

import database as db                                   # noqa: E402
import rate_limit                                       # noqa: E402
import app as appmod                                    # noqa: E402
from config import AVAILABLE_MODELS                     # noqa: E402
from flask import message_flashed                       # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
appmod._send_invite_email = lambda *a, **k: None

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


class _FakeStream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(["Order 20 cases."])

    def get_final_message(self):
        return SimpleNamespace(usage=None)


class _FakeAnthropic:
    calls = []

    def __init__(self, **kw):
        pass

    @property
    def messages(self):
        class _M:
            def stream(self, **kw):
                _FakeAnthropic.calls.append(kw)
                return _FakeStream()

            def create(self, **kw):
                return SimpleNamespace(model=kw.get("model"), usage=None,
                                       content=[SimpleNamespace(type="text", text="NORDVIK chat")])
        return _M()


appmod._anthropic.Anthropic = _FakeAnthropic

# OLD is a model no longer offered (an "(old)" row on /admin); NEW is the first
# offered one. Read from config so the test holds whichever list is current.
OLD = "claude-sonnet-4-6"
NEW = AVAILABLE_MODELS[0][0]
assert OLD not in [m for m, _ in AVAILABLE_MODELS]

ORG = "NORDVIK Trading"
uid = db.execute("INSERT INTO users (email, password_hash, org_name, model, tier, email_verified) "
                 "VALUES (?,?,?,?,?,1)",
                 ("buyer@nordvik.test", generate_password_hash("rye-flour-25kg"), ORG, OLD,
                  "enterprise"))

# The pilot user signs in through the real login form, so the cookie carries
# exactly what login puts there.
user = appmod.app.test_client()
r = user.post("/login", data={"email": "buyer@nordvik.test", "password": "rye-flour-25kg"})
_check("setup: user signed in", r.status_code in (302, 303), detail=str(r.status_code))

admin_id = db.query("SELECT id FROM users WHERE is_admin=1")[0]["id"]
admin = appmod.app.test_client()
with admin.session_transaction() as s:
    s.update(user_id=admin_id, email="admin@berthcast.com", org_name="berthcast Admin",
             model=NEW, is_admin=True, tier="enterprise", role="admin")


def _stored():
    return db.query("SELECT model FROM users WHERE id=?", (uid,))[0]["model"]


_flashed = []
message_flashed.connect(lambda sender, message, category, **k: _flashed.append(category), weak=False)


def _change(model=None, user_id=None):
    """POST change_model; returns (status, flash categories)."""
    data = {"action": "change_model", "user_id": str(uid if user_id is None else user_id)}
    if model is not None:
        data["model"] = model
    _flashed.clear()
    try:
        resp = admin.post("/admin", data=data)
    except Exception:  # TESTING re-raises; in production this is a 500
        return 500, list(_flashed)
    return resp.status_code, list(_flashed)


# ── 1. Live effect: the switch reaches the already signed-in user ─────────────
status, cats = _change(NEW)
_check("switch to an offered model is stored", _stored() == NEW, detail=_stored())
_check("switch to an offered model flashes success", cats == ["success"], detail=str(cats))

rate_limit._hits.clear()
_FakeAnthropic.calls = []
r = user.post("/api/chat", json={"message": "NORDVIK rye flour cover?"})
r.get_data(as_text=True)
r.close()
_check("signed-in user is NOT signed out by the switch", r.status_code == 200, detail=str(r.status_code))
used = _FakeAnthropic.calls[0]["model"] if _FakeAnthropic.calls else None
_check("signed-in user's chat runs on the model the operator switched to",
       used == NEW, detail=f"chat ran on {used!r}, /admin shows {_stored()!r}")

# An invite copies the inviter's model: it must be the switched one, not the cookie's old one.
with user.session_transaction() as s:
    s["role"] = "admin"
r = user.post("/settings", data={"action": "invite_user", "invite_email": "picker@nordvik.test",
                                 "invite_role": "reviewer"})
inv = db.query("SELECT model FROM users WHERE email=?", ("picker@nordvik.test",))
_check("invite after the switch copies the new model",
       bool(inv) and inv[0]["model"] == NEW, detail=str(inv[0]["model"] if inv else None))

# ── 2. Validation: only offered models (or the current stored value) ──────────
for label, bad in (("model not on the offered list", "claude-nordvik-9"),
                   ("typo", "claude-sonet-5-5"),
                   ("blank", "")):
    status, cats = _change(bad)
    _check(f"{label} rejected without a 500", status != 500 and cats == ["error"],
           detail=f"{status} {cats}")
    _check(f"{label} not stored", _stored() == NEW, detail=_stored())

status, cats = _change(None)
_check("missing model field rejected without a 500", status != 500 and cats == ["error"],
       detail=f"{status} {cats}")
_check("missing model field not stored", _stored() == NEW, detail=_stored())

status, cats = _change(NEW, user_id=999999)
_check("unknown account rejected without a 500", status != 500 and cats == ["error"],
       detail=f"{status} {cats}")

status, cats = _change(NEW, user_id="abc")
_check("non-numeric account id rejected without a 500", status != 500 and cats == ["error"],
       detail=f"{status} {cats}")

# Keeping an "(old)" model the account already runs is allowed (no forced move).
db.execute("UPDATE users SET model=? WHERE id=?", (OLD, uid))
status, cats = _change(OLD)
_check("keeping the current (old) model is accepted", cats == ["success"], detail=str(cats))
_check("keeping the current (old) model leaves it stored", _stored() == OLD, detail=_stored())

# But an old model is not a free pass for a different account.
other = db.execute("INSERT INTO users (email, password_hash, org_name, model, tier, email_verified) "
                   "VALUES (?,?,?,?,?,1)",
                   ("clerk@brookvale.test", generate_password_hash("x"), "BROOKVALE Foods", NEW,
                    "enterprise"))
status, cats = _change(OLD, user_id=other)
_check("another account's old model can't be copied onto this one", cats == ["error"], detail=str(cats))
_check("other account keeps its model",
       db.query("SELECT model FROM users WHERE id=?", (other,))[0]["model"] == NEW)

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll model-switch break tests passed.")
