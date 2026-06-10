"""Tests for two security hardening additions:

  1. A generic request throttle (rate_limit.hit) used on the public POST
     endpoints — sign-up, password reset, contact — to slow down abuse
     (email bombing, spam, mass fake sign-ups).

  2. Security response headers set on every response (X-Frame-Options,
     X-Content-Type-Options, Referrer-Policy, Content-Security-Policy), with
     HSTS gated to production only.

Dependency-free: run with `python tests/test_security_hardening.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys
import tempfile

# Throwaway DB + NOT on Render (so importing app doesn't trip the storage guard
# and HSTS stays off, which we assert below).
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "sec_hardening_test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
os.environ.setdefault("SECRET_KEY", "test-secret-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rate_limit
import app as app_module


_FAILED = False


def _check(name, cond):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        _FAILED = True


# ---------------------------------------------------------------------------
# 1. The generic throttle
# ---------------------------------------------------------------------------
def _test_throttle():
    t = 1000.0
    # limit=3 within a 100s window: first 3 allowed, 4th blocked.
    results = [rate_limit.hit("k1", 3, 100, now=t) for _ in range(4)]
    _check("first 3 requests allowed", results[:3] == [False, False, False])
    _check("4th request blocked", results[3] is True)

    # A different key is independent.
    _check("separate key unaffected", rate_limit.hit("k2", 3, 100, now=t) is False)

    # After the window passes, the count resets.
    _check("resets after the window", rate_limit.hit("k1", 3, 100, now=t + 101) is False)


# ---------------------------------------------------------------------------
# 2. Security headers
# ---------------------------------------------------------------------------
def _test_headers():
    client = app_module.app.test_client()
    resp = client.get("/")
    h = resp.headers

    _check("X-Frame-Options set", h.get("X-Frame-Options") == "SAMEORIGIN")
    _check("X-Content-Type-Options set", h.get("X-Content-Type-Options") == "nosniff")
    _check("Referrer-Policy set", h.get("Referrer-Policy") == "strict-origin-when-cross-origin")

    csp = h.get("Content-Security-Policy", "")
    _check("CSP present", "default-src 'self'" in csp)
    _check("CSP blocks external framing", "frame-ancestors 'self'" in csp)
    _check("CSP allows Google Fonts stylesheet", "https://fonts.googleapis.com" in csp)
    _check("CSP allows Google Fonts files", "https://fonts.gstatic.com" in csp)

    _check("Permissions-Policy denies unused browser features",
           "camera=()" in h.get("Permissions-Policy", ""))
    _check("Cross-Origin-Opener-Policy set",
           h.get("Cross-Origin-Opener-Policy") == "same-origin")

    # HSTS must be OFF when not on Render (we didn't set RENDER), so local
    # http development isn't pinned to https.
    _check("HSTS off outside production", "Strict-Transport-Security" not in h)


def main():
    _test_throttle()
    _test_headers()
    if _FAILED:
        print("\nSOME TESTS FAILED")
        sys.exit(1)
    print("\nAll security-hardening tests passed.")


if __name__ == "__main__":
    main()
