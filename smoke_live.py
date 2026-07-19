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
print(f"\nelapsed: {time.time() - t0:.0f}s")

if _FAILED:
    print("\nSMOKE FAILED")
    sys.exit(1)
print("\nSMOKE PASSED — real pipeline healthy on dummy data.")
