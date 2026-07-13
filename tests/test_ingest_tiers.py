"""execute_recipe stamps a confident/degraded tier + a question on the read-back."""
import os
import sys
import csv
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ingest_recipe import execute_recipe, validate_recipe  # noqa: E402

F = []


def _check(c, m):
    if not c:
        F.append(m)


TODAY = date(2026, 7, 1)


def _grid(rows):
    fd, p = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return p


def _recipe(n_cols, months):
    raw = {"layout": "wide_matrix", "header_row": 1, "item_col": 1,
           "month_cols": {str(c): m for c, m in months.items()},
           "supplier_col": None, "leadtime_col": None}
    return validate_recipe(raw, n_rows=99, n_cols=n_cols)


# --- Confident: clean 6-month grid, no projections, all numeric ---
hdr = ["Item", "Jan", "Feb", "Mar", "Apr", "May", "Jun"]
rows = [hdr] + [[f"I{i}", 10 + i, 20 + i, 30 + i, 40 + i, 50 + i, 60 + i] for i in range(8)]
p = _grid(rows)
rec = _recipe(7, {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6})
_, rb = execute_recipe(p, rec, today=TODAY)
os.remove(p)
_check(rb["tier"] == "confident", f"clean grid should be confident, got {rb['tier']}")
_check(rb.get("question") in (None, ""), "confident has no question")

# --- Degraded: last real month copied forward into a flat projection tail ---
# Jan..May real & varied; Jun real (own number) but == Jul..Dec for MOST items
# (copied forward), so the flat-tail rule drops Jun; a minority differ -> rescue.
hdr = ["Item"] + ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
rows = [hdr]
# 8 items where Jun==Jul..Dec (looks flat) ...
for i in range(8):
    v = 100 + i
    rows.append([f"F{i}", 1, 2, 3, 4, 5, v, v, v, v, v, v, v])
# 4 items where Jun differs from the flat Jul..Dec tail (Jun is its own real number)
for i in range(4):
    rows.append([f"D{i}", 1, 2, 3, 4, 5, 999, 7, 7, 7, 7, 7, 7])
p = _grid(rows)
rec = _recipe(13, {c: c - 1 for c in range(2, 14)})  # cols 2..13 -> months 1..12
_, rb = execute_recipe(p, rec, today=TODAY)
os.remove(p)
_check(rb["tier"] == "degraded", f"copied-forward boundary should degrade, got {rb['tier']}")
_check(6 in rb["months_kept"], "borderline June must be KEPT by default")
_check(7 not in rb["months_kept"], "clear projection July stays dropped")
_check(rb.get("question"), "degraded must carry a question")

# --- Degraded: a month column that is mostly numeric but part text ---
hdr = ["Item", "Jan", "Feb", "Mar", "Apr", "May", "Jun"]
rows = [hdr]
for i in range(8):
    rows.append([f"I{i}", 10, 20, 30, 40, 50, 60])
# make Jun ~25% text (2 of 8)
rows[1][6] = "n/a"
rows[2][6] = "tbd"
p = _grid(rows)
rec = _recipe(7, {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6})
_, rb = execute_recipe(p, rec, today=TODAY)
os.remove(p)
_check(rb["tier"] == "degraded", f"mixed text column should degrade, got {rb['tier']}")
_check(rb.get("question"), "mixed-column degrade must carry a question")

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All ingest-tier tests passed.")
