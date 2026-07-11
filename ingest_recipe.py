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


def validate_recipe(recipe, n_rows: int, n_cols: int) -> dict:
    """Deterministic, field-by-field validation of the mapper's JSON.
    The model's output is never trusted structurally. Returns a normalised
    copy (int keys) or raises RecipeRefusal."""
    def _bad(why):
        raise RecipeRefusal(f"recipe rejected: {why}")

    if not isinstance(recipe, dict):
        _bad("not an object")
    if recipe.get("layout") != "wide_matrix":
        _bad(f"unsupported layout {recipe.get('layout')!r}")

    def _int_in(name, lo, hi, allow_none=False):
        v = recipe.get(name)
        if v is None and allow_none:
            return None
        if not isinstance(v, int) or isinstance(v, bool):
            _bad(f"{name}={v!r} must be an integer")
        if not (lo <= v <= hi):
            _bad(f"{name}={v!r} out of bounds 1..{hi}")
        return v

    header_row = _int_in("header_row", 1, n_rows)
    item_col   = _int_in("item_col", 1, n_cols)
    sup_col    = _int_in("supplier_col", 1, n_cols, allow_none=True)
    lt_col     = _int_in("leadtime_col", 1, n_cols, allow_none=True)

    raw_months = recipe.get("month_cols")
    if not isinstance(raw_months, dict) or len(raw_months) < MIN_MONTH_COLS:
        _bad("month_cols missing or too few")
    month_cols = {}
    for k, m in raw_months.items():
        try:
            col = int(k)
        except (TypeError, ValueError):
            _bad(f"month col key {k!r} not an int")
        if not (1 <= col <= n_cols):
            _bad(f"month col {col} out of bounds")
        if not isinstance(m, int) or isinstance(m, bool) or not (1 <= m <= 12):
            _bad(f"month number {m!r} invalid")
        month_cols[col] = m
    if len(month_cols) != len(raw_months):
        _bad("duplicate month columns after normalisation")
    if len(set(month_cols.values())) != len(month_cols):
        _bad("duplicate month numbers")
    if item_col in month_cols:
        _bad("item_col collides with a month column")
    for _name, _c in (("supplier_col", sup_col), ("leadtime_col", lt_col)):
        if _c is not None and _c in month_cols:
            _bad(f"{_name} collides with a month column")
    non_null = [c for c in (item_col, sup_col, lt_col) if c is not None]
    if len(set(non_null)) != len(non_null):
        _bad("item/supplier/leadtime columns must be distinct")

    return {"header_row": header_row, "item_col": item_col,
            "month_cols": month_cols, "supplier_col": sup_col,
            "leadtime_col": lt_col}


_LEAD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(WEEK|DAY|MONTH)", re.IGNORECASE)


def lead_time_days(raw) -> str:
    """'10 WEEKS' -> '70'; '14 DAYS' -> '14'; '2 MONTHS' -> '60'; else ''."""
    m = _LEAD_RE.search(str(raw or ""))
    if not m:
        return ""
    factor = {"WEEK": 7, "DAY": 1, "MONTH": 30}[m.group(2).upper()]
    return str(int(round(float(m.group(1)) * factor)))


def _num(cell):
    """Cell -> float or None. Accepts real numbers and numeric strings
    ('0', ' 12 '); everything else (blank, text, formula string) is None."""
    if isinstance(cell, numbers.Number):
        return float(cell)
    s = str(cell or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _assumed_year(last_actual_month: int, today) -> int:
    """The grid states no year. If its last actual month hasn't happened yet
    this calendar year, it must be last year's report."""
    return today.year - 1 if last_actual_month > today.month else today.year


def _split_projection_tail(data_rows, month_cols, item_col_1based):
    """Flat-tail rule: walking from the LAST month backwards, a month joins
    the projection tail while >= FLAT_TAIL_SHARE of items with a positive
    value there repeat ONE identical value across the whole remaining tail.
    Tails must span >= 2 months — a single trailing month is always
    "one distinct value" and must never be dropped on that alone.
    Returns (kept_months, dropped_months). Known false positive: a catalogue
    dominated by fixed standing orders — accepted, the read-back names the
    dropped months loudly (spec, residual hole #4)."""
    months = [m for _c, m in sorted(month_cols, key=lambda kv: kv[1])]
    cols = {m: c for c, m in month_cols}
    item_i = item_col_1based - 1
    series = []
    for r in data_rows:
        name = str((r[item_i] if item_i < len(r) else "") or "").strip()
        if not name or "TOTAL" in name.upper():
            continue        # same row filter as conversion/verification
        vals = {m: _num(r[cols[m] - 1] if cols[m] - 1 < len(r) else None) for m in months}
        if any(v is not None for v in vals.values()):
            series.append(vals)

    cut = len(months)                    # months[cut:] are dropped
    for i in range(len(months) - 2, 0, -1):   # tail >= 2 months; month 1 never drops
        tail = months[i:]
        flat = with_val = 0
        for vals in series:
            tv = [vals[m] for m in tail if vals[m] is not None]
            if len(tv) != len(tail) or tv[0] <= 0:
                continue
            with_val += 1
            if len(set(tv)) == 1:
                flat += 1
        if with_val and flat / with_val >= FLAT_TAIL_SHARE:
            cut = i
        else:
            break
    return months[:cut], months[cut:]


def execute_recipe(filepath, recipe, today=None):
    """Apply a VALIDATED recipe over the whole file. Returns
    (csv_path, readback_dict) or raises RecipeRefusal.

    Verification: per-month totals are recomputed from the source cells by
    an independent second pass and must equal the CSV's totals exactly.
    (Honest limit: this proves Python followed the recipe, not that the
    recipe is true — the truth check is the human read-back.)"""
    today = today or date.today()
    rows = _raw_rows(filepath)
    hdr = recipe["header_row"]
    item_c = recipe["item_col"] - 1
    sup_c = (recipe["supplier_col"] or 0) - 1
    lt_c = (recipe["leadtime_col"] or 0) - 1
    month_cols = sorted(recipe["month_cols"].items(), key=lambda kv: kv[1])

    # ── month-column content scan ────────────────────────────────────────────
    # A claimed month column with no numbers at all is either a mislabeled
    # text column (mapper error -> refuse) or a genuinely empty future month
    # (year-to-date grids -> drop the month, not the run).
    numeric_n = {m: 0 for _c, m in month_cols}
    text_n    = {m: 0 for _c, m in month_cols}
    for r in rows[hdr:]:
        name = str((r[item_c] if item_c < len(r) else "") or "").strip()
        if not name or "TOTAL" in name.upper():
            continue
        for col, m in month_cols:
            cell = r[col - 1] if col - 1 < len(r) else None
            if _num(cell) is not None:
                numeric_n[m] += 1
            elif str(cell or "").strip():
                text_n[m] += 1
    for _c, m in month_cols:
        if numeric_n[m] == 0 and text_n[m] > 0:
            raise RecipeRefusal(
                f"claimed month column for month {m} contains text, not quantities")
    empty_months = {m for _c, m in month_cols if numeric_n[m] == 0}
    live_cols = [(c, m) for c, m in month_cols if m not in empty_months]
    if not live_cols:
        raise RecipeRefusal("no month column contains numeric data")
    kept_months, flat_dropped = _split_projection_tail(rows[hdr:], live_cols, recipe["item_col"])
    dropped = sorted(set(flat_dropped) | empty_months)

    out, items = [], 0
    cur_sup, cur_lt = "", ""
    year = _assumed_year(max(kept_months), today) if kept_months else today.year
    for r in rows[hdr:]:
        def _cell(i):
            return r[i] if 0 <= i < len(r) else None
        name = str(_cell(item_c) or "").strip()
        if not name:
            continue
        if "TOTAL" in name.upper():
            continue
        if recipe["supplier_col"] and str(_cell(sup_c) or "").strip():
            cur_sup = str(_cell(sup_c)).strip()
            cur_lt = ""    # new block: never inherit the previous block's lead time
        if recipe["leadtime_col"] and str(_cell(lt_c) or "").strip():
            cur_lt = lead_time_days(_cell(lt_c))
        items += 1
        for col, m in month_cols:
            if m not in kept_months:
                continue
            v = _num(_cell(col - 1))
            if v is None:
                continue
            out.append([f"{year}-{m:02d}-15", name, v, cur_sup, cur_lt])

    if not out:
        raise RecipeRefusal("conversion produced no rows")

    # ── independent verification pass ────────────────────────────────────────
    src_sums = {m: 0.0 for m in kept_months}
    for r in rows[hdr:]:
        name = str((r[item_c] if item_c < len(r) else "") or "").strip()
        if not name or "TOTAL" in name.upper():
            continue
        for col, m in month_cols:
            if m in kept_months:
                v = _num(r[col - 1] if col - 1 < len(r) else None)
                if v is not None:
                    src_sums[m] += v
    csv_sums = {m: 0.0 for m in kept_months}
    for row in out:
        csv_sums[int(row[0][5:7])] += row[2]
    if any(abs(src_sums[m] - csv_sums[m]) > 1e-6 for m in kept_months):
        raise RecipeRefusal("verification failed: converted totals != source totals")

    csv_path = filepath + ".converted.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["Date", "Item Description", "Qty Sold", "Supplier", "Lead Time Days"])
        w.writerows(out)

    readback = {
        "items": items,
        "months_kept": sorted(kept_months),
        "months_dropped": sorted(dropped),
        "assumed_year": year,
        "total_units": round(sum(r[2] for r in out), 1),
    }
    return csv_path, readback
