"""Post-run verifier for the inventory health report. Pure Python, no LLM.

The health statuses are deterministic given the numbers we hand Claude —
months of supply, lead time, stock, total sold — because the prompt's status
rules are plain arithmetic. Claude applies them across hundreds of lines and
can slip; nothing used to check what came back, so one bad label would ship
straight to the results page AND into the recommendation agent's input. Same
guardrail idea as quantity.sanitize_suggested_quantity, applied to the report.

Two hard principles:
  1. Judge from EXACTLY what Claude saw. The inputs captured at prompt-build
     time are the same rounded values printed into the prompt, so the verifier
     can never "correct" Claude for faithfully reading the data we gave it.
  2. Only provably-wrong fields are corrected. Judgment calls stay Claude's:
     an item WITH sales data and zero sold may legitimately be DEAD or
     (new/seasonal) not-DEAD — we never second-guess that.
"""

from .shared import normalise_match_key


def expected_status(months_supply, lt_months, stock, total_sold):
    """The status the prompt's own rules dictate, or None where the rules
    leave room for judgment.

    total_sold is None when the sales file did not cover the item at all
    (missing data, not a real zero) — the prompt mandates DEAD at zero stock
    and HEALTHY otherwise, never a demand judgment. months_supply / lt_months
    are the 1-decimal figures printed into the prompt (None when absent)."""
    if total_sold is None:
        if stock == 0:
            return "DEAD"
        if stock > 0:
            return "HEALTHY"
        return None  # negative stock with no sales data: rules are silent
    if total_sold > 0 and stock == 0:
        return "CRITICAL"
    if months_supply is None:
        return None  # no velocity: DEAD-vs-new/seasonal is Claude's judgment
    lo = lt_months if lt_months else 1.0  # fixed 1/3-month thresholds when no lead time
    if months_supply < lo:
        return "CRITICAL"
    if months_supply <= lo + 2:
        return "LOW"
    return "HEALTHY"


def verify_inventory_report(report, inputs_by_key):
    """Correct provably-wrong fields in the health report, in place.

    inputs_by_key: normalise_match_key(item name) -> {"months_supply",
    "lt_months", "stock", "total_sold"} as captured at prompt-build time.
    Items the map doesn't cover are left untouched (never guess).

    Returns (status_fixes, dos_fixes, spoilage_fixes)."""
    n_status = n_dos = n_spoil = 0
    for r in (report if isinstance(report, list) else []):
        if not isinstance(r, dict):
            continue
        inp = inputs_by_key.get(normalise_match_key(r.get("item", "")))
        if not inp:
            continue

        cur = str(r.get("status") or "").strip().upper()
        exp = expected_status(inp.get("months_supply"), inp.get("lt_months"),
                              inp.get("stock"), inp.get("total_sold"))
        sold = inp.get("total_sold")
        if cur == "DEAD" and sold is not None and sold == 0:
            pass  # legitimate judgment: has sales data, zero sold
        elif exp is not None and cur != exp:
            r["status"] = exp
            r["_status_corrected"] = True
            n_status += 1

        # days_of_supply must be months-of-supply x 30 when supply is known.
        # Tolerance allows Claude's own rounding; beyond it, the arithmetic
        # figure wins (it feeds the order-by date on the results page).
        ms = inp.get("months_supply")
        if ms is not None and str(r.get("status") or "").upper() != "DEAD":
            exp_dos = ms * 30
            try:
                cur_dos = float(r.get("days_of_supply"))
            except (TypeError, ValueError):
                cur_dos = None
            if cur_dos is None or abs(cur_dos - exp_dos) > max(3.0, 0.1 * abs(exp_dos)):
                r["days_of_supply"] = round(exp_dos)
                n_dos += 1

        # Prompt mandate: once DEAD, spoilage risk is NONE.
        if str(r.get("status") or "").upper() == "DEAD" and \
                str(r.get("spoilage_risk") or "").strip().upper() != "NONE":
            r["spoilage_risk"] = "NONE"
            n_spoil += 1

    return n_status, n_dos, n_spoil
