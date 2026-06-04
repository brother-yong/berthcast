"""Proves the Gmail "send-as" separation in emails.py.

The point of the change: log in to Gmail with the REAL account
(MAIL_USERNAME, which owns the app password) but make the email show up as
coming from MAIL_SENDER (e.g. admin@berthcast.com, a verified alias).

These tests fake the SMTP connection, so nothing is actually sent and no
network or real password is needed. They check three things:
  1. login uses MAIL_USERNAME, while the From header shows MAIL_SENDER
  2. old setups still work: with no MAIL_USERNAME, login falls back to MAIL_SENDER
  3. with nothing configured, no email is attempted at all

Dependency-free: run with `python tests/test_email_send_as.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys
import tempfile
import email as email_parser

# Point the DB at a throwaway file and pretend we're not on Render, so importing
# emails -> database doesn't create anything in the repo or trip the storage guard.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "send_as_test.db"))
os.environ.pop("RENDER", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import emails


class _FakeSMTP:
    """Stand-in for smtplib.SMTP_SSL that records calls instead of using a network."""
    last = None  # most recent instance that actually sent, for the test to inspect

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.login_args = None
        self.sendmail_args = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, from_addr, to_addr, message):
        self.sendmail_args = (from_addr, to_addr, message)
        _FakeSMTP.last = self


def _run(fn, *args, env):
    """Call an emails.* function with a clean env + faked SMTP; return the fake
    instance that sent (or None if nothing was sent). Restores env + SMTP after."""
    keys = ("MAIL_USERNAME", "MAIL_SENDER", "MAIL_APP_PASSWORD", "MAIL_RECIPIENT")
    saved_env = {k: os.environ.get(k) for k in keys}
    saved_smtp = emails.smtplib.SMTP_SSL
    _FakeSMTP.last = None
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(env)
        emails.smtplib.SMTP_SSL = _FakeSMTP
        fn(*args)
        return _FakeSMTP.last
    finally:
        emails.smtplib.SMTP_SSL = saved_smtp
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# 1. The real change: login as the Gmail account, From shows the alias.
fake = _run(
    emails._send_verification_email,
    "customer@example.com", "https://berthcast.com/verify/abc",
    env={
        "MAIL_USERNAME": "tanyonghan41@gmail.com",
        "MAIL_SENDER": "admin@berthcast.com",
        "MAIL_APP_PASSWORD": "app-pw-1234",
    },
)
_check("an email was attempted", fake is not None)
if fake is not None:
    _check("logs in with the real Gmail account, not the alias",
           fake.login_args == ("tanyonghan41@gmail.com", "app-pw-1234"),
           detail=str(fake.login_args))
    from_addr, to_addr, raw = fake.sendmail_args
    parsed = email_parser.message_from_string(raw)
    _check("From header shown to recipients is admin@berthcast.com",
           parsed["From"] == "admin@berthcast.com", detail=str(parsed["From"]))
    _check("email goes to the intended recipient",
           to_addr == "customer@example.com", detail=str(to_addr))

# 2. Backwards compatible: only MAIL_SENDER set (an old real-gmail config) ->
#    login falls back to it, so existing deployments keep working unchanged.
fake = _run(
    emails._send_verification_email,
    "customer@example.com", "https://berthcast.com/verify/abc",
    env={
        "MAIL_SENDER": "tanyonghan41@gmail.com",
        "MAIL_APP_PASSWORD": "app-pw-1234",
    },
)
_check("old config still sends", fake is not None)
if fake is not None:
    _check("with no MAIL_USERNAME, login falls back to MAIL_SENDER",
           fake.login_args == ("tanyonghan41@gmail.com", "app-pw-1234"),
           detail=str(fake.login_args))

# 3. The separation also holds for a different sender (contact form).
fake = _run(
    emails._send_contact_email,
    "Jane Buyer", "jane@cust.com", "Acme Foods", "Do you handle frozen goods?",
    env={
        "MAIL_USERNAME": "tanyonghan41@gmail.com",
        "MAIL_SENDER": "admin@berthcast.com",
        "MAIL_APP_PASSWORD": "app-pw-1234",
        "MAIL_RECIPIENT": "tanyonghan41@gmail.com",
    },
)
_check("contact form email was attempted", fake is not None)
if fake is not None:
    _check("contact form logs in with the Gmail account",
           fake.login_args == ("tanyonghan41@gmail.com", "app-pw-1234"),
           detail=str(fake.login_args))
    parsed = email_parser.message_from_string(fake.sendmail_args[2])
    _check("contact form From header is admin@berthcast.com",
           parsed["From"] == "admin@berthcast.com", detail=str(parsed["From"]))

# 4. Nothing configured -> nothing sent (so we never half-send broken mail).
fake = _run(
    emails._send_verification_email,
    "customer@example.com", "https://berthcast.com/verify/abc",
    env={},
)
_check("sends nothing when unconfigured", fake is None)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll email send-as tests passed.")
