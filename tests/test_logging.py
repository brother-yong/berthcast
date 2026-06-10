"""Proof for the logging fix (audit finding #5).

Errors used to be swallowed with `except: pass` everywhere — most dangerously
around email, so a critical-stock alert could fail to reach a client and nobody
would know. Now failures log through logging_setup.logger.

Proves:
  1. setup_logging() is idempotent (importing from many modules won't stack
     duplicate handlers — which would multiply every log line).
  2. A message actually reaches the logger.
  3. A failed email send is logged (with subject + recipient) instead of vanishing.

Dependency-free:  python tests/test_logging.py
"""
import os
import sys
import logging
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="berth_log_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging_setup

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. Idempotent configuration ─────────────────────────────────────────────
before = len(logging_setup.logger.handlers)
logging_setup.setup_logging()
logging_setup.setup_logging()
after = len(logging_setup.logger.handlers)
_check("setup_logging adds no duplicate handlers when called again",
       after == before, detail=f"{before} -> {after}")
_check("logger has at least a stdout handler", before >= 1, detail=str(before))


# ── Capture handler for the remaining assertions ────────────────────────────
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


cap = _Capture()
logging.getLogger("berthcast").addHandler(cap)

# ── 2. A message reaches the logger ─────────────────────────────────────────
logging_setup.logger.warning("hello from the test")
_check("a logged message is captured",
       any("hello from the test" in r.getMessage() for r in cap.records))


# ── 3. A failed email send is logged, not swallowed ─────────────────────────
import emails

os.environ["MAIL_SENDER"] = "admin@berthcast.com"
os.environ["MAIL_APP_PASSWORD"] = "app-password"


def _boom(*a, **k):
    raise RuntimeError("smtp is down")


emails.smtplib.SMTP_SSL = _boom   # force the send to fail
cap.records.clear()
emails._send_reset_email("user@example.com", "https://berthcast.com/reset/abc")

logged = [r for r in cap.records
          if "Failed to send" in r.getMessage() and "user@example.com" in r.getMessage()]
_check("a failed email is logged with its recipient", bool(logged),
       detail=str([r.getMessage() for r in cap.records]))
_check("the failed email is logged at WARNING or higher",
       bool(logged) and logged[0].levelno >= logging.WARNING)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll logging tests passed.")
