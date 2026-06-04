"""Tests for the login throttle in rate_limit.py.

The throttle slows down password guessing: after MAX_FAILURES failed attempts
from the same key (client IP) within WINDOW_SECONDS, that key is locked until
enough of those failures age out of the window. A successful login clears it.

Time is injected (the `now` argument) so the tests are deterministic and fast —
no sleeping. Dependency-free: run with `python tests/test_rate_limit.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rate_limit


_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _reset():
    rate_limit.clear("ip-a")
    rate_limit.clear("ip-b")


M = rate_limit.MAX_FAILURES
W = rate_limit.WINDOW_SECONDS
T = 1000.0  # an arbitrary fixed "now" base, in seconds

# 1. Fresh key is not locked.
_reset()
_check("a fresh key is not locked", rate_limit.is_locked("ip-a", now=T) is False)

# 2. One below the threshold is still allowed.
_reset()
for i in range(M - 1):
    rate_limit.record_failure("ip-a", now=T + i)
_check("M-1 failures: still not locked",
       rate_limit.is_locked("ip-a", now=T + M) is False)

# 3. Hitting the threshold locks the key.
_reset()
for i in range(M):
    rate_limit.record_failure("ip-a", now=T + i)
_check("M failures: locked", rate_limit.is_locked("ip-a", now=T + M) is True)

# 4. After the window passes, the failures age out and the key unlocks.
_reset()
for i in range(M):
    rate_limit.record_failure("ip-a", now=T + i)
_check("locked right after the failures", rate_limit.is_locked("ip-a", now=T + M) is True)
_check("unlocked once the window has fully passed",
       rate_limit.is_locked("ip-a", now=T + W + M + 1) is False)

# 5. A successful login (clear) immediately resets the count.
_reset()
for i in range(M):
    rate_limit.record_failure("ip-a", now=T + i)
rate_limit.clear("ip-a")
_check("clear() unlocks immediately", rate_limit.is_locked("ip-a", now=T + M) is False)

# 6. Different keys are independent — one attacker can't lock everyone out.
_reset()
for i in range(M):
    rate_limit.record_failure("ip-a", now=T + i)
_check("ip-a is locked", rate_limit.is_locked("ip-a", now=T + M) is True)
_check("ip-b is unaffected", rate_limit.is_locked("ip-b", now=T + M) is False)

# 7. seconds_until_unlock: 0 when not locked, positive (<= window) when locked.
_reset()
_check("seconds_until_unlock is 0 when not locked",
       rate_limit.seconds_until_unlock("ip-a", now=T) == 0)
_reset()
for i in range(M):
    rate_limit.record_failure("ip-a", now=T + i)
secs = rate_limit.seconds_until_unlock("ip-a", now=T + M)
_check("seconds_until_unlock is positive while locked", secs > 0, detail=str(secs))
_check("seconds_until_unlock never exceeds the window", secs <= W, detail=str(secs))

_reset()
if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll rate-limit tests passed.")
