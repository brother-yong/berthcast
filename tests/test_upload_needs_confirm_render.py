"""A needs_confirm sales slot renders the question + a confirm button, and does
NOT enable Continue (sales is not 'done')."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import app  # noqa: E402

F = []


def _check(c, m):
    if not c:
        F.append(m)


conv = {
    "sales": {"status": "needs_confirm", "rows": 891,
              "readback": {"items": 149, "months_kept": [1, 2, 3, 4, 5, 6],
                           "months_dropped": [7, 8, 9, 10, 11, 12],
                           "assumed_year": 2026, "total_units": 4015509.0,
                           "tier": "degraded",
                           "question": "We kept June as real sales, but the months after it look like typed projections."}},
    "inventory": {"status": "done", "rows": 10},
}
tables = {"inventory": True, "sales": True, "purchase_orders": False, "suppliers": False}
file_names = {"sales": "s.xlsx", "inventory": "i.xlsx"}

with app.test_request_context():
    from flask import render_template
    html = render_template("upload.html", session_id=1, conversion_status=conv,
                           tables=tables, file_names=file_names)

_check("We kept June as real sales" in html, "question text must render")
_check("confirmReadback" in html, "confirm button must be wired (onclick)")
_check("confirm-readback" in html, "confirm JS must POST to the route")
# Continue must be disabled: sales is needs_confirm, not done.
_check('id="continue-btn"' in html, "continue button present")
_check("disabled" in html, "continue button should be disabled while needs_confirm")
# The calm read-back must NOT also render for the same slot (avoid double panels).
_check(html.count("We kept June as real sales") == 1, "question renders exactly once")

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All needs_confirm render tests passed.")
