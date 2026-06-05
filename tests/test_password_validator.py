"""Tests for validators.password_error — the rule behind the admin
'set a new password' button.

It must reject blank or too-short passwords (under 8 chars, matching the rest
of the app) and accept anything 8+ characters. Pure function, no DB.
Run: python tests/test_password_validator.py  (exits non-zero on first failure).
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


_check("empty password is rejected", validators.password_error("") is not None)
_check("None is rejected", validators.password_error(None) is not None)
_check("7 characters is rejected", validators.password_error("abcdefg") is not None)
_check("exactly 8 characters is accepted", validators.password_error("abcdefgh") is None)
_check("a long password is accepted", validators.password_error("a-perfectly-fine-password") is None)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll password-validator tests passed.")
