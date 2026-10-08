"""_call_claude retries transient API failures instead of dying on the first.

14 Jul 2026: a real client run lost its whole recommendation step to ONE
'overloaded_error' (HTTP 529) — the API said "busy, try again" and berthcast
gave up. Locks:
  - overloaded/429/5xx and connection errors are retried with a pause
  - a non-transient error (e.g. 401 auth) raises immediately, no retry
  - retries exhausted -> the last error raises (callers keep their handling)
  - a successful call logs its tokens and rough cost and adds them to USAGE

Run: python tests/test_call_claude_retry.py
"""
import os
import sys
from types import SimpleNamespace

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

    def get_final_message(self):
        return SimpleNamespace(usage=SimpleNamespace(
            input_tokens=1000, output_tokens=500,
            cache_creation_input_tokens=None, cache_read_input_tokens=2000))


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
        self.last_kw = kw
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

# 4) a successful call logs its tokens and rough cost, tagged with the calling
#    module, and adds them to the running total (29 Sep 2026: the API bill spiked
#    and nothing recorded which calls spent it). A list-of-blocks user message
#    passes through untouched so a script can cache a repeated stock list.
_logged = []
_orig_info = shared.logger.info
shared.logger.info = lambda msg, *a: _logged.append(msg % a)
for k in shared.USAGE:
    shared.USAGE[k] = 0
blocks = [{"type": "text", "text": "stock", "cache_control": {"type": "ephemeral"}},
          {"type": "text", "text": "lines"}]
shared.client = _FakeClient(failures=0, exc=_Overloaded)
try:
    shared._call_claude("claude-sonnet-5", "sys", blocks)
    _check(shared.client.messages.last_kw["messages"][0]["content"] is blocks,
           "list-of-blocks user content must reach the API as given")
    u = shared.USAGE
    _check((u["calls"], u["input"], u["output"], u["cache_write"], u["cache_read"]) == (1, 1000, 500, 0, 2000),
           f"running total wrong: {u}")
    # 1000 in x $2 + 500 out x $10 + 2000 cache read x $0.20, per million tokens
    _check(abs(u["usd"] - 0.0074) < 1e-9, f"cost should be 0.0074, got {u['usd']}")
    _check(len(_logged) == 1 and f"[{__name__}, claude-sonnet-5]" in _logged[0] and "~US$0.0074" in _logged[0],
           f"log line should name the caller, model and cost: {_logged}")
finally:
    shared.client = _orig

# 4b) Haiku 5.5 has two rate cards: a prompt over 100K tokens costs 5x every rate.
class _U:
    def __init__(self, i, o): self.input_tokens, self.output_tokens = i, o
    cache_creation_input_tokens = cache_read_input_tokens = 0
for k in shared.USAGE:
    shared.USAGE[k] = 0
shared.record_usage("claude-haiku-5-5", lambda: _U(100_000, 1000), "test")
_check(abs(shared.USAGE["usd"] - 0.0105) < 1e-9, f"100K prompt is the cheap card: {shared.USAGE['usd']}")
shared.record_usage("claude-haiku-5-5", lambda: _U(100_001, 1000), "test")
_check(abs(shared.USAGE["usd"] - 0.0105 - 0.0525005) < 1e-9, f"over 100K costs 5x: {shared.USAGE['usd']}")

# 5) a missing usage object or an unpriced model is logged, never raised
_logged.clear()
try:
    shared.record_usage("some-future-model", lambda: None, "test")
    _check(len(_logged) == 1 and "price unknown" in _logged[0], f"unpriced model log: {_logged}")
except Exception as e:  # noqa: BLE001
    _check(False, f"record_usage must never raise, got {e!r}")
finally:
    shared.logger.info = _orig_info


# 6) the call is already billed: if fetching usage blows up, _call_claude still
#    returns the text on the first attempt (no failed run, no second paid retry)
class _NoUsageStream(_FakeStream):
    def get_final_message(self):
        raise RuntimeError("connection reset while reading usage")  # would look transient


class _NoUsageMessages(_FakeMessages):
    def stream(self, **kw):
        self.calls += 1
        return _NoUsageStream("text-despite-usage-error")


_warned = []
_orig_warning = shared.logger.warning
shared.logger.warning = lambda msg, *a: _warned.append(msg % a)
shared.client = _FakeClient(failures=0, exc=_Overloaded)
shared.client.messages = _NoUsageMessages(0, _Overloaded)
try:
    out = shared._call_claude("claude-sonnet-5", "sys", "user")
    _check(out == "text-despite-usage-error", f"text must come back despite the usage error, got {out!r}")
    _check(shared.client.messages.calls == 1, f"usage error must not retry, got {shared.client.messages.calls} attempts")
    _check(any("Could not record Claude usage" in w for w in _warned), f"usage error should be logged: {_warned}")
except Exception as e:  # noqa: BLE001
    _check(False, f"usage error must not escape _call_claude, got {e!r}")
finally:
    shared.client = _orig
    shared.logger.warning = _orig_warning


# 7) a reply with no text block (a refusal, or Haiku 5.5's thinking used the whole
#    cap) comes back as "" so the caller skips that batch: no crash, no paid retry,
#    and the billed call is still logged. The SDK raises RuntimeError for it.
class _NoTextStream(_FakeStream):
    def get_final_text(self):
        raise RuntimeError(".get_final_text() can only be called when the API returns a `text` content block.")


class _NoTextMessages(_FakeMessages):
    def stream(self, **kw):
        self.calls += 1
        return _NoTextStream("")


_warned.clear()
_logged.clear()
shared.logger.warning = lambda msg, *a: _warned.append(msg % a)
shared.logger.info = lambda msg, *a: _logged.append(msg % a)
shared.client = _FakeClient(failures=0, exc=_Overloaded)
shared.client.messages = _NoTextMessages(0, _Overloaded)
try:
    out = shared._call_claude("claude-haiku-5-5", "sys", "user")
    _check(out == "", f"no-text reply should come back as empty text, got {out!r}")
    _check(shared.client.messages.calls == 1, f"no-text reply must not retry, got {shared.client.messages.calls}")
    _check(any("no text" in w for w in _warned), f"no-text reply should be logged: {_warned}")
    _check(any("Claude usage" in m for m in _logged), f"billed call must still be logged: {_logged}")
except Exception as e:  # noqa: BLE001
    _check(False, f"no-text reply must not escape _call_claude, got {e!r}")
finally:
    shared.client = _orig
    shared.logger.warning = _orig_warning
    shared.logger.info = _orig_info

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All _call_claude retry tests passed.")
