"""19 Jul 2026: a sparse sales-sheet Supplier column must not paint one name
onto every item that follows it.

A live smoke test uploaded ONLY the two compulsory files (inventory + sales).
The sales file's Supplier column was mostly empty, with two supplier names
scattered across ~1/3 of rows. The recommendation agent's sales-sheet
fallback filled the last-seen name down the column unconditionally — a rule
meant for merged-cell summary exports (where only the first row of each
supplier's block carries the name) — so on a transaction dump it painted
whichever name came before onto every following item. The resulting Purchase
Order sheet assigned all 39 reorder items to those two suppliers, including
absurd pairings.

Covers three sales-sheet shapes:
  1. Scattered column (a transaction dump — the bug): fill-down must NOT
     apply; an item with no supplier on its own row(s) resolves to Unknown,
     and an item whose own rows disagree on the supplier also resolves to
     Unknown rather than guessing.
  2. Blocky column (a genuine merged-cell summary export): fill-down must
     still work, so this legitimate layout doesn't regress.
  3. No supplier column at all: nothing to attribute, no caveat needed.

Scenarios 1 and 2 also check the results-page caveat: a run where any
item's supplier came only from the sales sheet must append exactly one
data note; a run with no supplier column must append none.

Run: python tests/test_supplier_attribution_guard.py
"""
import json
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_supattrguard.db")
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

import database as db                      # noqa: E402
import agents.shared as shared             # noqa: E402
import agents.recommendation as rec_mod    # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── Session + inventory table (no supplier listing, no PO file uploaded) ────
db.init_db()
SID = 982
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "SupAttrGuardOrg", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{SID} '
           '("description" TEXT, "qty_on_hand" TEXT, "category" TEXT, "uom" TEXT)')
_INV_ITEMS = [
    ("ALPHA MILK 1L",    "0", "DAIRY",      "CTN"),
    ("BRAVO RICE 5KG",   "0", "DRY",        "BAG"),
    ("CHARLIE OIL 1L",   "0", "DRY",        "CTN"),
    ("DELTA SAUCE 500G", "0", "CONDIMENTS", "CTN"),
]
for r in _INV_ITEMS:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", r)

# Keyword fallback for any auto-mapper LLM call — not exercised directly here.
shared._call_claude = lambda *a, **k: "{}"

_REC_REPORT = [
    {"item": name, "category": cat, "stock": 0, "status": "CRITICAL",
     "spoilage_risk": "NONE", "days_of_supply": 0, "observation": "t"}
    for (name, _stock, cat, _uom) in _INV_ITEMS
]


def _echo_supplier_claude(model, system, user, max_tokens=64000):
    """Echo back one rec per item, copying its enriched Supplier value (same
    trick as test_dummy_data_fixture.py's rec stub). Whatever supplier the
    agent decided to put in the prompt is exactly what this test inspects."""
    recs = []
    for block in user.split("---"):
        m_item = re.search(r"Item: (.+)", block)
        if not m_item:
            continue
        m_sup = re.search(r"Supplier: (.+?) \(", block)
        recs.append({
            "item": m_item.group(1).strip(),
            "supplier": m_sup.group(1).strip() if m_sup else "Unknown",
            "supplier_type": "other", "lead_time_days": None, "days_of_supply": 0,
            "recommended_action": "REORDER", "suggested_quantity": "Verify with team",
            "confidence": "LOW", "consequence_if_acting": "a",
            "consequence_if_not_acting": "b", "supplier_risk": "None",
            "mitigation": "", "flags": [], "reason": "test",
        })
    return json.dumps(recs)


rec_mod._call_claude = _echo_supplier_claude


def _reset_sales_table(with_supplier=True):
    db.execute(f"DROP TABLE IF EXISTS sales_{SID}")
    cols = '"trans_date" TEXT, "item_description" TEXT, "qty" TEXT'
    if with_supplier:
        cols += ', "supplier" TEXT'
    db.execute(f"CREATE TABLE sales_{SID} ({cols})")


def _insert_with_supplier(rows):
    for item, sup in rows:
        db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?,?)",
                   ("2026-06-15", item, "10", sup))


def _insert_no_supplier(items):
    for item in items:
        db.execute(f"INSERT INTO sales_{SID} VALUES (?,?,?)",
                   ("2026-06-15", item, "10"))


def _run():
    notes = []
    recs = rec_mod.run_recommendation_agent(SID, "m", list(_REC_REPORT), {}, None,
                                             data_notes=notes)
    by_item = {r["item"]: r.get("supplier") for r in recs if isinstance(r, dict)}
    return by_item, notes


# ── Scenario 1: scattered column (transaction dump) — the bug ───────────────
_reset_sales_table(with_supplier=True)
_insert_with_supplier([
    ("ALPHA MILK 1L",    "Sunrise Foods"),
    ("BRAVO RICE 5KG",   ""),
    ("CHARLIE OIL 1L",   "Ocean Trading"),
    ("DELTA SAUCE 500G", ""),
    ("ALPHA MILK 1L",    ""),
    ("BRAVO RICE 5KG",   "Sunrise Foods"),
    ("CHARLIE OIL 1L",   ""),
    ("DELTA SAUCE 500G", ""),
    ("ALPHA MILK 1L",    "Sunrise Foods"),
    ("BRAVO RICE 5KG",   "Ocean Trading"),
    ("CHARLIE OIL 1L",   ""),
    ("DELTA SAUCE 500G", ""),
])
suppliers_1, notes_1 = _run()
_check("scattered: ALPHA keeps its own-row supplier",
       suppliers_1.get("ALPHA MILK 1L") == "Sunrise Foods", detail=str(suppliers_1))
_check("scattered: BRAVO's own rows disagree ('Sunrise Foods' vs 'Ocean "
       "Trading') -> Unknown, not a guess",
       suppliers_1.get("BRAVO RICE 5KG") == "Unknown", detail=str(suppliers_1))
_check("scattered: CHARLIE keeps its own-row supplier",
       suppliers_1.get("CHARLIE OIL 1L") == "Ocean Trading", detail=str(suppliers_1))
_check("scattered: DELTA has no own-row value and the column isn't blocky "
       "-> Unknown, NOT the fill-down name (this is the bug this plan fixes)",
       suppliers_1.get("DELTA SAUCE 500G") == "Unknown", detail=str(suppliers_1))
_check("scattered: exactly one supplier-source caveat appended",
       len(notes_1) == 1, detail=str(notes_1))
_check("scattered: the caveat says suppliers came from the sales file itself",
       bool(notes_1) and "read from the sales file itself" in notes_1[0], detail=str(notes_1))

# ── Scenario 2: blocky column (merged-cell summary export) — must keep working
_reset_sales_table(with_supplier=True)
_insert_with_supplier([
    ("ALPHA MILK 1L",    "Sunrise Foods"),
    ("BRAVO RICE 5KG",   ""),
    ("CHARLIE OIL 1L",   "Ocean Trading"),
    ("DELTA SAUCE 500G", ""),
])
suppliers_2, notes_2 = _run()
_check("blocky: ALPHA (block owner) keeps its supplier",
       suppliers_2.get("ALPHA MILK 1L") == "Sunrise Foods", detail=str(suppliers_2))
_check("blocky: BRAVO inherits its block's supplier via fill-down",
       suppliers_2.get("BRAVO RICE 5KG") == "Sunrise Foods", detail=str(suppliers_2))
_check("blocky: CHARLIE (2nd block owner) keeps its supplier",
       suppliers_2.get("CHARLIE OIL 1L") == "Ocean Trading", detail=str(suppliers_2))
_check("blocky: DELTA inherits its block's supplier via fill-down",
       suppliers_2.get("DELTA SAUCE 500G") == "Ocean Trading", detail=str(suppliers_2))
_check("blocky: exactly one supplier-source caveat appended",
       len(notes_2) == 1, detail=str(notes_2))

# ── Scenario 3: no supplier column at all ────────────────────────────────────
_reset_sales_table(with_supplier=False)
_insert_no_supplier(["ALPHA MILK 1L", "BRAVO RICE 5KG", "CHARLIE OIL 1L", "DELTA SAUCE 500G"])
suppliers_3, notes_3 = _run()
_check("no supplier column: all four items -> Unknown",
       all(suppliers_3.get(name) == "Unknown" for name, *_ in _INV_ITEMS),
       detail=str(suppliers_3))
_check("no supplier column: no supplier-source caveat appended",
       notes_3 == [], detail=str(notes_3))

# ── Scenario 4: blocky-by-the-runs-test but its first name sits deep in the
# file — the miniature of the real fixture failure this revision fixes. Two
# leading items get 3 blank rows each, THEN two items each carry their own
# supplier name on every one of their own rows (satisfying the "one
# contiguous run per name" test on its own), THEN a trailing item gets 3 more
# blank rows. Unconditional runs-based blockiness would treat this as a
# genuine merged-cell export and let "Ocean Trading" (the last name seen)
# fill down onto HOTEL TEA — an item that has nothing to do with either
# supplier. The row-position gate must refuse that: the first non-blank cell
# here sits at row index 6, not within the first two rows.
_NEW_ITEMS = [
    ("INDIA JUICE 1L",    "0", "DRY", "CTN"),
    ("JULIET FLOUR 1KG",  "0", "DRY", "BAG"),
    ("FOXTROT BUTTER 5KG","0", "DAIRY", "CTN"),
    ("GOLF RICE 5KG",     "0", "DRY", "BAG"),
    ("HOTEL TEA 250G",    "0", "DRY", "CTN"),
]
for r in _NEW_ITEMS:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", r)
_REC_REPORT_4 = [
    {"item": name, "category": cat, "stock": 0, "status": "CRITICAL",
     "spoilage_risk": "NONE", "days_of_supply": 0, "observation": "t"}
    for (name, _stock, cat, _uom) in _NEW_ITEMS
]

_reset_sales_table(with_supplier=True)
_insert_with_supplier([
    ("INDIA JUICE 1L",     ""),
    ("INDIA JUICE 1L",     ""),
    ("INDIA JUICE 1L",     ""),
    ("JULIET FLOUR 1KG",   ""),
    ("JULIET FLOUR 1KG",   ""),
    ("JULIET FLOUR 1KG",   ""),
    ("FOXTROT BUTTER 5KG", "Sunrise Foods"),
    ("FOXTROT BUTTER 5KG", "Sunrise Foods"),
    ("FOXTROT BUTTER 5KG", "Sunrise Foods"),
    ("GOLF RICE 5KG",      "Ocean Trading"),
    ("GOLF RICE 5KG",      "Ocean Trading"),
    ("GOLF RICE 5KG",      "Ocean Trading"),
    ("HOTEL TEA 250G",     ""),
    ("HOTEL TEA 250G",     ""),
    ("HOTEL TEA 250G",     ""),
])
notes_4 = []
recs_4 = rec_mod.run_recommendation_agent(SID, "m", list(_REC_REPORT_4), {}, None,
                                          data_notes=notes_4)
suppliers_4 = {r["item"]: r.get("supplier") for r in recs_4 if isinstance(r, dict)}
_check("tail-drift: FOXTROT keeps its own-row supplier",
       suppliers_4.get("FOXTROT BUTTER 5KG") == "Sunrise Foods", detail=str(suppliers_4))
_check("tail-drift: GOLF keeps its own-row supplier",
       suppliers_4.get("GOLF RICE 5KG") == "Ocean Trading", detail=str(suppliers_4))
_check("tail-drift: HOTEL (trailing, blank) -> Unknown, NOT filled down from "
       "GOLF's block (this is the dummy-fixture bug in miniature)",
       suppliers_4.get("HOTEL TEA 250G") == "Unknown", detail=str(suppliers_4))
_check("tail-drift: INDIA (leading, blank) -> Unknown",
       suppliers_4.get("INDIA JUICE 1L") == "Unknown", detail=str(suppliers_4))
_check("tail-drift: JULIET (leading, blank) -> Unknown",
       suppliers_4.get("JULIET FLOUR 1KG") == "Unknown", detail=str(suppliers_4))
_check("tail-drift: exactly one supplier-source caveat appended",
       len(notes_4) == 1, detail=str(notes_4))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll supplier-attribution-guard tests passed.")
