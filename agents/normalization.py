"""Agent 1 — the "same product, different name" finder.

Scans item names across inventory/PO/sales and groups duplicates so the rest of
the pipeline treats one product as one item. Moved verbatim from agents.py.
"""

from database import query, get_company_config
from .shared import _call_claude, _emit, _extract_json_array


def run_normalization_agent(session_id: int, model: str, progress_emit=None) -> dict:
    _emit(progress_emit, "Reading item names from your inventory, purchase orders, and sales files")

    _sess = query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    _org_name    = _sess[0]["org_name"] if _sess else "your company"
    _norm_config = get_company_config(_org_name)
    _company_desc = _norm_config.get("company_description") or _org_name
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
        f"You are a data normalisation specialist for {_company_desc}.\n"
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
