#!/usr/bin/env python
"""Post-deploy check for berthcast.

Waits for the commit you just pushed to go live on the site, then confirms the
DB watchdog thread is actually RUNNING in the production worker.

Why this exists: on 9 Jul 2026 the watchdog fix shipped green (51 tests passing)
but never ran in the gunicorn worker — a thread started at import died at fork.
Local tests cannot see that. This hits the live site and proves the deployed
code is really running, so "shipped but not running" fails loudly here instead
of via a frozen login.

    python check_deploy.py                    # checks https://berthcast.com
    python check_deploy.py https://other...   # different base URL

Exit 0 = pushed commit is live AND watchdog up. Non-zero = something is wrong.
Run it right after `git push` (give Render a minute to build first).
"""
import json
import subprocess
import sys
import time
import urllib.request

DEADLINE_S = 300        # max wait for the build+deploy to land
WD_GRACE_S = 45         # once live, how long to allow the watchdog to come up
POLL_S = 10


def local_sha():
    """Short SHA of HEAD, or '' if git is unavailable."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"], text=True).strip()
    except Exception:
        return ""


def verdict(want, code, body, live_since, now, grace_s=WD_GRACE_S):
    """Pure decision for a single /health poll. Returns (state, message) where
    state is 'pass', 'fail', or 'wait'.

    want       local short SHA we expect live ('' = unknown, skip version gate)
    code       HTTP status from /health
    body       parsed /health JSON
    live_since timestamp we first saw the target version live (None = not yet)
    now        current timestamp
    """
    live = body.get("version", "")
    wd = body.get("watchdog", "?")
    is_live = (not want) or live == want or live == "unknown"
    if not is_live:
        return "wait", f"old version still live (live={live}, want={want})"
    if code == 200 and wd == "up":
        return "pass", "new deploy live and watchdog up"
    # Deploy has landed but it isn't fully healthy yet — give it a grace window
    # (the watchdog boots on the first request) before calling it a failure.
    if live_since is not None and (now - live_since) > grace_s:
        if wd != "up":
            return "fail", "deploy is live but watchdog is DOWN — fix not running in worker"
        return "fail", f"deploy is live but /health returned {code}"
    return "wait", f"deploy live, waiting for watchdog (health={code}, watchdog={wd})"


def _health(base):
    with urllib.request.urlopen(base + "/health", timeout=15) as r:
        return r.status, json.load(r)


def main(argv):
    base = (argv[1] if len(argv) > 1 else "https://berthcast.com").rstrip("/")
    want = local_sha()
    print(f"target {base}  expecting commit {want or '(unknown — no version gate)'}")

    start = time.time()
    live_since = None
    while time.time() - start < DEADLINE_S:
        now = time.time()
        try:
            code, body = _health(base)
        except Exception as e:
            print(f"  ...not answering yet ({e.__class__.__name__})")
            time.sleep(POLL_S)
            continue

        live = body.get("version", "")
        if ((not want) or live == want or live == "unknown") and live_since is None:
            live_since = now
        state, msg = verdict(want, code, body, live_since, now)
        print(f"  health={code} version={live} watchdog={body.get('watchdog','?')} -> {state}")
        if state == "pass":
            print(f"PASS: {msg}")
            return 0
        if state == "fail":
            print(f"FAIL: {msg}")
            return 1
        time.sleep(POLL_S)

    print(f"FAIL: timed out after {DEADLINE_S}s waiting for {want or 'the deploy'} to go live.")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
