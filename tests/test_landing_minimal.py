"""The landing page: warehouse-photo hero with one gold scan-line sweep, plus
the trimmed sections. Locks in what stays deleted (features grid, screenshots,
stats strip, problem pull-quotes, the old count-up report card) so it can't
creep back.

Run: python tests/test_landing_minimal.py
"""
import os
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

# what stays
_check("Every stockout is a" in html, "hero headline kept")
_check("from the files you already export" in html, "hero sub kept")
_check('class="hero-bg"' in html, "warehouse hero image present")
_check('id="heroScan"' in html, "gold scan-line present")
_check("Where the money leaks out" in html, "problem section present")
_check("How it works" in html, "how-it-works section kept")
_check("Three outputs, every run" in html, "what-you-get section present")
_check("Who it's for" in html, "who-it's-for section present")
_check("Book a pilot" in html, "pilot CTA present")

# AEO/GEO: machine-readable schema so answer engines describe berthcast right.
_check('application/ld+json' in html, "JSON-LD structured data present")
_check('"SoftwareApplication"' in html, "SoftwareApplication schema present")
_check('"Organization"' in html, "Organization schema present")

# what must be GONE
_check("feat-grid" not in html, "features grid deleted")
_check("screenshot-inventory" not in html, "screenshots section deleted")
_check("strip-inner" not in html, "stats strip deleted")
_check("running-head" not in html, "old running head gone")
_check("pullquote" not in html, "old pull-quote layout gone")
_check("snapQty" not in html, "hero report card + count-up script gone")
_check("stampIn" not in html, "card animations gone")
_check("srlist" not in html, "old ranked-sort list gone")
_check("ex-num" not in html, "old worked-example panel gone")

# nav: exactly the kept links
_check('href="#how"' in html, "nav links to #how")
_check("#features" not in html, "features nav link gone")

# hamburger menu: every page reachable without scrolling to the footer
_check("menu-panel" in html, "hamburger menu present")
for page in ("/terms", "/privacy", "/about", "/contact"):
    _check(f'href="{page}"' in html and html.index(f'href="{page}"') < html.index("<footer"),
           f"{page} reachable from the top menu (not just the footer)")

if F:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll minimal-landing tests passed.")
