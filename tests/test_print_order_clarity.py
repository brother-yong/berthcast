"""Proof for the clearer purchase-order outputs (a regional food distributor feedback).

a regional food distributor staff couldn't read the printed order: the quantity column was ambiguous
(order vs on-hand), the order-by dates had all passed, and "stock runway" was unclear.
The fix makes the print sheet, the PDF, and the CSV all show the SAME columns:
  #  Item  On hand  Qty to order  Supplier  Order by  Current stock lasts  Notes
with an overdue date shown as "ASAP", a new current-stock column joined from the
inventory report, and only human-typed notes (the AI reasoning prose is dropped).

Drives the three export routes through Flask's test client. Run:
    python tests/test_print_order_clarity.py
"""
import os
import sys
import json
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_printclarity_")
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


# ── Seed a completed session: inventory (with stock) + approved recs ─────────
db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
           ("buyer@example.com", generate_password_hash("x"), "a regional food distributor",
            "claude-sonnet-4-6", "enterprise"))
uid = db.query("SELECT id FROM users WHERE email=?", ("buyer@example.com",))[0]["id"]
sid = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
                 (uid, "a regional food distributor", "complete"))

inventory = [
    {"item": "Frozen Salmon", "stock": "0 CTN", "status": "CRITICAL", "days_of_supply": 5},
    {"item": "EDAM CHEESE",   "stock": "50 KG", "status": "LOW",      "days_of_supply": 100},
    # NOTE: no "Ghost Item" here — it must fall back to "—" on the sheet.
]
recs = [
    # Overdue (dos 5 - lead 56 < 0) -> Order by should read "ASAP". Stock 0 CTN on hand.
    {"item": "Frozen Salmon", "supplier": "AMMERLAND", "approved": True,
     "suggested_quantity": "420 CTN", "days_of_supply": 5, "lead_time_days": 56,
     "confidence": "HIGH", "reason": "SECRET_REASON_TEXT_salmon", "note": ""},
    # Not overdue (dos 100 - lead 21 > 0) -> a future date. Has a human note; AI reason must be dropped.
    {"item": "EDAM CHEESE", "supplier": "AMMERLAND", "approved": True,
     "suggested_quantity": "8000 KG", "days_of_supply": 100, "lead_time_days": 21,
     "avg_monthly_sales": 2433,  # 8000 / 2433 = ~3.3 months of coverage
     "confidence": "MEDIUM", "reason": "SECRET_REASON_TEXT_edam", "note": "Check with warehouse first"},
    # No matching inventory row -> On hand must show "—".
    {"item": "Ghost Item", "supplier": "Local SG", "approved": True,
     "suggested_quantity": "10 BAG", "days_of_supply": 3, "lead_time_days": 21,
     "confidence": "LOW", "reason": "SECRET_REASON_TEXT_ghost", "note": ""},
]
db.execute("INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
           (sid, json.dumps(inventory), json.dumps(recs)))

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"]  = uid
    s["email"]    = "buyer@example.com"
    s["org_name"] = "a regional food distributor"
    s["model"]    = "claude-sonnet-4-6"
    s["is_admin"] = False
    s["tier"]     = "enterprise"
    s["role"]     = "admin"


# ── 1. Printed sheet ─────────────────────────────────────────────────────────
r = client.get(f"/results/{sid}/print")
html = r.get_data(as_text=True)
_check("print sheet returns 200", r.status_code == 200, detail=str(r.status_code))
_check("has 'On hand' header", "On hand" in html)
_check("has 'Qty to order' header", "Qty to order" in html)
_check("has 'Current stock lasts' header", "Current stock lasts" in html)
_check("shows current stock on hand (0 CTN)", "0 CTN" in html)
_check("overdue order-by shows ASAP", "ASAP" in html)
_check("no-match item falls back to dash", "Ghost Item" in html and "—" in html)
_check("human note is shown", "Check with warehouse first" in html)
_check("AI reasoning prose is NOT printed", "SECRET_REASON_TEXT" not in html)
_check("has 'This order lasts' column", "This order lasts" in html)
_check("order-coverage months computed (~3.3 mo)", "~3.3 mo" in html)


# ── 2. CSV export (same columns) ─────────────────────────────────────────────
r = client.get(f"/results/{sid}/export.csv")
csv_body = r.get_data(as_text=True)
_check("CSV returns 200", r.status_code == 200, detail=str(r.status_code))
_check("CSV header 'On Hand'", "On Hand" in csv_body)
_check("CSV header 'Qty To Order'", "Qty To Order" in csv_body)
_check("CSV header 'Current Stock Lasts (months)'", "Current Stock Lasts (months)" in csv_body)
_check("CSV shows on-hand stock value", "50 KG" in csv_body or "0 CTN" in csv_body)
_check("CSV overdue order-by shows ASAP", "ASAP" in csv_body)
_check("CSV header 'This Order Lasts (months)'", "This Order Lasts (months)" in csv_body)
_check("CSV order-coverage value present", "3.3" in csv_body)
_check("CSV does NOT leak AI reasoning", "SECRET_REASON_TEXT" not in csv_body)


# ── 3. PDF export (builds without error) ─────────────────────────────────────
# reportlab is a real dependency (requirements.txt) but may be absent locally;
# skip the PDF assertion when it isn't installed rather than fail the suite.
try:
    import reportlab  # noqa: F401
    _have_reportlab = True
except ImportError:
    _have_reportlab = False
    print("skip: reportlab not installed locally — PDF export not exercised here")

if _have_reportlab:
    r = client.get(f"/results/{sid}/export.pdf")
    _check("PDF returns 200", r.status_code == 200, detail=str(r.status_code))
    _check("PDF is a non-empty application/pdf",
           r.mimetype == "application/pdf" and len(r.get_data()) > 500,
           detail=f"{r.mimetype}, {len(r.get_data())} bytes")


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll print-order clarity tests passed.")
