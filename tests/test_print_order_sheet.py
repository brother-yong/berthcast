"""The print sheet is the paper half of the ordering loop: staff carry it,
tick what they ordered, and write the PO number and ETA on it by hand.

Asserts it prints EVERY recommendation with its saved action and decision,
and provides both the tick box and the write-in space.

Run with: python tests/test_print_order_sheet.py
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_print_sheet.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

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

db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role, analyses_used, chat_messages_used) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    ("printsheet@test.com", generate_password_hash("x"), "PrintOrg",
     "claude-haiku-4-5-20251001", "enterprise", 1, "admin", 0, 0),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("printsheet@test.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "PrintOrg", "complete"),
)

recs = [
    {"item": "BROOKVALE PRAWN MEAT 400G 12X", "supplier": "Nordvik Foods",
     "supplier_type": "import", "lead_time_days": 105, "days_of_supply": 12,
     "recommended_action": "REORDER", "suggested_quantity": "240 CTN",
     "confidence": "MEDIUM", "supplier_risk": "None", "flags": [],
     "reason": "Tight stock.", "approved": True},
    {"item": "PADIMAS JASMINE RICE 5KG", "supplier": "Kessington Trading",
     "supplier_type": "local", "lead_time_days": 21, "days_of_supply": 9,
     "recommended_action": "HOLD", "suggested_quantity": "60 BAG",
     "confidence": "MEDIUM", "supplier_risk": "None", "flags": [],
     "reason": "Steady sales.", "dismissed": True},
    {"item": "BROOKVALE OAT DRINK 1L", "supplier": "Kessington Trading",
     "supplier_type": "local", "lead_time_days": 21, "days_of_supply": 30,
     "recommended_action": "MONITOR", "suggested_quantity": "12 CTN",
     "confidence": "MEDIUM", "supplier_risk": "None", "flags": [],
     "reason": "Watch stock."},
    {"error": "recommendation agent failed"},
]
inv = [
    {"item": "BROOKVALE PRAWN MEAT 400G 12X", "status": "CRITICAL", "spoilage_risk": "NONE",
     "days_of_supply": 12, "category": "FROZEN", "stock": "38 CTN", "observation": "low"},
    {"item": "PADIMAS JASMINE RICE 5KG", "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 9, "category": "DRY", "stock": "6 BAG", "observation": "low"},
    {"item": "BROOKVALE OAT DRINK 1L", "status": "HEALTHY", "spoilage_risk": "NONE",
     "days_of_supply": 30, "category": "DRY", "stock": "25 CTN", "observation": "steady"},
]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, json.dumps(inv), json.dumps(recs)),
)
db.execute("UPDATE analysis_results SET created_at=? WHERE session_id=?",
           ("2026-04-17 09:30:00", sid))

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "printsheet@test.com"
    s["org_name"] = "PrintOrg"
    s["model"] = "claude-haiku-4-5-20251001"
    s["is_admin"] = False
    s["tier"] = "enterprise"
    s["role"] = "admin"

resp = client.get(f"/results/{sid}/print")
html = resp.get_data(as_text=True)


def _item_row_contains(item, *phrases):
    start = html.find(f"<strong>{item}</strong>")
    if start < 0:
        return False
    end = html.find("</tr>", start)
    return end >= 0 and all(phrase in html[start:end] for phrase in phrases)


checks = {
    "page returns 200": resp.status_code == 200,
    "approved item printed": "BROOKVALE PRAWN MEAT 400G 12X" in html,
    "UNapproved item printed too": "PADIMAS JASMINE RICE 5KG" in html,
    "pending item printed too": "BROOKVALE OAT DRINK 1L" in html,
    "failed rec never printed": "recommendation agent failed" not in html,
    "saved analysis date shown": "Analysis run: 17/04/2026" in html,
    "reopen date not substituted": "new Date()" not in html,
    "stock identified as snapshot": "On hand at analysis" in html,
    "current ERP stock check stated": "Check current ERP stock before ordering" in html,
    "quantity is a suggestion": "Suggested qty" in html,
    "approved REORDER row labelled": _item_row_contains(
        "BROOKVALE PRAWN MEAT 400G 12X", "Action: REORDER", "Decision: Approved"),
    "dismissed HOLD row labelled": _item_row_contains(
        "PADIMAS JASMINE RICE 5KG", "Action: HOLD", "Decision: Dismissed"),
    "pending MONITOR row labelled": _item_row_contains(
        "BROOKVALE OAT DRINK 1L", "Action: MONITOR", "Decision: Pending"),
    "tick-box column header": "Ordered" in html,
    "one tick box per item": html.count('class="tickbox"') == 3,
    "write-in column header": "PO no. / ETA" in html,
    "one write-in cell per item": html.count('class="writein"') == 3,
    "landscape page rule present": "landscape" in html,
    "both suppliers grouped": html.count("group-head") >= 2,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"{'ok ' if ok else 'FAIL'}: {name}")

if failed:
    print(f"\n{len(failed)} check(s) failed.")
    sys.exit(1)
print("\nAll print sheet checks passed.")
