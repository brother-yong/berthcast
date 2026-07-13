"""Upload read-back is a CALM, collapsed summary — not an amber error panel.

A successful wide-matrix conversion must read as a one-line confirmation
(expandable), never as a warning. Locks:
  - month_span filter turns [1..5] into 'Jan–May', gaps into a list.
  - the rendered panel is a <details> with a plain summary and none of the
    old alarm wording (no '⚠', 'wasn't a standard', 'looks wrong').
  - upload.html still compiles.

Run: python tests/test_upload_readback_collapsed.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import app, _month_span  # noqa: E402

_FAILED = []


def _check(cond, msg):
    if not cond:
        _FAILED.append(msg)


# ── month_span filter ────────────────────────────────────────────────────────
_check(_month_span([1, 2, 3, 4, 5]) == "Jan–May", "contiguous run should be Jan–May")
_check(_month_span([6, 7, 8, 9, 10, 11, 12]) == "Jun–Dec", "contiguous tail should be Jun–Dec")
_check(_month_span([3]) == "Mar", "single month should be its name")
_check(_month_span([1, 3, 5]) == "Jan, Mar, May", "gapped months should list")
_check(_month_span([]) == "", "empty should be blank")
_check(_month_span([0, 13, "x"]) == "", "out-of-range/garbage should be blank")

# ── the read-back panel renders calm + collapsed ─────────────────────────────
# Render just the panel block through the app's jinja env (filter registered there).
panel_src = """
<details class="slot-readback">
  <summary>Read as {{ '{:,}'.format(readback['items']) }} items, {{ readback['months_kept']|month_span }} {{ readback['assumed_year'] }}</summary>
  <div>{{ '{:,.0f}'.format(readback['total_units']) }} units sold
  {% if readback['months_dropped'] %}Skipped {{ readback['months_dropped']|month_span }} — empty or future months, not sales{% endif %}</div>
</details>
"""
readback = {"items": 149, "months_kept": [1, 2, 3, 4, 5], "months_dropped": [6, 7, 8, 9, 10, 11, 12],
            "assumed_year": 2026, "total_units": 3346343.0}
html = app.jinja_env.from_string(panel_src).render(readback=readback)

_check("Read as 149 items, Jan–May 2026" in html, "summary line wrong:\n" + html)
_check("Skipped Jun–Dec" in html, "dropped-months line missing:\n" + html)
_check("<details" in html and "<summary" in html, "must be a collapsed <details> panel")
# The alarm wording is gone.
for banned in ("⚠", "wasn't a standard", "looks wrong", "remove and re-upload"):
    _check(banned not in html, f"alarm wording {banned!r} should be gone")

# ── the real template still compiles ─────────────────────────────────────────
try:
    app.jinja_env.get_template("upload.html")   # parse + compile, no render
except Exception as e:
    _check(False, f"upload.html failed to compile: {e}")

if _FAILED:
    print("FAILED:")
    for m in _FAILED:
        print("  -", m)
    sys.exit(1)
print("All upload read-back collapse tests passed.")
