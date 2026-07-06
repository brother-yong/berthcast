"""Rows inside each supplier group must be urgency-sorted:
overdue first (most overdue first), then urgent, then ok by ascending buffer;
recs with no order-by date sort CRITICAL before LOW before the rest, last.

Run: python tests/test_urgency_sort.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rec_logic import _group_recs_by_supplier  # noqa: E402

# All from one supplier so they land in one group. days_of_supply - lead_time_days
# = buffer: negative = overdue, 0-7 = urgent, >7 = ok, missing = unknown.
recs = [
    {"item": "OK item",        "supplier": "S", "days_of_supply": 60, "lead_time_days": 10},   # buffer 50, ok
    {"item": "No-date LOW",    "supplier": "S"},                                               # unknown
    {"item": "Overdue small",  "supplier": "S", "days_of_supply": 10, "lead_time_days": 20},   # buffer -10
    {"item": "No-date CRIT",   "supplier": "S"},                                               # unknown
    {"item": "Urgent item",    "supplier": "S", "days_of_supply": 25, "lead_time_days": 20},   # buffer 5, urgent
    {"item": "Overdue big",    "supplier": "S", "days_of_supply": 10, "lead_time_days": 66},   # buffer -56
]
status_by_item = {
    "No-date CRIT": "CRITICAL",
    "No-date LOW":  "LOW",
    "Overdue big":  "CRITICAL",
    "Overdue small": "CRITICAL",
    "Urgent item":  "LOW",
    "OK item":      "HEALTHY",
}

groups = _group_recs_by_supplier(recs, status_by_item)
assert len(groups) == 1, f"expected 1 group, got {len(groups)}"
order = [r["item"] for r in groups[0]["recs"]]
expected = ["Overdue big", "Overdue small", "Urgent item", "OK item",
            "No-date CRIT", "No-date LOW"]
assert order == expected, f"wrong order:\n  got      {order}\n  expected {expected}"

print("All urgency-sort tests passed.")
