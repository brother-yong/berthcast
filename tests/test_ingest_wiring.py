"""Orchestration: detector -> mapper -> validate -> execute -> re-ingest.
Uses an injected fake mapper (no API). Verifies the three outcomes:
clean (no-op), converted (table replaced + readback), unreadable (table
cleared + guidance).

Dependency-free:  python tests/test_ingest_wiring.py
"""
import os
import sys
import tempfile
import types
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berth_ingwire.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                      # noqa: E402
from ingest_recipe import maybe_convert_sales   # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


TMP = tempfile.mkdtemp(prefix="berth_wire_")


def make_wide_xlsx(rows, name):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    path = os.path.join(TMP, name)
    wb.save(path)
    return path


WIDE_ROWS = [
    ["INVENTORY", "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "SUPPLIER", "LEAD TIME"],
    ["ALPHA BEANS 1KG", 100, 110, 90, 100, 105, 95, "SUPPLIER A", "10 WEEKS"],
    ["BRAVO RICE 10KG", 200, 210, 190, 205, 195, 200, "SUPPLIER B", "14 DAYS"],
]
TXN_ROWS = [
    ["Date", "Item Description", "Qty Sold"],
    ["2026-01-05", "ALPHA BEANS 1KG", 10],
]
GOOD_RECIPE = {"layout": "wide_matrix", "header_row": 1, "item_col": 1,
               "month_cols": {"2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6},
               "supplier_col": 8, "leadtime_col": 9}

SID = 4242

# clean transaction file -> no-op ("clean"), table untouched
txn = make_wide_xlsx(TXN_ROWS, "txn.xlsx")
db.excel_to_sqlite(txn, "sales", SID)
state, payload = maybe_convert_sales(txn, SID, mapper=lambda s: GOOD_RECIPE,
                                     today=date(2026, 7, 11))
_check("clean file -> 'clean'", state == "clean", state)
_check("clean file table intact", db.table_exists(f"sales_{SID}"))

# wide file + good mapper -> converted, table replaced with canonical rows
wide = make_wide_xlsx(WIDE_ROWS, "wide.xlsx")
db.excel_to_sqlite(wide, "sales", SID)
state, rb = maybe_convert_sales(wide, SID, mapper=lambda s: GOOD_RECIPE,
                                today=date(2026, 7, 11))
_check("wide file -> 'converted'", state == "converted", state)
_check("readback items", rb["items"] == 2, rb)
rows = db.query(f"SELECT * FROM sales_{SID} LIMIT 1")
cols = set(rows[0].keys())
_check("canonical columns in table", "date" in cols and "qty_sold" in cols, cols)
n = db.query(f"SELECT COUNT(*) AS n FROM sales_{SID}")[0]["n"]
_check("12 canonical rows (2 items x 6 months)", n == 12, n)

# the mapper must receive a rendered sample (row-prefixed, pipe-separated)
seen = {}


def _spy_mapper(sample):
    seen["sample"] = sample
    return GOOD_RECIPE


db.excel_to_sqlite(wide, "sales", SID)
maybe_convert_sales(wide, SID, mapper=_spy_mapper, today=date(2026, 7, 11))
_check("mapper sees row-prefixed sample", seen["sample"].startswith("R1: "), seen["sample"][:40])
_check("mapper sample contains item name", "ALPHA BEANS 1KG" in seen["sample"])

# inventory table present -> coverage line computed
# (3+ columns: database.py's xlsx header-row detection requires >= 3 filled
# header cells to pick a header row, unlike its CSV path which needs only 1 —
# a pre-existing quirk in database.py, not something ingest_recipe.py controls)
db.excel_to_sqlite(make_wide_xlsx(
    [["Item Description", "Stock Qty", "Category"],
     ["ALPHA BEANS 1KG", 5, "DRY"],
     ["BRAVO RICE 10KG", 3, "DRY"], ["CHARLIE OIL 1L", 9, "DRY"]], "inv.xlsx"),
    "inventory", SID)
db.excel_to_sqlite(wide, "sales", SID)
state, rb = maybe_convert_sales(wide, SID, mapper=lambda s: GOOD_RECIPE,
                                today=date(2026, 7, 11))
_check("coverage matched 2", rb.get("coverage", {}).get("sales_items_matched") == 2, rb)
_check("coverage total 3", rb.get("coverage", {}).get("inventory_items_total") == 3, rb)

# mapper says unknown -> unreadable, sales table CLEARED
db.excel_to_sqlite(wide, "sales", SID)
state, guidance = maybe_convert_sales(wide, SID, mapper=lambda s: {"layout": "unknown"},
                                      today=date(2026, 7, 11))
_check("unknown layout -> 'unreadable'", state == "unreadable", state)
_check("guidance is plain text with a fix", "one row per sale" in guidance, guidance)
_check("naive sales table cleared", not db.table_exists(f"sales_{SID}"))

# mapper returns None (API failed) -> unreadable
db.excel_to_sqlite(wide, "sales", SID)
state, _g = maybe_convert_sales(wide, SID, mapper=lambda s: None,
                                today=date(2026, 7, 11))
_check("mapper None -> 'unreadable'", state == "unreadable", state)

# mapper raises -> unreadable (never crashes the upload thread)
db.excel_to_sqlite(wide, "sales", SID)


def _boom(sample):
    raise RuntimeError("mapper exploded")


state, _g = maybe_convert_sales(wide, SID, mapper=_boom, today=date(2026, 7, 11))
_check("mapper exception -> 'unreadable'", state == "unreadable", state)

# review fixes: re-ingest failure, drop failure, coverage column ranking
db.excel_to_sqlite(wide, "sales", SID)
_orig_ing = db.excel_to_sqlite
db.excel_to_sqlite = lambda *a, **k: {"ok": False, "error": "disk full"}
state, _g = maybe_convert_sales(wide, SID, mapper=lambda s: GOOD_RECIPE,
                                today=date(2026, 7, 11))
db.excel_to_sqlite = _orig_ing
_check("re-ingest failure -> 'unreadable'", state == "unreadable", state)

# drop-table failure inside the refusal path must not crash the upload thread
db.excel_to_sqlite(wide, "sales", SID)
_orig_exec = db.execute


def _no_drop(sql, *a, **k):
    if sql.strip().upper().startswith("DROP"):
        raise RuntimeError("locked")
    return _orig_exec(sql, *a, **k)


db.execute = _no_drop
state, _g = maybe_convert_sales(wide, SID, mapper=lambda s: {"layout": "unknown"},
                                today=date(2026, 7, 11))
db.execute = _orig_exec
_check("drop failure still returns 'unreadable' cleanly", state == "unreadable", state)

# coverage must prefer a description-ish column over a code-ish one:
# 'item_code' sits first in table order and would shadow 'item_description'
# under naive first-match, showing a misleading near-zero overlap
db.excel_to_sqlite(make_wide_xlsx(
    [["Item Code", "Item Description", "Stock Qty"],
     ["A1", "ALPHA BEANS 1KG", 5],
     ["B2", "BRAVO RICE 10KG", 3],
     ["C3", "CHARLIE OIL 1L", 9]], "inv2.xlsx"),
    "inventory", SID)
db.excel_to_sqlite(wide, "sales", SID)
state, rb = maybe_convert_sales(wide, SID, mapper=lambda s: GOOD_RECIPE,
                                today=date(2026, 7, 11))
_check("coverage uses description column not code column",
       rb.get("coverage", {}).get("sales_items_matched") == 2, rb)

# ── status plumbing ───────────────────────────────────────────────────────────
db.init_db()  # upload_sessions doesn't exist yet in this ad-hoc test DB
sid2 = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (1,'Test Org','uploading')")
db.set_conversion_status(sid2, "sales", "done", rows_count=12,
                         readback={"items": 2, "months_kept": [1, 2]})
cs = db.get_conversion_status(sid2)
_check("readback stored", cs["sales"]["readback"]["items"] == 2, cs)
db.set_conversion_status(sid2, "sales", "unreadable", error="guidance text")
cs = db.get_conversion_status(sid2)
_check("unreadable status stored", cs["sales"]["status"] == "unreadable", cs)
_check("readback dropped when absent", "readback" not in cs["sales"], cs)

# ── end-to-end through _start_processing's thread (real wiring, fake mapper) ──
import time as _time
import app as appmod
import agents.ingest_mapper as _im

_im_orig = _im.propose_recipe
_im.propose_recipe = lambda sample: GOOD_RECIPE
sid3 = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (1,'Test Org','uploading')")
appmod._start_processing(wide, "sales", sid3, "sales", "wide.xlsx")
for _ in range(100):
    _time.sleep(0.1)
    _cs = db.get_conversion_status(sid3).get("sales", {})
    if _cs.get("status") in ("done", "error", "unreadable"):
        break
_im.propose_recipe = _im_orig
_check("wired: sales slot converts via thread", _cs.get("status") == "done", _cs)
_check("wired: readback rides the status", (_cs.get("readback") or {}).get("items") == 2, _cs)
_check("wired: rows count is canonical", _cs.get("rows") == 12, _cs)

_im.propose_recipe = lambda sample: {"layout": "unknown"}
sid4 = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (1,'Test Org','uploading')")
appmod._start_processing(wide, "sales", sid4, "sales", "wide.xlsx")
for _ in range(100):
    _time.sleep(0.1)
    _cs = db.get_conversion_status(sid4).get("sales", {})
    if _cs.get("status") in ("done", "error", "unreadable"):
        break
_im.propose_recipe = _im_orig
_check("wired: unreadable status set", _cs.get("status") == "unreadable", _cs)
_check("wired: guidance in error field", "one row per sale" in (_cs.get("error") or ""), _cs)
_check("wired: sales table cleared", not db.table_exists(f"sales_{sid4}"))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll ingest-wiring tests passed.")
