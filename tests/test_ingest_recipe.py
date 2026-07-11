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

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll ingest-recipe tests passed.")
