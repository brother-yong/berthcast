"""A saved analysis with missing sales matches stays honest in chat."""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="berth_chat_review_"), "test.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

if "anthropic" not in sys.modules:
    stub = types.ModuleType("anthropic")

    class AnthropicStub:
        def __init__(self, *args, **kwargs):
            pass

    stub.Anthropic = AnthropicStub
    stub.AnthropicError = Exception
    sys.modules["anthropic"] = stub

import database as db  # noqa: E402
from chat_logic import _build_chat_context, PRODUCT_GUIDE  # noqa: E402

failed = False


def check(name, condition):
    global failed
    print(("ok: " if condition else "FAIL: ") + name)
    failed = failed or not condition


org = "Synthetic Distributor"
db.init_db()
uid = db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
    ("review@example.com", "unused", org, "claude-sonnet-5", "enterprise"),
)
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status, created_at) VALUES (?,?,?,?)",
    (uid, org, "complete", "2026-09-23 10:00:00"),
)
inventory = [
    {"item": "BROOKVALE APRICOT MIX 500G", "status": "REVIEW", "stock": "0 BOX",
     "days_of_supply": None, "observation": "No matching sales record."},
    {"item": "PADIMAS RICE 5KG", "status": "CRITICAL", "stock": "0 BAG",
     "days_of_supply": 0, "observation": "Sold recently."},
    {"item": "NORDVIK SALT 1KG", "status": "DEAD", "stock": "4 BAG",
     "days_of_supply": None, "observation": "No sales in recorded period."},
]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) "
    "VALUES (?,?,?)",
    (sid, json.dumps(inventory), "[]"),
)

ctx = _build_chat_context(uid, org, detailed=True)
summary = ctx["summary_text"]
detail = ctx["detailed_text"]
check("chat calls the saved run a snapshot", "SAVED INVENTORY SNAPSHOT" in summary
      and "2026-09-23" in summary and "LIVE INVENTORY DATA" not in summary)
check("chat counts review status separately", "REVIEW: 1" in summary)
check("chat explains review is not an automatic order", "sales match" in summary.lower()
      and "order" in summary.lower())
dead_section = detail.split("DEAD SKUs", 1)[-1].split("NEEDS SALES MATCH", 1)[0]
check("detailed chat lists review item separately from dead items",
      "BROOKVALE APRICOT MIX 500G" not in dead_section
      and "NEEDS SALES MATCH" in detail
      and "BROOKVALE APRICOT MIX 500G" in detail)
check("guide describes the staff review and paper print", "Needs sales match" in PRODUCT_GUIDE
      and "all" in PRODUCT_GUIDE.split("Print / PDF")[-1].lower()
      and "approved" in PRODUCT_GUIDE.split("CSV")[-1].lower())

if failed:
    sys.exit(1)
print("All chat review context checks passed.")
