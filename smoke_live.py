"""Live smoke test: run the REAL agent pipeline (real Claude API) on the messy
dummy CSVs in sample_uploads/, on a throwaway DB. Proves a change works
end-to-end before it ships — the stubbed test suite can't catch model-facing
regressions (prompt drift, reply-shape changes, hallucinated quantities).

Costs real API money (~US$0.10-0.40 per run on sonnet). Run after major
changes to agents/, app.py, or database.py — not on every save.

Run: python smoke_live.py
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# Throwaway DB — set before any project import.
_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_smoke_live.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)

# The key lives in the Windows user profile, which a shell started before it
# was set won't have inherited — read it from the registry as a fallback.
if not os.environ.get("ANTHROPIC_API_KEY") and sys.platform == "win32":
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            os.environ["ANTHROPIC_API_KEY"], _ = winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")
    except OSError:
        pass
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("FAIL: ANTHROPIC_API_KEY not set (env or HKCU\\Environment)")
    sys.exit(1)

import database as db                              # noqa: E402
from agents import run_normalization_agent, run_pipeline  # noqa: E402

MODEL = "claude-sonnet-5"   # what the pilot org actually uses
SAMPLES = os.path.join(ROOT, "sample_uploads")
_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _emit(msg, agent=None):
    print(f"   .. {msg}")


t0 = time.time()
db.init_db()
SID = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?)", (1, "SmokeOrg", "uploading", "all", "{}"))

for slot in ("inventory", "sales", "purchase_orders", "suppliers"):
    path = os.path.join(SAMPLES, f"{slot}_messy.csv")
    if not os.path.exists(path):
        print(f"FAIL: missing dummy file {path}")
        sys.exit(1)
    db.excel_to_sqlite(path, slot, SID)
    n = db.query(f'SELECT COUNT(*) AS n FROM "{slot}_{SID}"')[0]["n"]
    print(f"ingested {slot}: {n} rows")
    _check(f"{slot} ingested rows > 0", n > 0, detail=str(n))

print("\n-- normalization agent (real Claude) --")
norm = run_normalization_agent(SID, MODEL, progress_emit=_emit)
_check("normalization returned groups list", isinstance(norm.get("groups"), list),
       detail=str(norm.get("message")))
print(f"   groups proposed: {len(norm.get('groups') or [])}")

print("\n-- inventory + recommendation pipeline (real Claude) --")
result = run_pipeline(SID, MODEL, norm.get("groups") or [], {}, emit=_emit)

_check("pipeline returned no error", "error" not in result, detail=str(result.get("error")))
if "error" in result:
    print("\nSMOKE FAILED")
    sys.exit(1)

report = result["inventory_report"]
recs = result["recommendations"]
_check("inventory report non-empty", isinstance(report, list) and len(report) > 0,
       detail=str(type(report)))
_check("recommendations non-empty", isinstance(recs, list) and len(recs) > 0,
       detail=str(type(recs)))
_check("no rec error dicts", not any("error" in r and "item" not in r for r in recs if isinstance(r, dict)))

# Sanity on quantities: the messy sample sells tens-per-month — five digits is
# a hallucination the guard should have caught.
_bad_qty = [r.get("item") for r in recs if isinstance(r, dict)
            and str(r.get("suggested_quantity", "")).strip().split(" ")[0].replace(",", "").isdigit()
            and int(str(r.get("suggested_quantity")).strip().split(" ")[0].replace(",", "")) > 10000]
_check("no absurd suggested quantities (>10000)", not _bad_qty, detail=str(_bad_qty))

_status = {}
for r in report:
    if isinstance(r, dict):
        _status[r.get("status", "?")] = _status.get(r.get("status", "?"), 0) + 1
print(f"\nstatus breakdown: {_status}")
print(f"recommendations: {len(recs)}")
for r in recs[:3]:
    if isinstance(r, dict):
        print(f"  - {r.get('item')} | {r.get('recommended_action')} | "
              f"qty {r.get('suggested_quantity')} | {r.get('confidence')}")

# Saved sales links, switched on, through the real model. The messy sample has
# no item-code column, so links can only be exercised on a table of their own.
print("\n-- sales links: seeded links, switched on (real Claude) --")
from agents.sales_links import make_entry             # noqa: E402
from agents.shared import normalise_match_key         # noqa: E402
from quantity import parse_quantity                   # noqa: E402
from rec_logic import LINK_UNSURE_FLAG                # noqa: E402

LINKS_ORG = "SmokeLinksOrg"
SPAG_LINE = "SPAGHETTI 500G BROOKVALE/NORDVIK"
NECTAR = "PADIMAS ORANGE NECTAR 1L"
SID2 = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?)", (1, LINKS_ORG, "uploading", "all", "{}"))
db.execute(f"CREATE TABLE inventory_{SID2} (inventory_code TEXT, location_code TEXT, "
           "description TEXT, uom TEXT, qty_on_hand TEXT, free_balance TEXT)")
_stock = [("BRK-SP500", "BROOKVALE SPAGHETTI 500G", "PKT", "0", "0"),
          ("NRD-SP500", "NORDVIK SPAGHETTI 500G", "PKT", "400", "400"),
          ("PDM-ON1L", NECTAR, "PKT", "50", "50"),
          ("PDM-OJ1L", "PADIMAS ORANGE JUICE 100% 1L", "PKT", "80", "80"),
          ("KES-CD330", "KESSINGTON COCONUT DRINK 330ML", "CTN", "0", "0")]
for code, desc, uom, on_hand, free in _stock:
    db.execute(f"INSERT INTO inventory_{SID2} VALUES (?,?,?,?,?,?)",
               (code, "WAREHOUSE", desc, uom, on_hand, free))
db.execute(f"CREATE TABLE sales_{SID2} (date TEXT, item_description TEXT, qty_sold TEXT, supplier TEXT)")
for line, monthly in ((SPAG_LINE, 300), ("ORANGE NECTAR 1L", 40), ("COCONUT DRINK 330ML", 20)):
    for month in (6, 7, 8):
        db.execute(f"INSERT INTO sales_{SID2} VALUES (?,?,?,?)",
                   (f"2026-{month:02}-15", line, str(monthly), ""))
_names = {code: desc for code, desc, *_ in _stock}
_seeded = [make_entry(SPAG_LINE, [{"code": c, "name": _names[c]} for c in ("BRK-SP500", "NRD-SP500")],
                      "high", "admin"),
           make_entry("ORANGE NECTAR 1L", [{"code": "PDM-ON1L", "name": NECTAR}], "medium", "ai",
                      model="claude-sonnet-5"),
           make_entry("COCONUT DRINK 330ML", [{"code": "KES-CD330", "name": _names["KES-CD330"]}],
                      "high", "admin")]


def _seed_links(lines):
    for entry in _seeded:
        lines[normalise_match_key(entry["line"])] = entry
    return True


db.update_sales_links(LINKS_ORG, _seed_links, "smoke")
db.set_sales_links_enabled(LINKS_ORG, True, "smoke")
result2 = run_pipeline(SID2, MODEL, [], {}, emit=_emit)
_check("links run returned no error", "error" not in result2, detail=str(result2.get("error")))
report2 = result2.get("inventory_report") or []
recs2 = [r for r in result2.get("recommendations") or [] if isinstance(r, dict)]
_keys = [normalise_match_key(r.get("item", "")) for r in report2 if isinstance(r, dict)]
_check("one report row for the spaghetti line, none per brand",
       _keys.count(normalise_match_key(SPAG_LINE)) == 1
       and normalise_match_key("BROOKVALE SPAGHETTI 500G") not in _keys
       and normalise_match_key("NORDVIK SPAGHETTI 500G") not in _keys, detail=str(_keys))
_spag = [r for r in recs2 if normalise_match_key(r.get("item", "")) == normalise_match_key(SPAG_LINE)]
_check("spaghetti family has an order", bool(_spag), detail=str([r.get("item") for r in recs2]))
for r in _spag:
    _link = r.get("sales_link") if isinstance(r.get("sales_link"), dict) else {}
    _check("spaghetti order carries a two-item link", len(_link.get("members") or []) == 2,
           detail=str(_link))
    _calc = r.get("order_calc") if isinstance(r.get("order_calc"), dict) else {}
    if _calc.get("state") == "order":
        _check("spaghetti quantity is Python's order figure",
               parse_quantity(r.get("suggested_quantity")) == _calc.get("order"),
               detail=f"{r.get('suggested_quantity')} vs {_calc.get('order')}")
_nectar = [r for r in recs2 if normalise_match_key(r.get("item", "")) == normalise_match_key(NECTAR)]
_check("nectar item has an order", bool(_nectar), detail=str([r.get("item") for r in recs2]))
for r in _nectar:
    _check("unsure nectar link warns first", (r.get("flags") or [None])[0] == LINK_UNSURE_FLAG,
           detail=str(r.get("flags")))
_bad_qty2 = [r.get("item") for r in recs2
             if (parse_quantity(r.get("suggested_quantity")) or 0) > 10000]
_check("links run has no quantity above 10000", not _bad_qty2, detail=str(_bad_qty2))
print(f"   link notes: {result2.get('link_notes') or []}")
for r in recs2:
    print(f"  - {r.get('item')} | qty {r.get('suggested_quantity')} | flags {(r.get('flags') or [])[:1]}")
print(f"\nelapsed: {time.time() - t0:.0f}s")

if _FAILED:
    print("\nSMOKE FAILED")
    sys.exit(1)
print("\nSMOKE PASSED — real pipeline healthy on dummy data.")
