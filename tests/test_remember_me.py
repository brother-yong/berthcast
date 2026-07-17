"""'Keep me signed in' on the login page.

Ticked  -> permanent session cookie, ~30-day expiry survives browser restart.
Unticked -> plain browser-session cookie (today's behaviour, no Expires).
Logout  -> session gone either way.

Throwaway temp DB + stubbed anthropic; CSRF disabled for the test client only.
Run: python tests/test_remember_me.py
"""
import os
import sys
import tempfile
import types
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_remember_me.db")
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


db.execute("INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
           ("staff@example.com.sg", generate_password_hash("correct-horse-9"),
            "a regional food distributor", "claude-sonnet-4-6"))


def _session_cookie(resp):
    """The Set-Cookie line for the Flask session, or None."""
    for c in resp.headers.getlist("Set-Cookie"):
        if c.startswith("session="):
            return c
    return None


# 1) remember ticked -> permanent cookie with ~30-day expiry
c1 = flask_app.test_client()
r = c1.post("/login", data={"email": "staff@example.com.sg",
                            "password": "correct-horse-9", "remember": "on"})
_check("remembered login redirects", r.status_code == 302, detail=str(r.status_code))
ck = _session_cookie(r)
_check("session cookie set", ck is not None)
_check("remembered cookie has an expiry", ck is not None and "Expires=" in ck, detail=str(ck))
if ck and "Expires=" in ck:
    raw = [p for p in ck.split("; ") if p.startswith("Expires=")][0][len("Expires="):]
    exp = parsedate_to_datetime(raw)
    days = (exp - datetime.now(timezone.utc)).days
    _check("expiry is ~30 days out", 28 <= days <= 31, detail=f"{days} days")

# 2) remember NOT ticked -> browser-session cookie, no expiry
c2 = flask_app.test_client()
r = c2.post("/login", data={"email": "staff@example.com.sg",
                            "password": "correct-horse-9"})
_check("plain login redirects", r.status_code == 302, detail=str(r.status_code))
ck = _session_cookie(r)
_check("plain cookie has NO expiry", ck is not None and "Expires=" not in ck and "Max-Age=" not in ck,
       detail=str(ck))

# 3) logout after a remembered login -> protected page bounces to login
r = c1.get("/logout", follow_redirects=False)
_check("logout redirects", r.status_code == 302, detail=str(r.status_code))
r = c1.get("/dashboard", follow_redirects=False)
_check("protected page redirects to login after logout",
       r.status_code == 302 and "/login" in r.headers.get("Location", ""),
       detail=f"{r.status_code} -> {r.headers.get('Location')}")

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll remember-me tests passed.")
