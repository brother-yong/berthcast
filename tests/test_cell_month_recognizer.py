"""_cell_month recognises month names, M-codes, and month-year dates; 0 otherwise."""
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ingest_recipe import _cell_month  # noqa: E402

F = []


def _check(c, m):
    if not c:
        F.append(m)


# names (existing behaviour must survive)
_check(_cell_month("Jan") == 1, "Jan -> 1")
_check(_cell_month("DECEMBER") == 12, "DECEMBER -> 12")
# M-codes
_check(_cell_month("M1") == 1, "M1 -> 1")
_check(_cell_month("m12") == 12, "m12 -> 12")
_check(_cell_month("M13") == 0, "M13 -> 0")
# month-year date strings
_check(_cell_month("Jan-26") == 1, "Jan-26 -> 1")
_check(_cell_month("Jan 2026") == 1, "Jan 2026 -> 1")
_check(_cell_month("2026-01") == 1, "2026-01 -> 1")
_check(_cell_month("01/2026") == 1, "01/2026 -> 1")
_check(_cell_month("2026-13") == 0, "2026-13 -> 0")
# real datetime cell (openpyxl data_only gives these back)
_check(_cell_month(datetime(2026, 3, 15)) == 3, "datetime March -> 3")
# non-months
_check(_cell_month("Q1") == 0, "Q1 -> 0 (quarters not months)")
_check(_cell_month("Item") == 0, "text -> 0")
_check(_cell_month("7") == 0, "bare int not a month here")
_check(_cell_month("") == 0, "blank -> 0")
_check(_cell_month(None) == 0, "None -> 0")

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All _cell_month tests passed.")
