"""Notes must save via /recommend/edit on blur — no approve/dismiss needed.

Checks: note persists, action state untouched, blank clears, omitted note
leaves an existing note alone.

Run: python tests/test_rec_note_save.py
Uses a throwaway temp DB so it never touches real data.
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Point the app at a throwaway DB and give the anthropic client a dummy key
# (no API call is made). MUST be set before importing app/db.
_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_note_save.db")
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
# CSRF is exercised elsewhere; here we test the route logic.
flask_app.config["WTF_CSRF_ENABLED"] = False

# ── Seed user + session + one rec ──────────────────────────────────────────────
db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role, analyses_used, chat_messages_used) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    ("note@test.com", generate_password_hash("x"), "NoteOrg",
     "claude-haiku-4-5-20251001", "enterprise", 1, "admin", 0, 0),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("note@test.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "NoteOrg", "complete"),
)
recs = [{"item": "Brookvale UHT Milk 1L", "supplier": "Nordvik Dairy",
         "supplier_type": "import", "suggested_quantity": "1570 CTN",
         "confidence": "HIGH", "reason": "Out of stock.", "flags": []}]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, json.dumps([]), json.dumps(recs)),
)

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "note@test.com"
    s["org_name"] = "NoteOrg"
    s["model"] = "claude-haiku-4-5-20251001"
    s["is_admin"] = False
    s["tier"] = "enterprise"
    s["role"] = "admin"


def _saved_rec():
    row = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (sid,))
    return json.loads(row[0]["recommendations_json"])[0]


checks = {}

# 1. Note saves WITHOUT any approve/dismiss action
r = client.post("/recommend/edit", json={
    "session_id": sid, "item": "Brookvale UHT Milk 1L", "note": "called supplier"})
checks["edit-with-note returns ok"] = r.status_code == 200 and r.get_json()["ok"]
checks["note persisted"] = _saved_rec().get("note") == "called supplier"

# 2. Action state untouched by a note-only save
rec0 = _saved_rec()
checks["note save does not approve"] = not rec0.get("approved") and not rec0.get("dismissed")

# 3. Blank note clears it
r = client.post("/recommend/edit", json={
    "session_id": sid, "item": "Brookvale UHT Milk 1L", "note": "   "})
checks["blank note clears"] = r.status_code == 200 and _saved_rec().get("note") is None

# 4. Omitted note leaves an existing note alone (qty-only edit)
client.post("/recommend/edit", json={
    "session_id": sid, "item": "Brookvale UHT Milk 1L", "note": "keep me"})
client.post("/recommend/edit", json={
    "session_id": sid, "item": "Brookvale UHT Milk 1L", "edited_quantity": "1600 CTN"})
checks["omitted note untouched"] = _saved_rec().get("note") == "keep me"

failed = [n for n, ok in checks.items() if not ok]
for n, ok in checks.items():
    print(f"{'ok ' if ok else 'FAIL'}: {n}")
if failed:
    print(f"\n{len(failed)} check(s) failed.")
    sys.exit(1)
print("\nAll note-save tests passed.")
