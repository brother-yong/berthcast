"""Findings-ticker count helpers (orchestrator.inventory_findings / recommendation_findings).

These feed the live "Findings so far" ticker on the analysis progress page. They
are pure functions over the inventory report and the recommendations list, so a
wrong count — or a crash on odd input — would show the user wrong numbers, or
break the page, while their data is processing. Lock the counting rules down.

Dependency-free: run with `python tests/test_findings_stats.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="berth_findings_"), "t.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.orchestrator import inventory_findings, recommendation_findings

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        _FAILED = True


# ── inventory report → below_safe / critical / spoilage ───────────────────────
report = [
    {"status": "CRITICAL", "spoilage_risk": "HIGH"},
    {"status": "CRITICAL", "spoilage_risk": None},
    {"status": "LOW",      "spoilage_risk": "MEDIUM"},
    {"status": "HEALTHY",  "spoilage_risk": "LOW"},
    {"status": "DEAD",     "spoilage_risk": None},
    "not a dict",   # malformed row must be ignored, not crash
]
inv = inventory_findings(report)
_check("below_safe = LOW + CRITICAL (3)", inv["below_safe"] == 3, inv)
_check("critical = CRITICAL only (2)",     inv["critical"] == 2, inv)
_check("spoilage = HIGH + MEDIUM (2)",     inv["spoilage"] == 2, inv)

# ── recommendations → recs / supplier_risk ───────────────────────────────────
recs = [
    {"recommended_action": "REORDER", "supplier_risk": "HIGH"},
    {"recommended_action": "HOLD",    "supplier_risk": "LOW"},
    {"recommended_action": "REORDER", "supplier_risk": "HIGH"},
    {"error": "bad batch"},   # error stub must be ignored
]
rec = recommendation_findings(recs)
_check("recs = valid rows only (3)",       rec["recs"] == 3, rec)
_check("supplier_risk = HIGH only (2)",    rec["supplier_risk"] == 2, rec)

# ── odd inputs never crash (page must never break on a bad report) ────────────
_check("inventory_findings(None) -> {}",      inventory_findings(None) == {})
_check("recommendation_findings(None) -> {}", recommendation_findings(None) == {})
_check("inventory_findings([]) all zero",
       inventory_findings([]) == {"below_safe": 0, "critical": 0, "spoilage": 0})
_check("recommendation_findings([]) all zero",
       recommendation_findings([]) == {"recs": 0, "supplier_risk": 0})

sys.exit(1 if _FAILED else 0)
