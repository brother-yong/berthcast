"""In-memory login throttle — slows down password guessing.

After MAX_FAILURES failed logins from the same key (the client IP) within
WINDOW_SECONDS, that key is locked until enough of those failures age out of
the window. A successful login calls clear() to reset immediately.

Render runs a single gunicorn worker, so a process-local dict is enough; it
resets on restart, which is fine (a restart already interrupts any attack).
This won't stop a distributed attacker rotating IPs, but it turns "unlimited
guesses" into a handful per window, which is the point. Time is passed in via
`now` so the logic is deterministic and unit-testable without sleeping.
"""
import threading
import time as _time

MAX_FAILURES = 5            # failures allowed within the window before locking
WINDOW_SECONDS = 15 * 60    # rolling window length and lockout duration

_failures = {}              # key -> list[float timestamps]
_lock = threading.Lock()


def _recent(times, now):
    """The timestamps still inside the window."""
    return [t for t in times if now - t < WINDOW_SECONDS]


def record_failure(key, now=None):
    """Record one failed login attempt for this key."""
    now = _time.time() if now is None else now
    with _lock:
        times = _recent(_failures.get(key, []), now)
        times.append(now)
        _failures[key] = times


def is_locked(key, now=None):
    """True if this key has hit the failure limit within the window."""
    now = _time.time() if now is None else now
    with _lock:
        times = _recent(_failures.get(key, []), now)
        if times:
            _failures[key] = times      # keep the pruned list
        else:
            _failures.pop(key, None)    # nothing recent — forget the key
        return len(times) >= MAX_FAILURES


def seconds_until_unlock(key, now=None):
    """Seconds until the key is no longer locked (0 if it isn't locked).

    Once enough failures age out to drop below MAX_FAILURES the key unlocks,
    so the wait is measured from the oldest failure currently in the window.
    """
    now = _time.time() if now is None else now
    with _lock:
        times = _recent(_failures.get(key, []), now)
        if len(times) < MAX_FAILURES:
            return 0
        oldest = min(times)
        return max(0, int(WINDOW_SECONDS - (now - oldest)) + 1)


def clear(key):
    """Forget all failures for this key (call on a successful login)."""
    with _lock:
        _failures.pop(key, None)
