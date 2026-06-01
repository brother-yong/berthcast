"""Shared building blocks for the agents: the Claude client, lead-time/spoilage
constants, supplier resolution, and the small JSON/parsing helpers every agent uses.

Moved verbatim from the old single-file agents.py — no logic changes.
"""

import json
import os
from database import (
    query,
    get_supplier_profile,
)

import anthropic

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

LEAD_TIME_DAYS = {
    "import": 16 * 7,
    "local":   3 * 7,
    # "other" deliberately omitted — no reliable default, treat as unknown
}

SPOILAGE_THRESHOLD_DAYS = {
    "chill":   14,
    "frozen":  60,
    "dry":     180,
}

# Category-based supplier type inference — shared between inventory and rec agents.
# Used as a last-resort fallback when the supplier listing and PO table don't
# have a match. Food-distribution defaults; non-food clients should populate
# their own supplier profiles via the settings page.
CATEGORY_SUPPLIER_TYPE = {
    "bread": "local", "bun": "local", "hotdog": "local", "prata": "local",
    "eggs": "local", "egg": "local", "water": "local",
    "coca-cola": "local", "fanta": "local", "sprite": "local", "pepsi": "local",
    "7up": "local", "carbonated": "local",
    "spring roll skin": "local", "spring roll": "local",
    "milk": "import", "cheese": "import", "butter": "import",
    "cream": "import", "yoghurt": "import", "yogurt": "import",
    "ice cream": "import", "muesli": "import", "cereal": "import",
    "pasta": "import", "noodle": "import", "flour": "import",
    "biscuit": "import", "cracker": "import", "cookie": "import",
    "juice": "import", "coffee": "import", "sauce": "import",
    "ketchup": "import", "canned": "import", "tortilla": "import",
    "pizza": "import", "pastry": "import", "puff": "import",
    "mozzarella": "import", "parmesan": "import", "edam": "import",
    "cheddar": "import", "feta": "import", "gouda": "import",
    "cottage cheese": "import", "emmenthal": "import",
}

LEAD_TIME_BY_TYPE = {"import": 112, "local": 21, "other": 56}


def _infer_supplier_type(item_name: str) -> str:
    """Guess import vs local from product keywords. Last-resort fallback."""
    name_lower = item_name.lower()
    for keyword, stype in CATEGORY_SUPPLIER_TYPE.items():
        if keyword in name_lower:
            return stype
    return "other"


def _resolve_item_suppliers(session_id: int, org_name: str, config: dict,
                            alias_map: dict = None, progress_emit=None):
    """Build per-item supplier context: supplier name, type, lead time, risk.

    Returns two dicts:
      item_supplier_map:  {item_name: supplier_name}
      item_lead_time_map: {item_name: {"supplier": str, "type": str,
                                        "lead_time_days": int|None,
                                        "delay_prob": float, "high_risk": bool}}
    """
    alias_map = alias_map or {}
    sup_table = f"suppliers_{session_id}"
    po_table  = f"purchase_orders_{session_id}"

    # 1. Build supplier_type_map from the Supplier Listing upload
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

    # 2. Build item→supplier from Purchase Orders (most recent PO per item)
    item_supplier_map = {}
    try:
        sample = query(f"SELECT * FROM {po_table} LIMIT 1")
        if sample:
            cols = list(sample[0].keys())
            desc_col = next((c for c in cols if c in (
                "inventory_desc", "item_description", "description", "product_name")), None)
            sup_col = next((c for c in cols if "supplier" in c and "name" in c), None) or \
                      next((c for c in cols if "supplier" in c), None)
            if desc_col and sup_col:
                po_rows = query(
                    f'SELECT "{desc_col}" as item_name, "{sup_col}" as sup_name '
                    f'FROM {po_table} WHERE "{desc_col}" IS NOT NULL '
                    f'ORDER BY rowid DESC LIMIT 3000'
                )
                for row in po_rows:
                    item = row.get("item_name", "")
                    sup  = row.get("sup_name", "")
                    if item and item not in item_supplier_map:
                        item_supplier_map[item] = sup
                    # Also try canonical name so inventory names match
                    if item and alias_map:
                        canonical = alias_map.get(str(item).strip().lower())
                        if canonical and canonical not in item_supplier_map:
                            item_supplier_map[canonical] = sup
    except Exception:
        pass

    _emit(progress_emit,
          f"Mapped {len(supplier_type_map)} suppliers, {len(item_supplier_map)} item→supplier links")

    # 3. For each known item, resolve lead time from profile → type default → config default
    item_lead_time_map = {}
    for item_name, supplier in item_supplier_map.items():
        stype = supplier_type_map.get(supplier, "other")
        if stype == "other" and (not supplier or supplier == "Unknown"):
            stype = _infer_supplier_type(item_name)

        sup_profile = get_supplier_profile(org_name, supplier)
        if not supplier or supplier == "Unknown":
            lt_days = None
        else:
            lt_days = (sup_profile.get("avg_lead_time_days")
                       or LEAD_TIME_BY_TYPE.get(stype)
                       or config.get("default_lead_time_days")
                       or None)
        delay_prob = sup_profile.get("delay_probability", 0.2)
        quality    = sup_profile.get("data_quality_score", 0.3)
        high_risk  = delay_prob > 0.30 or quality < 0.50

        item_lead_time_map[item_name] = {
            "supplier":       supplier,
            "type":           stype,
            "lead_time_days": lt_days,
            "delay_prob":     delay_prob,
            "high_risk":      high_risk,
        }

    return item_supplier_map, item_lead_time_map, supplier_type_map


# ---------------------------------------------------------------------------
# Consequence engine — pure Python, no LLM involvement
# ---------------------------------------------------------------------------

def _call_claude(model: str, system: str, user: str, max_tokens: int = 4096) -> str:
    # Use streaming internally — Anthropic requires it for large max_tokens values.
    # Callers receive the complete text string exactly as before.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user}]
    ) as stream:
        return stream.get_final_text()


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
