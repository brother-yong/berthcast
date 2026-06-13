"""Data safety net: pre-report confidence checks.

Goal (see docs/superpowers/specs/2026-06-13-data-safety-net-design.md): berthcast
must never show a trusted report built on a file it could not read correctly.
Every upload is assessed here and the outcome is one of:

  - no findings   -> OK: the report is shown normally;
  - WARN finding  -> the report is shown WITH a plain-English caveat banner;
  - BLOCK finding -> the analysis stops and the user is told what is wrong.

These checks DETECT problems and fail loud. They deliberately do not try to FIX
messy data (correct European decimals, convert units): detection is cheaper and
far more certain, and it is what protects an unattended free-tier upload. The
real fixes are parked until a real client needs one.

`assess_upload(session_id, column_map)` reads the already-ingested
inventory_<sid> / sales_<sid> tables and returns a list of findings. It is the
single entry point used by both the live pipeline and the broken-file corpus
test, so what we prove in tests is exactly what runs in production.
"""
import re

from database import query
from agents.shared import (
    detect_inventory_columns,
    normalise_match_key,
    SalesNameIndex,
    _to_num,
)

WARN = "warn"
BLOCK = "block"

# Tunable thresholds — kept here so tuning is one edit (see spec).
LARGE_FILE_ROWS         = 4000   # above this, suggest narrowing scope
MIN_SALES_FOR_OVERLAP   = 5      # need a few sales items before judging overlap
LOW_OVERLAP_RATIO       = 0.20   # < this share of sales rows matched -> WARN
EURO_NUMBER_RATIO       = 0.30   # >= this share of cells look Euro-formatted -> WARN
UNPARSEABLE_STOCK_RATIO = 0.10   # < this share of stock cells are numbers -> BLOCK
SAMPLE_SIZE             = 200

# Stock-column names that usually mean a DIFFERENT quantity than on-hand.
_STOCK_NAME_SUSPECT = ("on_order", "onorder", "allocated", "reserved",
                       "back_order", "backorder", "sold", "qty_sold")

# Unambiguous European/Indonesian number signatures: dot-grouped thousands
# (1.234 / 1.234.567) with an optional comma decimal (1.234,56), or a plain
# comma decimal (12,5). We require the comma decimal or multiple dot-groups so a
# lone "1.234" (which could be US 1.234) is never counted.
_EURO_RE = re.compile(r"^\s*\d{1,3}(\.\d{3})+,\d+\s*$"     # 1.234,56 / 1.234.567,8
                      r"|^\s*\d{1,3}(\.\d{3}){2,}\s*$"      # 1.234.567
                      r"|^\s*\d+,\d{1,2}\s*$")              # 12,5
# US/plain signatures so we never flag normal data.
_US_RE = re.compile(r"^\s*\d{1,3}(,\d{3})+(\.\d+)?\s*$|^\s*\d+(\.\d+)?\s*$")


def _finding(level, code, message):
    return {"level": level, "code": code, "message": message}


def _looks_unit(s):
    s = (s or "").strip().upper()
    return 1 <= len(s) <= 6 and s.isalpha()


def _is_numberish(v):
    """True if the cell is a number we can read OR a number written in a foreign
    format. Used so a Euro-formatted column is WARNed about, never wrongly
    BLOCKed as 'not numbers'."""
    return _to_num(v, default=None) is not None or bool(_EURO_RE.match(str(v)))


def _safe_rows(sql, params=()):
    try:
        return query(sql, params)
    except Exception:
        return []


def _dominant_unit(table, col):
    rows = _safe_rows(
        f'SELECT UPPER(TRIM("{col}")) AS u, COUNT(*) AS n FROM {table} '
        f'WHERE TRIM(COALESCE("{col}", \'\')) != \'\' GROUP BY u ORDER BY n DESC LIMIT 1')
    if rows and _looks_unit(rows[0]["u"]):
        return rows[0]["u"]
    return None


def _pick(cols, column_map, kw, field):
    v = (column_map or {}).get(field)
    return v if (isinstance(v, str) and v in cols) else kw.get(field)


def assess_upload(session_id, column_map=None):
    """Return a list of findings for one upload session. Empty list == OK.

    `column_map` is the mapping the inventory agent resolved (description / stock
    / uom). When omitted, the gate detects columns itself, so the corpus test can
    exercise detection too.
    """
    inv_table = f"inventory_{session_id}"
    inv_sample = _safe_rows(f"SELECT * FROM {inv_table} LIMIT 1")
    if not inv_sample:
        return [_finding(BLOCK, "empty_file",
            "We couldn't read any rows from your Inventory Report. The file may be "
            "empty, the wrong document, or in a format we can't open. Upload an "
            "inventory export (Excel or CSV) with one row per product.")]

    cols = [c for c in inv_sample[0].keys() if c != "_session_id"]
    kw = detect_inventory_columns(cols)
    desc_col = _pick(cols, column_map, kw, "description")
    stock_col = _pick(cols, column_map, kw, "stock")
    uom_col = _pick(cols, column_map, kw, "uom")

    if not desc_col or not stock_col:
        return [_finding(BLOCK, "no_columns",
            "We couldn't find both an item-name column and a current-stock column "
            "in your Inventory Report. Make sure one column lists product names and "
            "one lists how much is in stock now (not quantity sold).")]

    # Sample the stock column once: feeds the unparseable BLOCK and the Euro WARN.
    stock_vals = [r["v"] for r in _safe_rows(
        f'SELECT "{stock_col}" AS v FROM {inv_table} '
        f'WHERE TRIM(COALESCE("{stock_col}", \'\')) != \'\' LIMIT {SAMPLE_SIZE}')]
    if stock_vals:
        numberish = sum(1 for v in stock_vals if _is_numberish(v))
        if numberish / len(stock_vals) < UNPARSEABLE_STOCK_RATIO:
            return [_finding(BLOCK, "stock_not_numeric",
                f"The column we read as stock ('{stock_col}') doesn't contain "
                "numbers. This usually means the wrong column was matched or the "
                "wrong file was uploaded. Check that your current stock quantities "
                "are in their own column.")]

    findings = []

    # WARN: the stock column's name hints it is the wrong kind of quantity.
    if any(tok in stock_col.lower() for tok in _STOCK_NAME_SUSPECT):
        findings.append(_finding(WARN, "stock_column_suspect",
            f"We read '{stock_col}' as your current stock, but its name suggests it "
            "might be a different figure (on-order, allocated, or sold). If the "
            "health labels look wrong, check this is really the quantity on hand."))

    # Gather the sales table's columns (if any) for the cross-file checks.
    sal_table = f"sales_{session_id}"
    sal_sample = _safe_rows(f"SELECT * FROM {sal_table} LIMIT 1")
    sales_cols = [c for c in sal_sample[0].keys() if c != "_session_id"] if sal_sample else []

    # WARN: numbers look European/Indonesian ("1.200,50").
    fmt_pool = list(stock_vals)
    if sales_cols:
        s_qty = next((c for c in sales_cols if c in ("qty", "quantity", "qty_sold", "billing_qty")), None) \
                or next((c for c in sales_cols if "qty" in c.lower() or "quantity" in c.lower()), None)
        if s_qty:
            fmt_pool += [r["v"] for r in _safe_rows(
                f'SELECT "{s_qty}" AS v FROM {sal_table} '
                f'WHERE TRIM(COALESCE("{s_qty}", \'\')) != \'\' LIMIT {SAMPLE_SIZE}')]
    considered = [v for v in fmt_pool if str(v).strip()]
    euro = sum(1 for v in considered if _EURO_RE.match(str(v)) and not _US_RE.match(str(v)))
    if considered and euro / len(considered) >= EURO_NUMBER_RATIO:
        findings.append(_finding(WARN, "number_format",
            'Some numbers look like they use a comma for the decimal point '
            '(e.g. "1.200,50" meaning 1,200.5). berthcast currently reads numbers '
            'the US/UK way, so these may be misread. Check the quantities on the '
            "results page, and tell us if your files use this format."))

    # Cross-file name + unit checks (need a sales description column).
    if sales_cols:
        s_desc = next((c for c in sales_cols if c in (
                    "inventory_desc", "item_description", "description", "product_name")), None) \
                 or next((c for c in sales_cols if ("desc" in c.lower() or "item" in c.lower()
                          or "product" in c.lower()) and "supplier" not in c.lower()), None)
        if s_desc:
            sales_names = [r["v"] for r in _safe_rows(
                f'SELECT DISTINCT "{s_desc}" AS v FROM {sal_table} '
                f'WHERE TRIM(COALESCE("{s_desc}", \'\')) != \'\' LIMIT 5000')]
            inv_names = [r["v"] for r in _safe_rows(
                f'SELECT DISTINCT "{desc_col}" AS v FROM {inv_table} '
                f'WHERE TRIM(COALESCE("{desc_col}", \'\')) != \'\' LIMIT 20000')]
            claimed = {normalise_match_key(n) for n in inv_names if n}
            idx = SalesNameIndex({n: {"_n": 1} for n in sales_names}, claimed_keys=claimed)
            # Count INVENTORY items that received sales. idx.get(sales_name) is the
            # wrong test: the index keeps unmatched sales rows under their own key
            # (so the agent can still see them), so it returns non-None even for a
            # row that matched nothing. idx.get(inventory_name) is None unless some
            # sales row actually resolved onto that item.
            matched = sum(1 for n in inv_names if idx.get(n) is not None)
            if (len(sales_names) >= MIN_SALES_FOR_OVERLAP
                    and matched / len(sales_names) < LOW_OVERLAP_RATIO):
                findings.append(_finding(WARN, "low_name_overlap",
                    "Almost none of the items in your sales file matched an item in "
                    "your inventory file. They may use different names or product "
                    "codes. Without a match, sales history can't guide the "
                    "recommendations, so reorder sizes will be weaker. Use the same "
                    "product names in both files."))

            # WARN: the two files are measured in different units.
            if uom_col:
                s_unit = next((c for c in sales_cols if c in ("unit", "uom")), None) \
                         or next((c for c in sales_cols if "unit" in c.lower() or c.lower() == "uom"), None)
                if s_unit:
                    inv_unit = _dominant_unit(inv_table, uom_col)
                    sal_unit = _dominant_unit(sal_table, s_unit)
                    if inv_unit and sal_unit and inv_unit != sal_unit:
                        findings.append(_finding(WARN, "unit_mismatch",
                            f"Your inventory is mostly measured in {inv_unit} but your "
                            f"sales file is mostly in {sal_unit}. If these are different "
                            "units for the same products, the monthly sales and "
                            "suggested order sizes will be off. Make both files use the "
                            "same unit per product."))

    # WARN: very large file (slow, costly, truncation risk).
    n_rows = (_safe_rows(f"SELECT COUNT(*) AS n FROM {inv_table}") or [{"n": 0}])[0]["n"] or 0
    if n_rows > LARGE_FILE_ROWS:
        findings.append(_finding(WARN, "large_file",
            f"Your inventory has {n_rows:,} rows. A full scan will take a while and "
            "cost more. If you only need the items you actually sell, choose a Top-N "
            "scope so berthcast focuses on those."))

    return findings
