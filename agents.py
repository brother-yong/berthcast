"""
BerthAI - Three-Agent System
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


def run_normalization_agent(session_id: int, model: str) -> dict:
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
        return {"groups": [], "message": "No item names found in uploaded data."}

    items_list = sorted(list(item_names))[:1500]

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
            return {"groups": [], "message": "No duplicates found."}
        msg = "Groupings repaired from truncated output." if repaired else ""
        return {"groups": groups, "total_items_scanned": len(items_list), "message": msg}
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
                sal_rows = query(
                    'SELECT "' + desc_col + '" as item_name, SUM(CAST("' + qty_col + '" AS REAL)) as total_qty, '
                    'COUNT(*) as txn_count FROM ' + sal_table + ' GROUP BY "' + desc_col + '" LIMIT 5000'
                )
                sales_by_item = {r["item_name"]: r for r in sal_rows if r["item_name"]}
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

    inv_summary_lines = []
    for row in inventory[:500]:
        desc = row.get(_desc_col) or "Unknown"
        qty  = row.get(_qty_col)  or "0"
        cat  = (row.get(_cat_col) if _cat_col else None) or "DRY"
        canonical  = alias_map.get(str(desc).lower(), desc)
        sales_info = sales_by_item.get(desc, {})
        total_sold = sales_info.get("total_qty", 0) or 0
        inv_summary_lines.append(
            f"Item: {canonical} | Category: {cat} | Stock: {qty} | Total sold: {total_sold}"
        )

    if not inv_summary_lines:
        return {"error": f"No inventory rows. desc_col={_desc_col}, qty_col={_qty_col}, rows={len(inventory)}"}

    context_text = _format_context(context)

    system_prompt = (
        "You are an inventory health analyst for a food distribution company.\n\n"
        "For each item, determine:\n"
        "1. Status: HEALTHY / LOW / CRITICAL / DEAD\n"
        "2. Spoilage risk: HIGH / MEDIUM / LOW / NONE\n"
        "3. Days of supply estimate if calculable\n"
        "4. A one-line plain English observation\n\n"
        "Rules:\n"
        "- CHILL items with slow movement are HIGH spoilage risk\n"
        "- FROZEN items with no movement in 60+ days are MEDIUM-HIGH spoilage risk\n"
        "- DRY items with no movement in 180+ days are LOW risk but flag as DEAD SKU\n"
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
            return {"error": f"Inventory agent returned no usable JSON. First 400 chars: {raw[:400]}"}
        return {"report": report, "items_analysed": len(report), "partial": repaired}
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

    unreliable_suppliers = {"el sabah", "abd khan", "nhan tu"}
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
        supplier   = item_supplier_map.get(iname, "Unknown supplier") or "Unknown supplier"
        stype      = supplier_type_map.get(supplier, "other")
        lt_days    = LEAD_TIME_DAYS[stype]
        unreliable = supplier.lower() in unreliable_suppliers
        unrel_text = "YES - flag this" if unreliable else "No"
        enriched_lines.append(
            f"Item: {iname} | Status: {item.get('status')} | Spoilage risk: {item.get('spoilage_risk')} | "
            f"Stock: {item.get('stock')} | Days of supply: {item.get('days_of_supply')} | "
            f"Supplier: {supplier} ({stype}, lead time: {lt_days} days) | "
            f"Unreliable supplier: {unrel_text} | "
            f"Observation: {item.get('observation')}"
        )

    system_prompt = (
        "You are a purchasing advisor for a food distribution company in Singapore.\n\n"
        "Rules:\n"
        "- Import suppliers (lead time 112 days): recommend ordering if stock runs out within 4 months\n"
        "- Local suppliers (lead time 21 days): recommend ordering if stock runs out within 1 month\n"
        "- Other suppliers (lead time 56 days): recommend ordering if stock runs out within 2 months\n"
        "- Flag unreliable suppliers clearly - team should verify before ordering\n"
        "- Do NOT recommend ordering dead SKUs\n"
        "- Confidence: HIGH / MEDIUM / LOW\n\n"
        "Return ONLY a JSON array of objects with these keys:\n"
        "item, supplier, supplier_type, lead_time_days, recommended_action, suggested_quantity, confidence, flags, reason\n"
        "- flags: array of strings\n"
        "- reason: 1-2 plain English sentences, no jargon\n"
        "- suggested_quantity: number or \"Verify with team\"\n"
        "Do not include text outside the JSON array."
    )

    user_prompt = (
        f"Items requiring attention ({len(enriched_lines)} items):\n\n"
        + "\n".join(enriched_lines)
        + f"\n\nContext from purchasing team:\n{context_text}\n\nGenerate purchase recommendations."
    )

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=12000)
        recs, _repaired = _extract_json_array(raw)
        if recs is None:
            return [{"error": f"Recommendation agent returned no usable JSON. First 400 chars: {raw[:400]}"}]
        return recs
    except Exception as e:
        return [{"error": str(e)}]
