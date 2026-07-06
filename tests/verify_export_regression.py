"""End-to-end regression proof for the order-sheet exports after the compact-rows
redesign: drive the REAL approve + note endpoints through the test client, then
confirm CSV export and the Print/PDF page still produce correct output — and
that headless Edge can actually print the sheet to a PDF file.

Run with: python tests/verify_export_regression.py
Uses a throwaway temp DB so it never touches real data. Not part of run_tests.py
on purpose (it shells out to Edge); run it manually before results-page changes.
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point the app at a throwaway DB and give the anthropic client a dummy key
# (no API call is made). MUST be set before importing app/db.
_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_verify_export.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

# Stub the anthropic SDK so we don't need it installed.
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
# CSRF is exercised elsewhere; here we test the export flow logic.
flask_app.config["WTF_CSRF_ENABLED"] = False

# ── Seed a user, a completed session, and two recommendations ──────────────────
db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role, analyses_used, chat_messages_used) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    ("export@test.com", generate_password_hash("x"), "ExportOrg",
     "claude-haiku-4-5-20251001", "enterprise", 1, "admin", 0, 0),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("export@test.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "ExportOrg", "complete"),
)

recs = [
    {"item": "Frozen Salmon 1kg", "supplier": "Ocean Fresh", "supplier_type": "import",
     "lead_time_days": 105, "days_of_supply": 36, "recommended_action": "REORDER",
     "suggested_quantity": "160 CTN", "confidence": "HIGH",
     "supplier_risk": "None", "flags": [],
     "reason": "Tight stock with a slow import supplier.",
     "avg_monthly_sales": 40, "uom_label": " CTN"},
    {"item": "Plain Crackers", "supplier": "Local Co", "supplier_type": "local",
     "lead_time_days": 14, "days_of_supply": 20, "recommended_action": "MONITOR",
     "suggested_quantity": "30 BOX", "confidence": "MEDIUM",
     "supplier_risk": "None", "flags": [],
     "reason": "Some stock left; keep an eye on it."},
]
inv = [
    {"item": "Frozen Salmon 1kg", "status": "CRITICAL", "spoilage_risk": "HIGH",
     "days_of_supply": 36, "category": "FROZEN", "stock": "12 CTN", "observation": "low"},
    {"item": "Plain Crackers", "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 20, "category": "DRY", "stock": "50", "observation": "ok"},
]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, json.dumps(inv), json.dumps(recs)),
)

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "export@test.com"
    s["org_name"] = "ExportOrg"
    s["model"] = "claude-haiku-4-5-20251001"
    s["is_admin"] = False
    s["tier"] = "enterprise"
    s["role"] = "admin"

checks = {}

# ── 1. Approve one rec WITH a note and an edited qty via the real endpoint ─────
r = client.post("/recommend/action", json={
    "session_id": sid, "item": "Frozen Salmon 1kg", "action": "approve",
    "note": "call to confirm ETA", "edited_quantity": "170 CTN",
    "edited_supplier": None})
checks["action approve ok"] = r.status_code == 200 and r.get_json()["ok"]

# ── 2. Note-only save on the OTHER rec via /recommend/edit (new path) ──────────
r = client.post("/recommend/edit", json={
    "session_id": sid, "item": "Plain Crackers", "note": "check with team"})
checks["note-only edit ok"] = r.status_code == 200 and r.get_json()["ok"]

# ── 3. Results page renders with both notes ────────────────────────────────────
r = client.get(f"/results/{sid}")
html = r.get_data(as_text=True)
checks["results 200"] = r.status_code == 200
checks["note A on page"] = "call to confirm ETA" in html
checks["note B on page"] = "check with team" in html

# ── 4. CSV export: approved rec, edited qty + AI original, note carried ────────
r = client.get(f"/results/{sid}/export.csv")
csv_text = r.get_data(as_text=True)
checks["csv 200"] = r.status_code == 200
checks["csv content-type"] = r.mimetype == "text/csv"
checks["csv header row"] = csv_text.splitlines()[0].startswith("Item,On Hand,Qty To Order")
checks["csv has approved item"] = "Frozen Salmon 1kg" in csv_text
checks["csv shows edited qty with AI original"] = "170 CTN (AI: 160 CTN)" in csv_text
checks["csv carries the note"] = "call to confirm ETA" in csv_text
checks["csv excludes unapproved"] = "Plain Crackers" not in csv_text

# ── 5. Print page (the Print/PDF flow) renders the approved order sheet ────────
r = client.get(f"/results/{sid}/print")
print_html = r.get_data(as_text=True)
checks["print 200"] = r.status_code == 200
checks["print has approved item"] = "Frozen Salmon 1kg" in print_html
checks["print shows qty"] = "170 CTN" in print_html
checks["print excludes unapproved"] = "Plain Crackers" not in print_html

# ── 6. Headless Edge prints the sheet to a real PDF file ───────────────────────
import subprocess
import pathlib
out_dir = pathlib.Path(tempfile.gettempdir()) / "berthcast_export_proof"
out_dir.mkdir(exist_ok=True)
print_file = out_dir / "print_page.html"
print_file.write_text(print_html, encoding="utf-8")
pdf_file = out_dir / "order_sheet.pdf"
if pdf_file.exists():
    pdf_file.unlink()
_edge_candidates = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
edge = next((p for p in _edge_candidates if os.path.exists(p)), None)
if edge:
    subprocess.run([edge, "--headless", "--disable-gpu",
                    f"--print-to-pdf={pdf_file}", print_file.as_uri()],
                   timeout=60, check=False, capture_output=True)
    checks["pdf produced (>1KB)"] = pdf_file.exists() and pdf_file.stat().st_size > 1024
else:
    checks["pdf produced (>1KB)"] = None   # Edge missing — SKIP, don't fail

failed = [n for n, ok in checks.items() if ok is False]
for n, ok in checks.items():
    print(f"{'SKIP' if ok is None else ('ok ' if ok else 'FAIL')}: {n}")
if failed:
    print(f"\n{len(failed)} check(s) failed.")
    sys.exit(1)
print("\nAll export-regression checks passed.")
