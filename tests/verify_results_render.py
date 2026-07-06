"""Integration verification: render /results through Flask and confirm the new
'stakes' markup appears for a loaded rec and is hidden for a bare rec.

Run with: python tests/verify_results_render.py
Uses a throwaway temp DB so it never touches real data.
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point the app at a throwaway DB and give the anthropic client a dummy key
# (no API call is made during rendering). MUST be set before importing app/db.
_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_verify_render.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

# Stub the anthropic SDK so we don't need it installed — no API call is made
# while rendering a results page; the client is only constructed at import.
import types  # noqa: E402
if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")
    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass
    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db          # noqa: E402
import app as appmod           # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

flask_app = appmod.app

# ── Seed a user, a completed session, and two recommendations ──────────────────
db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role, analyses_used, chat_messages_used) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    ("verify@test.com", generate_password_hash("x"), "VerifyOrg",
     "claude-haiku-4-5-20251001", "enterprise", 1, "admin", 0, 0),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("verify@test.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "VerifyOrg", "complete"),
)

recs = [
    {   # fully loaded: consequences + mitigation + monthly sales + high risk
        "item": "Frozen Salmon 1kg", "supplier": "Ocean Fresh", "supplier_type": "import",
        "lead_time_days": 105, "days_of_supply": 36, "recommended_action": "REORDER",
        "suggested_quantity": "160 CTN", "confidence": "HIGH",
        "consequence_if_not_acting": "a regional food distributor runs out of frozen salmon within days, leaving customer orders unfilled.",
        "consequence_if_acting": "Ordering now ties up cash in three months of frozen stock.",
        "supplier_risk": "HIGH",
        "mitigation": "Call Ocean Fresh to confirm the delivery date before committing.",
        "flags": [], "reason": "Tight stock with a slow import supplier.",
        "avg_monthly_sales": 40, "uom_label": " CTN",
    },
    {   # bare: no consequences, no monthly sales, not high-risk → everything hides
        "item": "Plain Crackers", "supplier": "Local Co", "supplier_type": "local",
        "lead_time_days": None, "days_of_supply": 20, "recommended_action": "MONITOR",
        "suggested_quantity": "Verify with team", "confidence": "INSUFFICIENT_DATA",
        "supplier_risk": "None", "flags": [],
        "reason": "Some stock left; keep an eye on it.",
    },
    {   # approved with order placed: line 2 must show the outcome question
        "item": "Padimas Jasmine Rice 5kg", "supplier": "Local Co", "supplier_type": "local",
        "lead_time_days": 21, "days_of_supply": 9, "recommended_action": "REORDER",
        "suggested_quantity": "80 BAG", "confidence": "MEDIUM",
        "supplier_risk": "None", "flags": [], "reason": "Stock low against steady sales.",
        "approved": True, "order_placed": True, "note": "told team already",
    },
]
inv = [
    {"item": "Frozen Salmon 1kg", "status": "CRITICAL", "spoilage_risk": "HIGH",
     "days_of_supply": 36, "category": "FROZEN", "stock": "12 CTN", "observation": "low"},
    {"item": "Plain Crackers", "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 20, "category": "DRY", "stock": "50", "observation": "ok"},
    {"item": "Padimas Jasmine Rice 5kg", "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 9, "category": "DRY", "stock": "6 BAG", "observation": "low"},
]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, json.dumps(inv), json.dumps(recs)),
)

# ── Render the page as a logged-in user ────────────────────────────────────────
client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "verify@test.com"
    s["org_name"] = "VerifyOrg"
    s["model"] = "claude-haiku-4-5-20251001"
    s["is_admin"] = False
    s["tier"] = "enterprise"
    s["role"] = "admin"

resp = client.get(f"/results/{sid}")
html = resp.get_data(as_text=True)

checks = {
    "page returns 200": resp.status_code == 200,
    "negative stakes label shown": "If you don't order" in html,
    "positive stakes label shown": "If you order" in html,
    "negative consequence text shown": "leaving customer orders unfilled" in html,
    "positive consequence text shown": "ties up cash in three months" in html,
    "mitigation strip shown": "What to do about the supplier risk" in html,
    "mitigation text shown": "Call Ocean Fresh" in html,
    "quantity basis shown": "You sell about 40 CTN/month" in html,
    "quantity basis mentions lead time": "about 3.5 months" in html,
    "quantity basis mentions the qty": "Suggested order: 160 CTN" in html,
    "no stray 'None' in quantity basis": "You sell about None" not in html,
    # Graceful hiding: only the salmon rec qualifies, so exactly one of each block.
    "exactly one stakes block": html.count('class="rec-stakes"') == 1,
    "exactly one quantity-basis line": html.count('class="rec-qty-basis"') == 1,
    "exactly one mitigation strip": html.count('class="rec-mitigation"') == 1,

    # ── compact-row markup (2026-07 redesign) ──
    "three row containers": html.count('rec-row-main') == 3,
    "three hidden panels": html.count('class="rec-row-panel"') == 3,
    "note inputs on rows": html.count('class="rec-note"') == 3,
    "saved note rendered": 'told team already' in html,
    "critical row colour hook present": 'data-status="CRITICAL"' in html,
    "low-confidence tag shown": 'rec-row-lowconf' in html,
    "reason snippet on line 2": 'Tight stock with a slow import supplier.' in html,
    "approved row shows outcome question": 'Was the stockout avoided?' in html,
    "qty on line 1": '160 CTN' in html,
    "confidence ring is gone": 'confidence-ring' not in html,
    "popover is gone": 'conf-popover' not in html,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"{'ok ' if ok else 'FAIL'}: {name}")

if resp.status_code != 200:
    print("\n--- first 800 chars of response (for debugging) ---")
    print(html[:800])

if failed:
    print(f"\n{len(failed)} check(s) failed.")
    sys.exit(1)
print("\nAll render checks passed.")
