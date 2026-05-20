"""
BerthAI - Three-Agent System
"""

import json
import os
from database import (
    query, get_db,
    get_company_config, get_supplier_profile,
    get_supplier_profiles,
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
                        f'SUM(CAST("{rank_col}" AS REAL)) as metric '
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
                           f'ORDER BY SUM(CAST("{rank_col}" AS REAL)) DESC LIMIT {n}'
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

    inv_summary_lines = []
    for row in inventory_sorted[:800]:
        desc = row.get(_desc_col) or "Unknown"
        qty  = row.get(_qty_col)  or "0"
        cat  = (row.get(_cat_col) if _cat_col else None) or "GENERAL"
        canonical  = alias_map.get(str(desc).lower(), desc)
        sales_info = sales_by_item.get(desc, {})
        total_sold = sales_info.get("total_qty", 0) or 0
        txn_count  = sales_info.get("txn_count",  0) or 0
        # Flag items with no recorded sales so the AI has a hard data signal
        movement_tag = " | NEVER_SOLD" if (total_sold == 0 and txn_count == 0) else ""
        inv_summary_lines.append(
            f"Item: {canonical} | Category: {cat} | Stock: {qty} | Total sold: {total_sold}{movement_tag}"
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
        "DEAD SKU rules (apply strictly — DEAD items are never ordered):\n"
        "- Any item tagged NEVER_SOLD is DEAD unless there is a clear reason it is new stock.\n"
        "- Any item with zero total sold AND stock on hand is DEAD.\n"
        "- Any item with zero total sold AND zero stock is DEAD (not CRITICAL).\n"
        "- Once marked DEAD, the spoilage_risk should be set to NONE regardless of category.\n\n"
        "Other status rules:\n"
        "- CRITICAL: actively selling item with stock at or near zero — needs urgent reorder.\n"
        "- LOW: actively selling item with declining stock — reorder soon.\n"
        "- HEALTHY: adequate stock relative to sales velocity.\n\n"
        "Spoilage rules:\n"
        + spoilage_rules +
        "\nReturn ONLY a JSON array of objects with keys:\n"
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

    # Strip dead SKUs first — they must never reach the recommendation agent
    live_items = [r for r in inventory_report if r.get("status") != "DEAD"]
    dead_count = len(inventory_report) - len(live_items)
    if dead_count:
        _emit(progress_emit, f"Excluded {dead_count} dead SKUs from recommendations")

    actionable = [
        r for r in live_items
        if r.get("status") in ("LOW", "CRITICAL") or r.get("spoilage_risk") in ("HIGH", "MEDIUM")
    ][:150]

    if not actionable:
        _emit(progress_emit, "No items need attention right now — inventory looks healthy")
        return []

    _emit(progress_emit, f"Filtered to {len(actionable)} items needing attention")
    _emit(progress_emit, "Building supplier context for consequence reasoning")

    # Build enriched item lines for Claude
    enriched_lines = []
    for inv_item in actionable:
        iname    = inv_item.get("item", "Unknown")
        supplier = item_supplier_map.get(iname, "Unknown") or "Unknown"
        stype    = supplier_type_map.get(supplier, "other")

        sup_profile = get_supplier_profile(org_name, supplier)
        lt_days     = sup_profile.get("avg_lead_time_days") or LEAD_TIME_DAYS.get(stype, config.get("default_lead_time_days", 56))
        delay_prob  = sup_profile.get("delay_probability", 0.2)
        quality     = sup_profile.get("data_quality_score", 0.3)
        sup_notes   = sup_profile.get("notes", "")
        high_risk   = delay_prob > 0.30 or quality < 0.50
        known_sup   = quality >= 0.5

        acc      = get_supplier_accuracy(org_name, supplier)
        acc_note = ""
        if acc.get("total_recs", 0) > 0:
            acc_note = (f" | Past recs: {acc['total_recs']} — "
                        f"{acc['approved']} approved, {acc['dismissed']} dismissed")

        enriched_lines.append(
            f"---\n"
            f"Item: {iname}\n"
            f"Status: {inv_item.get('status')} | Spoilage risk: {inv_item.get('spoilage_risk')}\n"
            f"Stock: {inv_item.get('stock')} units | Days of supply: {inv_item.get('days_of_supply', 'unknown')}\n"
            f"Supplier: {supplier} ({stype}, lead time: {lt_days} days)\n"
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
        'if sales slow, Cool Link risks wastage in cold storage."\n'
        '  "consequence_if_not_acting": "Without a reorder, Cool Link will run out of frozen salmon '
        'within 4 days, leaving active customer orders unfulfilled."\n\n'
        "MANDATORY OUTPUT FORMAT — JSON array, one object per item:\n"
        "{\n"
        '  "item": "<name>",\n'
        '  "supplier": "<name>",\n'
        '  "supplier_type": "<import|local|other>",\n'
        '  "lead_time_days": <number>,\n'
        '  "recommended_action": "<REORDER|HOLD|ESCALATE|MONITOR>",\n'
        '  "suggested_quantity": <number or "Verify with team">,\n'
        '  "confidence": "<HIGH|MED|LOW|INSUFFICIENT_DATA>",\n'
        '  "consequence_if_acting": "<1 plain sentence>",\n'
        '  "consequence_if_not_acting": "<1 plain sentence>",\n'
        '  "supplier_risk": "<None|LOW|HIGH>",\n'
        '  "mitigation": "<Concrete action if HIGH risk, else empty string>",\n'
        '  "flags": ["<string>"],\n'
        '  "reason": "<2 sentences max. Plain English.>"\n'
        "}\n\n"
        "RULES:\n"
        "1. consequence_if_acting and consequence_if_not_acting must be plain business statements. "
        "   No SGD amounts unless you have reliable sales data. Name the company and item.\n"
        "2. confidence = INSUFFICIENT_DATA if supplier is not known to the system. Do not guess.\n"
        "3. supplier_risk = HIGH and mitigation REQUIRED if delay rate > 30% or supplier unknown. "
        "   Mitigation must be actionable (e.g. 'Confirm stock availability before raising PO').\n"
        "4. Every recommendation must have a confidence level. No exceptions.\n"
        "5. Do NOT recommend ordering dead SKUs.\n"
        "6. Return ONLY a valid JSON array. No text outside the array."
    )

    user_prompt = (
        f"Items requiring attention ({len(enriched_lines)} items):\n\n"
        + "\n".join(enriched_lines)
        + f"\n\nContext from purchasing team:\n{context_text}\n\n"
        "Generate consequence-aware purchase recommendations."
    )

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=12000)
        recs, _repaired = _extract_json_array(raw)
        if recs is None:
            _emit(progress_emit, "Recommendation agent returned no usable response")
            return [{"error": f"Recommendation agent returned no usable JSON. First 400 chars: {raw[:400]}"}]

        # Save outcome stubs for future learning
        for rec in recs:
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
