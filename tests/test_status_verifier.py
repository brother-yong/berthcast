"""Status verifier (agents/verifier.py) — deterministic safety net for the
inventory health report.

The prompt's status rules are arithmetic on numbers WE computed (months of
supply, lead time, stock, total sold). Claude applies them across hundreds of
lines and can slip; before this, a wrong label shipped straight to the results
page and into the recommendation agent. The verifier recomputes the same rules
from the same (rounded, as-printed) inputs and corrects provable slips only —
judgment calls (DEAD vs new/seasonal on real zero-sales) stay Claude's.

Run: python tests/test_status_verifier.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="berth_verifier_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                       # noqa: E402
import agents.shared as shared              # noqa: E402
import agents.inventory as inv_mod          # noqa: E402
from agents.verifier import expected_status, verify_inventory_report  # noqa: E402
from agents.shared import normalise_match_key  # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. expected_status: the prompt's rules, verbatim ──────────────────────────
# Lead-time-aware thresholds (LT = 3.7 months).
_check("supply < LT -> CRITICAL",        expected_status(2.0, 3.7, 100, 500) == "CRITICAL")
_check("supply = LT -> LOW",             expected_status(3.7, 3.7, 100, 500) == "LOW")
_check("supply = LT+2 -> LOW",           expected_status(5.7, 3.7, 100, 500) == "LOW")
_check("supply just past LT+2 -> HEALTHY", expected_status(5.8, 3.7, 100, 500) == "HEALTHY")
# Fixed thresholds when no lead time.
_check("no LT: supply 0.9 -> CRITICAL",  expected_status(0.9, None, 100, 500) == "CRITICAL")
_check("no LT: supply 1.0 -> LOW",       expected_status(1.0, None, 100, 500) == "LOW")
_check("no LT: supply 3.0 -> LOW",       expected_status(3.0, None, 100, 500) == "LOW")
_check("no LT: supply 3.1 -> HEALTHY",   expected_status(3.1, None, 100, 500) == "HEALTHY")
# Mandated overrides.
_check("sold > 0 and stock = 0 -> CRITICAL, ignores supply",
       expected_status(None, None, 0, 500) == "CRITICAL")
_check("no sales data + stock 0 -> DEAD", expected_status(None, None, 0, None) == "DEAD")
_check("no sales data + stock > 0 -> HEALTHY", expected_status(None, None, 5, None) == "HEALTHY")
# Where the rules leave judgment to Claude: no expectation.
_check("no sales data + negative stock -> None (rules silent)",
       expected_status(None, None, -1, None) is None)
_check("zero sold with data, no velocity -> None (DEAD vs seasonal is judgment)",
       expected_status(None, None, 100, 0) is None)


# ── 2. verify_inventory_report: corrects only what's provable ─────────────────
def _inputs(**by_name):
    return {normalise_match_key(k): v for k, v in by_name.items()}


report = [
    {"item": "WRONG HEALTHY", "status": "HEALTHY", "spoilage_risk": "LOW",
     "days_of_supply": 15},                                    # supply 0.5 -> CRITICAL
    {"item": "RIGHT LOW", "status": "LOW", "spoilage_risk": "LOW",
     "days_of_supply": 60},                                    # supply 2.0, dos exact
    {"item": "DEAD WITH SALES", "status": "DEAD", "spoilage_risk": "NONE",
     "days_of_supply": 0},                                     # sold 500, stock 0 -> CRITICAL
    {"item": "LEGIT DEAD", "status": "DEAD", "spoilage_risk": "HIGH",
     "days_of_supply": 0},                                     # data says 0 sold: Claude's call
    {"item": "NOT IN MAP", "status": "HEALTHY"},               # unknown: untouched
    "not a dict",                                              # ignored
]
n_st, n_dos, n_sp = verify_inventory_report(report, _inputs(**{
    "WRONG HEALTHY":   {"months_supply": 0.5, "lt_months": None, "stock": 100, "total_sold": 2400},
    "RIGHT LOW":       {"months_supply": 2.0, "lt_months": None, "stock": 200, "total_sold": 1200},
    "DEAD WITH SALES": {"months_supply": 0.0, "lt_months": None, "stock": 0,   "total_sold": 500},
    "LEGIT DEAD":      {"months_supply": None, "lt_months": None, "stock": 80, "total_sold": 0},
}))
_by = {r["item"]: r for r in report if isinstance(r, dict)}
_check("wrong HEALTHY corrected to CRITICAL",
       _by["WRONG HEALTHY"]["status"] == "CRITICAL", detail=str(_by["WRONG HEALTHY"]))
_check("correction is marked", _by["WRONG HEALTHY"].get("_status_corrected") is True)
_check("correct LOW left alone (no flag)",
       _by["RIGHT LOW"]["status"] == "LOW" and "_status_corrected" not in _by["RIGHT LOW"])
_check("DEAD-with-sales corrected to CRITICAL (the 12-June error class)",
       _by["DEAD WITH SALES"]["status"] == "CRITICAL")
_check("legitimate DEAD (data shows 0 sold) untouched",
       _by["LEGIT DEAD"]["status"] == "DEAD")
_check("legitimate DEAD gets spoilage forced to NONE (prompt mandate)",
       _by["LEGIT DEAD"]["spoilage_risk"] == "NONE")
_check("item not in inputs map untouched", _by["NOT IN MAP"]["status"] == "HEALTHY")
_check("status fix count = 2", n_st == 2, detail=str((n_st, n_dos, n_sp)))
_check("spoilage fix count = 1", n_sp == 1, detail=str((n_st, n_dos, n_sp)))

# days_of_supply: exact/close figures stay; wild ones are recomputed.
_check("dos exactly supply*30 untouched", _by["RIGHT LOW"]["days_of_supply"] == 60)
r2 = [{"item": "A", "status": "LOW", "spoilage_risk": "LOW", "days_of_supply": 999},
      {"item": "B", "status": "LOW", "spoilage_risk": "LOW", "days_of_supply": 60},
      {"item": "C", "status": "LOW", "spoilage_risk": "LOW"}]
n_st2, n_dos2, _ = verify_inventory_report(r2, _inputs(
    A={"months_supply": 2.0, "lt_months": None, "stock": 1, "total_sold": 1},
    B={"months_supply": 2.1, "lt_months": None, "stock": 1, "total_sold": 1},
    C={"months_supply": 2.0, "lt_months": None, "stock": 1, "total_sold": 1},
))
_check("dos 999 vs 60 recomputed to 60", r2[0]["days_of_supply"] == 60, detail=str(r2[0]))
_check("dos 60 vs 63 within tolerance, untouched", r2[1]["days_of_supply"] == 60)
_check("missing dos filled in", r2[2]["days_of_supply"] == 60)
_check("dos fix count = 2", n_dos2 == 2, detail=str(n_dos2))


# ── 3. Integration: a slipping Claude gets corrected inside the agent ─────────
db.init_db()
SID = 61
db.execute("INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
           "VALUES (?,?,?,?,?,?)", (SID, 1, "VerifyCo", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{SID} ("description" TEXT, "qty_on_hand" TEXT, "uom" TEXT)')
for r in [
    ("EDAM CHEESE WHEEL", "300",  "KG"),   # 300/mo velocity -> 1.0 mo supply -> LOW
    ("GOUDA BLOCK",       "2000", "KG"),   # 6.7 mo supply -> HEALTHY
    ("BUTTER PKT",        "0",    "PKT"),  # sold>0, stock 0 -> CRITICAL mandated
    ("OLD DISPLAY STAND", "40",   "PCS"),  # no sales data, stock>0 -> HEALTHY mandated
]:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?)", r)
db.execute(f'CREATE TABLE sales_{SID} ("item_name" TEXT, "qty" TEXT, "date" TEXT)')
for r in [
    ("EDAM CHEESE WHEEL", "300", "2026-05-05"),
    ("EDAM CHEESE WHEEL", "300", "2026-06-05"),
    ("GOUDA BLOCK",       "300", "2026-05-05"),
    ("GOUDA BLOCK",       "300", "2026-06-05"),
    ("BUTTER PKT",        "300", "2026-05-05"),
    ("BUTTER PKT",        "300", "2026-06-05"),
]:
    db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?)", r)

# A Claude that slips: wrong label on EDAM, absurd days_of_supply, and the
# 12-June error class on BUTTER and the display stand.
_WRONG = {
    "EDAM CHEESE WHEEL": ("HEALTHY", 999),
    "GOUDA BLOCK":       ("HEALTHY", 200),   # correct, dos within tolerance of 201
    "BUTTER PKT":        ("DEAD", 0),
    "OLD DISPLAY STAND": ("DEAD", 0),
}


def _slipping_claude(model, system, user, max_tokens=4096):
    items = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            name = line.split("|")[0].replace("Item:", "").strip()
            st, dos = _WRONG.get(name, ("HEALTHY", 0))
            items.append({"item": name, "category": "X", "stock": 0, "status": st,
                          "spoilage_risk": "HIGH", "days_of_supply": dos,
                          "observation": "t"})
    return json.dumps(items)


inv_mod._call_claude = _slipping_claude
shared._call_claude = lambda *a, **k: "{}"  # column mapper: keyword fallback
_log = []
res = inv_mod.run_inventory_agent(SID, "m", [], {}, progress_emit=_log.append)
_rep = {r["item"]: r for r in (res.get("report") or []) if isinstance(r, dict)}

_check("agent run completes", "report" in res, detail=str(res)[:200])
_check("EDAM: wrong HEALTHY corrected to LOW (1.0 mo supply)",
       _rep.get("EDAM CHEESE WHEEL", {}).get("status") == "LOW",
       detail=str(_rep.get("EDAM CHEESE WHEEL")))
_check("EDAM: absurd 999 days of supply recomputed to 30",
       _rep.get("EDAM CHEESE WHEEL", {}).get("days_of_supply") == 30)
_check("GOUDA: correct answer left alone",
       _rep.get("GOUDA BLOCK", {}).get("status") == "HEALTHY"
       and _rep.get("GOUDA BLOCK", {}).get("days_of_supply") == 200)
_check("BUTTER: DEAD-with-sales corrected to CRITICAL",
       _rep.get("BUTTER PKT", {}).get("status") == "CRITICAL",
       detail=str(_rep.get("BUTTER PKT")))
_check("display stand: no-data DEAD corrected to HEALTHY (never dead on missing data)",
       _rep.get("OLD DISPLAY STAND", {}).get("status") == "HEALTHY",
       detail=str(_rep.get("OLD DISPLAY STAND")))
_check("safety-check correction announced in progress",
       any("Safety check: corrected" in l for l in _log), detail=str(_log[-6:]))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll status-verifier tests passed.")
