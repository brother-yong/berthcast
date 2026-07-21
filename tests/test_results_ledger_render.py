"""Render /results through Flask and assert the ledger markup (2026-07 redesign).

Guards the three faults the redesign fixes:
  1. the item name must never be truncated by CSS,
  2. filter chip counts must come from recommendations, not inventory,
  3. the outcome prompt and its sales line must be gone from the page.

Run with: python tests/test_results_ledger_render.py
Uses a throwaway temp DB so it never touches real data.
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_ledger_render.db")
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
    ("ledger@test.com", generate_password_hash("x"), "LedgerOrg",
     "claude-haiku-4-5-20251001", "enterprise", 1, "admin", 0, 0),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("ledger@test.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "LedgerOrg", "complete"),
)

# A deliberately long name — the fault this redesign exists to fix.
LONG_NAME = "BROOKVALE AMMONIA CHILLED PRAWN MEAT PEELED DEVEINED 400G 12X CARTON"

recs = [
    {"item": LONG_NAME, "supplier": "Nordvik Foods", "supplier_type": "import",
     "lead_time_days": 105, "days_of_supply": 12, "recommended_action": "REORDER",
     "suggested_quantity": "240 CTN", "confidence": "LOW", "supplier_risk": "None",
     "flags": [], "reason": "Tight stock against a long import lead time.",
     "avg_monthly_sales": 60, "uom_label": " CTN"},
    {"item": "PADIMAS JASMINE RICE 5KG", "supplier": "Kessington Trading",
     "supplier_type": "local", "lead_time_days": 21, "days_of_supply": 9,
     "recommended_action": "REORDER", "suggested_quantity": "60 BAG",
     "confidence": "MEDIUM", "supplier_risk": "None", "flags": [],
     "reason": "Stock low against steady sales.",
     "approved": True, "note": "called supplier"},
    {"item": "NORDVIK COD FILLET SKIN-ON 1KG", "supplier": "Nordvik Foods",
     "supplier_type": "import", "lead_time_days": 98, "days_of_supply": 40,
     "recommended_action": "MONITOR", "suggested_quantity": "18 CTN",
     "confidence": "HIGH", "supplier_risk": "None", "flags": [],
     "reason": "Comfortable for now."},
]

# Inventory deliberately carries MORE criticals than the recommendations do.
# If a filter chip count is read off inventory, it reads 4; off recommendations, 1.
inv = [
    {"item": LONG_NAME, "status": "CRITICAL", "spoilage_risk": "HIGH",
     "days_of_supply": 12, "category": "FROZEN", "stock": "38 CTN", "observation": "low"},
    {"item": "PADIMAS JASMINE RICE 5KG", "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 9, "category": "DRY", "stock": "6 BAG", "observation": "low"},
    {"item": "NORDVIK COD FILLET SKIN-ON 1KG", "status": "HEALTHY", "spoilage_risk": "NONE",
     "days_of_supply": 40, "category": "FROZEN", "stock": "90 CTN", "observation": "ok"},
    {"item": "ASTELLA CHICKPEA 400G 24X", "status": "CRITICAL", "spoilage_risk": "NONE",
     "days_of_supply": 3, "category": "DRY", "stock": "2 CTN", "observation": "low"},
    {"item": "MERIDYNE OLIVE OIL 1L", "status": "CRITICAL", "spoilage_risk": "NONE",
     "days_of_supply": 4, "category": "DRY", "stock": "5 CTN", "observation": "low"},
    {"item": "HAVLUND TUNA CHUNK 185G", "status": "CRITICAL", "spoilage_risk": "NONE",
     "days_of_supply": 2, "category": "DRY", "stock": "1 CTN", "observation": "low"},
]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, json.dumps(inv), json.dumps(recs)),
)

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "ledger@test.com"
    s["org_name"] = "LedgerOrg"
    s["model"] = "claude-haiku-4-5-20251001"
    s["is_admin"] = False
    s["tier"] = "enterprise"
    s["role"] = "admin"

resp = client.get(f"/results/{sid}")
html = resp.get_data(as_text=True)

checks = {
    "page returns 200": resp.status_code == 200,

    # ── Fault 1: the item name must survive intact ──
    "long item name renders in full": LONG_NAME in html,
    "name sits in its own grid cell": html.count('class="rec-row-namecell"') == 3,
    "ledger column headings render once per supplier group": html.count('class="rec-ledger-head"') == 2,
    "supplier type shows on every row": html.count('class="rec-row-type"') == 3,

    # ── Fault 2: chip counts come from recommendations, not inventory ──
    "chip count placeholders exist": html.count('class="mc-chip-count"') >= 9,
    "sidebar is gone": 'class="mc-sidebar"' not in html,
    "filter bar replaces it": 'class="mc-filterbar"' in html,
    "stat boxes are gone": 'mc-side-stat' not in html,
    "spoilage left the recommendation filters": 'data-val="SPOILAGE"' not in html,

    # ── Fault 3: the outcome prompt and its sales line are gone ──
    "order-placed question removed": 'Did you place this order?' not in html,
    "stockout question removed": 'Was the stockout avoided?' not in html,
    "sales line removed": "proves it's saving you money" not in html,
    "outcome span removed": 'class="rec-row-outcome"' not in html,

    # ── Note moved into the expanded panel ──
    "one note input per row": html.count('class="rec-note"') == 3,
    "note inputs live inside panels": html.count('rec-panel-note') == 3,
    "saved note still renders": 'called supplier' in html,
    "note button in the action column": html.count('data-rec-action="note"') == 3,

    # ── Preserved behaviour ──
    "three rows rendered": html.count('rec-row-main') == 3,
    "three expandable panels": html.count('class="rec-row-panel"') == 3,
    "critical colour hook intact": 'data-status="CRITICAL"' in html,
    "low-confidence tag intact": 'rec-row-lowconf' in html,
    "approve-all demoted to a link": 'rec-supplier-approve-link' in html,
    "keyboard hint bar intact": 'kb-hint-bar' in html,
    "quantity still rendered": '240 CTN' in html,
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
print("\nAll ledger render checks passed.")
