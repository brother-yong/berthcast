"""Safety net for the agents refactor (single file -> agents/ package + orchestrator).

What this proves:
  The three agents do a lot of *deterministic* work in plain Python BEFORE and
  AROUND the call to Claude — detecting spreadsheet columns, computing sales
  velocity, resolving suppliers/lead times, sizing order quantities, parsing the
  JSON reply. This test fakes Claude's reply (so it costs nothing and needs no
  network/API key) and runs the agents against a tiny throwaway SQLite database.
  If the refactor drops a helper, breaks an import, or changes any of that wiring,
  these assertions fail.

Run it on the OLD code and the NEW code — it must pass identically both times.
That is the proof the move changed nothing.

Dependency-free: run with `python tests/test_agents_refactor.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys
import json
import importlib
import tempfile

# ── Point everything at a throwaway DB + dummy key BEFORE importing app modules.
#    database.py reads DB_PATH at import time, so this must happen first.
_TMPDIR = tempfile.mkdtemp(prefix="berth_agents_test_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

# Make the project root importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import agents


# ---------------------------------------------------------------------------
# Tiny test helpers (same style as the other tests in this folder)
# ---------------------------------------------------------------------------
_FAILED = False


def _check(name, cond):
    global _FAILED
    if cond:
        print(f"ok: {name}")
    else:
        print(f"FAIL: {name}")
        _FAILED = True


def _resolve_module(attr):
    """Find whichever module currently holds an internal helper, so the test
    works before the refactor (helpers live in `agents`) and after (they live
    in `agents.shared`, optionally re-exported from `agents`)."""
    for name in ("agents.shared", "agents"):
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if hasattr(mod, attr):
            return mod
    raise AssertionError(f"could not find helper {attr!r} in agents or agents.shared")


def _patch_call_claude(fake):
    """Replace `_call_claude` on every agent module that exposes it. This lands
    the fake regardless of how the refactored package imports the name."""
    for name in ("agents", "agents.shared", "agents.normalization",
                 "agents.inventory", "agents.recommendation", "agents.orchestrator"):
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if hasattr(mod, "_call_claude"):
            mod._call_claude = fake


# ---------------------------------------------------------------------------
# Fake Claude — returns canned JSON based on which agent's system prompt it sees
# ---------------------------------------------------------------------------
_INVENTORY_REPLY = [
    # Note: bread is canned as LOW but 5 units at 40/month is ~4 days of stock —
    # the status verifier corrects it to CRITICAL after the reply is parsed.
    # (No assertion pins bread's status; salmon and the dead item are pinned.)
    {"item": "White Bread 400g", "category": "BREAD", "stock": "5",
     "status": "LOW", "spoilage_risk": "LOW", "days_of_supply": 7,
     "observation": "Running low."},
    {"item": "Frozen Salmon", "category": "FROZEN", "stock": "0",
     "status": "CRITICAL", "spoilage_risk": "MEDIUM", "days_of_supply": 0,
     "observation": "Out of stock."},
    {"item": "Old Stock Item", "category": "DRY", "stock": "100",
     "status": "DEAD", "spoilage_risk": "NONE", "days_of_supply": 999,
     "observation": "No sales on record."},
]

_RECOMMENDATION_REPLY = [
    {"item": "White Bread 400g", "supplier": "Local SG", "supplier_type": "local",
     "lead_time_days": 21, "days_of_supply": 7, "recommended_action": "REORDER",
     "suggested_quantity": "80 PCS", "confidence": "HIGH",
     "consequence_if_acting": "Ordering ties up some cash in bread.",
     "consequence_if_not_acting": "TestCo runs out of bread within the week.",
     "supplier_risk": "None", "mitigation": "", "flags": [], "reason": "Low stock, reliable supplier."},
    {"item": "Frozen Salmon", "supplier": "Import Other", "supplier_type": "import",
     "lead_time_days": 56, "days_of_supply": 0, "recommended_action": "REORDER",
     "suggested_quantity": "30 CTN", "confidence": "MEDIUM",
     "consequence_if_acting": "Frozen salmon locks up cold-storage space.",
     "consequence_if_not_acting": "TestCo cannot fill salmon orders.",
     "supplier_risk": "None", "mitigation": "", "flags": [], "reason": "Out of stock, long lead time."},
]


def _fake_call_claude(model, system, user, max_tokens=4096):
    s = (system or "").lower()
    if "normalisation specialist" in s:
        return json.dumps([{"canonical": "White Bread 400g", "variants": ["WHT BRD 400G"]}])
    if "inventory health analyst" in s:
        return json.dumps(_INVENTORY_REPLY)
    if "purchasing advisor" in s:
        return json.dumps(_RECOMMENDATION_REPLY)
    return "[]"


# ---------------------------------------------------------------------------
# Build a tiny, realistic dataset for session_id = 1, org = TestCo
# ---------------------------------------------------------------------------
def _build_fixture():
    db.init_db()
    sid = 1

    db.execute(
        "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
        "VALUES (?,?,?,?,?,?)",
        (sid, 1, "TestCo", "complete", "all", "{}"),
    )

    # Inventory: name / quantity / category / unit-of-measure (+ inert extra col)
    db.execute('CREATE TABLE inventory_1 ("description" TEXT, "qty_on_hand" TEXT, '
               '"category" TEXT, "uom" TEXT, "_session_id" TEXT)')
    for row in [
        ("White Bread 400g", "5",   "BREAD",  "PCS", "1"),
        ("Frozen Salmon",    "0",   "FROZEN", "CTN", "1"),
        ("Old Stock Item",   "100", "DRY",    "PCS", "1"),
    ]:
        db.execute("INSERT INTO inventory_1 VALUES (?,?,?,?,?)", row)

    # Sales: two months of bread + one salmon sale (drives velocity + month count).
    # Old Stock Item gets a zero-qty line so the sales data COVERS it and shows
    # 0 sold — legitimate DEAD evidence. (With no row at all, "DEAD" would be
    # the marked-dead-on-missing-data error the status verifier now corrects.)
    db.execute('CREATE TABLE sales_1 ("description" TEXT, "qty" TEXT, '
               '"net_amount" TEXT, "date" TEXT, "_session_id" TEXT)')
    for row in [
        ("White Bread 400g", "40", "200", "2026-01-15", "1"),
        ("White Bread 400g", "40", "200", "2026-02-15", "1"),
        ("Frozen Salmon",    "10", "500", "2026-01-20", "1"),
        ("Old Stock Item",   "0",  "0",   "2026-01-10", "1"),
    ]:
        db.execute("INSERT INTO sales_1 VALUES (?,?,?,?,?)", row)

    # Purchase orders: item -> supplier
    db.execute('CREATE TABLE purchase_orders_1 ("inventory_desc" TEXT, '
               '"supplier_name" TEXT, "_session_id" TEXT)')
    for row in [
        ("White Bread 400g", "Local SG",     "1"),
        ("Frozen Salmon",    "Import Other", "1"),
    ]:
        db.execute("INSERT INTO purchase_orders_1 VALUES (?,?,?)", row)

    # Supplier listing: local vs import
    db.execute('CREATE TABLE suppliers_1 ("supplier_name" TEXT, '
               '"supplier_type" TEXT, "_session_id" TEXT)')
    for row in [
        ("Local SG",     "local",  "1"),
        ("Import Other", "import", "1"),
    ]:
        db.execute("INSERT INTO suppliers_1 VALUES (?,?,?)", row)

    # Supplier profiles (lead times / reliability)
    db.upsert_supplier_profile("TestCo", "Local SG",
        delay_probability=0.10, avg_lead_time_days=21, data_quality_score=0.9, notes="Reliable local")
    db.upsert_supplier_profile("TestCo", "Import Other",
        delay_probability=0.25, avg_lead_time_days=56, data_quality_score=0.5, notes="Moderate import")


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------
def _test_pure_helpers():
    """Deterministic helpers — no DB, no Claude. Same result before/after a faithful move."""
    extract = _resolve_module("_extract_json_array")._extract_json_array
    fmt     = _resolve_module("_format_context")._format_context
    infer   = _resolve_module("_infer_supplier_type")._infer_supplier_type

    parsed, repaired = extract('[{"a": 1}]')
    _check("extract_json: plain array parses", parsed == [{"a": 1}] and repaired is False)

    parsed, _ = extract('```json\n[{"a": 1}]\n```')
    _check("extract_json: fenced code block parses", parsed == [{"a": 1}])

    parsed, _ = extract('not json at all')
    _check("extract_json: junk returns None", parsed is None)

    _check("format_context: surfaces delayed suppliers",
           "ACME" in fmt({"delayed_suppliers": "ACME"}))
    _check("format_context: empty -> default text",
           fmt({}) == "No additional context provided.")

    _check("infer_supplier_type: milk -> import", infer("Fresh Milk 1L") == "import")
    _check("infer_supplier_type: bread -> local", infer("White Bread 400g") == "local")
    _check("infer_supplier_type: unknown -> other", infer("Random Widget") == "other")


def _test_agents_end_to_end():
    _patch_call_claude(_fake_call_claude)

    # ── Agent 1: normalisation ───────────────────────────────────────────
    norm = agents.run_normalization_agent(1, "fake-model")
    _check("normalisation: returns one duplicate group", len(norm.get("groups", [])) == 1)
    _check("normalisation: canonical name is right",
           norm["groups"][0]["canonical"] == "White Bread 400g")

    # ── Agent 2: inventory health ────────────────────────────────────────
    inv = agents.run_inventory_agent(1, "fake-model", [], {})
    _check("inventory: no error", "error" not in inv)
    report = inv.get("report", [])
    _check("inventory: 3 items classified", len(report) == 3)
    statuses = {r["item"]: r["status"] for r in report}
    _check("inventory: salmon is CRITICAL", statuses.get("Frozen Salmon") == "CRITICAL")
    _check("inventory: old item is DEAD", statuses.get("Old Stock Item") == "DEAD")

    # ── Agent 3: recommendations ─────────────────────────────────────────
    recs = agents.run_recommendation_agent(1, "fake-model", report, {})
    valid = [r for r in recs if isinstance(r, dict) and not r.get("error")]
    _check("recommendation: no error returned", len(valid) == len(recs))
    _check("recommendation: dead SKU excluded, 2 actionable items", len(valid) == 2)

    by_item = {r["item"]: r for r in valid}
    _check("recommendation: bread present", "White Bread 400g" in by_item)
    _check("recommendation: salmon present", "Frozen Salmon" in by_item)

    # The real proof: deterministic enrichment from the DB flowed through.
    # 80 units sold across 2 distinct months = 40/month; unit "PCS" from inventory.
    wb = by_item.get("White Bread 400g", {})
    _check("recommendation: monthly-sales basis computed from DB (40/mo)",
           wb.get("avg_monthly_sales") == 40.0)
    _check("recommendation: unit label resolved from inventory (PCS)",
           wb.get("uom_label") == " PCS")


def _test_orchestrator_if_present():
    """After Commit 2 the orchestrator exists; before it, this is skipped.
    Proves the 'big boss' runs the agents in order and fires its progress hooks."""
    try:
        orch = importlib.import_module("agents.orchestrator")
    except Exception:
        print("skip: orchestrator not present yet (expected before Commit 2)")
        return
    if not hasattr(orch, "run_pipeline"):
        print("skip: orchestrator.run_pipeline not present yet")
        return

    _patch_call_claude(_fake_call_claude)
    marks, emits = [], []
    result = orch.run_pipeline(
        1, "fake-model", [], {},
        emit=lambda m: emits.append(m),
        mark=lambda name, status, summary=None: marks.append((name, status)),
    )
    _check("orchestrator: returned inventory + recommendations",
           isinstance(result, dict) and "inventory_report" in result and "recommendations" in result)
    _check("orchestrator: produced the 2 actionable recommendations",
           len([r for r in result.get("recommendations", []) if isinstance(r, dict) and not r.get("error")]) == 2)
    _check("orchestrator: marked inventory then recommendation",
           ("inventory", "running") in marks and ("recommendation", "running") in marks)


def main():
    _build_fixture()
    _test_pure_helpers()
    _test_agents_end_to_end()
    _test_orchestrator_if_present()

    if _FAILED:
        print("\nSOME TESTS FAILED")
        sys.exit(1)
    print("\nAll agents-refactor tests passed.")


if __name__ == "__main__":
    main()
