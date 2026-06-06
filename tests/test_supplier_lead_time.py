"""Regression test for the supplier lead-time resolution bug.

Symptom seen in a live run: every item — local and import alike — was sized and
classified against a flat 56-day lead time, ignoring the supplier listing. A
Frozen Salmon (import) was called HEALTHY when it should have been LOW, and all
reorder quantities used the wrong horizon.

Root cause: get_supplier_profile() returns a default dict with
avg_lead_time_days = 56 for suppliers that have no saved profile (i.e. every
supplier for a brand-new client). The resolver does:

    lt_days = (profile.get("avg_lead_time_days")   # 56 -> truthy, STOPS here
               or LEAD_TIME_BY_TYPE.get(stype)      # 112 import / 21 local — skipped
               or config.get("default_lead_time_days"))

so the import/local lead time was never consulted.

This test builds the supplier listing + purchase orders exactly as the dummy
data does and asserts the resolved lead times honour the supplier TYPE.

Dependency-free: run with `python tests/test_supplier_lead_time.py`.
"""
import os
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="berth_leadtime_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
from agents.shared import _resolve_item_suppliers


_FAILED = False


def _check(name, cond):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        _FAILED = True


def _build_fixture():
    db.init_db()
    sid = 1

    # Supplier listing: one local, one import (no saved profiles — fresh client).
    db.execute('CREATE TABLE suppliers_1 ("supplier_name" TEXT, '
               '"supplier_type" TEXT, "_session_id" TEXT)')
    for row in [
        ("SG Local Supplies", "Local",  "1"),
        ("Ocean Import Co",   "Import", "1"),
    ]:
        db.execute("INSERT INTO suppliers_1 VALUES (?,?,?)", row)

    # Purchase orders link each item to its supplier.
    db.execute('CREATE TABLE purchase_orders_1 ("inventory_desc" TEXT, '
               '"supplier_name" TEXT, "_session_id" TEXT)')
    for row in [
        ("White Bread 400g", "SG Local Supplies", "1"),
        ("Cooking Oil 5L",   "Ocean Import Co",   "1"),
    ]:
        db.execute("INSERT INTO purchase_orders_1 VALUES (?,?,?)", row)


def main():
    _build_fixture()
    config = db.get_company_config("TestCo")  # defaults: default_lead_time_days = 56

    item_supplier_map, item_lt_map, supplier_type_map = _resolve_item_suppliers(
        1, "TestCo", config, {}
    )

    # Supplier types read correctly from the listing.
    _check("local supplier typed as local",
           supplier_type_map.get("SG Local Supplies") == "local")
    _check("import supplier typed as import",
           supplier_type_map.get("Ocean Import Co") == "import")

    # The actual fix: lead time must follow the supplier TYPE, not the flat 56.
    bread = item_lt_map.get("White Bread 400g", {})
    oil   = item_lt_map.get("Cooking Oil 5L", {})

    _check("local item resolves to 21-day lead time (was wrongly 56)",
           bread.get("lead_time_days") == 21)
    _check("import item resolves to 112-day lead time (was wrongly 56)",
           oil.get("lead_time_days") == 112)

    if _FAILED:
        print("\nSOME TESTS FAILED")
        sys.exit(1)
    print("\nAll supplier lead-time tests passed.")


if __name__ == "__main__":
    main()
