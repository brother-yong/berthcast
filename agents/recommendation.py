"""Agent 3 — the purchasing advisor.

Takes the flagged items from the health check and produces consequence-aware
reorder recommendations (act vs don't-act, supplier risk, sized quantity).
Moved verbatim from agents.py.
"""

from database import (
    query,
    get_company_config,
    get_supplier_profile,
    save_recommendation_outcomes_bulk,
    get_supplier_accuracy,
)
from .shared import (
    _emit,
    _resolve_item_suppliers,
    _infer_supplier_type,
    _format_context,
    _call_claude,
    _extract_json_array,
    LEAD_TIME_BY_TYPE,
    SalesNameIndex,
    normalise_match_key,
    alias_map_from_groups,
    monthly_pattern_stats,
    apply_sales_pattern_flags,
    wrap_untrusted,
    UNTRUSTED_GUARD,
)
from quantity import sanitize_suggested_quantity, parse_quantity
from rec_logic import LINK_UNSURE_FLAG, FLAG_UNSURE_LINKS
from .sales_links import MAX_KEY_CHARS, MAX_MEMBERS, MAX_SHOWN_NAME_CHARS


# About one week of the old rate still counts as near zero after sales stop.
_STOPPED_NEAR_ZERO_MONTHS = 0.25

# Keys only Python (the order maths) or staff (approve, edit, note, outcome)
# write on a rec. A model reply carrying one is dropped, never trusted.
_NOT_MODEL_KEYS = frozenset({
    "order_calc", "avg_monthly_sales", "uom_label", "sales_link",
    "edited_quantity", "edited_supplier", "approved", "dismissed", "note",
    "order_placed", "order_placed_at", "outcome_status", "outcome_recorded_at"})

def run_recommendation_agent(session_id: int, model: str, inventory_report: list, context: dict, progress_emit=None,
                             data_notes=None, row_numbers=None, confirmed_groups=None) -> list:
    _emit(progress_emit, "Loading company config and supplier profiles")

    # Pull org name from session
    sess_rows = query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    org_name  = sess_rows[0]["org_name"] if sess_rows else "Unknown"

    config = get_company_config(org_name)
    alias_map = alias_map_from_groups(confirmed_groups)

    _emit(progress_emit, "Reading supplier list (local vs import) to set lead times")
    item_supplier_map, item_lt_map, supplier_type_map = _resolve_item_suppliers(
        session_id, org_name, config, alias_map, progress_emit=progress_emit
    )

    # Missing sales cannot support an order quantity, even via the spoilage fallback.
    live_items = [r for r in inventory_report if r.get("status") not in ("DEAD", "REVIEW")]
    dead_count = sum(1 for r in inventory_report if r.get("status") == "DEAD")
    review_count = sum(1 for r in inventory_report if r.get("status") == "REVIEW")
    if dead_count:
        _emit(progress_emit, f"Excluded {dead_count} dead SKUs from recommendations")
    if review_count:
        _emit(progress_emit, f"Excluded {review_count} items needing a sales match from recommendations")

    actionable = [
        r for r in live_items
        if r.get("status") in ("LOW", "CRITICAL")
        or (r.get("status") != "HEALTHY" and r.get("spoilage_risk") in ("HIGH", "MEDIUM"))
    ]

    if not actionable:
        if review_count:
            _emit(progress_emit, "No automatic reorder recommendations; check the items needing a sales match")
        else:
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
                    key = normalise_match_key(row.get("item"))
                    uom_val = str(row.get("uom") or "").strip()
                    if uom_val and key and key not in uom_by_item_r:
                        uom_by_item_r[key] = uom_val
    except Exception:
        uom_by_item_r = {}

    # Demand and stock come from the inventory step; only supplier names
    # are read again here for the existing fallback.
    sales_supplier_idx = None  # supplier names read off the sales sheet
    _claimed_rec = {normalise_match_key(r.get("item"))
                    for r in inventory_report
                    if isinstance(r, dict) and r.get("item")}
    # Sales-pattern stats (spec 2026-07-10): spiky velocity correction,
    # per-item prompt notes, and the deterministic post-pass all read this.
    pattern_stats = monthly_pattern_stats(session_id)
    _sup_raw = {}
    try:
        sal_table_r = f"sales_{session_id}"
        s_sample = query(f"SELECT * FROM {sal_table_r} LIMIT 1")
        if s_sample:
            s_cols = list(s_sample[0].keys())
            s_desc = next((c for c in s_cols if c in ("inventory_desc", "item_description", "description", "product_name")), None)
            if not s_desc:
                s_desc = next((c for c in s_cols if any(k in c.lower() for k in ("desc", "item_name", "product_name", "item")) and "supplier" not in c.lower()), None)
            # Supplier names from the sales sheet — lowest-priority source,
            # consulted only for items the PO table knows nothing about.
            # Supplier/category columns in summary exports use MERGED cells:
            # only the first row of each group carries the value, so those
            # exports need the last seen name filled down the column before
            # mapping. But a transaction dump's supplier column can be sparse
            # or per-row too, and unconditional fill-down there paints
            # whichever name happened to come before onto every following
            # item (19 Jul 2026: a live file did exactly this and put all 39
            # reorder items onto two suppliers, wrongly). So fill-down is
            # only trusted when the column actually LOOKS like a merged-cell
            # export, which takes TWO conditions:
            #   1. every supplier name must appear in exactly one contiguous
            #      run down the column (a genuine block export can't repeat a
            #      name in two separate, non-adjacent groups). Formally, with
            #      `vals` the non-blank cells in row order, that's
            #      `runs(vals) == len(set(vals))` — a scattered/random column
            #      fails this because the same name resurfaces in unrelated
            #      rows.
            #   2. the FIRST non-blank cell must appear within the first two
            #      data rows. A genuine merged-cell report's first group
            #      header sits on row one — every row belongs to a block. A
            #      supplier column whose first name appears deep into the
            #      file is a transaction dump with a couple of stray labels,
            #      and fill-down from its last "island" would run unbounded
            #      to end-of-file. (This is exactly how the dummy fixture
            #      broke: two real-world items each had their own supplier
            #      named on every one of their rows — technically satisfying
            #      condition 1 — and the second one sat near the file's tail,
            #      so every unrelated item after it inherited that name.)
            # Own-row values are read separately below and are unaffected by
            # either condition — this gate only decides whether the carried-
            # down (fill-down) value may ALSO be trusted.
            _sup_col = next((c for c in s_cols
                             if "supplier" in c.lower() or "vendor" in c.lower()), None)
            if s_desc and _sup_col:
                _sup_rows = query(
                    f'SELECT "{s_desc}" as item, "{_sup_col}" as sup '
                    f'FROM {sal_table_r} ORDER BY rowid LIMIT 5000')
                _raw_vals = [str(_r.get("sup") or "").strip() for _r in _sup_rows]
                _first_idx = next((i for i, v in enumerate(_raw_vals) if v), None)
                _seq = [v for v in _raw_vals if v]
                _runs = 1 + sum(1 for a, b in zip(_seq, _seq[1:]) if a != b) if _seq else 0
                _is_blocky = (bool(_seq) and _runs == len(set(_seq))
                              and _first_idx is not None and _first_idx <= 1)

                # Two buckets per item, built in one pass: `_own` from the
                # item's OWN row(s), `_filled` from the carried-down name
                # (collected only when the column is blocky). An item is
                # mapped from its own row when possible; the fill-down value
                # is used only as a fallback, and only on blocky files.
                _own, _filled = {}, {}
                _last_sup = None
                for _r in _sup_rows:
                    _sv  = str(_r.get("sup") or "").strip()
                    _itm = str(_r.get("item") or "").strip()
                    if _sv:
                        _last_sup = _sv
                        if _itm:
                            _own.setdefault(_itm, set()).add(_sv)
                    elif _itm and _is_blocky and _last_sup:
                        _filled.setdefault(_itm, set()).add(_last_sup)

                # A set with two+ distinct names means the source disagrees
                # with itself for this item — crediting either one would be
                # a guess, so the item is left out entirely (resolves to
                # "Unknown" downstream) rather than picking a side.
                _sup_raw = {}
                for _itm in set(_own) | set(_filled):
                    _names = _own.get(_itm) or _filled.get(_itm)
                    if _names and len(_names) == 1:
                        _sup_raw[_itm] = {"supplier": next(iter(_names))}

                if _sup_raw:
                    sales_supplier_idx = SalesNameIndex(_sup_raw, alias_map, claimed_keys=_claimed_rec)
                    _emit(progress_emit,
                          f"Supplier names read from the sales sheet "
                          f"({len(_sup_raw)} items"
                          + (", merged cells filled down)" if _is_blocky else ", no fill-down — column isn't a merged-cell export)"))
    except Exception:
        sales_supplier_idx = None

    # Build enriched item lines for Claude
    enriched_lines = []
    # Capture the monthly-sales figure + unit used to size each suggested
    # quantity, keyed by item name. Attached to the saved recs below so the
    # results page can explain where the number came from.
    qty_basis_by_item = {}
    # Counts items whose supplier came from the sales-sheet fallback below —
    # drives the results-page caveat after this loop: a report built without
    # a supplier listing or PO file needs an explicit "verify before you
    # order" flag, since nothing here confirmed those names.
    _sup_from_sales_count = 0
    _missing_stamp_count = 0
    # One DB read per distinct supplier per run, not 3-4 per item — query()
    # opens a fresh SQLite connection every call.
    _profile_memo, _acc_memo = {}, {}

    def _profile(sup):
        if sup not in _profile_memo:
            _profile_memo[sup] = get_supplier_profile(org_name, sup)
        return _profile_memo[sup]

    def _accuracy(sup):
        if sup not in _acc_memo:
            _acc_memo[sup] = get_supplier_accuracy(org_name, sup)
        return _acc_memo[sup]

    for inv_item in actionable:
        iname    = inv_item.get("item", "Unknown")
        stamp = (row_numbers or {}).get(normalise_match_key(iname)) or {}
        if not stamp:
            _missing_stamp_count += 1

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
                _names = {_sup_raw[source]["supplier"]
                          for source in sales_supplier_idx.sources(iname)
                          if source in _sup_raw}
                if len(_names) == 1:
                    supplier = next(iter(_names))
                    _sup_from_sales_count += 1
            stype    = supplier_type_map.get(supplier, "other")
            if stype == "other" and supplier == "Unknown":
                stype = _infer_supplier_type(iname)
            sup_profile = _profile(supplier)
            if supplier == "Unknown":
                lt_days = None
            else:
                lt_days = (sup_profile.get("avg_lead_time_days")
                           or LEAD_TIME_BY_TYPE.get(stype)
                           or config.get("default_lead_time_days") or None)
            delay_prob = sup_profile.get("delay_probability", 0.2)
            high_risk  = delay_prob > 0.30 or sup_profile.get("data_quality_score", 0.3) < 0.50

        if stamp.get("lead_time_days"):
            lt_days = stamp["lead_time_days"]

        _prof     = _profile(supplier)
        quality   = _prof.get("data_quality_score", 0.3)
        sup_notes = _prof.get("notes", "")
        known_sup = quality >= 0.5

        acc      = _accuracy(supplier)
        acc_note = ""
        if acc.get("total_recs", 0) > 0:
            acc_note = (f" | Past recs: {acc['total_recs']} — "
                        f"{acc['approved']} approved, {acc['dismissed']} dismissed")

        # Compute suggested order quantity with adaptive safety buffer.
        # Buffer scales with supplier reliability instead of a flat 1.5 months:
        #   - Reliable local supplier (delay_prob < 0.15):  +0.5 months
        #   - Average supplier (delay_prob 0.15–0.35):      +1.5 months
        #   - Unreliable import (delay_prob > 0.35):         +2.5 months
        avg_monthly = round(stamp.get("avg_monthly") or 0, 1)
        uom = stamp.get("uom") or uom_by_item_r.get(normalise_match_key(iname), "")
        uom_label = f" {uom}" if uom else " units"
        position = stamp.get("position")
        since = stamp.get("stopped_since")
        calc = flag = None
        suggested_qty = None
        suggested_qty_str = "insufficient sales data"
        if avg_monthly > 0:
            lt_months = (lt_days / 30) if lt_days else 2.0
            if delay_prob <= 0.15:
                safety_buffer = 0.5
            elif delay_prob <= 0.35:
                safety_buffer = 1.5
            else:
                safety_buffer = 2.5
            need = round(avg_monthly * (lt_months + safety_buffer))
            pos = round(position) if position is not None else None
            label = stamp.get("position_label") or "On hand"
            order = need - pos if pos is not None else None
            if pos is None:
                state = "no_position"
                suggested_qty_str = "insufficient stock data"
                flag = (f"Stock figure unreadable: need about {need}{uom_label} for the lead time "
                        "plus buffer; take off what you hold before ordering.")
            elif since and position > _STOPPED_NEAR_ZERO_MONTHS * avg_monthly:
                state = "not_moving"
                suggested_qty_str = f"none: no sales since {since}"
                flag = (f"Not moving since {since}: {pos}{uom_label} in stock and no sales "
                        "since then. No order suggested.")
            elif order <= 0:
                state = "covered"
                suggested_qty_str = "none needed: free stock already covers the lead time plus buffer"
                flag = (f"Covered: {label.lower()} {pos}{uom_label} against a need of "
                        f"{need}{uom_label}; check any incoming stock arrives.")
            else:
                state = "order"
                suggested_qty = order
                suggested_qty_str = f"{order}{uom_label}"
                if since:
                    flag = f"No sales since {since}: out of stock that long, or dropped? Check before ordering."
            sources = stamp.get("sales_sources") or []
            # Sales-line names are uploaded text: save only the three the card
            # shows, each capped, plus a count, so a crafted sales file cannot
            # bloat the saved JSON that every page load parses.
            sales_from = None
            if sources and not (len(sources) == 1 and
                                normalise_match_key(sources[0]) == normalise_match_key(iname)):
                sales_from = [str(source).strip()[:80] for source in sources[:3]]
            calc = {"state": state, "need": need, "position": pos, "position_label": label,
                    "order": order if state == "order" else None,
                    "spare": pos - need if state == "covered" else None,
                    "lead_months": round(lt_months, 1), "lead_known": bool(lt_days),
                    "buffer_months": safety_buffer, "cover_months": round(lt_months + safety_buffer, 1),
                    "stopped_since": since, "sales_from": sales_from,
                    "sales_from_count": len(sources) if sales_from else None}
        # This basis is Python-owned. A model name that misses it cannot carry
        # an order quantity through to the saved report.
        qty_basis_by_item[normalise_match_key(iname)] = {
            "avg": avg_monthly, "uom": uom_label, "pre": suggested_qty,
            "lt": lt_days, "calc": calc, "flag": flag, "link": stamp.get("link")}

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
            f"Stock: {inv_item.get('stock')}{uom_label} | Days of supply: {inv_item.get('days_of_supply', 'unknown')}"
            f" | Free stock: {str(round(position)) + uom_label if position is not None else 'unknown'}\n"
            + (f"Sales stopped: no sales since {since} (ran out, or dropped?)\n" if since else "") +
            f"Avg monthly sales: {avg_monthly}{uom_label}\n"
            f"{pattern_line}"
            f"Pre-computed suggested order quantity: {suggested_qty_str}\n"
            f"Supplier: {supplier} ({stype}, lead time: {lt_days if lt_days else 'unknown — do not guess'})\n"
            f"Supplier delay rate: {int(delay_prob*100)}% | "
            f"Supplier known to system: {'Yes' if known_sup else 'No'}"
            + (f" | Notes: {sup_notes}" if sup_notes else "")
            + (f"{acc_note}" if acc_note else "") + "\n"
            f"High-risk supplier: {'YES' if high_risk else 'No'}\n"
            f"Observation: {inv_item.get('observation', '')}\n"
        )

    if _missing_stamp_count:
        _emit(progress_emit, f"{_missing_stamp_count} item(s) came back renamed by the model; "
              "their quantities are left for the team to check")

    # A run whose actionable items took their supplier only from the sales
    # sheet had no supplier listing or PO file to confirm those names —
    # exactly the situation that produced the 19 Jul 2026 bad Purchase Order
    # sheet. One plain-English caveat covers the whole run; it doesn't need
    # to name every affected item, just tell the client to check before they
    # send an order built on it.
    if data_notes is not None and _sup_from_sales_count > 0:
        data_notes.append(
            "Supplier names on this report were read from the sales file "
            "itself and weren't confirmed by a supplier list or "
            "purchase-order file. Double-check the supplier on each order "
            "before sending it.")

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
        "2. suggested_quantity: use the pre-computed value from input. It already takes free stock off (free stock = stock on hand plus stock on order minus stock owed to customers), so never subtract stock again. If it says 'insufficient sales data' or 'insufficient stock data', output 'Verify with team'. If it starts with 'none', output null.\n"
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
    expected_items = len(enriched_lines)
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

        if data_notes is not None and len(all_recs) < expected_items:
            data_notes.append(
                "The AI's reply was cut short or unusable for part of the "
                "recommendation step, so some items may be missing "
                "recommendations. Re-running the analysis usually completes it.")

        # Save outcome stubs for future learning, and attach the quantity
        # basis (monthly sales + unit) so the results page can explain the
        # suggested number. Matched by item name — the LLM echoes it back.
        # While we have the Python figure to hand, sanity-check the model's
        # suggested quantity against it so a hallucinated or missing number can
        # never reach the printed PO sheet.
        qty_corrections = 0
        outcome_rows = []
        for rec in recs:
            if isinstance(rec, dict):
                basis = qty_basis_by_item.get(normalise_match_key(rec.get("item", "")))
                # The model authors no arithmetic metadata, no human decision
                # and no display key: only Python and staff write those.
                for key in [k for k in rec if k in _NOT_MODEL_KEYS or str(k).startswith("_")]:
                    rec.pop(key)
                if basis:
                    rec["avg_monthly_sales"] = basis["avg"]
                    rec["uom_label"] = basis["uom"]
                    rec["lead_time_days"] = basis["lt"]
                    calc = basis["calc"]
                    corrected = False
                    if calc and calc["state"] in ("covered", "not_moving"):
                        rec["suggested_quantity"] = ""
                    elif calc and calc["state"] == "order":
                        corrected = parse_quantity(rec.get("suggested_quantity")) != calc["order"]
                        rec["suggested_quantity"] = f"{calc['order']}{basis['uom']}"
                    else:
                        clean, corrected = sanitize_suggested_quantity(
                            rec.get("suggested_quantity"), basis["pre"], basis["uom"])
                        rec["suggested_quantity"] = clean
                    if calc:
                        rec["order_calc"] = calc
                    if basis["flag"]:
                        if not isinstance(rec.get("flags"), list):
                            rec["flags"] = []
                        rec["flags"].insert(0, basis["flag"])
                    link = basis["link"]
                    if isinstance(link, dict):
                        # Saved with every rec and parsed on each page load:
                        # bounded like the stamp it comes from.
                        rec["sales_link"] = {
                            "line": link["line"], "sure": link["sure"], "ai": link["ai"],
                            "label": link["label"], "more": link["more"],
                            "members": [{"name": m["name"][:MAX_SHOWN_NAME_CHARS],
                                         "key": m["key"] if len(m["key"]) <= MAX_KEY_CHARS else "",
                                         "free": m["free"]}
                                        for m in link["members"][:MAX_MEMBERS]]}
                        if FLAG_UNSURE_LINKS and not link["sure"]:
                            if not isinstance(rec.get("flags"), list):
                                rec["flags"] = []
                            rec["flags"].insert(0, LINK_UNSURE_FLAG)
                else:
                    # Never sent this item, so none of its numbers came from Python.
                    rec.pop("lead_time_days", None)
                    rec["suggested_quantity"], _ = sanitize_suggested_quantity(
                        rec.get("suggested_quantity"), None)
                    corrected = True
                if corrected:
                    rec["_quantity_corrected"] = True
                    qty_corrections += 1
                outcome_rows.append({
                    "session_id": session_id,
                    "item": rec.get("item", ""),
                    "action_recommended": rec.get("recommended_action", ""),
                    "predicted_loss_no_act": 0,
                    "predicted_cost_act": 0,
                    "net_benefit": 0,
                    "confidence": rec.get("confidence", ""),
                    "supplier": rec.get("supplier", ""),
                })
        # One transaction for the whole run's outcome stubs (was one
        # connection+commit per rec). Non-fatal: the analysis must never
        # die because outcome stubs failed to save.
        try:
            save_recommendation_outcomes_bulk(outcome_rows)
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
        # Raw exception text is for the operator (logs + ALERT_EMAIL via the
        # returned error) — the user-facing progress log gets a generic line.
        _emit(progress_emit, "Recommendation agent hit an unexpected error — stopping")
        return [{"error": str(e)}]
