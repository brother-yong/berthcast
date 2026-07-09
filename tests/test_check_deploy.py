"""Proof for check_deploy.verdict — the post-deploy decision logic (10 Jul 2026).

check_deploy.py waits for the pushed commit to go live, then confirms the DB
watchdog is running in production. The network loop is thin; the branching lives
in verdict(), tested here offline with no HTTP.

Dependency-free:  python tests/test_check_deploy.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from check_deploy import verdict          # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


WANT = "abc1234"

# Old version still up (mid-deploy) → keep waiting, never a false pass/fail.
s, _ = verdict(WANT, 200, {"version": "old0000", "watchdog": "up"}, None, 100.0)
_check("old version still live -> wait", s == "wait")

# Target version live and watchdog up → pass.
s, _ = verdict(WANT, 200, {"version": WANT, "watchdog": "up"}, 100.0, 101.0)
_check("new version live + watchdog up -> pass", s == "pass")

# Live but watchdog down, still inside the grace window → wait (booting).
s, _ = verdict(WANT, 200, {"version": WANT, "watchdog": "down"}, 100.0, 120.0)
_check("watchdog down within grace -> wait", s == "wait")

# Live but watchdog still down past the grace window → fail (the 9 Jul bug).
s, m = verdict(WANT, 200, {"version": WANT, "watchdog": "down"}, 100.0, 200.0)
_check("watchdog down past grace -> fail", s == "fail", detail=m)
_check("failure message names the watchdog", "watchdog" in m.lower(), detail=m)

# Live but /health unhealthy past grace → fail.
s, _ = verdict(WANT, 503, {"version": WANT, "watchdog": "up"}, 100.0, 200.0)
_check("health 503 past grace -> fail", s == "fail")

# version 'unknown' (RENDER_GIT_COMMIT unset) → skip the gate, judge on watchdog.
s, _ = verdict(WANT, 200, {"version": "unknown", "watchdog": "up"}, 100.0, 101.0)
_check("unknown version treated as live -> pass on watchdog up", s == "pass")

# No local git SHA (want='') → cannot gate on version, judge on watchdog.
s, _ = verdict("", 200, {"version": "whatever", "watchdog": "up"}, 100.0, 101.0)
_check("no local SHA -> judge on watchdog only", s == "pass")


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll check_deploy tests passed.")
