"""Order-quantity parsing and safety checks. Pure functions — no Flask, no DB —
so they're easy to unit-test in isolation.

The order quantity is the number staff actually buy from. It is produced by the
Claude recommendation agent, which is *told* to reuse the Python-computed figure
but can override it. Nothing used to check what came back, so a model slip or a
fat-fingered human edit could put a wrong number straight onto the printed PO
sheet. These helpers are the guardrail.
"""
import re

# Leading number with optional thousands commas and decimals, e.g. "1,200.5".
# We deliberately ignore any trailing unit label ("120 CTN" -> 120).
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def parse_quantity(value):
    """Pull the numeric value out of a quantity (string or number).

    Handles "120", "120 CTN", "1,200", 120, 120.0. Returns a float, or None when
    there's no number to read ("Verify with team", "", None, NaN/inf).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
            return None
        return f
    m = _NUM_RE.search(str(value))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def _fmt(n, unit_label=""):
    """Render a quantity back as a clean display string, e.g. 120.0 -> '120 CTN'."""
    n = int(round(n))
    label = unit_label or ""
    return f"{n}{label}"


def sanitize_suggested_quantity(model_value, precomputed_qty, unit_label="", max_multiple=10):
    """Bound the agent's suggested order quantity to something safe to show staff.

    Returns (display_string, corrected) where `corrected` is True when we had to
    override what the model returned.

    Rules:
      - No sales baseline (precomputed_qty is None or <= 0): we have nothing to
        sanity-check against, so we never put an unverified buy number in front of
        staff. Keep a non-numeric model answer as-is ("Verify with team"); replace
        any model *number* with "Verify with team" (corrected=True).
      - With a baseline: fall back to the Python figure when the model value is
        missing, non-numeric, <= 0, or more than `max_multiple` x the baseline
        (an obvious hallucination). Otherwise keep the model's number, reformatted
        with the unit so display stays consistent.
    """
    try:
        pre = float(precomputed_qty) if precomputed_qty is not None else None
    except (TypeError, ValueError):
        pre = None

    n = parse_quantity(model_value)

    # No usable baseline — don't trust an unchecked buy number.
    if pre is None or pre <= 0:
        if n is None:
            text = str(model_value).strip() if model_value not in (None, "") else ""
            return (text or "Verify with team", False)
        return ("Verify with team", True)

    # Baseline available — sanity-check the model's number against it.
    if n is None or n <= 0 or n > pre * max_multiple:
        return (_fmt(pre, unit_label), True)

    return (_fmt(n, unit_label), False)
