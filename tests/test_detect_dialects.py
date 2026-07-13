"""detect_wide_matrix triggers on the new header dialects, not on transaction files."""
import os
import sys
import csv
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ingest_recipe import detect_wide_matrix  # noqa: E402

F = []


def _check(c, m):
    if not c:
        F.append(m)


def _csv(rows):
    fd, p = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return p


# M-code grid
p = _csv([["Item", "M1", "M2", "M3", "M4", "M5", "M6"], ["A", 1, 2, 3, 4, 5, 6]])
_check(detect_wide_matrix(p) is True, "M-code grid should trigger")
os.remove(p)

# date grid
p = _csv([["Item", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26", "Jun-26"], ["A", 1, 2, 3, 4, 5, 6]])
_check(detect_wide_matrix(p) is True, "date grid should trigger")
os.remove(p)

# ISO year-month grid
p = _csv([["Item", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"], ["A", 1, 2, 3, 4, 5, 6]])
_check(detect_wide_matrix(p) is True, "ISO year-month grid should trigger")
os.remove(p)

# transaction file must NOT trigger
p = _csv([["Date", "Item Description", "Qty Sold"], ["2026-01-15", "Widget", 5]])
_check(detect_wide_matrix(p) is False, "transaction file should not trigger")
os.remove(p)

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All detect-dialect tests passed.")
