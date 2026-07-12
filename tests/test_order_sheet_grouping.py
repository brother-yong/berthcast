"""Order sheets (print + CSV) group approved recs by supplier.

Two invariants:
  1. Flattening the groups (what export_csv does) keeps every approved row once,
     with each supplier's rows contiguous and most-critical supplier first.
  2. The print template renders those groups: one header per supplier and a
     single continuous item number across all groups.

Run: python tests/test_order_sheet_grouping.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rec_logic import _group_recs_by_supplier  # noqa: E402

# Two suppliers interleaved on input; ALPHA has a CRITICAL item, BETA doesn't,
# plus one rec with no supplier (lands in the "Unknown supplier" group, last).
recs = [
    {"item": "beta-1",  "supplier": "BETA",  "days_of_supply": 60, "lead_time_days": 10},
    {"item": "alpha-1", "supplier": "ALPHA", "days_of_supply": 10, "lead_time_days": 40},
    {"item": "beta-2",  "supplier": "BETA",  "days_of_supply": 90, "lead_time_days": 10},
    {"item": "nosup-1", "supplier": ""},
    {"item": "alpha-2", "supplier": "ALPHA", "days_of_supply": 80, "lead_time_days": 10},
]
status_by_item = {"alpha-1": "CRITICAL"}  # ALPHA has 1 critical, BETA/Unknown have 0

groups = _group_recs_by_supplier(recs, status_by_item)

# --- Invariant 1: flatten == what export_csv writes -------------------------
flat = [r for g in groups for r in g["recs"]]

_check_count = len(flat) == len(recs)
assert _check_count, f"row count changed on flatten: {len(flat)} != {len(recs)}"

_items = [r["item"] for r in flat]
assert sorted(_items) == sorted(r["item"] for r in recs), "rows dropped or duplicated"

# Suppliers must be contiguous: once a supplier ends it never reappears.
seen, last = set(), object()
for r in flat:
    s = r.get("supplier") or "Unknown supplier"
    if s != last:
        assert s not in seen, f"supplier {s!r} not contiguous in flattened order"
        seen.add(s)
        last = s

# ALPHA (has the critical item) must group before BETA and before Unknown.
names = [g["name"] for g in groups]
assert names[0] == "ALPHA", f"most-critical supplier not first: {names}"
assert names[-1] == "Unknown supplier", f"blank-supplier group not last: {names}"

# --- Invariant 2: print template renders with continuous numbering ----------
from jinja2 import Environment, FileSystemLoader  # noqa: E402

env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")))
html = env.get_template("print_order.html").render(
    groups=groups, total=len(flat), org_name="Test Co"
)

# One header cell per supplier group.
assert html.count('class="group-head"') == len(groups), "missing supplier headers"
# Continuous numbering 1..N appears once each, in order (cell is "<td>{n}</td>").
for n in range(1, len(flat) + 1):
    assert f"<td>{n}</td>" in html, f"missing row number {n}"
assert f"<td>{len(flat) + 1}</td>" not in html, "row numbers overran item count"
# Header shows the critical count for ALPHA.
assert "1 critical" in html, "critical count not shown in ALPHA header"

print("All order-sheet grouping tests passed.")
