"""The editorial landing page keeps navigation, original assets and search
metadata intact while explaining reviewable stock and order recommendations.

Run: python tests/test_landing_minimal.py
"""
import json
import os
import sys
import tempfile
import types
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["DB_PATH"] = os.path.join(_tmp.name, "landing.db")
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


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []
        self.images = []
        self.meta = {}
        self.headlines = 0
        self.schema = []
        self._schema = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs)
        elif tag == "img":
            self.images.append(attrs)
        elif tag == "meta":
            self.meta[attrs.get("name", attrs.get("property"))] = attrs.get("content")
        elif tag == "h1":
            self.headlines += 1
        elif tag == "script" and attrs.get("type") == "application/ld+json":
            self._schema = ""

    def handle_data(self, data):
        if self._schema is not None:
            self._schema += data

    def handle_endtag(self, tag):
        if tag == "script" and self._schema is not None:
            self.schema.append(json.loads(self._schema))
            self._schema = None


r = client.get("/")
html = r.get_data(as_text=True)
_check(r.status_code == 200, "landing returns 200")
page = _Page()
page.feed(html)

_check(page.headlines == 1 and "cash with an expiry date" in html, "one original hero headline")
_check("Confirm suggested matches" in html, "item matching requires human confirmation")
_check("suggests order quantities for you to review" in html, "order quantities presented as reviewable suggestions")
_check("Promised lead times measured against what was actually delivered" not in html,
       "unsupported supplier delivery claim removed")
_check(html.count("Discuss a pilot") == 2, "consistent pilot action in hero and closing section")

# Keep the actual ownership token, not just an empty tag with the same name.
_check(page.meta.get("google-site-verification") == "kQ1R_XtpFZEqkk07CEU33YtyLmDMeAP-adI1qeUHclM",
       "Google Search Console ownership preserved")
schema_types = {node.get("@type") for schema in page.schema for node in schema.get("@graph", [])}
_check({"Organization", "SoftwareApplication"} <= schema_types, "valid organization and software search schema")
_check('rel="canonical" href="https://berthcast.com/"' in html, "canonical URL preserved")
_check(page.meta.get("og:image") == "https://berthcast.com/static/logo.png", "social sharing image preserved")

_check(len(page.ids) == len(set(page.ids)), "page anchor IDs are unique")
for link in page.links:
    href = link.get("href", "")
    if href.startswith("#"):
        _check(href[1:] in page.ids, f"section link resolves: {href}")
    else:
        _check(href.startswith("/") and not href.startswith("//"), f"page link stays on this host: {href}")
        _check("target" not in link, f"ordinary navigation stays in the same tab: {href}")

hrefs = {link.get("href") for link in page.links}
_check({"/", "/pricing", "/login", "/about", "/data", "/contact", "/terms", "/privacy"} <= hrefs,
       "all public page destinations remain reachable")
_check({"top", "how", "what", "who", "pilot"} <= set(page.ids), "existing section bookmarks preserved")
_check(sum(link.get("href") == "/contact" for link in page.links) == 3,
       "both pilot actions and footer contact point to the contact form")

image_sources = {image.get("src") for image in page.images}
_check(image_sources == {"/static/logo-dark.png", "/static/hero-warehouse.jpg"}, "original local logo and warehouse assets used")
for src in sorted(image_sources):
    _check(client.get(src).status_code == 200, f"image loads: {src}")

for removed in ("feat-grid", "screenshot-inventory", "strip-inner", "running-head",
                "pullquote", "snapQty", "stampIn", "srlist", "ex-num", "heroScan"):
    _check(removed not in html, f"obsolete demo or animation remains absent: {removed}")

if F:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll minimal-landing tests passed.")
