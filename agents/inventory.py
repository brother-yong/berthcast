"""Agent 2 — the inventory health check.

Classifies every item as HEALTHY / LOW / CRITICAL / DEAD with spoilage risk and
days-of-supply, using lead-time-aware thresholds. Moved verbatim from agents.py.
"""

import json
import re

from database import query, execute, get_company_config
from .verifier import verify_inventory_report
from .shared import (
    _emit,
    _resolve_item_suppliers,
    _format_context,
    _call_claude,
    _extract_json_array,
    _num_sql,
    _to_num,
    count_sales_months,
    detect_inventory_columns,
    propose_inventory_columns,
    detect_avg_month_column,
    infer_months_from_item_stats,
    SalesNameIndex,
    normalise_match_key,
    monthly_pattern_stats,
    wrap_untrusted,
    UNTRUSTED_GUARD,
)

# Batch size for the health-check prompt. Sized so the FULL reply fits in
# _INV_MAX_TOKENS: ~60 output tokens per item -> 300 items ~= 18K tokens,
# comfortable inside 64K. 12 June: 800-item batches with max_tokens=16000
# couldn't fit; the JSON repair quietly kept only the front of each batch and
# most of the catalogue silently vanished from the report.
_INV_BATCH      = 300
_INV_MAX_TOKENS = 64000

# A bare total line ("Total", "Grand Total:", "SUBTOTAL") is never a product.
_PURE_TOTAL_RE = re.compile(r"^(?:grand\s+|sub\s*)?totals?\s*:?\s*$", re.IGNORECASE)
# "TOTAL CHEESE" / "CHEESE TOTAL" — total word at either end. Needs corroboration
# before dropping, because TOTAL is also a real dairy brand (FAGE Total yoghurt).
_EDGE_TOTAL_RE = re.compile(r"^(?:grand\s+|sub\s*)?totals?\b|\btotals?\s*:?\s*$",
                            re.IGNORECASE)


def run_inventory_agent(session_id: int, model: str, confirmed_groups: list, context: dict, progress_emit=None) -> dict:
    _emit(progress_emit, "Loading company config for analysis rules")
    sess_rows_inv = query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    org_name_inv  = sess_rows_inv[0]["org_name"] if sess_rows_inv else "Unknown"
    inv_config    = get_company_config(org_name_inv)
    industry      = (inv_config.get("industry") or "general").lower()
    company_desc  = inv_config.get("company_description") or org_name_inv

    _emit(progress_emit, "Reading inventory snapshot from your data")
    inv_table = f"inventory_{session_id}"
    sal_table = f"sales_{session_id}"

    try:
        inventory = query(f"SELECT * FROM {inv_table} LIMIT 3000")
        _emit(progress_emit, f"Loaded {len(inventory)} inventory rows")
    except Exception as e:
        # Raw exception text is for the operator (logs + ALERT_EMAIL via the
        # returned error) — the user-facing progress log gets a generic line.
        _emit(progress_emit, "Could not read the inventory table — stopping")
        return {"error": f"Could not read inventory table: {e}"}

    alias_map = {}
    for group in confirmed_groups:
        for variant in group.get("variants", []):
            alias_map[variant.lower()] = group["canonical"]

    # Resolve supplier + lead time for every item so the inventory agent can
    # use lead-time-relative thresholds instead of fixed 1/3-month cutoffs.
    _emit(progress_emit, "Resolving supplier lead times for each item")
    _, item_lt_map, _ = _resolve_item_suppliers(
        session_id, org_name_inv, inv_config, alias_map, progress_emit
    )

    _emit(progress_emit, "Computing sales velocity for each item")
    sales_by_item = {}
    try:
        sample = query(f"SELECT * FROM {sal_table} LIMIT 1")
        if sample:
            cols = list(sample[0].keys())
            desc_col = next((c for c in cols if c in ("inventory_desc", "item_description", "description", "product_name")), None)
            if not desc_col:
                desc_col = next((c for c in cols if any(k in c.lower() for k in ("desc", "item_name", "product_name", "item")) and "supplier" not in c.lower()), None)
            qty_col = next((c for c in cols if c in ("billing_qty", "qty", "quantity", "billing_quantity")), None)
            if not qty_col:
                qty_col = next((c for c in cols if any(k in c.lower() for k in ("qty", "quantity")) and "allocated" not in c.lower()), None)
            rev_col = next((c for c in cols if c in ("net_amount", "total_amount", "amount", "billing_amount",
                            "sales_value", "net_value", "total_value", "revenue", "ext_price")), None)
            if not rev_col:
                rev_col = next((c for c in cols if any(k in c.lower() for k in ("amount", "value", "revenue", "price"))), None)
            avg_col = detect_avg_month_column(cols)
            if avg_col:
                _emit(progress_emit,
                      f"Sales sheet states its own monthly average ('{avg_col}') — "
                      "using it directly for velocity")
            if desc_col and (qty_col or rev_col):
                select_parts = [f'"{desc_col}" as item_name']
                select_parts.append(f'SUM({_num_sql(qty_col)}) as total_qty' if qty_col else '0 as total_qty')
                select_parts.append(f'SUM({_num_sql(rev_col)}) as total_revenue' if rev_col else '0 as total_revenue')
                select_parts.append(f'AVG({_num_sql(avg_col)}) as avg_monthly_direct' if avg_col
                                    else '0 as avg_monthly_direct')
                select_parts.append('COUNT(*) as txn_count')
                sal_rows = query(
                    'SELECT ' + ', '.join(select_parts) +
                    ' FROM ' + sal_table + f' GROUP BY "{desc_col}" LIMIT 5000'
                )
                sales_by_item = {r["item_name"]: r for r in sal_rows if r["item_name"]}
                _emit(progress_emit, f"Sales velocity computed for {len(sales_by_item)} items")
    except Exception:
        sales_by_item = {}

    _sample = inventory[0] if inventory else {}
    _cols = list(_sample.keys())

    # Column detection: a previously saved mapping wins; otherwise the AI maps
    # the columns itself (LLM proposal, validated in Python, keyword fallback —
    # propose_inventory_columns can only ever improve on the keyword guess).
    # The old "confirm your columns" user step is gone: workers shouldn't need
    # to understand column mapping for the pipeline to read their file right.
    _kw = detect_inventory_columns(_cols)
    _cmap = {}
    try:
        _cmap_rows = query("SELECT column_map_json FROM upload_sessions WHERE id=?", (session_id,))
        if _cmap_rows and _cmap_rows[0].get("column_map_json"):
            _cmap = json.loads(_cmap_rows[0]["column_map_json"]) or {}
    except Exception:
        _cmap = {}
    if not _cmap:
        try:
            _headers_for_map = [c for c in _cols if c != "_session_id"]
            _cmap = propose_inventory_columns(_headers_for_map, inventory[:8], model) or {}
            execute("UPDATE upload_sessions SET column_map_json=? WHERE id=?",
                    (json.dumps(_cmap), session_id))
            _emit(progress_emit, "AI mapped your file's columns automatically")
        except Exception:
            _cmap = {}

    def _pick_col(field):
        v = _cmap.get(field)
        return v if (isinstance(v, str) and v in _cols) else _kw.get(field)

    _desc_col = _pick_col("description")
    _qty_col  = _pick_col("stock")
    _cat_col  = _pick_col("category")
    _uom_col  = _pick_col("uom")

    if not _desc_col or not _qty_col:
        return {"blocked": True, "error": (
            "We couldn't find both an item-name column and a current-stock column in your "
            "Inventory Report. Make sure one column lists product names and one lists how much "
            "is in stock now (not quantity sold). "
            f"(Columns we saw: {[c for c in _cols if c != '_session_id']})"
        )}
    _emit(progress_emit,
          f"Columns detected — item: {_desc_col}, stock: {_qty_col}"
          + (f", category: {_cat_col}" if _cat_col else "")
          + (f", unit: {_uom_col}" if _uom_col else ""))

    # Guard: a stock column that exists but is entirely blank means the file
    # carries no stock data at all (e.g. a hand-made sheet whose balance column
    # was left empty). Every health label would be fiction — stop and say so.
    try:
        _filled = query(
            f'SELECT COUNT(*) AS n FROM {inv_table} '
            f'WHERE TRIM(COALESCE("{_qty_col}", \'\')) != \'\''
        )
        _n_filled = (_filled[0]["n"] or 0) if _filled else 0
    except Exception:
        _n_filled = 1  # if the count itself fails, don't block the run
    if _n_filled == 0:
        _emit(progress_emit,
              f"Stock column '{_qty_col}' is empty on every row — stopping")
        return {"blocked": True, "error": (
            f"The stock column in your Inventory Report ('{_qty_col}') is empty on every row. "
            "berthcast reads each item's current stock from this column to judge health, so it "
            "can't run without it. Either fill in that column (use 0 for items genuinely out of "
            "stock), or — if your stock levels are in a different file — upload that file as the "
            "Inventory Report instead."
        )}

    # ── Data safety net ───────────────────────────────────────────────────────
    # Detect files we may have misread and fail LOUD, never silently wrong.
    # BLOCK stops the run with a plain reason; WARN rides along to the
    # results-page banner via data_notes. The gate reads the ingested tables
    # itself (see data_quality.assess_upload) and must never break a run, so the
    # whole call is fail-open: a gate exception just means no extra checks.
    data_notes = []
    try:
        # Imported lazily: data_quality imports agents.shared, so a top-level
        # import here would be circular (agents/__init__ -> inventory -> data_quality).
        from data_quality import assess_upload
        _findings = assess_upload(session_id, column_map={
            "description": _desc_col, "stock": _qty_col, "uom": _uom_col})
    except Exception:
        _findings = []
    _block = next((f for f in _findings if f.get("level") == "block"), None)
    if _block:
        _emit(progress_emit, f"Stopping — data check failed ({_block['code']})")
        return {"error": _block["message"], "blocked": True}
    for _f in _findings:
        if _f.get("level") == "warn":
            data_notes.append(_f["message"])
            _emit(progress_emit, f"Data check flagged: {_f['code']}")

    # Forward-fill merged label cells. In Excel/ERP exports a category (or any
    # grouping label) that spans several rows is a MERGED cell: only the first
    # row of the group carries the value, the rest arrive blank. Inherit the
    # last seen value DOWN the column so every item keeps its group.
    #
    # We do this ONLY for the category label, never for numbers. Forward-filling
    # a stock or sales figure would fabricate data we don't have (it would copy
    # the row-above's number onto an item that's actually blank) — exactly the
    # kind of silent wrong answer we're trying to kill. `inventory` is still in
    # file order here (no ORDER BY on the load), which is what makes fill-down
    # correct; we mutate the row dicts in place before any sorting.
    if _cat_col:
        _last_cat = None
        _filled_cats = 0
        for _row in inventory:
            _v = str(_row.get(_cat_col) or "").strip()
            if _v:
                _last_cat = _v
            elif _last_cat is not None:
                _row[_cat_col] = _last_cat
                _filled_cats += 1
        if _filled_cats:
            _emit(progress_emit,
                  f"Filled {_filled_cats} blank '{_cat_col}' cells from merged groups above them")

    # Drop total/subtotal summary rows. ERP exports and hand-made sheets carry
    # "TOTAL CHEESE" / "Grand Total" lines inside the data; analysed as items
    # they double-count stock and pollute the report. A bare "Total" is dropped
    # on the name alone. "TOTAL <something>" needs corroboration — the row must
    # also lack a unit (real products carry KG/CTN/PCS; summary lines don't) —
    # so a genuine product like FAGE TOTAL yoghurt survives. Sheets with no
    # unit column keep edge-total rows: noise is better than dropping product.
    _kept_rows = []
    _dropped_totals = 0
    for _row in inventory:
        _name = str(_row.get(_desc_col) or "").strip()
        _uomv = str(_row.get(_uom_col) or "").strip() if _uom_col else ""
        _is_total = bool(_PURE_TOTAL_RE.match(_name)) or (
            bool(_EDGE_TOTAL_RE.search(_name)) and _uom_col and not _uomv
        )
        if _is_total:
            _dropped_totals += 1
        else:
            _kept_rows.append(_row)
    if _dropped_totals:
        inventory = _kept_rows
        _emit(progress_emit,
              f"Skipped {_dropped_totals} total/subtotal row(s) — summary lines, not items")

    # Combine rows that are the same item (per-warehouse / per-batch splits are
    # common in ERP exports). Judged separately, each row gets compared against
    # the item's FULL sales velocity, so a 100 + 400 split reads as two
    # near-critical items instead of one healthy 500. Sum stock only when the
    # rows agree on unit (or carry none) — adding 100 KG to 2 CTN would be
    # meaningless, so unit conflicts stay as separate rows. Blank names also
    # stay separate: they may be different items we just can't name.
    _first_by_key: dict = {}
    _agg_rows = []
    _merged_dups = 0
    for _row in inventory:
        _name = str(_row.get(_desc_col) or "").strip()
        if not _name:
            _agg_rows.append(_row)
            continue
        _ckey = alias_map.get(_name.lower(), _name.lower())
        _first = _first_by_key.get(_ckey)
        if _first is None:
            _first_by_key[_ckey] = _row
            _agg_rows.append(_row)
            continue
        _u1 = str(_first.get(_uom_col) or "").strip().upper() if _uom_col else ""
        _u2 = str(_row.get(_uom_col) or "").strip().upper() if _uom_col else ""
        if _u1 and _u2 and _u1 != _u2:
            _agg_rows.append(_row)
            continue
        _tot = _to_num(_first.get(_qty_col)) + _to_num(_row.get(_qty_col))
        # .10g not :g — plain :g goes scientific above ~1e6, which downstream
        # parsing would reject; 10 significant digits covers any real warehouse.
        _first[_qty_col] = f"{_tot:.10g}"
        if _uom_col and not _u1 and _u2:
            _first[_uom_col] = _row.get(_uom_col)
        _merged_dups += 1
    if _merged_dups:
        inventory = _agg_rows
        _emit(progress_emit,
              f"Combined {_merged_dups} duplicate row(s) — same item split across "
              "multiple rows, stock summed")

    # Match sales rows to inventory items with drift-tolerant name matching
    # (case, spacing, punctuation, and annotations staff typed into the sales
    # sheet — e.g. "ITEM NAME <- out of stock"). Exact matching silently lost
    # the sales history of exactly the items that most needed a reorder.
    claimed_keys = set()
    for _row in inventory:
        _nm = str(_row.get(_desc_col) or "").strip()
        if _nm:
            claimed_keys.add(normalise_match_key(alias_map.get(_nm.lower(), _nm)))
    sales_index = SalesNameIndex(sales_by_item, alias_map, claimed_keys=claimed_keys)
    _matched_n = sum(1 for k in claimed_keys if k in sales_index)
    _emit(progress_emit,
          f"Sales data matched {_matched_n} of {len(claimed_keys)} inventory items")
    if _matched_n < len(claimed_keys):
        _emit(progress_emit,
              "Items the sales file doesn't cover are judged on stock alone — "
              "missing sales data is never treated as proof an item is dead")

    # ── Read analysis scope (set by user on upload page) ──────────────────────
    scope_rows = query("SELECT scope FROM upload_sessions WHERE id=?", (session_id,))
    scope = (scope_rows[0]["scope"] if scope_rows and scope_rows[0]["scope"] else "all")
    _emit(progress_emit, f"Analysis scope: {scope}")

    # ── If scoped, rank items by revenue and keep only top N ──────────────────
    top_item_names = None  # None = no filter
    if scope != "all":
        try:
            n = int(scope)
            sal_sample = query(f"SELECT * FROM {sal_table} LIMIT 1")
            if sal_sample:
                sal_cols = list(sal_sample[0].keys())

                # Description column — broad match (exact list first, then fuzzy)
                desc_col_s = next((c for c in sal_cols if c in (
                    "inventory_desc", "item_description", "description", "product_name")), None)
                if not desc_col_s:
                    desc_col_s = next((c for c in sal_cols
                        if any(k in c.lower() for k in ("desc", "item_name", "product_name", "item"))
                        and "supplier" not in c.lower()), None)

                # Revenue column — exact list first, then fuzzy
                val_col_s = next((c for c in sal_cols if c in (
                    "net_amount", "total_amount", "amount", "value", "billing_amount",
                    "sales_value", "net_value", "total_value", "revenue", "ext_price",
                    "unit_price", "price", "sales_amount", "invoice_amount")), None)
                if not val_col_s:
                    val_col_s = next((c for c in sal_cols
                        if any(k in c.lower() for k in ("amount", "value", "revenue", "price"))), None)

                # Quantity column — exact list first, then fuzzy
                qty_col_s = next((c for c in sal_cols if c in (
                    "billing_qty", "qty", "quantity", "billing_quantity", "order_qty",
                    "sales_qty", "shipped_qty")), None)
                if not qty_col_s:
                    qty_col_s = next((c for c in sal_cols
                        if any(k in c.lower() for k in ("qty", "quantity"))), None)

                _emit(progress_emit,
                    f"Scope columns detected — desc: {desc_col_s}, revenue: {val_col_s}, qty: {qty_col_s}")

                if desc_col_s and (val_col_s or qty_col_s):
                    rank_col = val_col_s if val_col_s else qty_col_s
                    metric   = "revenue" if val_col_s else "quantity sold"
                    top_rows = query(
                        f'SELECT "{desc_col_s}" as item, '
                        f'SUM({_num_sql(rank_col)}) as metric '
                        f'FROM {sal_table} '
                        f'WHERE "{desc_col_s}" IS NOT NULL '
                        f'GROUP BY "{desc_col_s}" '
                        f'ORDER BY metric DESC '
                        f'LIMIT {n}'
                    )
                    # Alias to canonical, then normalise — case/spacing/punctuation
                    # drift between the sales and inventory files must not silently
                    # drop a top seller from a scoped run.
                    raw_names = {r["item"].strip().lower() for r in top_rows if r["item"]}
                    top_item_names = {
                        k for k in (normalise_match_key(alias_map.get(name, name))
                                    for name in raw_names) if k
                    }
                    _emit(progress_emit,
                        f"Top {n} items by {metric} identified ({len(top_item_names)} unique) — filtering inventory")

                    # Safety check: if matching produced nothing useful, fall back to all items
                    if not top_item_names:
                        _emit(progress_emit, "WARNING: scope filter produced 0 items — falling back to all items")
                        top_item_names = None
                else:
                    _emit(progress_emit,
                        f"Scope filter skipped — could not detect required columns in sales table "
                        f"(cols: {sal_cols[:10]})")
        except Exception:
            _emit(progress_emit, "Scope filter skipped (will use all items)")

    # ── Sort by quantity ascending (zero-stock first) ─────────────────────────
    def _qty_key(row):
        return _to_num(row.get(_qty_col))

    inventory_sorted = sorted(inventory, key=_qty_key)

    # Apply scope filter if active — same alias-then-normalise treatment as the
    # top-N names above, so both sides of the comparison speak the same key.
    if top_item_names is not None:
        def _scope_key(row):
            nm = str(row.get(_desc_col) or "").strip().lower()
            return normalise_match_key(alias_map.get(nm, nm))

        inventory_sorted = [row for row in inventory_sorted
                            if _scope_key(row) in top_item_names]
        # Second-chance: match top items by their raw (un-aliased) name, for
        # items alias_map didn't fold to a canonical key.
        if not inventory_sorted and top_item_names:
            inventory_sorted = [
                row for row in sorted(inventory, key=_qty_key)
                if normalise_match_key(str(row.get(_desc_col) or "").strip()) in top_item_names
            ]
        # Final safety: if still empty after both passes, use all items
        if not inventory_sorted:
            _emit(progress_emit,
                "WARNING: scope name matching found 0 inventory items — using all items instead")
            inventory_sorted = sorted(inventory, key=_qty_key)

    # Build UOM lookup: canonical item name → UOM string (e.g. "CTN", "KG", "PCS")
    uom_by_item: dict = {}
    if _uom_col:
        for row in inventory:
            desc = row.get(_desc_col) or ""
            canonical_key = alias_map.get(str(desc).strip().lower(), str(desc).strip().lower())
            uom_val = str(row.get(_uom_col) or "").strip()
            if uom_val and canonical_key and canonical_key not in uom_by_item:
                uom_by_item[canonical_key] = uom_val

    # Establish the sales period, in trust order:
    #   1. real dates in the sales file (ground truth);
    #   2. the sheet's own numbers — total qty ÷ stated Avg/Month per item
    #      (summary exports carry no dates but state their own averages);
    #   3. assume 12 months, and SAY SO on the results page, because a wrong
    #      period silently scales every velocity and suggested quantity.
    months_of_data = None
    months_assumed = False
    try:
        _sal_cols_sample = query(f"SELECT * FROM {sal_table} LIMIT 1")
        _sal_col_names   = list(_sal_cols_sample[0].keys()) if _sal_cols_sample else []
        _DATE_EXACT = ("date", "invoice_date", "order_date", "transaction_date",
                       "sales_date", "po_date", "doc_date", "posting_date")
        _date_col = next((c for c in _sal_col_names if c.lower() in _DATE_EXACT), None)
        if not _date_col:
            _date_col = next((c for c in _sal_col_names if "date" in c.lower()), None)
        if _date_col:
            # Parse dates in Python — handles DD/MM/YYYY, 15-Jun-26 and Excel
            # serials, none of which SQLite's strftime understands. substr(1,10)
            # trims any time-of-day part so a datetime-stamped export can't blow
            # the DISTINCT limit and undercount months (which inflates velocity).
            _date_rows = query(
                f'SELECT DISTINCT substr("{_date_col}", 1, 10) AS d '
                f'FROM {sal_table} LIMIT 5000')
            _counted = count_sales_months([r["d"] for r in _date_rows])
            if _counted:
                months_of_data, _fmt = _counted
                _emit(progress_emit,
                      f"Sales data covers {months_of_data} month(s) — dates read as {_fmt}")
            else:
                # Last resort: the old ISO-only SQL count.
                _mo_rows = query(
                    f'SELECT COUNT(DISTINCT strftime("%Y-%m", "{_date_col}")) as m '
                    f'FROM {sal_table} LIMIT 1')
                _m = (_mo_rows[0]["m"] or 0) if _mo_rows else 0
                if _m > 0:
                    months_of_data = _m
    except Exception:
        months_of_data = None
    if months_of_data is None:
        _inferred = infer_months_from_item_stats(sales_by_item.values())
        if _inferred:
            months_of_data, _n_used = _inferred
            _emit(progress_emit,
                  f"No dates in the sales file — period inferred from its own "
                  f"Qty ÷ Avg/Month figures: ~{months_of_data} month(s) "
                  f"(consistent across {_n_used} items)")
        else:
            months_of_data = 12
            months_assumed = True
            _emit(progress_emit,
                  "WARNING: the sales file has no dates and no Avg/Month column — "
                  "assuming 12 months. If it covers a different period, every "
                  "monthly figure is scaled wrong by that ratio.")
            data_notes.append(
                "The sales file has no dates and no average-per-month column, so "
                "monthly sales were estimated over an assumed 12 months. If the file "
                "covers a different period, every velocity and suggested quantity is "
                "scaled by that ratio — include a date or Avg/Month column for exact "
                "numbers.")

    # Sales-pattern stats (spec 2026-07-10): spiky items get their velocity
    # replaced by the typical month so one bulk order can't fake a CRITICAL.
    pattern_stats = monthly_pattern_stats(session_id)

    inv_summary_lines = []
    # The exact per-item numbers printed into the prompt, kept so the verifier
    # can recompute the status rules against what Claude actually saw.
    verify_inputs = {}
    for row in inventory_sorted:
        desc      = row.get(_desc_col) or "Unknown"
        qty_raw   = row.get(_qty_col)  or "0"
        cat       = (row.get(_cat_col) if _cat_col else None) or "GENERAL"
        canonical = alias_map.get(str(desc).strip().lower(), str(desc).strip())

        # Drift-tolerant lookup; None means the sales file did not cover this
        # item at all — which is missing data, not a real zero.
        sales_info    = sales_index.get(canonical)
        no_sales_data = sales_info is None
        total_sold    = (sales_info or {}).get("total_qty",     0) or 0
        total_revenue = (sales_info or {}).get("total_revenue", 0) or 0

        # Compute months of supply from concrete numbers. A monthly average the
        # sheet itself states beats anything we derive from a guessed period.
        stock_units = _to_num(qty_raw)
        _avg_direct = (sales_info or {}).get("avg_monthly_direct") or 0
        if _avg_direct > 0:
            avg_monthly = _avg_direct
        else:
            avg_monthly = total_sold / months_of_data if total_sold > 0 else 0
            # Spiky items: size on the typical month (median), never the
            # spike-inflated mean. Only on this derived path — a sheet-stated
            # average is the customer's own number and is never overridden.
            _pat = pattern_stats.get(normalise_match_key(canonical)) \
                   or pattern_stats.get(normalise_match_key(str(desc)))
            if _pat and _pat["pattern"] == "spiky" and _pat["corrected_avg"]:
                avg_monthly = _pat["corrected_avg"]
        months_supply = None
        if avg_monthly > 0:
            months_supply = round(stock_units / avg_monthly, 1)
            supply_tag = f" | Months of supply: {months_supply}"
        else:
            supply_tag = ""

        revenue_tag = f" | Revenue: {round(total_revenue)}" if total_revenue > 0 else ""

        # Lead time context — lets Claude judge urgency relative to reorder horizon
        lt_info = item_lt_map.get(canonical) or item_lt_map.get(desc)
        lt_months = None
        if lt_info and lt_info.get("lead_time_days"):
            lt_days = lt_info["lead_time_days"]
            lt_months = round(lt_days / 30, 1)
            lt_tag = f" | Lead time: {lt_days}d ({lt_months}mo)"
        else:
            lt_tag = ""

        sold_txt = ("no sales data in upload" if no_sales_data
                    else str(round(total_sold)))
        verify_inputs[normalise_match_key(canonical)] = {
            "months_supply": months_supply,
            "lt_months":     lt_months,
            "stock":         stock_units,
            # round() to match the displayed figure — Claude judges what it sees
            "total_sold":    None if no_sales_data else round(total_sold),
        }
        inv_summary_lines.append(
            f"Item: {canonical} | Category: {cat} | Stock: {qty_raw} | "
            f"Total sold ({months_of_data}mo): {sold_txt}{revenue_tag}{supply_tag}{lt_tag}"
        )

    if not inv_summary_lines:
        return {"error": f"No inventory rows. desc_col={_desc_col}, qty_col={_qty_col}, rows={len(inventory)}"}

    _emit(progress_emit, f"Prepared {len(inv_summary_lines)} items for analysis (zero-stock items prioritised)")

    context_text = _format_context(context)

    # Build spoilage rules block based on industry
    if "food" in industry or "beverage" in industry or "fmcg" in industry or "perishable" in industry:
        spoilage_rules = (
            "- CHILL items with slow movement are HIGH spoilage risk\n"
            "- FROZEN items with no movement in 60+ days are MEDIUM-HIGH spoilage risk\n"
            "- DRY items with no movement in 180+ days are LOW risk but flag as DEAD SKU\n"
        )
    else:
        spoilage_rules = (
            "- Items with no movement in 180+ days are LOW spoilage risk but flag as DEAD SKU\n"
            "- Perishable or time-sensitive items (if category indicates it) get HIGH spoilage risk\n"
            "- All other slow-moving items: NONE spoilage risk unless category suggests otherwise\n"
        )

    system_prompt = (
        f"You are an inventory health analyst for: {company_desc}\n\n"
        + UNTRUSTED_GUARD + "\n\n"
        "Each item line includes: item name, category, current stock, total units sold over N months, "
        "optional revenue, 'Months of supply' (stock ÷ avg monthly sales), and optionally "
        "'Lead time' (days and months the supplier takes to deliver).\n\n"
        "For each item, determine:\n"
        "1. Status: HEALTHY / LOW / CRITICAL / DEAD\n"
        "2. Spoilage risk: HIGH / MEDIUM / LOW / NONE\n"
        "3. Days of supply (use Months of supply × 30 when provided, otherwise estimate)\n"
        "4. A one-line plain English observation\n\n"
        "STATUS RULES — lead-time-aware thresholds:\n"
        "When Lead time IS provided for an item, use it to set dynamic thresholds:\n"
        "  Let LT = lead time in months (given in the data).\n"
        "  - CRITICAL: Months of supply < LT (not enough stock to survive one reorder cycle), "
        "    OR total sold > 0 AND stock = 0.\n"
        "  - LOW: Months of supply between LT and LT + 2 (tight — reorder window closing).\n"
        "  - HEALTHY: Months of supply > LT + 2.\n"
        "When Lead time is NOT provided, fall back to fixed thresholds:\n"
        "  - CRITICAL: Months of supply < 1, OR total sold > 0 AND stock = 0.\n"
        "  - LOW: Months of supply between 1 and 3.\n"
        "  - HEALTHY: Months of supply > 3.\n"
        "DEAD rules — require sales EVIDENCE, never absence of data:\n"
        "  - DEAD: the item HAS sales data and total sold = 0 (and nothing suggests it is "
        "new or seasonal). Once DEAD, set spoilage_risk = NONE.\n"
        "  - 'no sales data in upload' means the sales file did not cover this item. "
        "That is missing data, NOT proof the item doesn't sell.\n"
        "    - no sales data AND stock = 0: mark DEAD, but the observation MUST say sales "
        "data was missing and that the item should be included in the sales export if it still sells.\n"
        "    - no sales data AND stock > 0: mark HEALTHY, observation noting demand can't be "
        "judged from this upload. NEVER mark these DEAD.\n\n"
        "Spoilage rules:\n"
        + spoilage_rules +
        "\nReturn ONLY a JSON array of objects with keys:\n"
        "item, category, stock, status, spoilage_risk, days_of_supply, observation\n"
        "Do not include text outside the JSON array."
    )
    inv_batches = [inv_summary_lines[i:i+_INV_BATCH]
                   for i in range(0, len(inv_summary_lines), _INV_BATCH)]
    n_batches   = len(inv_batches)
    total_items = len(inv_summary_lines)
    if n_batches > 1:
        _emit(progress_emit,
              f"Reading your {total_items} items and checking each one against demand — this is the slow part")
    else:
        _emit(progress_emit,
              f"Reading your {total_items} items and checking each one against demand — up to a minute")

    try:
        all_items   = []
        any_repaired = False
        for i, batch in enumerate(inv_batches, 1):
            user_prompt = (
                f"Inventory snapshot"
                + (f" — batch {i}/{n_batches}" if n_batches > 1 else "")
                + f" ({len(batch)} items, data covers {months_of_data} months):\n\n"
                + wrap_untrusted("\n".join(batch))
                + "\n\nContext from purchasing team:\n"
                + wrap_untrusted(context_text)
                + "\n\nReturn the health report JSON."
            )
            raw = _call_claude(model, system_prompt, user_prompt, max_tokens=_INV_MAX_TOKENS)
            parsed, repaired = _extract_json_array(raw)
            if parsed is None:
                _emit(progress_emit,
                      f"WARNING: inventory batch {i}/{n_batches} returned no usable response — skipping")
                continue
            all_items.extend(parsed)
            if repaired:
                any_repaired = True
                _emit(progress_emit,
                      f"WARNING: the reply for batch {i}/{n_batches} was cut short — "
                      f"kept {len(parsed)} of {len(batch)} items")
            # Plain-language running count for the waiting screen.
            _crit = sum(1 for r in all_items if r.get("status") == "CRITICAL")
            _low  = sum(1 for r in all_items if r.get("status") == "LOW")
            _emit(progress_emit,
                  f"Checked {min(len(all_items), total_items)} of {total_items} items"
                  f" — {_crit} critical, {_low} running low so far")

        if not all_items:
            _emit(progress_emit, "Inventory agent returned no usable response")
            return {"error": "Inventory agent returned no usable JSON for any batch."}

        report = all_items

        # Deterministic safety net: recompute the prompt's own status rules
        # from the numbers Claude was given and correct provable slips before
        # they reach the results page or the recommendation agent.
        _n_st, _n_dos, _n_sp = verify_inventory_report(report, verify_inputs)
        if _n_st or _n_dos or _n_sp:
            _parts = []
            if _n_st:  _parts.append(f"{_n_st} status label(s)")
            if _n_dos: _parts.append(f"{_n_dos} days-of-supply figure(s)")
            if _n_sp:  _parts.append(f"{_n_sp} spoilage flag(s)")
            _emit(progress_emit,
                  "Safety check: corrected " + ", ".join(_parts) +
                  " that didn't match the item's own numbers")

        crit = sum(1 for r in report if r.get("status") == "CRITICAL")
        low  = sum(1 for r in report if r.get("status") == "LOW")
        _emit(progress_emit,
              f"Done checking stock — {len(report)} items: {crit} critical, {low} running low")
        return {"report": report, "items_analysed": len(report), "partial": any_repaired,
                "data_notes": data_notes}
    except Exception as e:
        # Raw exception text is for the operator (logs + ALERT_EMAIL via the
        # returned error) — the user-facing progress log gets a generic line.
        _emit(progress_emit, "Inventory agent hit an unexpected error — stopping")
        return {"error": f"Inventory agent error: {str(e)}"}
