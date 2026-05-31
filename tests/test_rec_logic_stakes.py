"""Standalone tests for the recommendation 'stakes' display helpers.

Dependency-free: run with `python tests/test_rec_logic_stakes.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys

# Make the project root importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rec_logic import _quantity_basis, _has_stakes


def _check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        sys.exit(1)
    print(f"ok: {name}")


# ── _quantity_basis ─────────────────────────────────────────────────────────

# Normal case: monthly sales + lead time + quantity all present.
normal = {
    "avg_monthly_sales": 40,
    "uom_label": " CTN",
    "lead_time_days": 105,          # 105 / 30 = 3.5 months
    "suggested_quantity": "160 CTN",
}
s = _quantity_basis(normal)
_check("normal returns a string", isinstance(s, str) and s)
_check("normal mentions monthly sales (40)", "40" in s)
_check("normal mentions the unit (CTN)", "CTN" in s)
_check("normal mentions lead time in months (3.5)", "3.5" in s)
_check("normal mentions the suggested quantity (160)", "160" in s)

# Float monthly sales should render cleanly (no trailing .0).
clean = dict(normal, avg_monthly_sales=40.0)
_check("whole-number float renders as 40 not 40.0", "40.0" not in _quantity_basis(clean))

# No usable sales data -> no line at all.
no_sales = {
    "avg_monthly_sales": 0,
    "uom_label": " CTN",
    "lead_time_days": 105,
    "suggested_quantity": "160 CTN",
}
_check("zero monthly sales returns None", _quantity_basis(no_sales) is None)
_check("missing monthly sales returns None",
       _quantity_basis({"suggested_quantity": "160 CTN"}) is None)

# Missing lead time -> still returns a sentence, omits lead-time clause, no 'None'.
no_lt = {
    "avg_monthly_sales": 40,
    "uom_label": " CTN",
    "lead_time_days": None,
    "suggested_quantity": "160 CTN",
}
s2 = _quantity_basis(no_lt)
_check("missing lead time still returns a string", isinstance(s2, str) and s2)
_check("missing lead time never prints the word None", "None" not in s2)
_check("missing lead time still mentions the quantity (160)", "160" in s2)

# Non-dict input must not crash.
_check("non-dict returns None", _quantity_basis("nope") is None)


# ── _has_stakes ─────────────────────────────────────────────────────────────

_check("both consequences present -> True", _has_stakes({
    "consequence_if_not_acting": "Runs out in 4 days.",
    "consequence_if_acting": "Ties up cash.",
}))
_check("only the negative consequence -> True", _has_stakes({
    "consequence_if_not_acting": "Runs out in 4 days.",
    "consequence_if_acting": "",
}))
_check("only the positive consequence -> True", _has_stakes({
    "consequence_if_acting": "Ties up cash.",
}))
_check("both empty strings -> False", not _has_stakes({
    "consequence_if_not_acting": "  ",
    "consequence_if_acting": "",
}))
_check("both missing -> False", not _has_stakes({"item": "x"}))
_check("non-dict -> False", not _has_stakes(None))


print("\nAll stakes-helper tests passed.")
