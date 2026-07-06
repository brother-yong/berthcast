"""Proof for the output-escaping batch (stop trusting data when rendering it).

Covers:
  1. validators.csv_safe_cell — defuses CSV/Excel formula injection.
  2. /results/<id>/export.csv — the route actually runs item/supplier/reason/note
     through csv_safe_cell, so an uploaded item named "=HYPERLINK(...)" is exported
     as text, not a live formula.
  3. emails._send_critical_alert — HTML-escapes item/observation, so a name like
     "<img src=x onerror=...>" can't inject markup into the alert email.
  4. emails._send_contact_email — collapses newlines in the Subject/Reply-To so a
     crafted name/email can't inject extra email headers.

Uses a throwaway DB + uploads dir, a stubbed anthropic client, and a captured
mail sender — touches no real data and sends nothing.

Run:  python tests/test_output_escaping.py
"""
import os
import sys
import json
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_outesc_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
# Make the mail senders run (they early-return when these are unset).
os.environ["MAIL_SENDER"] = "admin@berthcast.com"
os.environ["MAIL_APP_PASSWORD"] = "dummy-app-password"

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")
    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass
    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import validators                                       # noqa: E402
import database as db                                   # noqa: E402
import emails                                           # noqa: E402
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


# ── 1. csv_safe_cell (pure) ──────────────────────────────────────────────────
_check("= formula is quoted",  validators.csv_safe_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)")
_check("+ formula is quoted",  validators.csv_safe_cell("+1+1") == "'+1+1")
_check("- formula is quoted",  validators.csv_safe_cell("-2+3") == "'-2+3")
_check("@ formula is quoted",  validators.csv_safe_cell("@SUM(A1)") == "'@SUM(A1)")
_check("tab-led cell is quoted", validators.csv_safe_cell("\tx") == "'\tx")
_check("ordinary text is untouched", validators.csv_safe_cell("Frozen Salmon") == "Frozen Salmon")
_check("number string is untouched", validators.csv_safe_cell("120") == "120")
_check("None becomes empty string", validators.csv_safe_cell(None) == "")


# ── 2. export.csv route runs cells through the guard ─────────────────────────
db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
           ("buyer@example.com", generate_password_hash("x"), "a regional food distributor",
            "claude-sonnet-4-6", "enterprise"))
uid = db.query("SELECT id FROM users WHERE email=?", ("buyer@example.com",))[0]["id"]
sid = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
                 (uid, "a regional food distributor", "complete"))
rec = {
    "item": "=cmd|' /C calc'!A1",
    "supplier": "+SUM(1+1)",
    "supplier_type": "import",
    "reason": "-2+3+cmd",
    "note": "@SUM(A1)",
    "approved": True,
    "suggested_quantity": "100",
    "days_of_supply": 10,
    "confidence": "HIGH",
}
db.execute("INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
           (sid, "[]", json.dumps([rec])))

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"]  = uid
    s["email"]    = "buyer@example.com"
    s["org_name"] = "a regional food distributor"
    s["model"]    = "claude-sonnet-4-6"
    s["is_admin"] = False
    s["tier"]     = "enterprise"
    s["role"]     = "admin"

r = client.get(f"/results/{sid}/export.csv")
body = r.get_data(as_text=True)
_check("export.csv returns 200", r.status_code == 200, detail=str(r.status_code))
_check("item formula is neutralised in CSV", "'=cmd" in body)
_check("supplier formula is neutralised", "'+SUM(1+1)" in body)
# Note: the CSV no longer exports the AI 'reason' column (it now mirrors the
# printed sheet — human notes only), so the note column carries the @ payload.
_check("note formula is neutralised", "'@SUM(A1)" in body)
_check("no CSV field begins a line with a bare =",
       not any(ln.startswith("=") for ln in body.splitlines()))


# ── 3. critical-alert email HTML-escapes item/observation ────────────────────
_captured = {}


def _capture(msg, sender, password, recipient):
    _captured["msg"] = msg
    return True


emails._deliver = _capture  # intercept the actual send

emails._send_critical_alert(
    uid, sid,
    [{"item": "<img src=x onerror=alert(1)>", "days_of_supply": 3,
      "observation": "<script>bad()</script>"}],
    "https://berthcast.com",
)
_msg = _captured.get("msg")
_html_part = ""
if _msg is not None:
    for part in _msg.walk():
        if part.get_content_type() == "text/html":
            _html_part = part.get_payload(decode=True).decode("utf-8", "replace")
_check("alert email was built", _msg is not None)
_check("item is HTML-escaped in the alert email", "&lt;img" in _html_part)
_check("no live <img onerror> survives in the alert email",
       "<img src=x onerror" not in _html_part)
_check("no live <script> survives in the alert email", "<script>bad" not in _html_part)


# ── 4. contact email can't inject headers via newlines ───────────────────────
_captured.clear()
emails._send_contact_email("Evil\r\nBcc: attacker@evil.com", "a@b.com\r\nX: y",
                           "Co", "hello")
_msg2 = _captured.get("msg")
_check("contact email was built", _msg2 is not None)
_check("Subject has no embedded newline",
       _msg2 is not None and "\n" not in str(_msg2["Subject"]) and "\r" not in str(_msg2["Subject"]))
_check("Reply-To has no embedded newline",
       _msg2 is not None and "\n" not in str(_msg2["Reply-To"]) and "\r" not in str(_msg2["Reply-To"]))


# ── 5. results page: malicious item name can't inject markup or break out of JS ──
# Item names come from uploaded spreadsheets (semi-untrusted). The compact-row
# redesign must render them safely: no live HTML, and no user text spliced into
# an inline handler (buttons carry data attrs; one delegated listener acts).
xss_item = "Widget<img src=x onerror=alert(1)>"
quote_item = "Backslash\\'};alert(1);//"
rec_xss = [
    {"item": xss_item, "supplier": "Ocean Fresh", "supplier_type": "import",
     "reason": "safe reason", "suggested_quantity": "10", "confidence": "HIGH",
     "approved": True, "order_placed": False, "days_of_supply": 5},
    {"item": quote_item, "supplier": "Local Co", "supplier_type": "local",
     "reason": "safe reason", "suggested_quantity": "5", "confidence": "MEDIUM"},
]
inv_xss = [
    {"item": xss_item, "status": "CRITICAL", "spoilage_risk": "LOW",
     "days_of_supply": 5, "category": "DRY", "stock": "1", "observation": "x"},
    {"item": quote_item, "status": "LOW", "spoilage_risk": "LOW",
     "days_of_supply": 9, "category": "DRY", "stock": "2", "observation": "y"},
]
sid2 = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
                  (uid, "a regional food distributor", "complete"))
db.execute("INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
           (sid2, json.dumps(inv_xss), json.dumps(rec_xss)))

r2 = client.get(f"/results/{sid2}")
page = r2.get_data(as_text=True)
_check("results page with XSS item returns 200", r2.status_code == 200, detail=str(r2.status_code))
_check("no live <img onerror> survives on results page", "<img src=x onerror" not in page)
_check("item is HTML-escaped on results page", "&lt;img src=x onerror" in page)
# The dangerous old pattern is gone: no user item text spliced into inline JS.
_check("no inline onclick calls takeAction with an item literal",
       "onclick=\"takeAction(" not in page)
_check("no inline onclick calls recordOutcome with an item literal",
       "onclick=\"recordOutcome(" not in page)
_check("approve/dismiss buttons use the data-attr delegation pattern",
       'data-rec-action="approve"' in page)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll output-escaping tests passed.")
