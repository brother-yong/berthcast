"""New invites inherit the inviter's live trial date, never a stale cookie."""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="berthcast_invite_"), "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
os.environ.setdefault("SECRET_KEY", "test-secret-not-used")
stub = types.ModuleType("anthropic")
stub.Anthropic = lambda *a, **k: None
stub.AnthropicError = Exception
sys.modules["anthropic"] = stub

import database as db
import app as appmod

appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
# Keep the real route and DB; suppress the external email only.
appmod._send_invite_email = lambda *a, **k: None
_FAILED = False


def _check(name, cond):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        _FAILED = True


def _admin(label, trial):
    email = f"{label}@brookvale.example"
    uid = db.execute(
        "INSERT INTO users (email, password_hash, org_name, model, role, trial_ends_at) "
        "VALUES (?,?,?,?,?,?)",
        (email, "unused", "BROOKVALE", "claude-sonnet-5", "admin", trial))
    client = appmod.app.test_client()
    with client.session_transaction() as s:
        s.update(user_id=uid, email=email, org_name="BROOKVALE", model="claude-sonnet-5",
                 tier="enterprise", role="admin", sv=0, trial_ends_at=trial)
    return uid, client


for label, login_date, live_date in (
    ("trial", "2099-01-31", "2099-01-31"),
    ("permanent", None, None),
    ("shortened", "2099-01-31", "2020-01-31"),
    ("converted", "2099-01-31", None),
):
    uid, client = _admin(label, login_date)
    # This changes only the DB, deliberately leaving the session copy stale.
    db.execute("UPDATE users SET trial_ends_at=? WHERE id=?", (live_date, uid))
    invite_email = f"{label}-invite@nordvik.example"
    response = client.post("/settings", data={
        "action": "invite_user", "invite_email": invite_email, "invite_role": "reviewer"})
    rows = db.query("SELECT trial_ends_at, org_name, role FROM users WHERE email=?", (invite_email,))
    _check(f"{label}: invite creates an account", response.status_code == 302 and len(rows) == 1)
    _check(f"{label}: inherits the current DB trial date",
           len(rows) == 1 and rows[0]["trial_ends_at"] == live_date)
    _check(f"{label}: org and role preserved",
           len(rows) == 1 and rows[0]["org_name"] == "BROOKVALE" and rows[0]["role"] == "reviewer")

# An unrelated existing permanent colleague must not be backfilled.
_check("existing permanent invite stays permanent", db.query(
    "SELECT trial_ends_at FROM users WHERE email=?", ("permanent-invite@nordvik.example",)
)[0]["trial_ends_at"] is None)

uid, client = _admin("revoked", "2099-01-31")
db.bump_session_version(uid)
response = client.post("/settings", data={
    "action": "invite_user", "invite_email": "blocked@nordvik.example"})
_check("revoked session still cannot invite", response.status_code == 302 and not db.query(
    "SELECT id FROM users WHERE email=?", ("blocked@nordvik.example",)))

sys.exit(1 if _FAILED else 0)
