"""Clarity box gap counting (rec_logic.clarity_gaps).

Feeds the "Add these to sharpen your results" box at the top of the results
page. Wrong counts = wrong instructions to the user about what data to add, so
pin the counting rules: null lead times, unknown suppliers, un-sizeable
quantities — and that user edits resolve a gap for that item.

Dependency-free: run with `python tests/test_clarity_gaps.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="berth_clarity_"), "t.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rec_logic import clarity_gaps

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        _FAILED = True


def _by_label(gaps):
    return {g["label"]: g["count"] for g in gaps}


# ── each gap counted from the right signal ────────────────────────────────────
recs = [
    {"item": "A", "lead_time_days": None, "supplier": "Fresh Foods",
     "suggested_quantity": "120 units"},                       # lead-time gap only
    {"item": "B", "lead_time_days": 21,   "supplier": "Unknown",
     "suggested_quantity": "Verify with team"},                # supplier + qty gaps
    {"item": "C", "lead_time_days": 14,   "supplier": "Good Co",
     "suggested_quantity": "50 units"},                        # clean
    {"item": "D", "lead_time_days": "",   "supplier": "",
     "suggested_quantity": "Verify with team"},                # all three gaps
    {"error": "bad batch"},                                    # ignored
    "not a dict",                                              # ignored
]
g = _by_label(clarity_gaps(recs))
_check("lead-time gap counts null/empty (2)", g.get("no supplier lead time") == 2, g)
_check("unknown supplier counts Unknown/blank (2)", g.get("unrecognised supplier") == 2, g)
_check("qty gap counts 'Verify with team' (2)",
       g.get("not enough sales data to size the order") == 2, g)

# ── user edits resolve the gap for that item ──────────────────────────────────
edited = [
    {"item": "B", "lead_time_days": 21, "supplier": "Unknown",
     "edited_supplier": "Real Supplier Pte",
     "suggested_quantity": "Verify with team", "edited_quantity": "80 units"},
]
_check("edited supplier + quantity clear both gaps", clarity_gaps(edited) == [],
       clarity_gaps(edited))

# ── clean data and odd inputs ─────────────────────────────────────────────────
clean = [{"item": "C", "lead_time_days": 14, "supplier": "Good Co",
          "suggested_quantity": "50 units"}]
_check("clean recs -> no gaps", clarity_gaps(clean) == [])
_check("empty list -> no gaps", clarity_gaps([]) == [])
_check("error-only list -> no gaps", clarity_gaps([{"error": "x"}]) == [])
_check("supplier case-insensitive ('unknown')",
       _by_label(clarity_gaps([{"item": "E", "lead_time_days": 5,
                                "supplier": "UNKNOWN",
                                "suggested_quantity": "5 units"}])
                 ).get("unrecognised supplier") == 1)

# ── sales-pattern warnings surface as a counted gap (spec 2026-07-10) ────────
recs_pat = [
    {"item": "P1", "lead_time_days": 7, "supplier": "Good Co",
     "suggested_quantity": "10 units", "sales_pattern": "spiky"},
    {"item": "P2", "lead_time_days": 7, "supplier": "Good Co",
     "suggested_quantity": "10 units", "sales_pattern": "volatile"},
    {"item": "P3", "lead_time_days": 7, "supplier": "Good Co",
     "suggested_quantity": "10 units"},  # stable / no pattern
]
_check("unusual sales patterns counted (2)",
       _by_label(clarity_gaps(recs_pat)).get("unusual sales pattern") == 2,
       clarity_gaps(recs_pat))
_check("no pattern field -> no pattern gap",
       "unusual sales pattern" not in _by_label(clarity_gaps(recs_pat[2:])))

sys.exit(1 if _FAILED else 0)
