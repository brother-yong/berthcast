"""
BerthAI — Three-Agent System
────────────────────────────
Agent 1 — NormalizationAgent   : Deduplicates item names across uploaded files
Agent 2 — InventoryAgent       : Analyses inventory health (stock levels, spoilage risk, dead SKUs)
Agent 3 — RecommendationAgent  : Generates purchase recommendations with lead time logic
"""

import json
import os
from database import query, get_db

import anthropic

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# ─────────────────────────────────────────────
# Lead time buffer by supplier type (weeks → days)
# ─────────────────────────────────────────────
LEAD_TIME_DAYS = {
    "import": 16 * 7,   # 112 days — worst case
    "local":   3 * 7,   # 21 days
    "other":   8 * 7,   # 56 days — safe middle ground
}

# Category spoilage thresholds (days with no movement = flag)
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


# ─────────────────────────────────────────────
# AGENT 1 — NormalizationAgent
# ─────────────────────────────────────────────

def run_normalization_agent(session_id: int, model: str) -> dict:
    """
    Collect all unique item names from inventory + PO + sales tables,
    ask Claude to propose groupings for items that look like the same product
    described differently, and return proposed groups for user review.
    """

    # Pull item names from each uploaded table
    item_names = set()

    inv_table = f"inventory_{session_id}"
    po_table  = f"purchase_orders_{session_id}"
    sal_table = f"sales_{session_id}"

    def _col_candidates(table: str, candidates: list[str]) -> list[str]:
        """Return rows from the first candidate column that exists in table."""
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

    items_list = sorted(list(item_names))[:1500]  # Cap to avoid token overflow

    system_prompt = """You are a data normalisation specialist for a food distribution company.
Your job is to identify item names that clearly refer to the same product but are written differently.

Rules:
- Only group items you are confident are the same product (same product, same approximate size/weight)
- Do NOT merge items if you are uncertain — it is better to leave them separate
- Return ONLY a JSON array of groups, nothing else
- Each group must have: "canonical" (the clearest name) and "variants" (list of other names for the same item)
- Only include groups where there are 2 or more variants — do not include solo items
- Keep the list concise: focus on clear duplicates only

Example output:
[
  {"canonical": "White Bread 400g", "variants": ["WHT BRD 400G", "Bread White 400g", "White Bread 400"]},
  {"canonical": "Hamburger Buns 6pcs", "variants": ["HMB BUN 6S", "Burger Bun 6pc"]}
]"""

    user_prompt = f"""Here are {len(items_list)} unique item names from this company's data files.
Identify which ones are the same product described differently and group them.

Item names:
{chr(10).join(items_list)}"""

    try:
        raw = _call_claude(model, system_prompt, user_prompt, max_tokens=4096)
        # Extract JSON from response
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


# ─────────────────────────────────────────────
# AGENT 2 — InventoryAgent
# ─────────────────────────────────────────────

def run_inventory_agent(session_id: int, model: str, confirmed_groups: list, context: dict) -> dict:
    """
    Analyse inventory health:
    - Stock levels per SKU
    - Days of supply remaining (stock ÷ avg daily sales)
    - Spoilage risk flags by category (no movement past threshold)
    - Dead SKU filter: no sales in 90+ days → excluded from ordering flags
    """

    inv_table = f"inventory_{session_id}"
    sal_table = f"sales_{session_id}"

    # Pull inventory data
    try:
        inventory = query(f"SELECT * FROM {inv_table} LIMIT 3000")
    except Exception as e:
        return {"error": f"Could not read inventory table: {e}"}

    # Build alias map from confirmed dedup groups
    alias_map = {}
    for group in confirmed_groups:
        for variant in group.get("variants", []):
            alias_map[variant.lower()] = group["canonical"]

    # Pull recent sales summary — try known column name variants
    sales_by_item = {}
    try:
        # Detect which column names are actually in this sales table
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

    # Detect inventory column names once (not per row)
    _sample = inventory[0] if inventory else {}
    _cols   = list(_sample.keys())
    _desc_col = next((k for k in _cols if k in ("description", "item_description", "inventory_desc", "product_description")), None)
    # Prefer qty_on_hand over qty_on_hand_allocated — allocated = reserved, not available
    _qty_col  = next((k for k in _cols if k == "qty_on_hand"), None) or \
                next((k for k in _cols if k not in ("qty_on_hand_allocated",) and ("qty" in k or "quantity" in k or "stock" in k)), None)
    _cat_col  = next((k for k in _cols if "category" in k), None) or \
                next((k for k in _cols if "cat" in k or "class" in k), None)

    # Summarise inventory for the agent
    inv_summary_lines = []
    for row in inventory[:500]:  # Cap to avoid token overflow
        desc_col = _desc_col
        qty_col  = _qty_col
        cat_col  = _cat_col

        desc = row.get(desc_col, "Unknown") if desc_col else "Unknown"
        qty  = row.get(qty_col, "0") if qty_col else "0"
        cat  = row.get(cat_col, "DRY") if cat_col else "DRY"

        # Apply alias
        canonical = alias_map.get(desc.lower(), desc)
        sales_info = sales_by_item.get(desc, {})
        total_sold = sales_info.get("total_qty", 0) or 0

        inv_summary_lines.append(
            f"Item: {canonical} | Category: {cat} | Stock: {qty} | Total sold (data period): {total_sold}"
        )

    context_text = _format_context(context)

    system_prompt = """You are an inventory health analyst for a food distribution company.

Analyse the inventory data provided and produce a health report.

For each item, determine:
1. Status: HEALTHY / LOW / CRITICAL / DEAD (no sales, likely discontinued)
2. Spoilage risk: HIGH / MEDIUM / LOW / NONE (based on category and movement)
3. Days of supply estimate if calculable
4. A one-line plain English observation

Rules:
- CHILL items with slow movement are HIGH spoilage risk
- FROZEN items with no movement in 60+ days are MEDIUM-HIGH spoilage risk
- DRY items with no movement in 180+ days are LOW risk but flag as DEAD SKU
- Items with zero stock are not flagged for spoilage — they are either CRITICAL (still selling) or DEAD (not selling)
- Be conservative: only flag what you are confident about

Return ONLY valid JSON — an array of objects with keys:
item, category, stock, status, spoilage_risk, days_of_supply, observation

Do not include any text outside the JSON array."""

    user_prompt = f"""Inventory snapshot ({len(inv_summary_lines)} items):

{chr(10).join(inv_summary_lines)}

Additional context from the purchasing team:
{context_text}

Analyse and return the health report JSON."""

    if not inv_summary_lines:
        return {"error": f"No inventory data found. Check that your Inventory Report uploaded correctly. (desc_col={_desc_col}, qty_col={_qty_col}, cat_col={_cat_col}, rows={len(inventory)})"}

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


# ─────────────────────────────────────────────
# AGENT 3 — RecommendationAgent
# ─────────────────────────────────────────────

def run_recommendation_agent(
    session_id: int,
    model: str,
    inventory_report: list,
    context: dict
) -> list:
    """
    Generate purchase recommendations.
    - Uses supplier type to apply correct lead time buffer (import=16wk, local=3wk, other=8wk)
    - Flags unreliable suppliers
    - Considers context notes (delays, large upcoming orders, etc.)
    - Excludes dead SKUs from recommendations
    """

    sup_table = f"suppliers_{session_id}"
    po_table  = f"purchase_orders_{session_id}"

    # Build supplier type map
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

    # Build recent PO history per item (last supplier used)
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

    # Known unreliable suppliers from Cool Link context
    unreliable_suppliers = {"el sabah", "abd khan", "nhan tu"}

    context_text = _format_context(context)

    # Filter to items that need attention (exclude HEALTHY + DEAD)
    actionable = [
        r for r in inventory_report
        if r.get("status") in ("LOW", "CRITICAL") or r.get("spoilage_risk") in ("HIGH", "MEDIUM")
    ][:150]  # Cap

    if not actionable:
        return []

    # Enrich each item with supplier + lead time info
    enriched_lines = []
    for item in actionable:
        iname    = item.get("item", "Unknown")
        supplier = item_supplier_map.get(iname, "Unknown supplier")
        stype    = supplier_type_map.get(supplier, "other")
        lt_days  = LEAD_TIME_DAYS[stype]
        unreliable = supplier.lower() in unreliable_suppliers

        enriched_lines.append(
            f"Item: {iname} | Status: {item.get('status')} | Spoilage risk: {item.get('spoilage_risk')} | "
            f"Stock: {item.get('stock')} | Days of supply: {item.get('days_of_supply')} | "
            f"Supplier: {supplier} ({stype}, lead time: {lt_days} days) | "
            f"Unreliable supplier: {'YES — flag this' if unreliable else 'No'} | "
            f"Observation: {item.get('observation')}"
        )

    system_prompt = """You are a purchasing advisor for a food distribution company in Singapore.

Generate purchase recommendations based on the inventory data provided.

Rules:
- For import suppliers (lead time 112 days): recommend ordering if stock will run out within 4 months
- For local suppliers (lead time 21 days): recommend ordering if stock will run out within 1 month
- For other suppliers (lead time 56 days): recommend ordering if stock will run out within 2 months
- Flag unreliable suppliers clearly — purchasing team should verify before placing the order
- Consider any context notes provided by the team
- Do NOT recommend ordering dead SKUs (items with no sales)
- Confidence: HIGH = clear