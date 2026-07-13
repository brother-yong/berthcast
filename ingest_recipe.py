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
from datetime import date, datetime

from logging_setup import logger

MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}
_MONTH_LABEL = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]
_MCODE_RE = re.compile(r"^M(\d{1,2})$", re.IGNORECASE)
_YM_RE    = re.compile(r"^(\d{4})[-/](\d{1,2})$")   # 2026-01, 2026/1
_MY_RE    = re.compile(r"^(\d{1,2})[-/](\d{4})$")   # 01/2026, 1-2026
_NAME_DATE_RE = re.compile(r"^([A-Za-z]{3,9})[-/ ]?\d{2,4}$")  # Jan-26, Jan 2026


def _cell_month(cell) -> int:
    """Month 1-12 this HEADER cell names, else 0. Knows month names, M-codes
    (M1..M12), month-year dates (Jan-26, 2026-01, 01/2026) and real datetime
    cells. Bare integers are intentionally NOT months here — too ambiguous;
    they are handled row-level in the detector under a strict guard."""
    if isinstance(cell, (datetime, date)):
        return cell.month
    s = str(cell or "").strip()
    if not s:
        return 0
    up = s.upper()
    if up in MONTH_NAMES:
        return MONTH_NAMES[up]
    m = _MCODE_RE.match(s)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= 12 else 0
    m = _NAME_DATE_RE.match(s)
    if m:
        return MONTH_NAMES.get(m.group(1).upper(), 0)
    m = _YM_RE.match(s)
    if m:
        v = int(m.group(2))
        return v if 1 <= v <= 12 else 0
    m = _MY_RE.match(s)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= 12 else 0
    return 0


DETECT_ROWS    = 15    # rows scanned for the month-grid signature
SAMPLE_ROWS    = 30    # rows shown to the AI mapper
MIN_MONTH_COLS = 6     # a real month grid names at least half a year
FLAT_TAIL_SHARE = 0.5  # >=50% of items identical across the tail => projections
CONFIDENT_PROJECTION = 0.9  # earliest dropped month stays dropped only if this-flat vs next
MIXED_TEXT_SHARE = 0.2      # a month column this-much-text-or-more is "mixed" -> degrade

# Resource caps: this path re-reads the raw upload with openpyxl, which
# enforces NONE of database.py's zip-bomb limits — so it bounds itself.
# Planning grids are hundreds of rows and tens of KB; anything near these
# caps is not a planning grid and refuses loudly.
MAX_RECIPE_FILE_MB = 30     # raw upload size this path will even open
MAX_RECIPE_XML_MB  = 60     # declared decompressed xlsx payload (zip bomb gate)
MAX_RECIPE_ROWS    = 20000  # rows accumulated in memory before refusing

GUIDANCE = (
    "We couldn't safely read this sales file. Ask your ERP admin for a "
    "sales export with one row per sale, a date column, an item name and "
    "a quantity — then upload that instead. Nothing has been run or charged."
)

_TOTAL_LABELS = {"TOTAL", "TOTALS", "SUBTOTAL", "SUB-TOTAL", "SUB TOTAL", "GRAND TOTAL"}


def _is_total_row(name: str) -> bool:
    """Summary rows are skipped by exact LABEL match only — a real product
    can legitimately contain the word TOTAL ("TOTAL PROTEIN MIX 5KG")."""
    return name.strip().upper() in _TOTAL_LABELS


class RecipeRefusal(Exception):
    """Raised whenever the recipe path cannot proceed safely."""


def _month_of(cell) -> int:
    return MONTH_NAMES.get(str(cell or "").strip().upper(), 0)


def _size_guard(filepath):
    """Refuse before opening anything oversized. Python's zipfile truncates
    each archive member at its DECLARED size, so the declared sizes are a
    real ceiling on what openpyxl can decompress — checking them first
    closes the zip-bomb hole without decompressing a byte."""
    if os.path.getsize(filepath) > MAX_RECIPE_FILE_MB * 1024 * 1024:
        raise RecipeRefusal("file too large for the conversion path")
    if os.path.splitext(filepath)[1].lower() != ".csv":
        import zipfile
        try:
            with zipfile.ZipFile(filepath) as zf:
                # ALL members, not just cell data: openpyxl also parses
                # styles/theme/workbook XML in full just to open the file,
                # so a bomb planted in any of them inflates at load time.
                declared = sum(i.file_size for i in zf.infolist())
        except Exception:
            raise RecipeRefusal("not a readable xlsx")
        if declared > MAX_RECIPE_XML_MB * 1024 * 1024:
            raise RecipeRefusal("spreadsheet decompresses too large")


def _raw_rows(filepath, limit=None):
    """First `limit` raw rows (list of lists) of an .xlsx or .csv file.
    xlsx is read with cached formula VALUES (data_only) — planning sheets
    are full of `=489*12` cells and the formula text is useless."""
    _size_guard(filepath)
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        out = []
        with open(filepath, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            for i, row in enumerate(_csv.reader(f)):
                if limit is not None and i >= limit:
                    break
                if limit is None and len(out) >= MAX_RECIPE_ROWS:
                    raise RecipeRefusal("too many rows for a planning grid")
                out.append(row)
        return out
    import openpyxl
    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        out = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if limit is not None and i >= limit:
                break
            if limit is None and len(out) >= MAX_RECIPE_ROWS:
                raise RecipeRefusal("too many rows for a planning grid")
            out.append(list(row))
        return out
    finally:
        wb.close()


def detect_wide_matrix(filepath) -> bool:
    """True when one of the first rows names >= MIN_MONTH_COLS distinct months
    — the months-as-columns signature. Deterministic and cheap; the only
    trigger for the AI mapper (v1)."""
    try:
        rows = _raw_rows(filepath, limit=DETECT_ROWS)
    except Exception:
        return False
    for r in rows:
        months = {_cell_month(c) for c in r}
        months.discard(0)
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
    seq = [m for _c, m in sorted(month_cols.items())]
    if seq != sorted(seq):
        # Fiscal-year / rotated grids (APR..MAR) would get one year stamped
        # across a calendar boundary — three months land a year wrong, and the
        # projection tail walk would inspect the wrong end. Refuse loudly (v1).
        _bad("month columns are not in calendar order")
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
    if isinstance(cell, bool):
        return None
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
    dropped months loudly (spec, residual hole #4).

    Deliberate bias: when item coverage churns across tail lengths the walk
    stops early and KEEPS ambiguous months. Keeping a projection is loud
    (readback totals, spiky flags downstream); dropping a real month is
    silent data loss. We accept the first, never the second."""
    months = [m for _c, m in sorted(month_cols, key=lambda kv: kv[1])]
    cols = {m: c for c, m in month_cols}
    item_i = item_col_1based - 1
    series = []
    for r in data_rows:
        name = str((r[item_i] if item_i < len(r) else "") or "").strip()
        if not name or _is_total_row(name):
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


def _boundary_is_borderline(data_rows, boundary_m, next_m, cols_by_month, item_col_1based):
    """The earliest dropped month is borderline when a real minority of items
    carry their OWN value there (boundary != next), i.e. it was likely a real
    month copied forward to seed the projections. Returns True to KEEP + ask.
    ponytail: compares against boundary+1 only; a gap between them (an empty
    month) means no rescue — safe, that just falls back to the old drop."""
    if boundary_m not in cols_by_month or next_m not in cols_by_month:
        return False
    bc = cols_by_month[boundary_m] - 1
    nc = cols_by_month[next_m] - 1
    item_i = item_col_1based - 1
    flat = total = 0
    for r in data_rows:
        name = str((r[item_i] if item_i < len(r) else "") or "").strip()
        if not name or _is_total_row(name):
            continue
        b = _num(r[bc]) if bc < len(r) else None
        n = _num(r[nc]) if nc < len(r) else None
        if b is None or n is None or b <= 0:
            continue
        total += 1
        if b == n:
            flat += 1
    return total > 0 and (flat / total) < CONFIDENT_PROJECTION


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
        if not name or _is_total_row(name):
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
    # Type-sanity: a month column that is mostly numbers but carries a real
    # share of text is suspect (mapper may have grabbed a note/label column).
    # Converted anyway (text cells skipped), but flagged for a one-tap confirm.
    mixed_months = {m for _c, m in month_cols
                    if numeric_n[m] > 0 and text_n[m] > 0
                    and text_n[m] >= MIXED_TEXT_SHARE * (numeric_n[m] + text_n[m])}
    live_cols = [(c, m) for c, m in month_cols if m not in empty_months]
    if not live_cols:
        raise RecipeRefusal("no month column contains numeric data")
    kept_months, flat_dropped = _split_projection_tail(rows[hdr:], live_cols, recipe["item_col"])

    # Borderline rescue: if the earliest dropped month looks like a real month
    # copied forward to seed the projections (a minority of items disagree with
    # the flat value), KEEP it — dropping a real month is silent data loss — and
    # flag the run degraded so the user confirms before the paid analysis.
    cols_by_month = {m: c for c, m in month_cols}
    borderline_m = None
    flat_dropped = list(flat_dropped)
    if flat_dropped:
        boundary = min(flat_dropped)
        if _boundary_is_borderline(rows[hdr:], boundary, boundary + 1,
                                   cols_by_month, recipe["item_col"]):
            flat_dropped.remove(boundary)
            kept_months = sorted(set(kept_months) | {boundary})
            borderline_m = boundary

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
        if _is_total_row(name):
            continue
        _sup_val = str(_cell(sup_c) or "").strip() if recipe["supplier_col"] else ""
        if _sup_val and _sup_val != cur_sup:
            # New supplier block: never inherit the previous block's lead time.
            # Change-detection matters — files that repeat the supplier on every
            # row must not wipe the block's lead time on each repeat.
            cur_sup = _sup_val
            cur_lt = ""
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
        if not name or _is_total_row(name):
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

    tier = "degraded" if (borderline_m is not None or mixed_months) else "confident"
    parts = []
    if borderline_m is not None:
        nm = _MONTH_LABEL[borderline_m]
        parts.append(f"We kept {nm} as real sales, but the months after it look "
                     f"like typed projections. If {nm} is also a projection, "
                     f"replace the file without it.")
    if mixed_months:
        names = ", ".join(_MONTH_LABEL[m] for m in sorted(mixed_months))
        parts.append(f"The {names} column had some non-number cells — "
                     f"double-check it read correctly.")
    readback = {
        "items": items,
        "months_kept": sorted(kept_months),
        "months_dropped": sorted(dropped),
        "assumed_year": year,
        "total_units": round(sum(r[2] for r in out), 1),
        "tier": tier,
        "question": " ".join(parts) if parts else None,
    }
    return csv_path, readback


def _render_sample(rows) -> str:
    """First SAMPLE_ROWS rows as 'R<n>: a | b | c' lines for the mapper."""
    lines = []
    for i, r in enumerate(rows[:SAMPLE_ROWS], 1):
        cells = [str(c)[:40] if c is not None else "" for c in r]
        lines.append(f"R{i}: " + " | ".join(cells))
    return "\n".join(lines)


def maybe_convert_sales(filepath, session_id, mapper, today=None):
    """Run the smart-ingestion path for an already-ingested SALES upload.

    mapper: callable(sample_text) -> raw recipe dict | None  (injected so
    tests never touch the API; production passes
    agents.ingest_mapper.propose_recipe).

    Returns one of:
      ("clean", None)            — not a wide-matrix file; nothing changed
      ("converted", readback)    — table replaced with canonical rows
      ("unreadable", guidance)   — refused; naive sales table CLEARED so the
                                   analysis can't run on a known misread
    """
    import database as db

    if not detect_wide_matrix(filepath):
        return ("clean", None)

    def _refuse():
        # The naive ingest of a wide-matrix file is a known misread
        # (January-as-the-year). Leaving it would repeat the failure this
        # feature exists to kill — so clear it.
        try:
            db.execute(f'DROP TABLE IF EXISTS "sales_{int(session_id)}"')
        except Exception:
            logger.exception(
                "maybe_convert_sales: failed to drop sales_%s after refusal", session_id)
        return ("unreadable", GUIDANCE)

    try:
        rows = _raw_rows(filepath)
        raw = mapper(_render_sample(rows))
        if not isinstance(raw, dict) or raw.get("layout") != "wide_matrix":
            return _refuse()
        n_cols = max((len(r) for r in rows[:SAMPLE_ROWS]), default=0)
        recipe = validate_recipe(raw, n_rows=len(rows), n_cols=n_cols)
        csv_path, readback = execute_recipe(filepath, recipe, today=today)
        result = db.excel_to_sqlite(csv_path, "sales", session_id)
        if not result.get("ok"):
            logger.warning(
                "maybe_convert_sales: re-ingest of converted CSV failed for session %s: %s",
                session_id, result.get("error"))
            try:
                # Refusal must not orphan real client data on the disk.
                os.remove(csv_path)
            except OSError:
                pass
            return _refuse()
    except RecipeRefusal as e:
        # Expected refusal, not a crash — but the REASON must reach the
        # operator's logs; the user only ever sees the generic guidance.
        logger.info("maybe_convert_sales: recipe refused for session %s: %s",
                    session_id, e)
        return _refuse()
    except Exception:
        logger.exception("maybe_convert_sales: unexpected failure converting %s", filepath)
        return _refuse()

    # Coverage is an advisory read-back line only — a failure here (e.g. the
    # inventory table lookup itself raising) must never undo a conversion
    # that already succeeded and was already verified by execute_recipe's
    # independent totals check.
    try:
        readback["coverage"] = _coverage(db, session_id, csv_path)
    except Exception:
        logger.exception(
            "maybe_convert_sales: coverage computation failed for session %s", session_id)
        readback["coverage"] = {}
    try:
        # Already ingested and measured — converted CSVs must not pile up on
        # the data disk (one per wide-matrix upload, forever).
        os.remove(csv_path)
    except OSError:
        pass
    return ("converted", readback)


def _coverage(db, session_id, csv_path):
    """Approximate item overlap vs the session's inventory table (trimmed,
    case-insensitive exact match). {} when inventory isn't uploaded yet —
    the UI omits the line. Real matching happens later in dedup."""
    inv_table = f"inventory_{int(session_id)}"
    if not db.table_exists(inv_table):
        return {}
    try:
        row = db.query(f'SELECT * FROM "{inv_table}" LIMIT 1')
        if not row:
            return {}
        cols = [c for c in row[0].keys() if c != "_session_id"]
        # Rank the hints: description-ish columns beat code-ish ones —
        # 'item_code' must never shadow 'item_description', or the overlap
        # count compares names against codes and reads as near-zero.
        desc = None
        for h in ("desc", "product", "item", "sku"):
            desc = next((c for c in cols if h in c), None)
            if desc:
                break
        if not desc:
            return {}
        inv_names = {str(r[desc] or "").strip().lower()
                     for r in db.query(f'SELECT DISTINCT "{desc}" FROM "{inv_table}" LIMIT 5000')}
        inv_names.discard("")
        sales_names = set()
        with open(csv_path, encoding="utf-8") as f:
            for i, row_ in enumerate(_csv.DictReader(f)):
                if i > 200000:
                    break
                sales_names.add(row_["Item Description"].strip().lower())
        return {"sales_items_matched": len(sales_names & inv_names),
                "inventory_items_total": len(inv_names)}
    except Exception:
        # Coverage is advisory-only — never fail the conversion over it, but
        # don't hide the failure from the operator either.
        logger.exception("Coverage computation failed for session %s", session_id)
        return {}
