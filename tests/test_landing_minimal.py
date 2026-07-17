"""The landing page is the minimal version: pure-text hero + 3 trimmed
sections. Locks in what was deleted (features grid, screenshots, stats strip,
problem section, hero report card + animation scripts) so it can't creep back.

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
_check("Stop losing revenue" in html, "hero headline kept")
_check("berthcast reads your ERP exports and writes the order you should place." in html,
       "one-line hero sub")
_check("How it works" in html, "how-it-works section kept")
_check("1,570" in html, "worked example number kept")
_check("Get in touch" in html, "primary CTA kept")

# what must be GONE
_check("feat-grid" not in html, "features grid deleted")
_check("screenshot-inventory" not in html, "screenshots section deleted")
_check("strip-inner" not in html, "stats strip deleted")
_check("running-head" not in html, "running head deleted")
_check("pullquote" not in html, "problem section deleted")
_check("snapQty" not in html, "hero report card + count-up script deleted")
_check("stampIn" not in html, "card animations deleted")

# nav: exactly the kept links
_check('href="#how"' in html, "nav links to #how")
_check("#features" not in html, "features nav link gone")

if F:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll minimal-landing tests passed.")
