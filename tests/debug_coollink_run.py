"""One-off diagnostic: replay Cool Link's real files through the real ingestion
and inventory-agent path to find why a zero-stock item with sales produced no
recommendation. Reads files locally; prints only masked/aggregate evidence.

Run: python tests/debug_coollink_run.py "<inventory.xlsx>" "<sales.xlsx>"
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berth_coollink_debug.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _A:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _A
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db          # noqa: E402
import agents.inventory as inv_mod   # noqa: E402

INV_PATH, SAL_PATH = sys.argv[1], sys.argv[2]
SID = 901

db.init_db()
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "CoolLinkDebug", "uploading", "all", "{}"))

print("=== INGESTION ===")
r1 = db._xlsx_to_sqlite(INV_PATH, "inventory", SID)
r2 = db._xlsx_to_sqlite(SAL_PATH, "sales", SID)
print("inventory ingest:", {k: v for k, v in r1.items() if k != "error"} if r1.get("ok") else r1)
print("sales ingest:    ", {k: v for k, v in r2.items() if k != "error"} if r2.get("ok") else r2)

inv_cols = list(db.query(f"SELECT * FROM inventory_{SID} LIMIT 1")[0].keys())
sal_cols = list(db.query(f"SELECT * FROM sales_{SID} LIMIT 1")[0].keys())
print("inventory columns:", inv_cols)
print("sales columns:    ", sal_cols)
print("inventory rows:", db.query(f"SELECT COUNT(*) n FROM inventory_{SID}")[0]["n"])
print("sales rows:    ", db.query(f"SELECT COUNT(*) n FROM sales_{SID}")[0]["n"])


def _mask(s, keep=10):
    s = str(s)
    return (s[:keep] + "~" * max(0, min(6, len(s) - keep))) if len(s) > keep else s


print("\n=== NAME OVERLAP (sales vs inventory, lower/strip) ===")
# Use the same desc-col guesses the agent uses
def _pick_desc(cols):
    c = next((c for c in cols if c in ("inventory_desc", "item_description", "description", "product_name")), None)
    if not c:
        c = next((c for c in cols if any(k in c.lower() for k in ("desc", "item_name", "product_name", "item")) and "supplier" not in c.lower()), None)
    return c


inv_desc = _pick_desc(inv_cols)
sal_desc = _pick_desc(sal_cols)
print("inventory desc col guess:", inv_desc, "| sales desc col guess:", sal_desc)

inv_names = {str(r["d"] or "").strip().lower()
             for r in db.query(f'SELECT DISTINCT "{inv_desc}" d FROM inventory_{SID}')} - {""}
sal_names = {str(r["d"] or "").strip().lower()
             for r in db.query(f'SELECT DISTINCT "{sal_desc}" d FROM sales_{SID}')} - {""}
print(f"distinct inventory names: {len(inv_names)} | distinct sales names: {len(sal_names)}")
matched = sal_names & inv_names
print(f"sales names matching inventory exactly (ci): {len(matched)}/{len(sal_names)}")
for s in sorted(sal_names - matched)[:10]:
    print("  unmatched sales name:", _mask(s, 14))

print("\n=== FULL AGENT RUN (Claude stubbed) ===")
captured = {"system": None, "user": None}


def _fake_claude(model, system, user, max_tokens=4096):
    captured["system"] = system
    captured["user"] = user
    return "[]"


inv_mod._call_claude = _fake_claude
log = []
result = inv_mod.run_inventory_agent(SID, "stub-model", [], {}, progress_emit=log.append)

print("--- agent progress log ---")
for line in log:
    print(" ", line)
if isinstance(result, dict) and result.get("error"):
    print("--- agent returned error:", _mask(result["error"], 120))

if captured["user"]:
    lines = [l for l in captured["user"].splitlines() if l.startswith("Item:")]
    sold_zero = [l for l in lines if "Total sold" in l and ": 0" in l.split("Total sold")[1][:12]]
    stock_zero = [l for l in lines if "| Stock: 0 " in l or "| Stock: 0|" in l or "| Stock: 0\t" in l or l.rstrip().endswith("| Stock: 0")]
    both = [l for l in lines if l in sold_zero and ("Stock: 0 " in l or "Stock: 0|" in l)]
    print(f"\nitem lines sent to Claude: {len(lines)}")
    print(f"lines with Total sold = 0: {len(sold_zero)}")
    print(f"lines with sold > 0:       {len(lines) - len(sold_zero)}")
    # the case we care about: zero stock items — what does their line look like?
    zs = [l for l in lines if "Stock: 0 |" in l or "Stock: 0.0 |" in l]
    print(f"zero-stock item lines: {len(zs)}")
    for l in zs[:6]:
        # mask the item name, keep the numeric story
        body = l.split("|", 1)[1] if "|" in l else l
        print("  zero-stock:", "Item: ###", "|", body.strip())
else:
    print("\n(no prompt captured — agent failed before reaching Claude)")
