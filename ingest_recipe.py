"""Wide-matrix sales ingestion: deterministic detector + recipe executor.

"AI maps, Python converts" (spec 2026-07-11-smart-sales-ingestion): the
mapper (agents/ingest_mapper.py) reads a ~30-row sample and proposes a
layout recipe; everything numeric happens HERE in plain Python. Any doubt
raises RecipeRefusal — a loud refusal always beats a silent guess.
"""
import csv as _csv
import numbers
import os
import re
from datetime import date

MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}
DETECT_ROWS    = 15    # rows scanned for the month-grid signature
SAMPLE_ROWS    = 30    # rows shown to the AI mapper
MIN_MONTH_COLS = 6     # a real month grid names at least half a year
FLAT_TAIL_SHARE = 0.5  # >=50% of items identical across the tail => projections

GUIDANCE = (
    "We couldn't safely read this sales file. Ask your ERP admin for a "
    "sales export with one row per sale, a date column, an item name and "
    "a quantity — then upload that instead. Nothing has been run or charged."
)


class RecipeRefusal(Exception):
    """Raised whenever the recipe path cannot proceed safely."""


def _month_of(cell) -> int:
    return MONTH_NAMES.get(str(cell or "").strip().upper(), 0)


def _raw_rows(filepath, limit=None):
    """First `limit` raw rows (list of lists) of an .xlsx or .csv file.
    xlsx is read with cached formula VALUES (data_only) — planning sheets
    are full of `=489*12` cells and the formula text is useless."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        out = []
        with open(filepath, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            for i, row in enumerate(_csv.reader(f)):
                if limit is not None and i >= limit:
                    break
                out.append(row)
        return out
    import openpyxl
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    out = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if limit is not None and i >= limit:
            break
        out.append(list(row))
    wb.close()
    return out


def detect_wide_matrix(filepath) -> bool:
    """True when one of the first rows names >= MIN_MONTH_COLS distinct months
    — the months-as-columns signature. Deterministic and cheap; the only
    trigger for the AI mapper (v1)."""
    try:
        rows = _raw_rows(filepath, limit=DETECT_ROWS)
    except Exception:
        return False
    for r in rows:
        months = {_month_of(c) for c in r if _month_of(c)}
        if len(months) >= MIN_MONTH_COLS:
            return True
    return False
