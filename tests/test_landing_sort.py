"""The landing sort band: rows are authored in ranked order and carry the
scrambled start position the animation moves them from. The permutation is the
thing that can silently rot — a duplicate or missing data-from index leaves two
rows stacked on each other mid-animation.

Run: python tests/test_landing_sort.py
"""
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")
    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass
    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import app as appmod  # noqa: E402

appmod.app.config["TESTING"] = True
client = appmod.app.test_client()

F = []


def _check(c, m):
    print(("ok: " if c else "FAIL: ") + m)
    if not c:
        F.append(m)


r = client.get("/")
html = r.get_data(as_text=True)
_check(r.status_code == 200, "landing returns 200")
_check('id="srlist"' in html, "sort band rendered")

rows = re.findall(r'<li class="sr" data-from="(\d+)".*?class="sr-days[^"]*">(\d+)<', html)
_check(len(rows) == 6, f"six rows in the band (got {len(rows)})")

starts = [int(a) for a, _ in rows]
days = [int(b) for _, b in rows]

# markup order is the sorted end state: no-JS and reduced-motion see this and
# nothing else, so it has to be correct on its own
_check(days == sorted(days), f"rows authored in ascending days of cover (got {days})")

# every scrambled slot used exactly once, or rows overlap when the animation starts
_check(sorted(starts) == list(range(len(rows))),
       f"data-from is a clean permutation of 0..{len(rows) - 1} (got {starts})")
_check(any(s != i for i, s in enumerate(starts)), "start order actually differs from sorted order")

# the two guards that decide whether the animation runs at all
_check("prefers-reduced-motion" in html, "reduced-motion opt-out present")
_check("IntersectionObserver" in html, "observer guard present")

# public repo: invented brands only, never a real client's catalogue
_check("Brookvale" in html, "invented brand names used")

if F:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll landing-sort tests passed.")
