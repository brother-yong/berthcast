"""
BerthAI — Three-Agent System
"""

import json
import os
from database import query, get_db

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


def _call_claude(model: str, system: str, user: str, max_tokens: int = 4096) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return response.content[0].text


def run_normalization_agent(session_id: int, model: str) -> dict:
    item_names = set()

    inv_table = f"inventory_{session_id}"
    po_table  = f"purchase_orders_{session_id}"
    sal_table = f"sales_{session_id}"

    def _col_candidates(table: str, candidates: list) -> list:
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

    item_names.update(_col_candidates(inv_table, ["description", "item_description", "inventory_desc", "item_name", "product_description"]))
    item_names.update(_col_candidates(po_table,  ["inventory_desc", "item_description", "description", "product_name", "item_name"]))
    item_names.update(_col_candidates(sal_table, ["inventory_desc", "item_description", "description", "product_name", "item_name"]))

    if not item_names:
        return {"groups": [], "message": "No item names found in uploaded data."}

    items_list = sorted(list(item_names))[:1500]

    system_prompt = (
        "You are a data normalisation specialist for a food distribution company.\n"
        "Your job is to identify item names that clearly refer to the same product but are written differently.\n\n"
        "Rules:\n"
        "- Only group items you are confident are the same product (same product, same approximate size/weight)\n"
        "- Do NOT merge items if you are uncertain — it is better to leave them separate\n"
        "- Return ONLY a JSON array of groups, nothing else\n"
        "- Each group must have: \"canonical\" (the clearest name) and \"variants\" (list of other names for the same item)\n"
        "- Only include groups where there are 2 or more variants — do not include solo items\n"
        "- Keep the list concise: focus on clear duplicates only\n\n"
        "Example output:\n"
        "[\n"
        "  {\"canonical\": \"White Bread 400g\", \"variants\": [\"WHT BRD 400G\", \"Bread White 400g\"]},\n"
        "  {\"canonical\": \"Hamburger Buns 6pcs\", \"variants\": [\"HMB BUN 6S\", \"Burger Bun 6pc\"]}\n"
        "]"
    )

    user_prompt = f"Here are {len(items_list)} unique item names from this company's data files.\nIdentify which ones are the same product described differently and group them.\n\nItem names:\n" + "\n".join(items_list)

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=4096)
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return {"groups": [], "message": "No duplicates found."}
        groups = json.loads(raw[start:end])
        return {"groups": groups, "total_items_scanned": len(items_list)}
    except json.JSONDecodeError:
        return {"groups": [], "message": "Agent could not parse groupings. Proceeding without deduplication."}
    except Exception as e:
        return {"groups": [], "message": f"Normalisation agent error: {str(e)}"}


def run_inventory_agent(session_id: int, model: str, confirmed_groups: list, context: dict) -> dict:
    inv_table = f"inventory_{session_id}"
    sal_table = f"sales_{session_id}"

    try:
        inventory = query(f"SELECT * FROM {inv_table} LIMIT 3000")
    except Exception as e:
        return {"error": f"Could not read inventory table: {e}"}

    alias_map = {}
    for group in confirmed_groups:
        for variant in group.get("variants", []):
            alias_map[variant.lower()] = group["canonical"]

    sales_by_item = {}
    try:
        sample = query(f"SELECT * FROM {sal_table} LIMIT 1")
        if sample:
            cols = list(sample[0].keys())
            desc_col = next((c for c in cols if c in ("inventory_desc", "item_description", "description", "product_name")), None)
            qty_col  = next((c for c in cols if c in ("billing_qty", "qty", "quantity", "billing_quantity")), None)
            if desc_col and qty_col:
                sal_rows = query(f"""
                    SELECT "{desc_col}" as item_name, SUM(CAST("{qty_col}" AS REAL)) as total_qty, COUNT(*) as txn_count
                    FROM {sal_table}
                    GROUP BY "{desc_col}"
                    LIMIT 5000
                """)
                sales_by_item = {r["item_name"]: r for r in sal_rows if r["item_name"]}
    except Exception:
        sales_by_item = {}

    _sample   = inventory[0] if inventory else {}
    _cols     = list(_sample.keys())
    _desc_col = next((k for k in _cols if k in ("description", "item_description", "inventory_desc", "product_description")), None)
    _qty_col  = next((k for k in _cols if k == "qty_on_hand"), None) or \
                next((k for k in _cols if k not in ("qty_on_hand_allocated",) and ("qty" in k or "quantity" in k or "stock" in k)), None)
    _cat_col  = next((k for k in _cols if "category" in k), None) or \
                next((k for k in _cols if "cat" in k or "class" in k), None)

    inv_summary_lines = []
    for row in inventory[:500]:
        desc = row.get(_desc_col, "Unknown") if _desc_col else "Unknown"
        qty  = row.get(_qty_col, "0") if _qty_col else "0"
        cat  = row.get(_cat_col, "DRY") if _cat_col else "DRY"
        canonical  = alias_map.get(desc.lower(), desc)
        sales_info = sales_by_item.get(desc, {})
        total_sold = sales_info.get("total_qty", 0) or 0
        inv_summary_lines.append(
            f"Item: {canonical} | Category: {cat} | Stock: {qty} | Total sold (data period): {total_sold}"
        )

    if not inv_summary_lines:
        return {"error": f"No inventory data found. Check that your Inventory Report uploaded correctly. (desc_col={_desc_col}, qty_col={_qty_col}, cat_col={_cat_col}, rows={len(inventory)})"}

    context_text  = _format_context(context)

    system_prompt = (
        "You are an inventory health analyst for a food distribution company.\n\n"
        "Analyse the inventory data provided and produce a health report.\n\n"
        "For each item, determine:\n"
        "1. Status: HEALTHY / LOW / CRITICAL / DEAD (no sales, likely discontinued)\n"
        "2. Spoilage risk: HIGH / MEDIUM / LOW / NONE (based on category and movement)\n"
        "3. Days of supply estimate if calculable\n"
        "4. A one-line plain English observation\n\n"
        "Rules:\n"
        "- CHILL items with slow movement are HIGH spoilage risk\n"
        "- FROZEN items with no movement in 60+ days are MEDIUM-HIGH spoilage risk\n"
        "- DRY items with no movement in 180+ days are LOW risk but flag as DEAD SKU\n"
        "- Items with zero stock are not flagged for spoilage — they are either CRITICAL (still selling) or DEAD (not selling)\n"
        "- Be conservative: only flag what you are confident about\n\n"
        "Return ONLY valid JSON — an array of objects with keys:\n"
        "item, category, stock, status, spoilage_risk, days_of_supply, observation\n\n"
        "Do not include any text outside the JSON array."
    )

    user_prompt = (
        f"Inventory snapshot ({len(inv_summary_lines)} items):\n\n"
        + "\n".join(inv_summary_lines)
        + f"\n\nAdditional context from the purchasing team:\n{context_text}\n\nAnalyse and return the health report JSON."
    )

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=8000)
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return {"error": f"Inventory agent returned no JSON. Model response: {raw[:300]}"}
        report = json.loads(raw[start:end])
        return {"report": report, "items_analysed": len(report)}
    except json.JSONDecodeError as e:
        return {"error": f"Inventory agent returned malformed JSON: {str(e)}"}
    except Exception as e:
        return {"error": f"Inventory agent error: {str(e)}"}


def run_recommendation_agent(session_id: int, model: str, inventory_report: list, context: dict) -> list:
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
                po_rows = query(f"""
                    SELECT "{desc_col}" as item_name, "{sup_col}" as sup_name
                    FROM {po_table}
                    WHERE "{desc_col}" IS NOT NULL
                    ORDER BY rowid DESC
                    LIMIT 3000
                """)
                for row in po_rows:
                    item = row.get("item_name", "")
                    sup  = row.get("sup_name", "")
                    if item and item not in item_supplier_map:
                        item_supplier_map[item] = sup
    except Exception:
        pass

    unreliable_suppliers = {"overseas supplier", "abd khan", "nhan tu"}
    context_text = _format_context(context)

    actionable = [
        r for r in inventory_report
        if r.get("status") in ("LOW", "CRITICAL") or r.get("spoilage_risk") in ("HIGH", "MEDIUM")
    ][:150]

    if not actionable:
        return []

    enriched_lines = []
    for item in actionable:
        iname      = item.get("item", "Unknown")
        supplier   = item_supplier_map.get(iname, "Unknown supplier")
        stype      = supplier_type_map.get(supplier, "other")
        lt_days    = LEAD_TIME_DAYS[stype]
        unreliable = supplier.lower() in unreliable_suppliers
        enriched_lines.append(
            f"Item: {iname} | Status: {item.get('status')} | Spoilage risk: {item.get('spoilage_risk')} | "
            f"Stock: {item.get('stock')} | Days of supply: {item.get('days_of_supply')} | "
            f"Supplier: {supplier} ({stype}, lead time: {lt_days} days) | "
            f"Unreliable supplier: {'YES — flag this' if unreliable else 'No'} | "
            f"Observation: {item.get('observation')}"
        )

    system_prompt = (
        "You are a purchasing advisor for a food distribution company in Singapore.\n\n"
        "Generate purchase recommendations based on the inventory data provided.\n\n"
        "Rules:\n"
        "- For import suppliers (lead time 112 days): recommend ordering if stock will run out within 4 months\n"
        "- For local suppliers (lead time 21 days): recommend ordering if stock will run out within 1 month\n"
        "- For other suppliers (lead time 56 days): recommend ordering if stock will run out within 2 months\n"
        "- Flag unreliable suppliers clearly — purchasing team should verify before placing the order\n"
        "- Consider any context notes provided by the team\n"
        "- Do NOT recommend ordering dead SKUs (items with no sales)\n"
        "- Confidence: HIGH = clear data supports it, MEDIUM = some uncertainty, LOW = recommend verification first\n\n"
        "Return ONLY valid JSON — an array of objects with these exact keys:\n"
        "item, supplier, supplier_type, lead_time_days, recommended_action, suggested_quantity, confidence, flags, reason\n\n"
        "- flags: array of strings (e.g. [\"Unreliable supplier — verify before ordering\", \"Expiry risk\"])\n"
        "- reason: 1-2 plain English sentences, no jargon\n"
        "- suggested_quantity: number or \"Verify with team\"\n\n"
        "Do not include any text outside the JSON array."
    )

    user_prompt = (
        f"Items requiring attention ({len(enriched_lines)} items):\n\n"
        + "\n".join(enriched_lines)
        + f"\n\nAdditional context from the purchasing team:\n{context_text}\n\nGenerate purchase recommendations."
    )

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=8000)
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return []
        return json.loads(raw[start:end])
    except Exception as e:
        return [{"error": str(e)}]


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
