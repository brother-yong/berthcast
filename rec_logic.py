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


def _has_stakes(rec):
    """True when the rec has at least one non-empty consequence sentence to show.
    Lets the template decide whether to render the 'stakes' section at all."""
    if not isinstance(rec, dict):
        return False
    neg = (rec.get("consequence_if_not_acting") or "").strip()
    pos = (rec.get("consequence_if_acting") or "").strip()
    return bool(neg or pos)


def clarity_gaps(recommendations):
    """Counted data gaps for the clarity box at the top of the results page.

    Each gap = {"count": int, "label": str, "why": str} — what's missing, and
    what it costs in accuracy. Counts only what the user can actually fix by
    adding information. Empty list = no counted gaps (data complete).
    """
    valid = [r for r in recommendations
             if isinstance(r, dict) and not r.get("error")]
    if not valid:
        return []

    no_lead = sum(1 for r in valid if r.get("lead_time_days") in (None, "", "null"))
    unknown_sup = sum(1 for r in valid
                      if str(_effective_supplier(r)).strip().lower() in ("", "unknown"))
    # "Verify with team" is the sanitiser's marker for "no usable sales data
    # to size this order". An edited quantity resolves the gap for that item.
    no_qty = sum(1 for r in valid
                 if str(_effective_qty(r)).strip().lower() == "verify with team")

    gaps = []
    if no_lead:
        gaps.append({
            "count": no_lead,
            "label": "no supplier lead time",
            "why": "reorder dates are estimated",
        })
    if unknown_sup:
        gaps.append({
            "count": unknown_sup,
            "label": "unrecognised supplier",
            "why": "risk flags are cautious",
        })
    if no_qty:
        gaps.append({
            "count": no_qty,
            "label": "not enough sales data to size the order",
            "why": "quantities need a manual check",
        })
    return gaps


def _fmt_num(n):
    """Format a number for display without a trailing '.0' on whole values.
    Returns None if the value can't be read as a number."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return str(int(f)) if f == int(f) else str(round(f, 1))


def _quantity_basis(rec):
    """Plain-English sentence explaining how the suggested order quantity was sized,
    e.g. "You sell about 40 CTN/month, and this supplier takes about 3.5 months.
    Suggested order: 160 CTN — covers the wait plus a safety buffer."

    Returns None when there's no usable monthly-sales figure (the existing
    'insufficient sales data' case), so the template can hide the line. Described,
    not a strict equation — the agent may nudge the quantity, and a fake equation
    that doesn't add up would erode trust faster than no equation."""
    if not isinstance(rec, dict):
        return None
    raw_avg = rec.get("avg_monthly_sales")
    avg = _fmt_num(raw_avg)
    try:
        if avg is None or float(raw_avg) <= 0:
            return None
    except (TypeError, ValueError):
        return None

    uom = rec.get("uom_label") or " units"
    qty_str = str(_effective_qty(rec)).strip()

    lt = rec.get("lead_time_days")
    lt_months = None
    if lt not in (None, "", "null"):
        try:
            lt_months = _fmt_num(round(float(lt) / 30, 1))
        except (TypeError, ValueError):
            lt_months = None

    sentence = f"You sell about {avg}{uom}/month"
    if lt_months is not None:
        sentence += f", and this supplier takes about {lt_months} months"
    sentence += "."
    if qty_str:
        buffer_phrase = ("the wait plus a safety buffer" if lt_months is not None
                         else "expected demand plus a safety buffer")
        sentence += f" Suggested order: {qty_str} — covers {buffer_phrase}."
    return sentence
