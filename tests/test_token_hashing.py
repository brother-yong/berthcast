"""Proof for the token-hardening + per-account lockout changes.

1. Email-verification and password-reset tokens are stored as SHA-256 hashes:
   the raw token exists only inside the emailed link, so a leaked database
   cannot be replayed as working reset/verify links. The reset flow still works
   end-to-end (forgot -> reset -> login). Public sign-up + email verification are
   retired (access is operator-granted), so those paths are no longer exercised.
2. Login is throttled per ACCOUNT as well as per IP: five failures against one
   mailbox from five different IPs still lock the account.
3. The new public endpoints exist: /robots.txt, /.well-known/security.txt,
   and a friendly 404 page.

Throwaway DB + stubbed anthropic + captured emails; no network, no real mail.
Run: python tests/test_token_hashing.py
"""
import os
import sys
import time
import hashlib
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_tokens.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
# Mail env present but harmless — outbound mail is captured below, never sent.
os.environ["MAIL_SENDER"] = "admin@berthcast.com"
os.environ["MAIL_APP_PASSWORD"] = "not-a-real-password"

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db          # noqa: E402
import rate_limit              # noqa: E402
import app as appmod          # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True

# Capture outbound mail instead of sending it. The routes resolve these names
# from app-module globals at call time, so patching here intercepts the threads.
_sent = {"verify": [], "reset": []}
appmod._send_verification_email = lambda email, url: _sent["verify"].append(url)
appmod._send_reset_email = lambda email, url: _sent["reset"].append(url)

client = appmod.app.test_client()
_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _wait_for(bucket, n, timeout=3.0):
    """The capture happens on a background thread — wait briefly for it."""
    t0 = time.time()
    while len(_sent[bucket]) < n and time.time() - t0 < timeout:
        time.sleep(0.05)
    return len(_sent[bucket]) >= n


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


EMAIL = "ops@example-distributor.com"
PW1, PW2 = "first-password-9", "second-password-7"

# ── 1. Operator-provisioned account (public sign-up + verification retired) ──
db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, email_verified, tier) "
    "VALUES (?,?,?,?,1,'enterprise')",
    (EMAIL, generate_password_hash(PW1), "Example Distributor", "claude-sonnet-4-6"),
)
_check("account provisioned directly (no self-serve sign-up)",
       len(db.query("SELECT id FROM users WHERE email=?", (EMAIL,))) == 1)

# ── 3. Forgot password: stored reset token is the hash ───────────────────────
r = client.post("/forgot-password", data={"email": EMAIL})
_check("forgot-password email captured", _wait_for("reset", 1))
reset_url = _sent["reset"][0]
raw_reset = reset_url.rstrip("/").split("/")[-1]
rrow = db.query("SELECT token FROM password_reset_tokens")[0]
_check("DB does NOT hold the raw reset token", rrow["token"] != raw_reset)
_check("DB holds sha256(raw reset token)", rrow["token"] == _sha(raw_reset))

# ── 4. The emailed reset link still works; token is single-use ───────────────
r = client.post("/reset-password/" + raw_reset,
                data={"password": PW2, "password2": PW2}, follow_redirects=False)
_check("reset accepts the new password (redirects to login)", r.status_code == 302)
_check("reset token deleted after use",
       len(db.query("SELECT id FROM password_reset_tokens")) == 0)

r = client.post("/login", data={"email": EMAIL, "password": PW2}, follow_redirects=False)
_check("login works with the new password", r.status_code == 302)
client.get("/logout")

# ── 5. Per-account lockout survives IP rotation ──────────────────────────────
for i in range(5):
    client.post("/login", data={"email": EMAIL, "password": "wrong-guess"},
                headers={"X-Forwarded-For": f"10.0.0.{i+1}"})
r = client.post("/login", data={"email": EMAIL, "password": PW2},
                headers={"X-Forwarded-For": "10.0.0.99"})
_check("correct password from a fresh IP is still locked out",
       b"Too many failed sign-in attempts" in r.data)
rate_limit.clear(f"acct:{EMAIL}")
r = client.post("/login", data={"email": EMAIL, "password": PW2},
                headers={"X-Forwarded-For": "10.0.0.100"}, follow_redirects=False)
_check("after the lock clears, the account works again", r.status_code == 302)

# ── 6. New public endpoints ──────────────────────────────────────────────────
r = client.get("/robots.txt")
_check("robots.txt serves and excludes /admin",
       r.status_code == 200 and b"Disallow: /admin" in r.data)
r = client.get("/.well-known/security.txt")
_check("security.txt serves with a contact",
       r.status_code == 200 and b"Contact: mailto:admin@berthcast.com" in r.data)
r = client.get("/this-page-does-not-exist")
_check("custom 404 page renders", r.status_code == 404 and b"Page not found" in r.data)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll token-hashing and lockout tests passed.")
