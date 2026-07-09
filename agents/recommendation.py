"""Agent 3 — the purchasing advisor.

Takes the flagged items from the health check and produces consequence-aware
reorder recommendations (act vs don't-act, supplier risk, sized quantity).
Moved verbatim from agents.py.
"""

from database import (
    query,
    get_company_config,
    get_supplier_profile,
    save_recommendation_outcome,
    get_supplier_accuracy,
)
from .shared import (
    _emit,
    _resolve_item_suppliers,
    _infer_supplier_type,
    _format_context,
    _call_claude,
    _extract_json_array,
    _num_sql,
    count_sales_months,
    detect_avg_month_column,
    infer_months_from_item_stats,
    LEAD_TIME_BY_TYPE,
    SalesNameIndex,
    normalise_match_key,
    monthly_pattern_stats,
    apply_sales_pattern_flags,
    wrap_untrusted,
    UNTRUSTED_GUARD,
)
from quantity import sanitize_suggested_quantity


def run_recommendation_agent(session_id: int, model: str, inventory_report: list, context: dict, progress_emit=None) -> list:
    _emit(progress_emit, "Loading company config and supplier profiles")

    # Pull org name from session
    sess_rows = query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    org_name  = sess_rows[0]["org_name"] if sess_rows else "Unknown"

    config = get_company_config(org_name)

    _emit(progress_emit, "Reading supplier list (local vs import) to set lead times")
    item_supplier_map, item_lt_map, supplier_type_map = _resolve_item_suppliers(
        session_id, org_name, config, progress_emit=progress_emit
    )

    # Strip dead SKUs first — they must never reach the recommendation agent
    live_items = [r for r in inventory_report if r.get("status") != "DEAD"]
    dead_count = len(inventory_report) - len(live_items)
    if dead_count:
        _emit(progress_emit, f"Excluded {dead_count} dead SKUs from recommendations")

    actionable = [
        r for r in live_items
        if r.get("status") in ("LOW", "CRITICAL")
        or (r.get("status") != "HEALTHY" and r.get("spoilage_risk") in ("HIGH", "MEDIUM"))
    ]

    if not actionable:
        _emit(progress_emit, "No items need attention right now — inventory looks healthy")
        return []

    _emit(progress_emit, f"Filtered to {len(actionable)} items needing attention")
    _emit(progress_emit, "Building supplier context for consequence reasoning")

    # Build UOM lookup from inventory table
    uom_by_item_r: dict = {}
    try:
        inv_table_r = f"inventory_{session_id}"
        inv_sample_r = query(f"SELECT * FROM {inv_table_r} LIMIT 1")
        if inv_sample_r:
            inv_cols_r = list(inv_sample_r[0].keys())
            UOM_EXACT_R = ("uom", "unit_of_measure", "unit", "uom_code", "uom_description",
                           "base_uom", "purchase_uom", "sales_uom", "stock_uom")
            uom_col_r = next((c for c in inv_cols_r if c.lower() in UOM_EXACT_R), None) or \
                        next((c for c in inv_cols_r if "uom" in c.lower() or "unit_of" in c.lower()), None)
            desc_col_r = next((c for c in inv_cols_r if c in (
                "description", "item_description", "inventory_desc", "product_description",
                "item_name", "product_name", "stock_description", "item_desc")), None) or \
                next((c for c in inv_cols_r if ("desc" in c.lower() or "item_name" in c.lower())
                      and "supplier" not in c.lower()), None)
            if uom_col_r and desc_col_r:
                uom_rows_r = query(
                    f'SELECT "{desc_col_r}" as item, "{uom_col_r}" as uom '
                    f'FROM {inv_table_r} WHERE "{uom_col_r}" IS NOT NULL LIMIT 5000'
                )
                for row in uom_rows_r:
                    key = str(row.get("item") or "").strip().lower()
                    uom_val = str(row.get("uom") or "").strip()
                    if uom_val and key and key not in uom_by_item_r:
                        uom_by_item_r[key] = uom_val
    except Exception:
        uom_by_item_r = {}

    # Compute avg monthly sales per item so we can suggest order quantities.
    # Velocity sources in trust order: a monthly average the sheet itself
    # states > totals divided by the dated period > totals over an assumed
    # 12 months (the inventory agent has already flagged that assumption).
    sales_velocity = None      # SalesNameIndex when sales data is readable
    sales_supplier_idx = None  # supplier names read off the sales sheet
    _claimed_rec = {normalise_match_key(r.get("item"))
                    for r in inventory_report
                    if isinstance(r, dict) and r.get("item")}
    # Sales-pattern stats (spec 2026-07-10): spiky velocity correction,
    # per-item prompt notes, and the deterministic post-pass all read this.
    pattern_stats = monthly_pattern_stats(session_id)
    try:
        sal_table_r = f"sales_{session_id}"
        s_sample = query(f"SELECT * FROM {sal_table_r} LIMIT 1")
        if s_sample:
            s_cols = list(s_sample[0].keys())
            s_desc = next((c for c in s_cols if c in ("inventory_desc", "item_description", "description", "product_name")), None)
            if not s_desc:
                s_desc = next((c for c in s_cols if any(k in c.lower() for k in ("desc", "item_name", "product_name", "item")) and "supplier" not in c.lower()), None)
            s_qty = next((c for c in s_cols if c in ("billing_qty", "qty", "quantity", "billing_quantity")), None)
            if not s_qty:
                s_qty = next((c for c in s_cols if any(k in c.lower() for k in ("qty", "quantity")) and "allocated" not in c.lower()), None)
            s_avg = detect_avg_month_column(s_cols)
            vel_rows = []
            if s_desc and (s_avg or s_qty):
                # Period for the totals fallback, same trust order as the
                # inventory agent: dated months > the sheet's own Qty ÷ Avg
                # ratios > 12 assumed (the inventory agent already flags it).
                months_r = None
                try:
                    _DATE_EXACT_R = ("date", "invoice_date", "order_date", "transaction_date",
                                     "sales_date", "po_date", "doc_date", "posting_date")
                    _date_col_r = next((c for c in s_cols if c.lower() in _DATE_EXACT_R), None)
                    if not _date_col_r:
                        _date_col_r = next((c for c in s_cols if "date" in c.lower()), None)
                    if _date_col_r:
                        # Python-side parse handles DD/MM/YYYY, 15-Jun-26 and
                        # Excel serials; strftime (ISO-only) stays as fallback.
                        # substr(1,10) trims time-of-day so datetime stamps
                        # can't blow the DISTINCT limit and undercount months.
                        _d_rows = query(
                            f'SELECT DISTINCT substr("{_date_col_r}", 1, 10) AS d '
                            f'FROM {sal_table_r} LIMIT 5000')
                        _counted_r = count_sales_months([r["d"] for r in _d_rows])
                        if _counted_r:
                            months_r = _counted_r[0]
                        else:
                            mo_r = query(f'SELECT COUNT(DISTINCT strftime("%Y-%m", "{_date_col_r}")) as m FROM {sal_table_r} LIMIT 1')
                            months_r = ((mo_r[0]["m"] or 0) if mo_r else 0) or None
                except Exception:
                    months_r = None

                # One query carries BOTH velocity sources so each item can use
                # the best one it has. The old either/or read: when the sheet
                # had an Avg/Month column, an item with a blank avg cell but
                # real qty history got no velocity at all ("Verify with team")
                # — while the inventory agent sized the same item from totals.
                _sel = [f'"{s_desc}" as item']
                _sel.append(f'AVG({_num_sql(s_avg)}) as avg_direct' if s_avg
                            else '0 as avg_direct')
                _sel.append(f'SUM({_num_sql(s_qty)}) as total_qty' if s_qty
                            else '0 as total_qty')
                stat_rows = query('SELECT ' + ', '.join(_sel) +
                                  f' FROM {sal_table_r} GROUP BY "{s_desc}" LIMIT 5000')
                if months_r is None:
                    _inferred_r = infer_months_from_item_stats(
                        [{"total_qty": r["total_qty"], "avg_monthly_direct": r["avg_direct"]}
                         for r in stat_rows])
                    months_r = _inferred_r[0] if _inferred_r else 12
                for r in stat_rows:
                    _avg_d = r["avg_direct"] or 0
                    _tot   = r["total_qty"] or 0
                    _avg_m = _avg_d if _avg_d > 0 \
                             else (_tot / months_r if _tot > 0 else 0)
                    if _avg_d <= 0:
                        # Spiky: size on the typical month, not the spike-
                        # inflated mean (sheet-stated averages left alone).
                        _pat = pattern_stats.get(normalise_match_key(str(r["item"] or "")))
                        if _pat and _pat["pattern"] == "spiky" and _pat["corrected_avg"]:
                            _avg_m = _pat["corrected_avg"]
                    vel_rows.append({
                        "item": r["item"],
                        "avg_monthly": _avg_m,
                    })
            if vel_rows:
                # Drift-tolerant index, same as the inventory agent: a sales
                # name carrying a staff annotation must still feed this item's
                # velocity, or its suggested quantity degrades to "verify".
                _vel_raw = {r["item"]: {"avg_monthly": (r["avg_monthly"] or 0)}
                            for r in vel_rows if r["item"]}
                sales_velocity = SalesNameIndex(_vel_raw, claimed_keys=_claimed_rec)

            # Supplier names from the sales sheet — lowest-priority source,
            # consulted only for items the PO table knows nothing about.
            # Supplier/category columns in summary exports use MERGED cells:
            # only the first row of each group carries the value, so fill the
            # last seen name down the column before mapping.
            _sup_col = next((c for c in s_cols
                             if "supplier" in c.lower() or "vendor" in c.lower()), None)
            if s_desc and _sup_col:
                _sup_rows = query(
                    f'SELECT "{s_desc}" as item, "{_sup_col}" as sup '
                    f'FROM {sal_table_r} ORDER BY rowid LIMIT 5000')
                _last_sup = None
                _sup_raw = {}
                for _r in _sup_rows:
                    _sv = str(_r.get("sup") or "").strip()
                    if _sv:
                        _last_sup = _sv
                    _itm = str(_r.get("item") or "").strip()
                    if _itm and _last_sup and _itm not in _sup_raw:
                        _sup_raw[_itm] = {"supplier": _last_sup}
                if _sup_raw:
                    sales_supplier_idx = SalesNameIndex(_sup_raw, claimed_keys=_claimed_rec)
                    _emit(progress_emit,
                          f"Supplier names read from the sales sheet "
                          f"({len(_sup_raw)} items, merged cells filled down)")
    except Exception:
        sales_velocity = None
        sales_supplier_idx = None

    # Build enriched item lines for Claude
    enriched_lines = []
    # Capture the monthly-sales figure + unit used to size each suggested
    # quantity, keyed by item name. Attached to the saved recs below so the
    # results page can explain where the number came from.
    qty_basis_by_item = {}
    for inv_item in actionable:
        iname    = inv_item.get("item", "Unknown")

        # Use shared resolver results; fall back to direct lookup for items
        # that weren't in the PO table (and therefore not in item_lt_map).
        lt_info = item_lt_map.get(iname)
        if lt_info:
            supplier   = lt_info["supplier"]
            stype      = lt_info["type"]
            lt_days    = lt_info["lead_time_days"]
            delay_prob = lt_info["delay_prob"]
            high_risk  = lt_info["high_risk"]
        else:
            supplier = item_supplier_map.get(iname, "Unknown") or "Unknown"
            if supplier == "Unknown" and sales_supplier_idx is not None:
                # Last resort: the supplier named on the sales sheet itself.
                _ss = sales_supplier_idx.get(iname) or {}
                if _ss.get("supplier"):
                    supplier = _ss["supplier"]
            stype    = supplier_type_map.get(supplier, "other")
            if stype == "other" and supplier == "Unknown":
                stype = _infer_supplier_type(iname)
            sup_profile = get_supplier_profile(org_name, supplier)
            if supplier == "Unknown":
                lt_days = None
            else:
                lt_days = (sup_profile.get("avg_lead_time_days")
                           or LEAD_TIME_BY_TYPE.get(stype)
                           or config.get("default_lead_time_days") or None)
            delay_prob = sup_profile.get("delay_probability", 0.2)
            high_risk  = delay_prob > 0.30 or sup_profile.get("data_quality_score", 0.3) < 0.50

        quality   = get_supplier_profile(org_name, supplier).get("data_quality_score", 0.3)
        sup_notes = get_supplier_profile(org_name, supplier).get("notes", "")
        known_sup = quality >= 0.5

        acc      = get_supplier_accuracy(org_name, supplier)
        acc_note = ""
        if acc.get("total_recs", 0) > 0:
            acc_note = (f" | Past recs: {acc['total_recs']} — "
                        f"{acc['approved']} approved, {acc['dismissed']} dismissed")

        # Compute suggested order quantity with adaptive safety buffer.
        # Buffer scales with supplier reliability instead of a flat 1.5 months:
        #   - Reliable local supplier (delay_prob < 0.15):  +0.5 months
        #   - Average supplier (delay_prob 0.15–0.35):      +1.5 months
        #   - Unreliable import (delay_prob > 0.35):         +2.5 months
        _vel = sales_velocity.get(iname) if sales_velocity is not None else None
        avg_monthly = round((_vel or {}).get("avg_monthly", 0) or 0, 1)
        uom = uom_by_item_r.get(iname.lower(), "")
        uom_label = f" {uom}" if uom else " units"
        if avg_monthly > 0:
            lt_months = (lt_days / 30) if lt_days else 2.0
            if delay_prob <= 0.15:
                safety_buffer = 0.5
            elif delay_prob <= 0.35:
                safety_buffer = 1.5
            else:
                safety_buffer = 2.5
            suggested_qty = round(avg_monthly * (lt_months + safety_buffer))
            suggested_qty_str = f"{suggested_qty}{uom_label}"
        else:
            suggested_qty = None
            suggested_qty_str = None
        # Keep the Python-computed figure so we can sanity-check whatever the
        # model echoes back (see sanitize_suggested_quantity below).
        qty_basis_by_item[iname] = (avg_monthly, uom_label, suggested_qty)

        _pat = pattern_stats.get(normalise_match_key(iname))
        if _pat and _pat["pattern"] == "spiky":
            pattern_line = (f"Sales pattern: SPIKY — one month dominates; typical month "
                            f"(median) = {_pat['corrected_avg']}, raw average = {_pat['mean']}. "
                            f"Quantities are sized on the typical month.\n")
        elif _pat and _pat["pattern"] == "volatile":
            pattern_line = (f"Sales pattern: VOLATILE — monthly sales swing between "
                            f"{_pat['min']} and {_pat['max']}. The average may mislead; "
                            f"flag this for the buyer.\n")
        elif _pat and _pat["pattern"] == "lumpy":
            pattern_line = "Sales pattern: IRREGULAR — sells in bursts with many zero months.\n"
        else:
            pattern_line = ""

        enriched_lines.append(
            f"---\n"
            f"Item: {iname}\n"
            f"Status: {inv_item.get('status')} | Spoilage risk: {inv_item.get('spoilage_risk')}\n"
            f"Stock: {inv_item.get('stock')}{uom_label} | Days of supply: {inv_item.get('days_of_supply', 'unknown')}\n"
            f"Avg monthly sales: {avg_monthly}{uom_label}\n"
            f"{pattern_line}"
            f"Pre-computed suggested order quantity: {suggested_qty_str if suggested_qty_str is not None else 'insufficient sales data'}\n"
            f"Supplier: {supplier} ({stype}, lead time: {lt_days if lt_days else 'unknown — do not guess'})\n"
            f"Supplier delay rate: {int(delay_prob*100)}% | "
            f"Supplier known to system: {'Yes' if known_sup else 'No'}"
            + (f" | Notes: {sup_notes}" if sup_notes else "")
            + (f"{acc_note}" if acc_note else "") + "\n"
            f"High-risk supplier: {'YES' if high_risk else 'No'}\n"
            f"Observation: {inv_item.get('observation', '')}\n"
        )

    context_text = _format_context(context)
    company_desc_rec = config.get("company_description") or org_name
    industry_rec = (config.get("industry") or "general").lower()

    # Style example matched to the client's industry. Generic company wording
    # on purpose — a real client's name must never sit in another org's prompt.
    if ("food" in industry_rec or "beverage" in industry_rec
            or "fmcg" in industry_rec or "perishable" in industry_rec):
        example_consequences = (
            '  "consequence_if_acting": "Ordering now locks up cash in 3 months of frozen salmon stock — '
            'if sales slow, the company risks wastage in cold storage."\n'
            '  "consequence_if_not_acting": "Without a reorder, the company will run out of frozen salmon '
            'within 4 days, leaving active customer orders unfulfilled."\n\n'
        )
    else:
        example_consequences = (
            '  "consequence_if_acting": "Ordering now ties up cash in 3 months of stock for a slow-moving '
            'imported line — if demand dips, the company sits on it."\n'
            '  "consequence_if_not_acting": "Without a reorder, the company runs out within days, leaving '
            'active customer orders unfulfilled."\n\n'
        )

    system_prompt = (
        f"You are a purchasing advisor for: {company_desc_rec}\n\n"
        + UNTRUSTED_GUARD + "\n\n"
        "Your job is to recommend purchasing actions and explain the real-world consequences "
        "of each decision in plain business language — no formulas, no jargon.\n\n"
        "For every item you must reason through TWO scenarios before writing your output:\n"
        "1. What happens to this company if we ACT (place the order)?\n"
        "   Think: cash tied up, storage pressure, wastage if demand drops, overstock risk.\n"
        "2. What happens if we DON'T ACT (skip the order)?\n"
        "   Think: stockouts, lost revenue, customer impact, emergency sourcing cost, "
        "   reputational damage with key accounts.\n\n"
        "Write these as plain statements naming the company and the specific item. "
        "Example style (do not copy these, write fresh ones):\n"
        + example_consequences +
        "MANDATORY OUTPUT FORMAT — JSON array, one object per item:\n"
        "{\n"
        '  "item": "<name>",\n'
        '  "supplier": "<name>",\n'
        '  "supplier_type": "<import|local|other>",\n'
        '  "lead_time_days": <number or null — null when lead time is unknown, never guess>,\n'
        '  "days_of_supply": <number or null — copy from input>,\n'
        '  "recommended_action": "<REORDER|HOLD|ESCALATE|MONITOR>",\n'
        '  "suggested_quantity": <use the pre-computed quantity from input; only override with a number if you have strong reason>,\n'
        '  "confidence": "<HIGH|MEDIUM|LOW|INSUFFICIENT_DATA>",\n'
        '  "consequence_if_acting": "<1 plain sentence>",\n'
        '  "consequence_if_not_acting": "<1 plain sentence>",\n'
        '  "supplier_risk": "<None|LOW|HIGH>",\n'
        '  "mitigation": "<Concrete action if HIGH risk, else empty string>",\n'
        '  "flags": ["<string>"],\n'
        '  "reason": "<2 sentences max. Plain English. State the urgency and why.>"\n'
        "}\n\n"
        "RULES:\n"
        "1. lead_time_days: output null when the input says 'unknown'. Never invent a number.\n"
        "2. suggested_quantity: use the pre-computed value from input. If it says 'insufficient sales data', output 'Verify with team'.\n"
        "3. Do NOT mention any lead time or number of days in reason, consequence_if_acting, or consequence_if_not_acting. "
        "   Those fields are for urgency and business impact only.\n"
        "4. consequence_if_acting and consequence_if_not_acting must be plain business statements. "
        "   No SGD amounts unless you have reliable sales data. Name the company and item.\n"
        "5. confidence reflects data quality for the reorder decision: HIGH when stock level and sales velocity are clear; MEDIUM when either is estimated or thin; LOW when data is sparse; INSUFFICIENT_DATA only when there is no usable sales or stock data at all. Unknown supplier raises supplier_risk but does NOT force INSUFFICIENT_DATA.\n"
        "6. supplier_risk = HIGH and mitigation REQUIRED if delay rate > 30% or supplier unknown.\n"
        "7. Do NOT recommend ordering dead SKUs.\n"
        "8. Return ONLY a valid JSON array. No text outside the array."
    )

    # Process in batches — large catalogues need multiple Claude passes
    _REC_BATCH  = 150
    rec_batches = [enriched_lines[i:i+_REC_BATCH]
                   for i in range(0, len(enriched_lines), _REC_BATCH)]
    n_batches   = len(rec_batches)
    if n_batches > 1:
        _emit(progress_emit,
              f"Splitting into {n_batches} recommendation batches of up to {_REC_BATCH} items")

    try:
        all_recs = []
        for i, batch in enumerate(rec_batches, 1):
            if n_batches > 1:
                _emit(progress_emit,
                      f"Recommendations: batch {i}/{n_batches} ({len(batch)} items)")
            user_prompt = (
                f"Items requiring attention ({len(batch)} items"
                + (f", batch {i}/{n_batches}" if n_batches > 1 else "")
                + "):\n\n"
                + wrap_untrusted("\n".join(batch))
                + "\n\nContext from purchasing team:\n"
                + wrap_untrusted(context_text)
                + "\n\nGenerate consequence-aware purchase recommendations."
            )
            # 64000 so the full reply always fits: rec objects run ~150-200
            # output tokens each, so 150 items needs ~30K — 24000 used to
            # truncate and the JSON repair silently dropped the tail.
            raw = _call_claude(model, system_prompt, user_prompt, max_tokens=64000)
            recs_batch, _rec_repaired = _extract_json_array(raw)
            if recs_batch is None:
                _emit(progress_emit,
                      f"WARNING: recommendation batch {i}/{n_batches} returned no usable response — skipping")
                continue
            if _rec_repaired:
                _emit(progress_emit,
                      f"WARNING: the reply for recommendation batch {i}/{n_batches} was cut short — "
                      f"kept {len(recs_batch)} of {len(batch)} items")
            all_recs.extend(recs_batch)

        if not all_recs:
            _emit(progress_emit, "Recommendation agent returned no usable response")
            return [{"error": "Recommendation agent returned no usable JSON for any batch."}]

        recs = all_recs

        # Save outcome stubs for future learning, and attach the quantity
        # basis (monthly sales + unit) so the results page can explain the
        # suggested number. Matched by item name — the LLM echoes it back.
        # While we have the Python figure to hand, sanity-check the model's
        # suggested quantity against it so a hallucinated or missing number can
        # never reach the printed PO sheet.
        qty_corrections = 0
        for rec in recs:
            if isinstance(rec, dict):
                basis = qty_basis_by_item.get(rec.get("item", ""))
                if basis:
                    avg_m, uom_lbl, precomputed = basis
                    rec["avg_monthly_sales"] = avg_m
                    rec["uom_label"] = uom_lbl
                    clean, corrected = sanitize_suggested_quantity(
                        rec.get("suggested_quantity"), precomputed, uom_lbl)
                    rec["suggested_quantity"] = clean
                    if corrected:
                        rec["_quantity_corrected"] = True
                        qty_corrections += 1
            try:
                save_recommendation_outcome(
                    session_id=session_id,
                    item=rec.get("item", ""),
                    action_recommended=rec.get("recommended_action", ""),
                    predicted_loss_no_act=0,
                    predicted_cost_act=0,
                    net_benefit=0,
                    confidence=rec.get("confidence", ""),
                )
            except Exception:
                pass

        if qty_corrections:
            _emit(progress_emit,
                  f"Safety check: adjusted {qty_corrections} suggested "
                  f"quantit{'ies' if qty_corrections != 1 else 'y'} that were missing or out of range")

        pat_counts = apply_sales_pattern_flags(recs, pattern_stats)
        _n_pat = sum(pat_counts.values())
        if _n_pat:
            _emit(progress_emit,
                  f"Safety check: {_n_pat} item{'s' if _n_pat != 1 else ''} with unusual "
                  f"sales patterns ({pat_counts['spiky']} spiky, "
                  f"{pat_counts['volatile']} swingy, {pat_counts['lumpy']} irregular)")

        flagged = sum(1 for r in recs if r.get("flags"))
        high_risk_count = sum(1 for r in recs if r.get("supplier_risk") == "HIGH")
        _emit(progress_emit,
              f"Generated {len(recs)} recommendations — {flagged} flagged, {high_risk_count} high-risk suppliers")
        return recs
    except Exception as e:
        _emit(progress_emit, f"Recommendation agent error: {str(e)}")
        return [{"error": str(e)}]
