"""Pure recommendation-display helpers: confidence, effective qty/supplier,
order-by date, supplier grouping, confidence reasons. Extracted verbatim from app.py."""
from datetime import datetime, timedelta


_CONFIDENCE_ALIASES = {
    "MED":          "MEDIUM",
    "MID":          "MEDIUM",
    "M":            "MEDIUM",
    "H":            "HIGH",
    "L":            "LOW",
    "INSUFFICIENT": "INSUFFICIENT_DATA",
    "UNKNOWN":      "INSUFFICIENT_DATA",
    "N/A":          "INSUFFICIENT_DATA",
}


def _normalise_confidence(rec):
    """Coerce rec['confidence'] to one of HIGH / MEDIUM / LOW / INSUFFICIENT_DATA."""
    if not isinstance(rec, dict):
        return
    raw = (rec.get("confidence") or "").strip().upper()
    if not raw:
        return
    if raw in ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT_DATA"):
        rec["confidence"] = raw
        return
    rec["confidence"] = _CONFIDENCE_ALIASES.get(raw, "INSUFFICIENT_DATA")


def _effective_qty(rec):
    """Return the quantity to display/export: edited value if user adjusted it,
    otherwise the AI's suggested quantity."""
    if not isinstance(rec, dict):
        return ""
    edited = rec.get("edited_quantity")
    if edited not in (None, "", "null"):
        return edited
    return rec.get("suggested_quantity", "")


def _effective_supplier(rec):
    """Return the supplier to display/export: edited value if user adjusted it,
    otherwise the AI's suggested supplier."""
    if not isinstance(rec, dict):
        return ""
    edited = rec.get("edited_supplier")
    if edited not in (None, "", "null"):
        return edited
    return rec.get("supplier", "")


def _compute_order_by(rec):
    """Compute when the user must place this order to avoid a stockout.

    Returns a dict with:
      order_by_date: human-readable string like "12 Jun 2026", or None
      buffer_days:   int (negative = already overdue), or None
      status:        'overdue' | 'urgent' | 'ok' | 'unknown'
    """
    if not isinstance(rec, dict):
        return {"order_by_date": None, "buffer_days": None, "status": "unknown"}
    dos = rec.get("days_of_supply")
    lt  = rec.get("lead_time_days")
    try:
        dos = float(dos) if dos not in (None, "", "null") else None
        lt  = float(lt)  if lt  not in (None, "", "null") else None
    except (TypeError, ValueError):
        return {"order_by_date": None, "buffer_days": None, "status": "unknown"}

    if dos is None or lt is None:
        return {"order_by_date": None, "buffer_days": None, "status": "unknown"}

    buffer_days = int(round(dos - lt))
    order_by = datetime.utcnow() + timedelta(days=buffer_days)
    order_by_date = order_by.strftime("%d %b %Y")

    if buffer_days < 0:
        status = "overdue"
    elif buffer_days <= 7:
        status = "urgent"
    else:
        status = "ok"

    return {
        "order_by_date": order_by_date,
        "buffer_days":   buffer_days,
        "status":        status,
    }


def _group_recs_by_supplier(recommendations, status_by_item):
    """Group recommendations by their effective supplier (user-edited if present,
    otherwise the AI's suggestion). Returns a list of dicts, ordered with the
    most-urgent supplier first.

    Each group dict:
      - name:      supplier display name (or 'Unknown supplier' if blank)
      - key:       slug used as DOM id
      - count:     total recs in this group
      - critical:  number of CRITICAL items
      - low:       number of LOW items
      - supplier_type: 'import' / 'local' / 'other' (taken from first rec)
      - items:     list of item names (for the bulk-approve POST)
      - recs:      list of the actual rec dicts

    Groups are sorted: most critical first, then most items first, then name.
    """
    groups = {}
    for rec in recommendations:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        supplier = _effective_supplier(rec) or "Unknown supplier"
        key = supplier.strip() or "Unknown supplier"
        g = groups.get(key)
        if g is None:
            g = {
                "name":          key,
                "key":           "sg-" + "".join(c if c.isalnum() else "-" for c in key.lower())[:60],
                "count":         0,
                "critical":      0,
                "low":           0,
                "supplier_type": rec.get("supplier_type") or "other",
                "item_names":    [],
                "recs":          [],
            }
            groups[key] = g
        g["count"] += 1
        g["recs"].append(rec)
        g["item_names"].append(rec.get("item", ""))
        item_status = status_by_item.get(str(rec.get("item", "")), "")
        if item_status == "CRITICAL":
            g["critical"] += 1
        elif item_status == "LOW":
            g["low"] += 1

    ordered = sorted(
        groups.values(),
        key=lambda g: (-g["critical"], -g["count"], g["name"].lower())
    )
    return ordered


def _confidence_reasons(rec):
    """Build a short, plain-English list of reasons explaining why the AI
    settled on this confidence level. Used in the confidence-ring popover."""
    if not isinstance(rec, dict):
        return []
    reasons = []
    conf = (rec.get("confidence") or "").upper()
    if conf == "INSUFFICIENT_DATA":
        reasons.append("Supplier or sales history not in system.")

    if not rec.get("supplier") or rec.get("supplier") in ("Unknown", "unknown", "—"):
        reasons.append("Supplier not on file.")

    lt = rec.get("lead_time_days")
    if lt in (None, "", "null"):
        reasons.append("Lead time unknown — buffer based on supplier type.")
    else:
        try:
            lt_f = float(lt)
            reasons.append(f"Lead time on file: {int(lt_f)} days.")
        except (TypeError, ValueError):
            pass

    dos = rec.get("days_of_supply")
    if dos in (None, "", "null"):
        reasons.append("Stock runway unknown — limited sales history.")

    risk = rec.get("supplier_risk")
    if risk == "HIGH":
        reasons.append("Supplier flagged as high-risk (delays or unreliable).")

    flags = rec.get("flags") or []
    if isinstance(flags, list) and flags:
        for f in flags[:3]:
            if f:
                reasons.append(str(f))

    if not reasons:
        if conf == "HIGH":
            reasons.append("Strong sales history, known supplier, lead time on file.")
        elif conf in ("MED", "MEDIUM"):
            reasons.append("Some data missing — recommendation is solid but not certain.")
        elif conf == "LOW":
            reasons.append("Limited data — review before approving.")
    return reasons
