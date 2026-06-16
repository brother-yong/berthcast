"""Proof for the results-page clarity pass (Cool Link UX feedback).

Non-technical, limited-English purchasing staff use the results page. This pass:
  - shows plain status words (Critical / Running low / Well stocked / Not selling)
    while keeping the stored values unchanged,
  - adds a "what to do now" line at the top,
  - adds tap-to-explain help dots on the jargon,
  - softens the confidence + outcome wording.

Renders the real /results page through Flask's test client and asserts the new
copy is present. Run:  python tests/test_results_clarity.py
"""
import os
import sys
import json
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_resclarity_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")
    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass
    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                                   # noqa: E402
import app as appmod                                    # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── pure filter checks (display-only labels) ─────────────────────────────────
_check("status_label maps DEAD -> Not selling", appmod._status_label("DEAD") == "Not selling")
_check("status_label maps LOW -> Running low", appmod._status_label("LOW") == "Running low")
_check("status_label maps HEALTHY -> Well stocked", appmod._status_label("HEALTHY") == "Well stocked")
_check("conf_label maps INSUFFICIENT_DATA -> Need more data",
       appmod._conf_label("INSUFFICIENT_DATA") == "Need more data")


# ── seed a completed session ─────────────────────────────────────────────────
db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
           ("buyer@coollink.com", generate_password_hash("x"), "Cool Link", "claude-sonnet-4-6", "enterprise"))
uid = db.query("SELECT id FROM users WHERE email=?", ("buyer@coollink.com",))[0]["id"]
sid = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
                 (uid, "Cool Link", "complete"))

inventory = [
    {"item": "Frozen Salmon", "category": "Frozen", "stock": "0 CTN", "status": "CRITICAL",
     "spoilage_risk": "LOW", "days_of_supply": 5, "observation": "out of stock"},
    {"item": "EDAM CHEESE", "category": "Dairy", "stock": "50 KG", "status": "LOW",
     "spoilage_risk": "LOW", "days_of_supply": 100, "observation": "running down"},
    {"item": "Butter", "category": "Dairy", "stock": "300 KG", "status": "HEALTHY",
     "spoilage_risk": "LOW", "days_of_supply": 400, "observation": "fine"},
    {"item": "Old Sardines", "category": "Dry", "stock": "12 CTN", "status": "DEAD",
     "spoilage_risk": "LOW", "days_of_supply": 0, "observation": "no sales"},
]
recs = [
    {"item": "Frozen Salmon", "supplier": "AMMERLAND", "supplier_type": "import", "approved": True,
     "suggested_quantity": "420 CTN", "days_of_supply": 5, "lead_time_days": 56,
     "confidence": "HIGH", "reason": "out of stock, sells fast"},
    {"item": "EDAM CHEESE", "supplier": "Unknown", "supplier_type": "other", "approved": False,
     "suggested_quantity": "8000 KG", "days_of_supply": 100, "lead_time_days": 21,
     "confidence": "INSUFFICIENT_DATA", "reason": "limited history"},
]
db.execute("INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
           (sid, json.dumps(inventory), json.dumps(recs)))

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"]  = uid
    s["email"]    = "buyer@coollink.com"
    s["org_name"] = "Cool Link"
    s["model"]    = "claude-sonnet-4-6"
    s["is_admin"] = False
    s["tier"]     = "enterprise"
    s["role"]     = "admin"

r = client.get(f"/results/{sid}")
html = r.get_data(as_text=True)
_check("results page renders 200", r.status_code == 200, detail=str(r.status_code))

# Plain status words (display only)
_check("shows 'Running low'", "Running low" in html)
_check("shows 'Well stocked'", "Well stocked" in html)
_check("shows 'Not selling'", "Not selling" in html)
_check("does NOT show raw 'DEAD' badge text", ">DEAD<" not in html)

# "What do I do now" summary
_check("summary line: items to order", "to order" in html)
_check("summary line flags overdue first", "overdue" in html)

# Help dots + plainer jargon
_check("has tap-to-explain help dots", 'class="help-dot' in html)
_check("'stock runway' relabelled to 'Current stock lasts'", "Current stock lasts" in html)

# Confidence wording
_check("low-data confidence says 'Need more data'", "Need more data to be sure" in html)

# Outcome nudge
_check("outcome nudge explains why it matters", "proves it's saving you money" in html)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll results-clarity tests passed.")
