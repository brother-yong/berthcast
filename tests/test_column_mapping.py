"""Tests for the LLM-assisted column mapping (now fully automatic).

Context: column detection used to be pure keyword matching in Python, so a file
whose stock column was named something unexpected (or whose first quantity-ish
column was actually "Qty Sold") got read wrong, silently. Claude proposes the
mapping, Python validates it, keyword detection remains the fallback. The user
"confirm your columns" step was removed on 12 June 2026 — it confused the
workers actually running analyses, and the AI applies its validated mapping
itself (covered in tests/test_partial_sales_matching.py section 5).

This covers:
  1. propose_inventory_columns — LLM overrides the keyword guess only when its
     pick is a real, numeric, non-"sold/value" column; otherwise falls back.
  2. The inventory agent honours a saved column_map_json over keyword
     detection, and ignores a mapping that points at a missing column.
  3. The /context route no longer shows the confirm step, never needs an LLM
     call, and can never overwrite the saved mapping from form input.

Throwaway DB + stubbed anthropic; no network, no API calls.
Run: python tests/test_column_mapping.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_colmap.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
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

import database as db                 # noqa: E402
import agents.shared as shared        # noqa: E402
import agents.inventory as inv_mod    # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. propose_inventory_columns ─────────────────────────────────────────────
_HEADERS = ["category", "item_name", "supplier", "sales_pre_tax",
            "qty_sold", "unit", "avg_qty_month", "current_system_balance"]
_SAMPLES = [
    {"category": "CHEESE", "item_name": "EDAM", "supplier": "AMMERLAND",
     "sales_pre_tax": "62145", "qty_sold": "7299", "unit": "KG",
     "avg_qty_month": "2433", "current_system_balance": "120"},
    {"category": "CHEESE", "item_name": "GOUDA", "supplier": "AMMERLAND",
     "sales_pre_tax": "59496", "qty_sold": "6986", "unit": "KG",
     "avg_qty_month": "2328", "current_system_balance": "150"},
]

# Configurable fake for shared._call_claude.
_fake_reply = {"value": ""}


def _fake_call_claude(model, system, user, max_tokens=4096):
    return _fake_reply["value"]


shared._call_claude = _fake_call_claude

# (a) LLM correctly maps the awkward sheet.
_fake_reply["value"] = json.dumps({
    "description": "item_name", "stock": "current_system_balance",
    "category": "category", "uom": "unit"})
m = shared.propose_inventory_columns(_HEADERS, _SAMPLES, "claude-sonnet-4-6")
_check("LLM maps stock to current_system_balance", m["stock"] == "current_system_balance")
_check("LLM maps description to item_name", m["description"] == "item_name")
_check("LLM maps uom to unit", m["uom"] == "unit")

# (b) LLM hallucinates a column that doesn't exist -> keep keyword guess.
_fake_reply["value"] = json.dumps({"stock": "magic_stock_column"})
m = shared.propose_inventory_columns(_HEADERS, _SAMPLES, "claude-sonnet-4-6")
_check("hallucinated stock column is rejected (falls back to keyword)",
       m["stock"] == "current_system_balance", detail=str(m["stock"]))

# (c) LLM picks a NON-numeric column as stock -> rejected.
_fake_reply["value"] = json.dumps({"stock": "item_name"})
m = shared.propose_inventory_columns(_HEADERS, _SAMPLES, "claude-sonnet-4-6")
_check("non-numeric stock pick is rejected", m["stock"] != "item_name")

# (d) LLM picks a 'sold' column as stock -> rejected by the disqualifier list.
_fake_reply["value"] = json.dumps({"stock": "qty_sold"})
m = shared.propose_inventory_columns(_HEADERS, _SAMPLES, "claude-sonnet-4-6")
_check("qty_sold is never accepted as stock", m["stock"] != "qty_sold")

# (e) LLM call blows up -> keyword fallback, never crashes.
def _boom(*a, **k):
    raise RuntimeError("api down")


shared._call_claude = _boom
m = shared.propose_inventory_columns(_HEADERS, _SAMPLES, "claude-sonnet-4-6")
_check("LLM failure falls back to keyword guess",
       m["stock"] == "current_system_balance" and m["description"] == "item_name")
shared._call_claude = _fake_call_claude  # restore


# ── 2. Inventory agent honours a confirmed mapping ───────────────────────────
db.init_db()

SID_MAP, SID_BADMAP = 91, 92
for sid in (SID_MAP, SID_BADMAP):
    db.execute(
        "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
        "VALUES (?,?,?,?,?,?)",
        (sid, 1, "TestCo", "complete", "all", "{}"),
    )

# Inventory table with TWO plausible stock columns. Keyword detection picks
# "qty" (it's in the exact list); the user instead confirms "warehouse_count".
_COLS = '"item_name" TEXT, "qty" TEXT, "warehouse_count" TEXT, "category" TEXT, "uom" TEXT'
for sid in (SID_MAP, SID_BADMAP):
    db.execute(f'CREATE TABLE inventory_{sid} ({_COLS})')
    db.execute(f'CREATE TABLE sales_{sid} ("item_name" TEXT, "qty" TEXT, "date" TEXT)')
    for row in [("CHEDDAR", "1", "500", "CHEESE", "KG"),
                ("GOUDA",   "2", "640", "CHEESE", "KG")]:
        db.execute(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?,?)", row)

# Confirmed map points stock at warehouse_count.
db.execute("UPDATE upload_sessions SET column_map_json=? WHERE id=?",
           (json.dumps({"description": "item_name", "stock": "warehouse_count",
                        "category": "category", "uom": "uom"}), SID_MAP))
# Bad map points stock at a column that doesn't exist -> must fall back to keyword.
db.execute("UPDATE upload_sessions SET column_map_json=? WHERE id=?",
           (json.dumps({"stock": "does_not_exist"}), SID_BADMAP))

_calls = []


def _fake_inv_claude(model, system, user, max_tokens=4096):
    _calls.append(user)
    items = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            name = line.split("|")[0].replace("Item:", "").strip()
            items.append({"item": name, "category": "CHEESE", "stock": 0,
                          "status": "CRITICAL", "spoilage_risk": "MEDIUM",
                          "days_of_supply": 0, "observation": "t"})
    return json.dumps(items)


inv_mod._call_claude = _fake_inv_claude

_calls.clear()
res = inv_mod.run_inventory_agent(SID_MAP, "claude-sonnet-4-6", [], {}, None)
prompt = _calls[0] if _calls else ""
_check("confirmed map: stock read from warehouse_count (CHEDDAR shows 500)",
       "CHEDDAR" in prompt and "Stock: 500" in prompt, detail=prompt[:160])
_check("confirmed map: NOT read from keyword 'qty' (no Stock: 1)",
       "Stock: 1 " not in prompt and "Stock: 1\n" not in prompt)

_calls.clear()
res2 = inv_mod.run_inventory_agent(SID_BADMAP, "claude-sonnet-4-6", [], {}, None)
prompt2 = _calls[0] if _calls else ""
_check("invalid map falls back to keyword 'qty' (CHEDDAR shows 1)",
       "CHEDDAR" in prompt2 and "Stock: 1" in prompt2, detail=prompt2[:160])


# ── 3. /context route: no confirm step, no LLM call, no mapping override ─────
import app as appmod   # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
# Prove the route needs NO LLM at all: any call would blow up loudly.
shared._call_claude = _boom

SID_ROUTE = 93
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)",
    (SID_ROUTE, 1, "RouteOrg", "uploading", "all", "{}"),
)
db.execute(f'CREATE TABLE inventory_{SID_ROUTE} ('
           '"item_name" TEXT, "qty" TEXT, "warehouse_count" TEXT, "category" TEXT, '
           '"uom" TEXT, "_session_id" TEXT)')
db.execute(f'INSERT INTO inventory_{SID_ROUTE} VALUES (?,?,?,?,?,?)',
           ("CHEDDAR", "1", "500", "CHEESE", "KG", str(SID_ROUTE)))
# Simulate the agent's auto-saved mapping — the route must never clobber it.
db.execute("UPDATE upload_sessions SET column_map_json=? WHERE id=?",
           (json.dumps({"description": "item_name", "stock": "warehouse_count"}), SID_ROUTE))

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"] = 1
    s["email"] = "u@routeorg.com"
    s["org_name"] = "RouteOrg"
    s["model"] = "claude-sonnet-4-6"
    s["is_admin"] = True
    s["tier"] = "enterprise"
    s["role"] = "admin"

r = client.get(f"/context/{SID_ROUTE}")
body = r.data.decode("utf-8")
_check("context page renders 200 without any LLM call", r.status_code == 200,
       detail=str(r.status_code))
_check("confirm-columns step is gone", "Confirm your columns" not in body)
_check("no column dropdowns rendered", 'name="col_stock"' not in body)
_check("context questions still present", 'name="delayed_suppliers"' in body)

r2 = client.post(f"/context/{SID_ROUTE}", data={
    "col_stock": "evil_injection",  # stray/malicious field must be ignored
    "delayed_suppliers": "Supplier A delayed", "large_orders": "",
    "discontinue": "", "other": "",
}, follow_redirects=False)
_check("POST redirects onward", r2.status_code == 302)
row = db.query("SELECT context_json, column_map_json FROM upload_sessions WHERE id=?",
               (SID_ROUTE,))[0]
_check("context saved", "Supplier A delayed" in (row["context_json"] or ""))
saved_map = json.loads(row["column_map_json"]) if row["column_map_json"] else {}
_check("auto-saved mapping survives the POST untouched (form can't override it)",
       saved_map.get("stock") == "warehouse_count", detail=str(saved_map))
shared._call_claude = _fake_call_claude  # restore


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll column-mapping tests passed.")
