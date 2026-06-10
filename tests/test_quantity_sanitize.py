"""Proof for the order-quantity guardrail.

The suggested order quantity is the number staff actually buy from. It comes back
from the Claude agent, which can override the Python-computed figure, and nothing
used to check it. quantity.sanitize_suggested_quantity now bounds it: a missing,
non-numeric, negative, or wildly-too-large value falls back to the Python figure,
and a number with no sales baseline is downgraded to "Verify with team".

Pure functions, dependency-free:
    python tests/test_quantity_sanitize.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantity import parse_quantity, sanitize_suggested_quantity

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── parse_quantity ──────────────────────────────────────────────────────────
_check("parses a number with a unit label", parse_quantity("120 CTN") == 120.0)
_check("parses thousands separators", parse_quantity("1,200") == 1200.0)
_check("parses a plain int", parse_quantity(120) == 120.0)
_check("parses a decimal with unit", parse_quantity("12.5 KG") == 12.5)
_check("non-numeric text is None", parse_quantity("Verify with team") is None)
_check("empty string is None", parse_quantity("") is None)
_check("None is None", parse_quantity(None) is None)
_check("bool is rejected (not treated as 1/0)", parse_quantity(True) is None)

# ── sanitize_suggested_quantity — baseline present ──────────────────────────
_check("good in-range model number is kept (reformatted with unit)",
       sanitize_suggested_quantity("150 CTN", 160, " CTN") == ("150 CTN", False))
_check("bare model number gets the unit added",
       sanitize_suggested_quantity(150, 160, " CTN") == ("150 CTN", False))
_check("non-numeric model value falls back to the Python figure",
       sanitize_suggested_quantity("Verify with team", 160, " CTN") == ("160 CTN", True))
_check("absurdly large model number (>10x) falls back",
       sanitize_suggested_quantity("99999", 160, " CTN") == ("160 CTN", True))
_check("negative model number falls back",
       sanitize_suggested_quantity("-5", 160, " CTN") == ("160 CTN", True))
_check("zero model number falls back",
       sanitize_suggested_quantity("0", 160, " CTN") == ("160 CTN", True))

# ── sanitize_suggested_quantity — no sales baseline ─────────────────────────
_check("no baseline + model number -> Verify with team (don't trust it)",
       sanitize_suggested_quantity("500", None, " CTN") == ("Verify with team", True))
_check("no baseline + model text -> keep the text as-is",
       sanitize_suggested_quantity("Verify with team", None, " CTN") == ("Verify with team", False))
_check("no baseline + empty model -> Verify with team",
       sanitize_suggested_quantity("", None, " CTN") == ("Verify with team", False))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll quantity-sanitiser tests passed.")
