"""Proof that public self-service sign-up is disabled.

The org name is the tenant boundary. Self-registration used to let a stranger
create an account (and risk landing on an existing org's name). As of the
pricing/trial rework, **access is granted by the operator, never self-served**:
there is no public sign-up at all. `/register` is retired — it now just
redirects to the contact page and creates nothing.

This drives the real /register route (CSRF disabled for the test) and confirms
the database stays empty of self-registered accounts.

Dependency-free: run with `python tests/test_org_registration_isolation.py`.
"""
import os
import sys
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="berth_org_"), "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
os.environ.setdefault("SECRET_KEY", "test-secret-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import app as app_module

db.init_db()
app_module.app.config["WTF_CSRF_ENABLED"] = False
app_module.app.config["TESTING"] = True

_FAILED = False


def _check(name, cond):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        _FAILED = True


def main():
    client = app_module.app.test_client()

    # 1. GET /register no longer serves a sign-up page — it redirects to contact.
    r = client.get("/register", follow_redirects=False)
    _check("GET /register redirects (no sign-up page)", r.status_code == 302)
    _check("GET /register points to the contact page",
           "/contact" in r.headers.get("Location", ""))

    # 2. POST /register creates NO account and just redirects.
    r = client.post(
        "/register",
        data={"org_name": "Acme Trading", "email": "intruder@evil.example",
              "password": "password123", "password2": "password123",
              "accept_terms": "on"},
        follow_redirects=False,
    )
    _check("POST /register redirects (no self-serve account)", r.status_code == 302)
    _check("POST /register created NO account",
           len(db.query("SELECT id FROM users WHERE email=?", ("intruder@evil.example",))) == 0)

    if _FAILED:
        print("\nSOME TESTS FAILED")
        sys.exit(1)
    print("\nAll public-signup-disabled tests passed.")


if __name__ == "__main__":
    main()
