"""Agent 2 — the inventory health check.

Classifies every item as HEALTHY / LOW / CRITICAL / DEAD with spoilage risk and
days-of-supply, using lead-time-aware thresholds. Moved verbatim from agents.py.
"""

from database import query, get_company_config
from .shared import (
    _emit,
    _resolve_item_suppliers,
    _format_context,
    _call_claude,
    _extract_json_array,
    _num_sql,
    _pick_stock_column,
)


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
        _emit(progress_emit, f"Could not read inventory table: {e}")
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
            if desc_col and (qty_col or rev_col):
                select_parts = [f'"{desc_col}" as item_name']
                select_parts.append(f'SUM({_num_sql(qty_col)}) as total_qty' if qty_col else '0 as total_qty')
                select_parts.append(f'SUM({_num_sql(rev_col)}) as total_revenue' if rev_col else '0 as total_revenue')
                select_parts.append('COUNT(*) as txn_count')
                sal_rows = query(
                    'SELECT ' + ', '.join(select_parts) +
                    ' FROM ' + sal_table + f' GROUP BY "{desc_col}" LIMIT 5000'
                )
                sales_by_item = {r["item_name"]: r for r in sal_rows if r["item_name"]}
                _emit(progress_emit, f"Sales velocity computed for {len(sales_by_item)} items")
    except Exception:
        sales_by_item = {}

    # Build canonical-keyed lookup so inventory names (which may differ from sales names)
    # still resolve to their sales data after alias_map normalisation
    sales_by_canonical: dict = {}
    for raw_name, info in sales_by_item.items():
        key = alias_map.get(str(raw_name).strip().lower(), str(raw_name).strip().lower())
        if key in sales_by_canonical:
            ex = sales_by_canonical[key]
            sales_by_canonical[key] = {
                "item_name":     key,
                "total_qty":     (ex.get("total_qty")     or 0) + (info.get("total_qty")     or 0),
                "total_revenue": (ex.get("total_revenue") or 0) + (info.get("total_revenue") or 0),
                "txn_count":     (ex.get("txn_count")     or 0) + (info.get("txn_count")     or 0),
            }
        else:
            sales_by_canonical[key] = dict(info)
    _emit(progress_emit, f"Sales lookup built: {len(sales_by_canonical)} canonical items")

    _sample = inventory[0] if inventory else {}
    _cols = list(_sample.keys())

    DESC_EXACT = ("description", "item_description", "inventory_desc", "product_description",
                  "item_name", "product_name", "stock_description", "item_desc")
    CAT_EXACT  = ("category", "cat", "class", "item_category", "product_category", "storage_type")
    UOM_EXACT  = ("uom", "unit_of_measure", "unit", "uom_code", "uom_description",
                  "base_uom", "purchase_uom", "sales_uom", "stock_uom")

    _desc_col = next((k for k in _cols if k in DESC_EXACT), None) or \
                next((k for k in _cols if ("desc" in k or "item_name" in k or "product_name" in k) and "supplier" not in k), None)
    _qty_col  = _pick_stock_column(_cols)
    _cat_col  = next((k for k in _cols if k in CAT_EXACT), None) or \
                next((k for k in _cols if "cat" in k or "class" in k or "storage" in k), None)
    _uom_col  = next((k for k in _cols if k.lower() in UOM_EXACT), None) or \
                next((k for k in _cols if "uom" in k.lower() or "unit_of" in k.lower()), None)

    if not _desc_col or not _qty_col:
        return {"error": (
            "Could not detect description/quantity columns in your Inventory Report. "
            f"Detected columns: {_cols}. "
            f"Found desc_col={_desc_col}, qty_col={_qty_col}, cat_col={_cat_col}. "
            "Open the Inventory Report and confirm one column has the item name and one has "
            "the current stock balance. Note: a 'Qty Sold' column is sales history, not stock."
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
        return {"error": (
            f"The stock column in your Inventory Report ('{_qty_col}') is empty on every row. "
            "berthcast needs each item's current stock quantity to judge health — without it "
            "the analysis would be guesswork. Fill in that column (or re-export the report "
            "with stock balances included) and upload again."
        )}

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
                    # Normalise names through alias_map so variants map to canonical
                    raw_names = {r["item"].strip().lower() for r in top_rows if r["item"]}
                    top_item_names = {
                        alias_map.get(name, name) for name in raw_names
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
        except Exception as e:
            _emit(progress_emit, f"Scope filter skipped (will use all items): {e}")

    # ── Sort by quantity ascending (zero-stock first) ─────────────────────────
    def _qty_key(row):
        try:
            return float(str(row.get(_qty_col) or "0").replace(",", "").strip() or 0)
        except (ValueError, TypeError):
            return 0

    inventory_sorted = sorted(inventory, key=_qty_key)

    # Apply scope filter if active
    if top_item_names is not None:
        inv_names_normalised = {
            alias_map.get(str(row.get(_desc_col) or "").strip().lower(),
                          str(row.get(_desc_col) or "").strip().lower()): row
            for row in inventory_sorted
        }
        inventory_sorted = [
            row for row in inventory_sorted
            if alias_map.get(str(row.get(_desc_col) or "").strip().lower(),
                             str(row.get(_desc_col) or "").strip().lower()) in top_item_names
        ]
        # Second-chance: direct raw name match (catches items alias_map didn't normalise)
        if not inventory_sorted and top_item_names:
            raw_top = {r["item"].strip().lower() for r in
                       (query(
                           f'SELECT "{desc_col_s}" as item FROM {sal_table} '
                           f'WHERE "{desc_col_s}" IS NOT NULL GROUP BY "{desc_col_s}" '
                           f'ORDER BY SUM({_num_sql(rank_col)}) DESC LIMIT {n}'
                       ) if 'desc_col_s' in dir() else [])} if False else set()
            inventory_sorted = [
                row for row in sorted(inventory, key=_qty_key)
                if str(row.get(_desc_col) or "").strip().lower() in top_item_names
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

    # Estimate months of data coverage from distinct year-months in sales table.
    # Detect the actual date column name — ERP exports use different names.
    try:
        _sal_cols_sample = query(f"SELECT * FROM {sal_table} LIMIT 1")
        _sal_col_names   = list(_sal_cols_sample[0].keys()) if _sal_cols_sample else []
        _DATE_EXACT = ("date", "invoice_date", "order_date", "transaction_date",
                       "sales_date", "po_date", "doc_date", "posting_date")
        _date_col = next((c for c in _sal_col_names if c.lower() in _DATE_EXACT), None)
        if not _date_col:
            _date_col = next((c for c in _sal_col_names if "date" in c.lower()), None)
        if _date_col:
            _mo_rows = query(
                f'SELECT COUNT(DISTINCT strftime("%Y-%m", "{_date_col}")) as m FROM {sal_table} LIMIT 1'
            )
            months_of_data = max(1, (_mo_rows[0]["m"] or 0) if _mo_rows else 0) or 12
        else:
            months_of_data = 12
            _emit(progress_emit, "WARNING: no date column found in sales table — defaulting to 12 months for velocity calculation")
    except Exception:
        months_of_data = 12

    inv_summary_lines = []
    for row in inventory_sorted:
        desc      = row.get(_desc_col) or "Unknown"
        qty_raw   = row.get(_qty_col)  or "0"
        cat       = (row.get(_cat_col) if _cat_col else None) or "GENERAL"
        canonical = alias_map.get(str(desc).strip().lower(), str(desc).strip())

        # Canonical lookup handles name mismatches between inventory and sales tables
        lookup_key = canonical.strip().lower()
        sales_info    = sales_by_canonical.get(lookup_key, {})
        total_sold    = sales_info.get("total_qty",     0) or 0
        total_revenue = sales_info.get("total_revenue", 0) or 0

        # Compute months of supply from concrete numbers
        try:
            stock_units = float(str(qty_raw).replace(",", "").strip() or 0)
        except (ValueError, TypeError):
            stock_units = 0
        avg_monthly = total_sold / months_of_data if total_sold > 0 else 0
        if avg_monthly > 0:
            months_supply = round(stock_units / avg_monthly, 1)
            supply_tag = f" | Months of supply: {months_supply}"
        else:
            supply_tag = ""

        revenue_tag = f" | Revenue: {round(total_revenue)}" if total_revenue > 0 else ""

        # Lead time context — lets Claude judge urgency relative to reorder horizon
        lt_info = item_lt_map.get(canonical) or item_lt_map.get(desc)
        if lt_info and lt_info.get("lead_time_days"):
            lt_days = lt_info["lead_time_days"]
            lt_months = round(lt_days / 30, 1)
            lt_tag = f" | Lead time: {lt_days}d ({lt_months}mo)"
        else:
            lt_tag = ""

        inv_summary_lines.append(
            f"Item: {canonical} | Category: {cat} | Stock: {qty_raw} | "
            f"Total sold ({months_of_data}mo): {round(total_sold)}{revenue_tag}{supply_tag}{lt_tag}"
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
        "DEAD rules (always apply regardless of lead time):\n"
        "  - DEAD: total sold = 0 AND no reason to believe the item is new or seasonal. "
        "  Also DEAD: stock = 0 AND total sold = 0. Once DEAD, set spoilage_risk = NONE.\n\n"
        "Spoilage rules:\n"
        + spoilage_rules +
        "\nReturn ONLY a JSON array of objects with keys:\n"
        "item, category, stock, status, spoilage_risk, days_of_supply, observation\n"
        "Do not include text outside the JSON array."
    )
    # Process in batches — catalogues larger than 800 items need multiple passes
    _INV_BATCH  = 800
    inv_batches = [inv_summary_lines[i:i+_INV_BATCH]
                   for i in range(0, len(inv_summary_lines), _INV_BATCH)]
    n_batches   = len(inv_batches)
    if n_batches > 1:
        _emit(progress_emit,
              f"Catalogue too large for one pass — splitting into {n_batches} batches of up to {_INV_BATCH} items")
    else:
        _emit(progress_emit, "Asking Claude to assess inventory health (this is the slow part — up to a minute)")

    try:
        all_items   = []
        any_repaired = False
        for i, batch in enumerate(inv_batches, 1):
            if n_batches > 1:
                _emit(progress_emit,
                      f"Inventory health: batch {i}/{n_batches} ({len(batch)} items)")
            user_prompt = (
                f"Inventory snapshot"
                + (f" — batch {i}/{n_batches}" if n_batches > 1 else "")
                + f" ({len(batch)} items, data covers {months_of_data} months):\n\n"
                + "\n".join(batch)
                + f"\n\nContext from purchasing team:\n{context_text}\n\nReturn the health report JSON."
            )
            raw = _call_claude(model, system_prompt, user_prompt, max_tokens=16000)
            parsed, repaired = _extract_json_array(raw)
            if parsed is None:
                _emit(progress_emit,
                      f"WARNING: inventory batch {i}/{n_batches} returned no usable response — skipping")
                continue
            all_items.extend(parsed)
            if repaired:
                any_repaired = True

        if not all_items:
            _emit(progress_emit, "Inventory agent returned no usable response")
            return {"error": "Inventory agent returned no usable JSON for any batch."}

        report = all_items
        crit = sum(1 for r in report if r.get("status") == "CRITICAL")
        low  = sum(1 for r in report if r.get("status") == "LOW")
        _emit(progress_emit,
              f"Inventory health complete — {len(report)} items reviewed, {crit} critical, {low} low")
        return {"report": report, "items_analysed": len(report), "partial": any_repaired}
    except Exception as e:
        _emit(progress_emit, f"Inventory agent error: {str(e)}")
        return {"error": f"Inventory agent error: {str(e)}"}
