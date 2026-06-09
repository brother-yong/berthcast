"""Proof for the multi-tenancy registration guard.

The org name is the tenant boundary. Before this fix, a stranger could
self-register with the exact org name of an existing customer and land inside
their data. Real colleagues join an org through the admin invite flow, not
self-registration — so a self-registration whose org name already exists must
be refused.

This drives the real /register route (CSRF disabled for the test) and checks
the database to confirm no piggyback account was created.

Dependency-free: run with `python tests/test_org_registration_isolation.py`.
"""
import os
import sys
import tempfile

# Throwaway DB, not on Render, mail intentionally UNconfigured (so registration
# doesn't spawn email threads and auto-verifies instead).
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="berth_org_"), "test.db")
os.environ.pop("RENDER", None)
os.environ.pop("MAIL_SENDER", None)
os.environ.pop("MAIL_APP_PASSWORD", None)
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


def _register(org, email):
    """Fresh client each time so we're not carrying a logged-in session."""
    return app_module.app.test_client().post(
        "/register",
        data={"org_name": org, "email": email,
              "password": "password123", "password2": "password123",
              "accept_terms": "on"},
        follow_redirects=True,
    )


def main():
    # 1. First company registers cleanly.
    _register("Acme Trading", "founder@acme.example")
    _check("first registration created the account",
           len(db.query("SELECT id FROM users WHERE email=?", ("founder@acme.example",))) == 1)

    # 2. A stranger tries the SAME org name (different case + spacing) — must be refused.
    resp = _register("  acme trading  ", "intruder@evil.example")
    _check("piggyback registration created NO account",
           len(db.query("SELECT id FROM users WHERE email=?", ("intruder@evil.example",))) == 0)
    _check("stranger is shown the 'already registered' message",
           "already registered" in resp.get_data(as_text=True).lower())

    # 3. A genuinely different org name still works.
    _register("Different Co", "owner@different.example")
    _check("a new, distinct org name still registers",
           len(db.query("SELECT id FROM users WHERE email=?", ("owner@different.example",))) == 1)

    # 4. Only one account exists for the Acme org name — no second tenant slipped in.
    acme_accounts = db.query(
        "SELECT COUNT(*) AS c FROM users WHERE LOWER(TRIM(org_name)) = 'acme trading'"
    )[0]["c"]
    _check("exactly one account holds the Acme org name", acme_accounts == 1)

    if _FAILED:
        print("\nSOME TESTS FAILED")
        sys.exit(1)
    print("\nAll org-registration isolation tests passed.")


if __name__ == "__main__":
    main()
