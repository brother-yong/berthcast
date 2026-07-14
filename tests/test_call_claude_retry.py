"""_call_claude retries transient API failures instead of dying on the first.

14 Jul 2026: a real client run lost its whole recommendation step to ONE
'overloaded_error' (HTTP 529) — the API said "busy, try again" and berthcast
gave up. Locks:
  - overloaded/429/5xx and connection errors are retried with a pause
  - a non-transient error (e.g. 401 auth) raises immediately, no retry
  - retries exhausted -> the last error raises (callers keep their handling)

Run: python tests/test_call_claude_retry.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

import agents.shared as shared  # noqa: E402

F = []


def _check(c, m):
    if not c:
        F.append(m)


class _FakeStream:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_text(self):
        return self._text


class _Overloaded(Exception):
    status_code = 529

    def __str__(self):
        return "overloaded_error: Overloaded"


class _AuthError(Exception):
    status_code = 401

    def __str__(self):
        return "authentication_error"


class _FakeMessages:
    def __init__(self, failures, exc):
        self.calls = 0
        self._failures = failures
        self._exc = exc

    def stream(self, **kw):
        self.calls += 1
        if self.calls <= self._failures:
            raise self._exc()
        return _FakeStream("ok-after-retry")


class _FakeClient:
    def __init__(self, failures, exc):
        self.messages = _FakeMessages(failures, exc)

    def with_options(self, **kw):
        return self


_sleeps = []
shared._RETRY_SLEEP = lambda s: _sleeps.append(s)  # no real waiting in tests

# 1) two overloads then success -> retried and returned
_orig = shared.client
shared.client = _FakeClient(failures=2, exc=_Overloaded)
try:
    out = shared._call_claude("m", "sys", "user")
    _check(out == "ok-after-retry", f"should succeed after retries, got {out!r}")
    _check(shared.client.messages.calls == 3, f"expected 3 attempts, got {shared.client.messages.calls}")
    _check(len(_sleeps) == 2, f"expected 2 pauses, got {_sleeps}")
finally:
    shared.client = _orig

# 2) auth error -> immediate raise, exactly 1 attempt
_sleeps.clear()
shared.client = _FakeClient(failures=99, exc=_AuthError)
try:
    try:
        shared._call_claude("m", "sys", "user")
        _check(False, "auth error must raise")
    except _AuthError:
        pass
    _check(shared.client.messages.calls == 1, f"auth error must not retry, got {shared.client.messages.calls} attempts")
    _check(_sleeps == [], "no pause on non-transient error")
finally:
    shared.client = _orig

# 3) permanent overload -> retries exhausted, last error raises
_sleeps.clear()
shared.client = _FakeClient(failures=99, exc=_Overloaded)
try:
    try:
        shared._call_claude("m", "sys", "user")
        _check(False, "exhausted retries must raise")
    except _Overloaded:
        pass
    _check(shared.client.messages.calls == 3, f"expected 3 attempts total, got {shared.client.messages.calls}")
finally:
    shared.client = _orig

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All _call_claude retry tests passed.")
