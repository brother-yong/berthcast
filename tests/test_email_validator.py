"""Tests for validators.validate_email_change — the safety check behind the
admin 'change email' button.

It must: reject blanks and malformed addresses, reject an email already used by
a DIFFERENT account (so one user can't be given another's address), allow
re-setting the same account's own email, and normalise (trim + lowercase).

The account-lookup is injected so this stays a pure function — no DB needed.
Run: python tests/test_email_validator.py  (exits non-zero on first failure).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import validators

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _free(_email):
    """Lookup stub: every email is unused."""
    return None


def _taken_by(owner_id):
    """Lookup stub: the email belongs to owner_id."""
    return lambda _email: owner_id


# Target account being edited is user id 7 throughout.
TARGET = 7

# --- rejections ---------------------------------------------------------------
email, err = validators.validate_email_change("", TARGET, _free)
_check("empty is rejected", email is None and err)

email, err = validators.validate_email_change("   ", TARGET, _free)
_check("whitespace-only is rejected", email is None and err)

email, err = validators.validate_email_change("not-an-email", TARGET, _free)
_check("missing @ is rejected", email is None and err)

email, err = validators.validate_email_change("a@b@c.com", TARGET, _free)
_check("two @ is rejected", email is None and err)

email, err = validators.validate_email_change("jo hn@coollink.com.sg", TARGET, _free)
_check("space inside is rejected", email is None and err)

email, err = validators.validate_email_change("john@localhost", TARGET, _free)
_check("domain with no dot is rejected", email is None and err)

email, err = validators.validate_email_change("john@.com", TARGET, _free)
_check("domain starting with dot is rejected", email is None and err)

email, err = validators.validate_email_change("john@coollink.", TARGET, _free)
_check("domain ending with dot is rejected", email is None and err)

# --- uniqueness ---------------------------------------------------------------
email, err = validators.validate_email_change("taken@coollink.com.sg", TARGET, _taken_by(9))
_check("email owned by another account is rejected", email is None and err,
       detail=f"{email!r}, {err!r}")

email, err = validators.validate_email_change("mine@coollink.com.sg", TARGET, _taken_by(TARGET))
_check("re-setting the account's own email is allowed",
       email == "mine@coollink.com.sg" and err is None, detail=f"{email!r}, {err!r}")

# --- success + normalisation --------------------------------------------------
email, err = validators.validate_email_change("john@coollink.com.sg", TARGET, _free)
_check("a valid, free email is accepted", email == "john@coollink.com.sg" and err is None,
       detail=f"{email!r}, {err!r}")

email, err = validators.validate_email_change("  John.Doe@CoolLink.Com.SG  ", TARGET, _free)
_check("email is trimmed and lowercased", email == "john.doe@coollink.com.sg" and err is None,
       detail=f"{email!r}, {err!r}")


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll email-validator tests passed.")
