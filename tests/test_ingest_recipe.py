"""Wide-matrix sales ingestion: detector, recipe validation, executor
(spec 2026-07-11-smart-sales-ingestion). Synthetic generic fixtures only.

Dependency-free:  python tests/test_ingest_recipe.py
"""
import os
import sys
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


TMP = tempfile.mkdtemp(prefix="berth_ingest_")


def make_wide_xlsx(rows, name="wide.xlsx"):
    """Build a synthetic xlsx from a list-of-lists."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    path = os.path.join(TMP, name)
    wb.save(path)
    return path


# The canonical synthetic wide-matrix fixture: title junk, merged-style
# two-row header, supplier blocks, TOTAL row, text zero, blank cell.
WIDE_ROWS = [
    ["Generic Distributor Pte Ltd", "", "", "", "", "", "", "", "", ""],
    ["INVENTORY ", "TOTAL  SALES QTY ", "", "", "", "", "", "SUPPLIER ", "", "LEAD TIME "],
    ["", "JAN ", "FEB", "MAR", "APR", "MAY", "JUN", "", "", ""],
    ["ALPHA BEANS 1KG",  100, 110, 90, 100, 105, 95,  "SUPPLIER A", "", "10 WEEKS"],
    ["ALPHA BEANS 5KG",  10,  "0", 12, None, 11, 9,   "", "", ""],
    ["BRAVO RICE 10KG",  200, 210, 190, 205, 195, 200, "SUPPLIER B", "", "14 DAYS"],
    ["TOTAL",            310, 320, 292, 305, 311, 304, "", "", ""],
]

TXN_ROWS = [
    ["Date", "Item Description", "Qty Sold"],
    ["2026-01-05", "ALPHA BEANS 1KG", 10],
    ["2026-02-11", "BRAVO RICE 10KG", 4],
]

from ingest_recipe import detect_wide_matrix   # noqa: E402

# ── detector ──────────────────────────────────────────────────────────────────
_check("month-grid xlsx detected", detect_wide_matrix(make_wide_xlsx(WIDE_ROWS)) is True)
_check("transaction xlsx not detected",
       detect_wide_matrix(make_wide_xlsx(TXN_ROWS, "txn.xlsx")) is False)

# months scattered across DIFFERENT rows must not trigger (needs one row)
scattered = [["JAN", "x"], ["FEB", "x"], ["MAR", "x"], ["APR", "x"], ["MAY", "x"], ["JUN", "x"]]
_check("scattered month names not detected",
       detect_wide_matrix(make_wide_xlsx(scattered, "scat.xlsx")) is False)

# CSV variant
csv_path = os.path.join(TMP, "wide.csv")
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    import csv as _c
    w = _c.writer(f)
    for r in WIDE_ROWS:
        w.writerow(["" if c is None else c for c in r])
_check("month-grid csv detected", detect_wide_matrix(csv_path) is True)

# full month names count too
full = [["", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE"]]
_check("full month names detected",
       detect_wide_matrix(make_wide_xlsx(full, "full.xlsx")) is True)

from ingest_recipe import validate_recipe, RecipeRefusal   # noqa: E402

GOOD = {"layout": "wide_matrix", "header_row": 3, "item_col": 1,
        "month_cols": {"2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6},
        "supplier_col": 8, "leadtime_col": 10}


def _refused(recipe):
    try:
        validate_recipe(recipe, n_rows=7, n_cols=10)
        return False
    except RecipeRefusal:
        return True


_check("valid recipe accepted", not _refused(GOOD))
_check("unknown layout refused", _refused({**GOOD, "layout": "transactions"}))
_check("out-of-bounds month col refused",
       _refused({**GOOD, "month_cols": {**GOOD["month_cols"], "99": 7}}))
_check("duplicate months refused",
       _refused({**GOOD, "month_cols": {"2": 1, "3": 1, "4": 3, "5": 4, "6": 5, "7": 6}}))
_check("too few month cols refused",
       _refused({**GOOD, "month_cols": {"2": 1, "3": 2}}))
_check("item col colliding with month col refused",
       _refused({**GOOD, "item_col": 2}))
_check("missing field refused", _refused({"layout": "wide_matrix"}))
_check("null supplier col accepted",
       not _refused({**GOOD, "supplier_col": None, "leadtime_col": None}))
_check("non-dict refused", _refused("not a recipe"))

# review fixes: normalization collapse + column double-booking + type guards
_check("normalised key collision refused",
       _refused({**GOOD, "month_cols": {"2": 1, "02": 2, "3": 3, "4": 4, "5": 5, "6": 6}}))
_check("supplier col on a month column refused", _refused({**GOOD, "supplier_col": 2}))
_check("leadtime col equal to supplier col refused",
       _refused({**GOOD, "supplier_col": 8, "leadtime_col": 8}))
_check("supplier col equal to item col refused", _refused({**GOOD, "supplier_col": 1}))
_check("bool header_row refused", _refused({**GOOD, "header_row": True}))
_check("month number 13 refused",
       _refused({**GOOD, "month_cols": {"2": 13, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}}))
_check("bool month number refused",
       _refused({**GOOD, "month_cols": {"2": True, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}}))

from ingest_recipe import execute_recipe   # noqa: E402

wide_path = make_wide_xlsx(WIDE_ROWS, "exec.xlsx")
VALID = validate_recipe(GOOD, n_rows=7, n_cols=10)
csv_out, rb = execute_recipe(wide_path, VALID, today=date(2026, 7, 11))

import csv as _csv2
with open(csv_out, encoding="utf-8") as f:
    out_rows = list(_csv2.DictReader(f))

_check("header row correct",
       set(out_rows[0].keys()) == {"Date", "Item Description", "Qty Sold", "Supplier", "Lead Time Days"})
_check("TOTAL row skipped", not any("TOTAL" in r["Item Description"] for r in out_rows))
_check("3 items in readback", rb["items"] == 3, rb)

alpha1 = [r for r in out_rows if r["Item Description"] == "ALPHA BEANS 1KG"]
_check("6 months for clean item", len(alpha1) == 6)
_check("ISO mid-month dates", alpha1[0]["Date"] == "2026-01-15", alpha1[0]["Date"])
_check("supplier on its own row", alpha1[0]["Supplier"] == "SUPPLIER A")
_check("lead time 10 WEEKS -> 70", alpha1[0]["Lead Time Days"] == "70")

alpha5 = [r for r in out_rows if r["Item Description"] == "ALPHA BEANS 5KG"]
_check("text '0' kept as zero", any(float(r["Qty Sold"]) == 0 for r in alpha5))
_check("blank cell skipped (5 rows not 6)", len(alpha5) == 5, len(alpha5))
_check("fill-down supplier", alpha5[0]["Supplier"] == "SUPPLIER A")
_check("fill-down lead time", alpha5[0]["Lead Time Days"] == "70")

bravo = [r for r in out_rows if r["Item Description"] == "BRAVO RICE 10KG"]
_check("new block resets supplier", bravo[0]["Supplier"] == "SUPPLIER B")
_check("14 DAYS -> 14", bravo[0]["Lead Time Days"] == "14")

_check("per-month sums verified: JAN total",
       sum(float(r["Qty Sold"]) for r in out_rows if r["Date"].startswith("2026-01")) == 310.0)
_check("readback totals", rb["total_units"] == sum(float(r["Qty Sold"]) for r in out_rows), rb)
_check("readback months kept", rb["months_kept"] == [1, 2, 3, 4, 5, 6], rb)
_check("assumed year present", rb["assumed_year"] == 2026, rb)

# corrupted recipe (month col pointing at the supplier col) -> refusal, no CSV
bad = dict(VALID); bad["month_cols"] = {**VALID["month_cols"], 8: 7}
try:
    execute_recipe(wide_path, bad, today=date(2026, 7, 11))
    _check("corrupt recipe refused", False)
except RecipeRefusal:
    _check("corrupt recipe refused", True)

# 6 months where APR-JUN repeat one value per item for most items -> dropped
PROJ_ROWS = [
    ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN"],
    ["ITEM ONE",   10, 12, 11, 20, 20, 20],
    ["ITEM TWO",   30, 28, 33, 40, 40, 40],
    ["ITEM THREE", 55, 60, 52, 50, 50, 50],
]
proj_recipe = validate_recipe(
    {"layout": "wide_matrix", "header_row": 1, "item_col": 1,
     "month_cols": {"2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6},
     "supplier_col": None, "leadtime_col": None}, n_rows=4, n_cols=7)
_p, prb = execute_recipe(make_wide_xlsx(PROJ_ROWS, "proj.xlsx"), proj_recipe,
                         today=date(2026, 7, 11))
_check("flat tail APR-JUN dropped", prb["months_dropped"] == [4, 5, 6], prb)
_check("JAN-MAR kept", prb["months_kept"] == [1, 2, 3], prb)

# year rule: last kept month (3) <= July -> current year 2026
_check("year rule current year", prb["assumed_year"] == 2026, prb)
# full JAN-DEC actuals uploaded in July -> previous year
DEC_ROWS = [["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
            ["ITEM X"] + [10 + i for i in range(12)]]
dec_recipe = validate_recipe(
    {"layout": "wide_matrix", "header_row": 1, "item_col": 1,
     "month_cols": {str(i + 2): i + 1 for i in range(12)},
     "supplier_col": None, "leadtime_col": None}, n_rows=2, n_cols=13)
_d, drb = execute_recipe(make_wide_xlsx(DEC_ROWS, "dec.xlsx"), dec_recipe,
                         today=date(2026, 7, 11))
_check("varied full year kept", drb["months_kept"] == list(range(1, 13)), drb)
_check("year rule previous year", drb["assumed_year"] == 2025, drb)

# year-to-date grid: JUL-DEC columns exist but are blank -> dropped as empty,
# not refused, and not allowed to sweep real months with them
YTD_ROWS = [["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
            ["ITEM A", 10, 12, 11, 13, 12, 14, None, None, None, None, None, None],
            ["ITEM B", 20, 22, 21, 23, 22, 24, None, None, None, None, None, None]]
ytd_recipe = validate_recipe(
    {"layout": "wide_matrix", "header_row": 1, "item_col": 1,
     "month_cols": {str(i + 2): i + 1 for i in range(12)},
     "supplier_col": None, "leadtime_col": None}, n_rows=3, n_cols=13)
_y, yrb = execute_recipe(make_wide_xlsx(YTD_ROWS, "ytd.xlsx"), ytd_recipe,
                         today=date(2026, 7, 11))
_check("blank future months dropped as empty", yrb["months_dropped"] == [7, 8, 9, 10, 11, 12], yrb)
_check("real months survive empty tail", yrb["months_kept"] == [1, 2, 3, 4, 5, 6], yrb)

# regression: an item with PARTIAL tail coverage must not make the tail look
# flat (the singleton-sweep bug the coverage rule closes) — with the old
# `if not tv` rule, ITEM SPARSE's lone JUN value reads as "flat", hits the
# 50% share with only two items, and sweeps MAY-JUN out of the report
SPARSE_ROWS = [
    ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN"],
    ["ITEM VARIED", 10, 12, 11, 13, 12, 14],
    ["ITEM SPARSE", None, None, None, None, None, 9],
]
sparse_recipe = validate_recipe(
    {"layout": "wide_matrix", "header_row": 1, "item_col": 1,
     "month_cols": {"2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6},
     "supplier_col": None, "leadtime_col": None}, n_rows=3, n_cols=7)
_s, srb = execute_recipe(make_wide_xlsx(SPARSE_ROWS, "sparse.xlsx"), sparse_recipe,
                         today=date(2026, 7, 11))
_check("sparse item cannot flatten the tail", srb["months_dropped"] == [], srb)
_check("all real months kept with sparse item", srb["months_kept"] == [1, 2, 3, 4, 5, 6], srb)

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll ingest-recipe tests passed.")
