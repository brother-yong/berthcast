"""
BerthAI - Three-Agent System
"""

import json
import os
from database import (
    query, get_db,
    get_company_config, get_supplier_profile,
    save_recommendation_outcome,
    get_supplier_accuracy,
)

import anthropic

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

LEAD_TIME_DAYS = {
    "import": 16 * 7,
    "local":   3 * 7,
    "other":   8 * 7,
}

SPOILAGE_THRESHOLD_DAYS = {
    "chill":   14,
    "frozen":  60,
    "dry":     180,
}


# ---------------------------------------------------------------------------
# Consequence engine — pure Python, no LLM involvement
# ---------------------------------------------------------------------------

def _consequence_calc(item: dict, config: dict, supplier_profile: dict) -> dict:
    """
    Returns two cost scenarios per item:
      act_sgd      — cost of placing a reorder now
      no_act_sgd   — expected financial loss from NOT reordering
      net_benefit  — positive means reordering saves money
      p_stockout   — probability of stockout before resupply arrives
      confidence   — HIGH / MED / LOW / INSUFFICIENT_DATA
    """
    stock        = float(item.get("stock") or 0)
    daily_demand = float(item.get("daily_demand") or 0)
    unit_cost    = float(item.get("unit_cost") or 0)
    reorder_qty  = float(item.get("suggested_quantity") or 0)

    lead_time    = float(supplier_profile.get("avg_lead_time_days") or config.get("default_lead_time_days") or 56)
    variance     = float(config.get("lead_time_variance_days") or 14)
    delay_prob   = float(supplier_profile.get("delay_probability") or 0.2)
    quality      = float(supplier_profile.get("data_quality_score") or 0.3)

    holding_cpd  = float(config.get("holding_cost_per_unit_per_day") or 0.5)
    stockout_cpu = float(config.get("stockout_cost_per_unit") or 50.0)

    # Days until stockout (avoid div/0)
    days_cover = stock / max(daily_demand, 0.01) if daily_demand > 0 else 999

    # Effective lead time accounting for delay probability
    effective_lead = lead_time + (variance * delay_prob)

    # Probability stock runs out before order arrives
    shortfall_days = effective_lead - days_cover
    if shortfall_days <= 0:
        p_stockout = 0.05  # Tiny residual risk even with ample cover
    else:
        p_stockout = min(0.98, shortfall_days / max(effective_lead, 1))

    # Expected units short if stockout occurs
    units_short = max(0, (effective_lead - days_cover) * daily_demand) if daily_demand > 0 else 0

    # Scenario A — Act (reorder now)
    expected_demand_during_lead = daily_demand * lead_time
    overstock_units = max(0, reorder_qty - expected_demand_during_lead)
    act_cost = (reorder_qty * unit_cost) + (overstock_units * holding_cpd * lead_time)

    # Scenario B — Don't act
    no_act_cost = p_stockout * units_short * stockout_cpu

    net_benefit = no_act_cost - act_cost

    # Confidence based on data quality + how certain the stockout calc is
    if quality < 0.4:
        confidence = "INSUFFICIENT_DATA"
    elif quality >= 0.7 and p_stockout >= 0.6:
        confidence = "HIGH"
    elif quality >= 0.5 and p_stockout >= 0.3:
        confidence = "MED"
    else:
        confidence = "LOW"

    return {
        "act_sgd":     round(act_cost, 2),
        "no_act_sgd":  round(no_act_cost, 2),
        "net_benefit": round(net_benefit, 2),
        "p_stockout":  round(p_stockout, 2),
        "days_cover":  round(days_cover, 1),
        "units_short": round(units_short, 1),
        "confidence":  confidence,
        "delay_prob":  round(delay_prob, 2),
        "data_quality": round(quality, 2),
    }


def _call_claude(model: str, system: str, user: str, max_tokens: int = 4096) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return response.content[0].text


def _emit(progress_emit, msg: str) -> None:
    """Safely call optional progress callback. Never raise into agent flow."""
    if progress_emit is None:
        return
    try:
        progress_emit(msg)
    except Exception:
        pass


def _extract_json_array(raw: str):
    if not raw:
        return None, False
    s = raw.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3].rstrip()
    start = s.find("[")
    if start == -1:
        return None, False
    end = s.rfind("]")
    if end > start:
        try:
            return json.loads(s[start:end + 1]), False
        except json.JSONDecodeError:
            pass
    body = s[start + 1:]
    for needle in ("},", "}"):
        idx = body.rfind(needle)
        if idx == -1:
            continue
        repaired = "[" + body[:idx + 1].rstrip().rstrip(",") + "]"
        try:
            return json.loads(repaired), True
        except json.JSONDecodeError:
            continue
    return None, False


def _format_context(context: dict) -> str:
    if not context:
        return "No additional context provided."
    lines = []
    if context.get("delayed_suppliers"):
        lines.append(f"Delayed/uncontactable suppliers: {context['delayed_suppliers']}")
    if context.get("large_orders"):
        lines.append(f"Large upcoming orders: {context['large_orders']}")
    if context.get("discontinue"):
        lines.append(f"Items to be discontinued: {context['discontinue']}")
    if context.get("other"):
        lines.append(f"Other notes: {context['other']}")
    return "\n".join(lines) if lines else "No additional context provided."


def run_normalization_agent(session_id: int, model: str, progress_emit=None) -> dict:
    _emit(progress_emit, "Reading item names from your inventory, purchase orders, and sales files")
    item_names = set()
    inv_table = f"inventory_{session_id}"
    po_table  = f"purchase_orders_{session_id}"
    sal_table = f"sales_{session_id}"

    def _col_candidates(table, candidates):
        try:
            row = query(f"SELECT * FROM {table} LIMIT 1")
            if not row:
                return []
            cols = list(row[0].keys())
            for c in candidates:
                if c in cols:
                    rows = query(f"SELECT DISTINCT {c} FROM {table} WHERE {c} IS NOT NULL LIMIT 2000")
                    return [r[c] for r in rows if r[c]]
        except Exception:
            pass
        return []

    cand = ["description", "item_description", "inventory_desc", "item_name", "product_description", "product_name"]
    item_names.update(_col_candidates(inv_table, cand))
    item_names.update(_col_candidates(po_table,  cand))
    item_names.update(_col_candidates(sal_table, cand))

    if not item_names:
        _emit(progress_emit, "No item names found — nothing to deduplicate")
        return {"groups": [], "message": "No item names found in uploaded data."}

    items_list = sorted(list(item_names))[:1500]
    _emit(progress_emit, f"Found {len(item_names)} unique item names, scanning {len(items_list)} for duplicates")
    _emit(progress_emit, "Asking Claude to spot duplicates (same product, different wording)")

    system_prompt = (
        "You are a data normalisation specialist for a food distribution company.\n"
        "Identify item names that clearly refer to the same product but are written differently.\n\n"
        "Rules:\n"
        "- Only group items you are confident are the same product (same product, same size/weight)\n"
        "- Do NOT merge items if uncertain - leave them separate\n"
        "- Return ONLY a JSON array of groups\n"
        "- Each group has: \"canonical\" (clearest name) and \"variants\" (list of other names)\n"
        "- Only include groups with 2+ variants - skip solo items\n\n"
        "Example:\n"
        "[{\"canonical\": \"White Bread 400g\", \"variants\": [\"WHT BRD 400G\", \"Bread White 400g\"]}]"
    )
    user_prompt = (
        f"Here are {len(items_list)} unique item names. Group the duplicates.\n\nItem names:\n"
        + "\n".join(items_list)
    )

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=8000)
        groups, repaired = _extract_json_array(raw)
        if groups is None:
            _emit(progress_emit, "No duplicate groups found")
            return {"groups": [], "message": "No duplicates found."}
        _emit(progress_emit, f"Found {len(groups)} duplicate groups — review them on the next page")
        msg = "Groupings repaired from truncated output." if repaired else ""
        return {"groups": groups, "total_items_scanned": len(items_list), "message": msg}
    except Exception as e:
        _emit(progress_emit, f"Normalisation error: {str(e)}")
        return {"groups": [], "message": f"Normalisation agent error: {str(e)}"}


def run_inventory_agent(session_id: int, model: str, confirmed_groups: list, context: dict, progress_emit=None) -> dict:
    _emit(progress_emit, "Loading company config for analysis rules")
    from database import get_company_config
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

    _emit(progress_emit, "Computing sales velocity for each item")
    sales_by_item = {}
    try:
        sample = query(f"SELECT * FROM {sal_table} LIMIT 1")
        if sample:
            cols = list(sample[0].keys())
            desc_col = next((c for c in cols if c in ("inventory_desc", "item_description", "description", "product_name")), None)
            qty_col  = next((c for c in cols if c in ("billing_qty", "qty", "quantity", "billing_quantity")), None)
            if desc_col and qty_col:
                sal_rows = query(
                    'SELECT "' + desc_col + '" as item_name, SUM(CAST("' + qty_col + '" AS REAL)) as total_qty, '
                    'COUNT(*) as txn_count FROM ' + sal_table + ' GROUP BY "' + desc_col + '" LIMIT 5000'
                )
                sales_by_item = {r["item_name"]: r for r in sal_rows if r["item_name"]}
                _emit(progress_emit, f"Sales velocity computed for {len(sales_by_item)} items")
    except Exception:
        sales_by_item = {}

    _sample = inventory[0] if inventory else {}
    _cols = list(_sample.keys())

    DESC_EXACT = ("description", "item_description", "inventory_desc", "product_description",
                  "item_name", "product_name", "stock_description", "item_desc")
    QTY_EXACT  = ("qty_on_hand", "qty", "quantity", "stock_on_hand", "on_hand", "stock_qty",
                  "balance", "stock_balance", "closing_stock")
    CAT_EXACT  = ("category", "cat", "class", "item_category", "product_category", "storage_type")

    _desc_col = next((k for k in _cols if k in DESC_EXACT), None) or \
                next((k for k in _cols if ("desc" in k or "item_name" in k or "product_name" in k) and "supplier" not in k), None)
    _qty_col  = next((k for k in _cols if k in QTY_EXACT), None) or \
                next((k for k in _cols if ("qty" in k or "quantity" in k or "stock" in k or "balance" in k)
                                          and "allocated" not in k and "value" not in k), None)
    _cat_col  = next((k for k in _cols if k in CAT_EXACT), None) or \
                next((k for k in _cols if "cat" in k or "class" in k or "storage" in k), None)

    if not _desc_col or not _qty_col:
        return {"error": (
            "Could not detect description/quantity columns in your Inventory Report. "
            f"Detected columns: {_cols}. "
            f"Found desc_col={_desc_col}, qty_col={_qty_col}, cat_col={_cat_col}. "
            "Open the Inventory Report and confirm one column has the item name and one has the stock quantity."
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
            # Find revenue or quantity column in sales table
            sal_sample = query(f"SELECT * FROM {sal_table} LIMIT 1")
            if sal_sample:
                sal_cols  = list(sal_sample[0].keys())
                desc_col_s = next((c for c in sal_cols if c in (
                    "inventory_desc", "item_description", "description", "product_name")), None)
                val_col_s  = next((c for c in sal_cols if c in (
                    "net_amount", "total_amount", "amount", "value", "billing_amount",
                    "sales_value", "net_value", "total_value", "revenue", "ext_price")), None)
                qty_col_s  = next((c for c in sal_cols if c in (
                    "billing_qty", "qty", "quantity", "billing_quantity")), None)

                if desc_col_s and (val_col_s or qty_col_s):
                    rank_col = val_col_s if val_col_s else qty_col_s
                    metric   = "revenue" if val_col_s else "quantity sold"
                    top_rows = query(
                        f'SELECT "{desc_col_s}" as item, '
                        f'SUM(CAST("{rank_col}" AS REAL)) as metric '
                        f'FROM {sal_table} '
                        f'WHERE "{desc_col_s}" IS NOT NULL '
                        f'GROUP BY "{desc_col_s}" '
                        f'ORDER BY metric DESC '
                        f'LIMIT {n}'
                    )
                    top_item_names = {r["item"].strip().lower() for r in top_rows if r["item"]}
                    _emit(progress_emit,
                        f"Top {n} items by {metric} identified ({len(top_item_names)} matched) — filtering inventory")
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
        inventory_sorted = [
            row for row in inventory_sorted
            if str(row.get(_desc_col) or "").strip().lower() in top_item_names
        ]

    inv_summary_lines = []
    for row in inventory_sorted[:800]:
        desc = row.get(_desc_col) or "Unknown"
        qty  = row.get(_qty_col)  or "0"
        cat  = (row.get(_cat_col) if _cat_col else None) or "GENERAL"
        canonical  = alias_map.get(str(desc).lower(), desc)
        sales_info = sales_by_item.get(desc, {})
        total_sold = sales_info.get("total_qty", 0) or 0
        inv_summary_lines.append(
            f"Item: {canonical} | Category: {cat} | Stock: {qty} | Total sold: {total_sold}"
        )

    if not inv_summary_lines:
        return {"error": f"No inventory rows. desc_col={_desc_col}, qty_col={_qty_col}, rows={len(inventory)}"}

    _emit(progress_emit, f"Prepared {len(inv_summary_lines)} items for analysis (zero-stock items prioritised)")
    _emit(progress_emit, "Asking Claude to assess inventory health (this is the slow part — up to a minute)")

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
        "For each item, determine:\n"
        "1. Status: HEALTHY / LOW / CRITICAL / DEAD\n"
        "2. Spoilage risk: HIGH / MEDIUM / LOW / NONE\n"
        "3. Days of supply estimate if calculable\n"
        "4. A one-line plain English observation\n\n"
        "Rules:\n"
        + spoilage_rules +
        "- Zero stock items are CRITICAL (still selling) or DEAD (not selling)\n\n"
        "Return ONLY a JSON array of objects with keys:\n"
        "item, category, stock, status, spoilage_risk, days_of_supply, observation\n"
        "Do not include text outside the JSON array."
    )
    user_prompt = (
        f"Inventory snapshot ({len(inv_summary_lines)} items):\n\n"
        + "\n".join(inv_summary_lines)
        + f"\n\nContext from purchasing team:\n{context_text}\n\nReturn the health report JSON."
    )

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=16000)
        report, repaired = _extract_json_array(raw)
        if report is None:
            _emit(progress_emit, "Inventory agent returned no usable response")
            return {"error": f"Inventory agent returned no usable JSON. First 400 chars: {raw[:400]}"}
        crit = sum(1 for r in report if r.get("status") == "CRITICAL")
        low  = sum(1 for r in report if r.get("status") == "LOW")
        _emit(progress_emit, f"Inventory health complete — {len(report)} items reviewed, {crit} critical, {low} low")
        return {"report": report, "items_analysed": len(report), "partial": repaired}
    except Exception as e:
        _emit(progress_emit, f"Inventory agent error: {str(e)}")
        return {"error": f"Inventory agent error: {str(e)}"}


def run_recommendation_agent(session_id: int, model: str, inventory_report: list, context: dict, progress_emit=None) -> list:
    _emit(progress_emit, "Loading company config and supplier profiles")

    # Pull org name from session
    sess_rows = query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    org_name  = sess_rows[0]["org_name"] if sess_rows else "Unknown"

    config = get_company_config(org_name)

    _emit(progress_emit, "Reading supplier list (local vs import) to set lead times")
    sup_table = f"suppliers_{session_id}"
    po_table  = f"purchase_orders_{session_id}"

    supplier_type_map = {}
    try:
        sup_rows = query(f"SELECT * FROM {sup_table} LIMIT 1000")
        for row in sup_rows:
            name_col = next((k for k in row if "name" in k or "supplier" in k), None)
            type_col = next((k for k in row if "type" in k or "category" in k or "class" in k), None)
            if name_col and type_col:
                sname = str(row[name_col] or "").strip()
                stype = str(row[type_col] or "").strip().lower()
                if "import" in stype:
                    supplier_type_map[sname] = "import"
                elif "local" in stype:
                    supplier_type_map[sname] = "local"
                else:
                    supplier_type_map[sname] = "other"
    except Exception:
        pass

    item_supplier_map = {}
    try:
        sample = query(f"SELECT * FROM {po_table} LIMIT 1")
        if sample:
            cols = list(sample[0].keys())
            desc_col = next((c for c in cols if c in ("inventory_desc", "item_description", "description", "product_name")), None)
            sup_col  = next((c for c in cols if "supplier" in c and "name" in c), None) or \
                       next((c for c in cols if "supplier" in c), None)
            if desc_col and sup_col:
                po_rows = query(
                    'SELECT "' + desc_col + '" as item_name, "' + sup_col + '" as sup_name '
                    'FROM ' + po_table + ' WHERE "' + desc_col + '" IS NOT NULL '
                    'ORDER BY rowid DESC LIMIT 3000'
                )
                for row in po_rows:
                    item = row.get("item_name", "")
                    sup  = row.get("sup_name", "")
                    if item and item not in item_supplier_map:
                        item_supplier_map[item] = sup
    except Exception:
        pass

    _emit(progress_emit, f"Mapped {len(supplier_type_map)} suppliers, {len(item_supplier_map)} item-supplier links")

    actionable = [
        r for r in inventory_report
        if r.get("status") in ("LOW", "CRITICAL") or r.get("spoilage_risk") in ("HIGH", "MEDIUM")
    ][:150]

    if not actionable:
        _emit(progress_emit, "No items need attention right now — inventory looks healthy")
        return []

    _emit(progress_emit, f"Filtered to {len(actionable)} items needing attention")
    _emit(progress_emit, "Running consequence engine — calculating financial scenarios for each item")

    # Build enriched items with pre-computed consequence figures
    enriched_items = []
    for inv_item in actionable:
        iname    = inv_item.get("item", "Unknown")
        supplier = item_supplier_map.get(iname, "Unknown") or "Unknown"
        stype    = supplier_type_map.get(supplier, "other")
        lt_days  = LEAD_TIME_DAYS[stype]

        # Build a mini item dict for the consequence engine
        item_for_calc = {
            "stock":            inv_item.get("stock", 0),
            "daily_demand":     inv_item.get("daily_demand", 0),
            "unit_cost":        inv_item.get("unit_cost", 0),
            "suggested_quantity": inv_item.get("suggested_quantity") or inv_item.get("stock", 0),
        }

        sup_profile = get_supplier_profile(org_name, supplier)
        # Prefer profile lead time; fall back to type-based constant, then company default
        profile_lt = sup_profile.get("avg_lead_time_days")
        if not profile_lt or profile_lt == 56:
            profile_lt = LEAD_TIME_DAYS.get(stype, config.get("default_lead_time_days", 56))
        sup_profile["avg_lead_time_days"] = profile_lt
        lt_days = profile_lt

        conseq = _consequence_calc(item_for_calc, config, sup_profile)

        # Historical accuracy for this supplier
        accuracy = get_supplier_accuracy(org_name, supplier)

        enriched_items.append({
            "inv": inv_item,
            "supplier": supplier,
            "stype": stype,
            "lt_days": lt_days,
            "profile": sup_profile,
            "conseq": conseq,
            "accuracy": accuracy,
        })

    _emit(progress_emit, "Consequence calculations done — building prompts for Claude")

    # Format each item as a structured block for Claude
    enriched_lines = []
    for e in enriched_items:
        inv     = e["inv"]
        conseq  = e["conseq"]
        profile = e["profile"]
        acc     = e["accuracy"]
        sup     = e["supplier"]

        high_risk = (profile.get("delay_probability", 0) > 0.30 or
                     profile.get("data_quality_score", 1) < 0.50)

        acc_note = ""
        if acc.get("total_recs", 0) > 0:
            acc_note = (f" | Historical: {acc['total_recs']} past recs, "
                        f"{acc['approved']} approved, {acc['dismissed']} dismissed")

        enriched_lines.append(
            f"---\n"
            f"Item: {inv.get('item', 'Unknown')}\n"
            f"Status: {inv.get('status')} | Spoilage risk: {inv.get('spoilage_risk')}\n"
            f"Stock: {inv.get('stock')} units | Days of cover: {conseq['days_cover']}\n"
            f"Daily demand: {inv.get('daily_demand', 'unknown')} units/day\n"
            f"Supplier: {sup} ({e['stype']}, lead time: {e['lt_days']} days)\n"
            f"Supplier delay probability: {int(profile.get('delay_probability', 0)*100)}% | "
            f"Data quality: {int(profile.get('data_quality_score', 0)*100)}%{acc_note}\n"
            f"High-risk supplier: {'YES' if high_risk else 'No'}\n"
            f"--- Financial scenarios (pre-calculated) ---\n"
            f"IF YOU ACT (reorder now): SGD {conseq['act_sgd']:,.2f} cost\n"
            f"IF YOU DON'T ACT: SGD {conseq['no_act_sgd']:,.2f} expected loss "
            f"(stockout probability: {int(conseq['p_stockout']*100)}%, "
            f"~{conseq['units_short']:.0f} units short)\n"
            f"Net benefit of acting: SGD {conseq['net_benefit']:,.2f}\n"
            f"Pre-calculated confidence: {conseq['confidence']}\n"
            f"Observation: {inv.get('observation', '')}\n"
        )

    context_text = _format_context(context)

    company_desc_rec = config.get("company_description") or org_name

    system_prompt = (
        f"You are a consequence-aware purchasing advisor for: {company_desc_rec}\n\n"
        "Every recommendation MUST follow this exact structure. No exceptions.\n\n"
        "MANDATORY OUTPUT FORMAT per item:\n"
        "{\n"
        '  "item": "<name>",\n'
        '  "supplier": "<name>",\n'
        '  "supplier_type": "<import|local|other>",\n'
        '  "lead_time_days": <number>,\n'
        '  "recommended_action": "<REORDER|HOLD|ESCALATE|MONITOR>",\n'
        '  "suggested_quantity": <number or "Verify with team">,\n'
        '  "confidence": "<HIGH|MED|LOW|INSUFFICIENT_DATA>",\n'
        '  "consequence_if_acting": "<1 sentence, financial terms, SGD amounts>",\n'
        '  "consequence_if_not_acting": "<1 sentence, financial terms, stockout risk>",\n'
        '  "supplier_risk": "<None|LOW|HIGH>",\n'
        '  "mitigation": "<Required if supplier_risk=HIGH. Concrete action. Empty string if not HIGH.>",\n'
        '  "flags": ["<string>"],\n'
        '  "reason": "<2 sentences max. Plain English. No jargon.>"\n'
        "}\n\n"
        "RULES — enforce strictly:\n"
        "1. Use the pre-calculated SGD figures in consequence_if_acting and consequence_if_not_acting. Do not invent new numbers.\n"
        "2. If pre-calculated confidence = INSUFFICIENT_DATA, output INSUFFICIENT_DATA. Do not upgrade it.\n"
        "3. If supplier delay_probability > 30% OR data_quality < 50%, supplier_risk = HIGH and mitigation is REQUIRED. "
        "   Mitigation must be a concrete action (e.g. 'Contact supplier to confirm stock before placing PO', "
        "   'Split order across two suppliers to reduce dependency', 'Raise safety stock to 45 days cover').\n"
        "4. Never output a recommendation without a confidence level.\n"
        "5. Do NOT recommend ordering dead SKUs (zero sales for 6+ months).\n"
        "6. Return ONLY a valid JSON array. No text outside the array."
    )

    user_prompt = (
        f"Items requiring attention ({len(enriched_lines)} items):\n\n"
        + "\n".join(enriched_lines)
        + f"\n\nContext from purchasing team:\n{context_text}\n\n"
        "Generate consequence-aware purchase recommendations. Use the pre-calculated SGD figures."
    )

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=12000)
        recs, _repaired = _extract_json_array(raw)
        if recs is None:
            _emit(progress_emit, "Recommendation agent returned no usable response")
            return [{"error": f"Recommendation agent returned no usable JSON. First 400 chars: {raw[:400]}"}]

        # Attach pre-calculated consequence data and save outcomes to DB
        item_conseq_map = {
            e["inv"].get("item", ""): e["conseq"] for e in enriched_items
        }
        for rec in recs:
            iname = rec.get("item", "")
            c = item_conseq_map.get(iname)
            if c:
                rec["_act_sgd"]     = c["act_sgd"]
                rec["_no_act_sgd"]  = c["no_act_sgd"]
                rec["_net_benefit"] = c["net_benefit"]
                rec["_p_stockout"]  = c["p_stockout"]
                try:
                    save_recommendation_outcome(
                        session_id=session_id,
                        item=iname,
                        action_recommended=rec.get("recommended_action", ""),
                        predicted_loss_no_act=c["no_act_sgd"],
                        predicted_cost_act=c["act_sgd"],
                        net_benefit=c["net_benefit"],
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
