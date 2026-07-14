"""A failed recommendation step must be LOUD on the results page.

When the rec agent errors it returns [{"error": ...}]; that dict is saved in
recommendations_json. The page used to filter it silently and show the
innocent "No recommendations generated. Your inventory may be in good shape"
line — a masked crash (14 Jul 2026: a real client run had ~40 criticals and
rendered as a clean zero). Locks:
  - error dict -> amber failure panel naming the recommendation step
  - the "may be in good shape" line does NOT render alongside an error
  - genuinely-empty recs (no error dict) still show the calm line
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import app  # noqa: E402

F = []


def _check(c, m):
    if not c:
        F.append(m)


def _render(recs):
    with app.test_request_context():
        from flask import render_template
        return render_template(
            "results.html",
            summary={"to_order": 0, "order_now": 0, "urgent": 0},
            recommendations=recs,
            rec_groups=[],
            inventory=[{"item": "X", "status": "CRITICAL", "spoilage_risk": "HIGH",
                        "stock": "0", "days_of_supply": 0, "observation": "o", "category": "DRY"}],
            upload_session_id=1,
            org_name="T",
            generated_at="now",
            status_by_item={"X": "CRITICAL"},
            user_tier="enterprise",
            user_role="admin",
            supplier_score_map={},
            data_notes=[],
            gaps=[],
        )


# 1) error dict -> loud failure panel, no innocent line
html = _render([{"error": "Recommendation agent returned no usable JSON for any batch."}])
_check("recommendation step failed" in html.lower(), "error must be surfaced loudly")
_check("no usable JSON" in html, "the saved error text must be shown")
_check("may be in good shape" not in html, "innocent zero-recs line must NOT show on error")

# 2) genuinely empty recs -> calm line, no failure panel
html = _render([])
_check("may be in good shape" in html, "calm line stays for a real empty result")
_check("recommendation step failed" not in html.lower(), "no failure panel when no error")

# 3) the progress-page agent card must not say "none needed" on a failure
from agents.orchestrator import _summarise_recommendations  # noqa: E402
_check("failed" in _summarise_recommendations([{"error": "boom"}]).lower(),
       "agent card must say the step failed")
_check(_summarise_recommendations([]) == "No reorder recommendations needed",
       "agent card keeps the calm line for a genuine empty result")

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All rec-error surfacing tests passed.")
