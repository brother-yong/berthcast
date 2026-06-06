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
    LEAD_TIME_BY_TYPE,
)


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

    # Compute avg monthly sales per item so we can suggest order quantities
    sales_velocity: dict = {}
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
            if s_desc and s_qty:
                try:
                    _DATE_EXACT_R = ("date", "invoice_date", "order_date", "transaction_date",
                                     "sales_date", "po_date", "doc_date", "posting_date")
                    _date_col_r = next((c for c in s_cols if c.lower() in _DATE_EXACT_R), None)
                    if not _date_col_r:
                        _date_col_r = next((c for c in s_cols if "date" in c.lower()), None)
                    if _date_col_r:
                        mo_r = query(f'SELECT COUNT(DISTINCT strftime("%Y-%m", "{_date_col_r}")) as m FROM {sal_table_r} LIMIT 1')
                        months_r = max(1, (mo_r[0]["m"] or 0) if mo_r else 0) or 12
                    else:
                        months_r = 12
                except Exception:
                    months_r = 12
                vel_rows = query(
                    f'SELECT "{s_desc}" as item, '
                    f'SUM({_num_sql(s_qty)}) / {months_r} as avg_monthly '
                    f'FROM {sal_table_r} GROUP BY "{s_desc}" LIMIT 5000'
                )
                sales_velocity = {r["item"]: round(r["avg_monthly"] or 0, 1) for r in vel_rows if r["item"]}
    except Exception:
        sales_velocity = {}

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
        avg_monthly = sales_velocity.get(iname, 0)
        uom = uom_by_item_r.get(iname.lower(), "")
        uom_label = f" {uom}" if uom else " units"
        qty_basis_by_item[iname] = (avg_monthly, uom_label)
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
            suggested_qty_str = None

        enriched_lines.append(
            f"---\n"
            f"Item: {iname}\n"
            f"Status: {inv_item.get('status')} | Spoilage risk: {inv_item.get('spoilage_risk')}\n"
            f"Stock: {inv_item.get('stock')}{uom_label} | Days of supply: {inv_item.get('days_of_supply', 'unknown')}\n"
            f"Avg monthly sales: {avg_monthly}{uom_label}\n"
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

    system_prompt = (
        f"You are a purchasing advisor for: {company_desc_rec}\n\n"
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
        '  "consequence_if_acting": "Ordering now locks up cash in 3 months of frozen salmon stock — '
        'if sales slow, a regional food distributor risks wastage in cold storage."\n'
        '  "consequence_if_not_acting": "Without a reorder, a regional food distributor will run out of frozen salmon '
        'within 4 days, leaving active customer orders unfulfilled."\n\n'
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
        "5. confidence = INSUFFICIENT_DATA if supplier is not known to the system.\n"
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
                + "\n".join(batch)
                + f"\n\nContext from purchasing team:\n{context_text}\n\n"
                "Generate consequence-aware purchase recommendations."
            )
            raw = _call_claude(model, system_prompt, user_prompt, max_tokens=24000)
            recs_batch, _ = _extract_json_array(raw)
            if recs_batch is None:
                _emit(progress_emit,
                      f"WARNING: recommendation batch {i}/{n_batches} returned no usable response — skipping")
                continue
            all_recs.extend(recs_batch)

        if not all_recs:
            _emit(progress_emit, "Recommendation agent returned no usable response")
            return [{"error": "Recommendation agent returned no usable JSON for any batch."}]

        recs = all_recs

        # Save outcome stubs for future learning, and attach the quantity
        # basis (monthly sales + unit) so the results page can explain the
        # suggested number. Matched by item name — the LLM echoes it back.
        for rec in recs:
            if isinstance(rec, dict):
                basis = qty_basis_by_item.get(rec.get("item", ""))
                if basis:
                    rec["avg_monthly_sales"], rec["uom_label"] = basis
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

        flagged = sum(1 for r in recs if r.get("flags"))
        high_risk_count = sum(1 for r in recs if r.get("supplier_risk") == "HIGH")
        _emit(progress_emit,
              f"Generated {len(recs)} recommendations — {flagged} flagged, {high_risk_count} high-risk suppliers")
        return recs
    except Exception as e:
        _emit(progress_emit, f"Recommendation agent error: {str(e)}")
        return [{"error": str(e)}]
