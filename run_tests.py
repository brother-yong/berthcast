#!/usr/bin/env python
"""Run every test in tests/ and report a single pass/fail summary.

berthcast's tests are standalone scripts: each tests/test_*.py sets up its own
temp database, stubs the Anthropic client, runs its checks, and exits 0 on
success or non-zero on failure. This runner executes them all and aggregates the
result, so there is ONE command for "is everything still green" — used both
locally and by CI (.github/workflows/tests.yml).

    python run_tests.py        # run all tests, show each result
    python run_tests.py -q     # quiet: only show failures

The exit code is 0 only if every test passed, so CI fails the build on any
break. Cross-platform (uses sys.executable), so it runs the same on Windows and
on the Linux CI runner.
"""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    quiet = "-q" in sys.argv
    tests = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    if not tests:
        print("No tests found under tests/test_*.py")
        return 1

    passed, failed = [], []
    for path in tests:
        name = os.path.relpath(path, ROOT).replace("\\", "/")
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True)
        if proc.returncode == 0:
            passed.append(name)
            if not quiet:
                print(f"ok    {name}")
        else:
            failed.append(name)
            print(f"FAIL  {name}")
            # Show the tail so a break is visible without re-running by hand.
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-12:]
            for line in tail:
                print(f"        {line}")

    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
