# Smart Ingestion v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Widen the wide-matrix detector to more month-header dialects and replace the binary convert/refuse outcome with a confident/degraded/refuse tier that asks the user one tap before a paid run — without ever letting the AI type a number.

**Architecture:** Same "AI maps, Python counts" pipeline. The detector's month recognizer grows to accept dates/M-codes (and, optionally, guarded bare integers); the mapper prompt gains dialect examples; `execute_recipe` gains deterministic degrade signals (type-sanity on month columns + rescue of a copied-forward boundary month) and stamps a `tier`/`question` into its read-back; `app.py` maps a degraded tier to a new `needs_confirm` slot status that holds the Continue button until one acknowledgement.

**Tech Stack:** Python 3, Flask, openpyxl, SQLite, Jinja2, Anthropic Haiku (mapper). Tests are plain scripts (`_check()` + `sys.exit(1)`) auto-run by `run_tests.py`.

**Ground rules:** TDD every task. Synthetic generic fixtures only — never real client names/suppliers/numbers in any tracked file (public repo). Stage files by name; never `git add .`.

---

### Task 1: Broaden the month-cell recognizer

**Files:**
- Modify: `ingest_recipe.py` (add `_cell_month`, regexes near `MONTH_NAMES`)
- Test: `tests/test_cell_month_recognizer.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cell_month_recognizer.py
"""_cell_month recognises month names, M-codes, and month-year dates; 0 otherwise."""
import os, sys
from datetime import datetime
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ingest_recipe import _cell_month  # noqa: E402

F = []
def _check(c, m):
    if not c: F.append(m)

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
    print("FAILED:"); [print("  -", m) for m in F]; sys.exit(1)
print("All _cell_month tests passed.")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python tests/test_cell_month_recognizer.py`
Expected: FAIL — `ImportError: cannot import name '_cell_month'`.

- [ ] **Step 3: Implement `_cell_month`**

Add near the top of `ingest_recipe.py` (after `MONTH_NAMES`, and add `from datetime import date, datetime` to the existing datetime import):

```python
_MCODE_RE = re.compile(r"^M(\d{1,2})$", re.IGNORECASE)
_YM_RE    = re.compile(r"^(\d{4})[-/](\d{1,2})$")   # 2026-01, 2026/1
_MY_RE    = re.compile(r"^(\d{1,2})[-/](\d{4})$")   # 01/2026, 1-2026
_NAME_DATE_RE = re.compile(r"^([A-Za-z]{3,9})[-/ ]?\d{2,4}$")  # Jan-26, Jan 2026


def _cell_month(cell) -> int:
    """Month 1-12 this HEADER cell names, else 0. Knows month names, M-codes
    (M1..M12), month-year dates (Jan-26, 2026-01, 01/2026) and real datetime
    cells. Bare integers are intentionally NOT months here — too ambiguous;
    they are handled row-level in the detector under a strict guard."""
    if isinstance(cell, (datetime, date)):
        return cell.month
    s = str(cell or "").strip()
    if not s:
        return 0
    up = s.upper()
    if up in MONTH_NAMES:
        return MONTH_NAMES[up]
    m = _MCODE_RE.match(s)
    if m:
        v = int(m.group(1)); return v if 1 <= v <= 12 else 0
    m = _NAME_DATE_RE.match(s)
    if m:
        return MONTH_NAMES.get(m.group(1).upper(), 0)
    m = _YM_RE.match(s)
    if m:
        v = int(m.group(2)); return v if 1 <= v <= 12 else 0
    m = _MY_RE.match(s)
    if m:
        v = int(m.group(1)); return v if 1 <= v <= 12 else 0
    return 0
```

- [ ] **Step 4: Run it, verify it passes**

Run: `python tests/test_cell_month_recognizer.py`
Expected: `All _cell_month tests passed.`

- [ ] **Step 5: Commit**

```powershell
git add ingest_recipe.py tests/test_cell_month_recognizer.py
git commit -m "Broaden month-cell recognizer (dates, M-codes, datetimes)"
```

---

### Task 2: Route the detector through the new recognizer

**Files:**
- Modify: `ingest_recipe.py` — `detect_wide_matrix` (currently uses `_month_of`)
- Test: `tests/test_detect_dialects.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_detect_dialects.py
"""detect_wide_matrix triggers on the new header dialects, not on transaction files."""
import os, sys, csv, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ingest_recipe import detect_wide_matrix  # noqa: E402

F = []
def _check(c, m):
    if not c: F.append(m)

def _csv(rows):
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return p

# M-code grid
p = _csv([["Item", "M1", "M2", "M3", "M4", "M5", "M6"], ["A", 1, 2, 3, 4, 5, 6]])
_check(detect_wide_matrix(p) is True, "M-code grid should trigger"); os.remove(p)

# date grid
p = _csv([["Item", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26", "Jun-26"], ["A", 1, 2, 3, 4, 5, 6]])
_check(detect_wide_matrix(p) is True, "date grid should trigger"); os.remove(p)

# ISO year-month grid
p = _csv([["Item", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"], ["A", 1, 2, 3, 4, 5, 6]])
_check(detect_wide_matrix(p) is True, "ISO year-month grid should trigger"); os.remove(p)

# transaction file must NOT trigger
p = _csv([["Date", "Item Description", "Qty Sold"], ["2026-01-15", "Widget", 5]])
_check(detect_wide_matrix(p) is False, "transaction file should not trigger"); os.remove(p)

if F:
    print("FAILED:"); [print("  -", m) for m in F]; sys.exit(1)
print("All detect-dialect tests passed.")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python tests/test_detect_dialects.py`
Expected: FAIL — M-code/date grids return False (detector still name-only).

- [ ] **Step 3: Point `detect_wide_matrix` at `_cell_month`**

In `ingest_recipe.py`, change the loop body of `detect_wide_matrix`:

```python
def detect_wide_matrix(filepath) -> bool:
    try:
        rows = _raw_rows(filepath, limit=DETECT_ROWS)
    except Exception:
        return False
    for r in rows:
        months = {_cell_month(c) for c in r}
        months.discard(0)
        if len(months) >= MIN_MONTH_COLS:
            return True
    return False
```

(`_month_of` stays for now — it is still used by nothing else critical; leave it.)

- [ ] **Step 4: Run it + the existing detector test, verify pass**

Run: `python tests/test_detect_dialects.py`
Expected: `All detect-dialect tests passed.`
Run the existing detector test too (whatever its filename): `python run_tests.py`
Expected: full suite green.

- [ ] **Step 5: Commit**

```powershell
git add ingest_recipe.py tests/test_detect_dialects.py
git commit -m "Detect month grids labelled with dates and M-codes"
```

---

### Task 3 (OPTIONAL — build only if wanted): guarded bare-integer detection

Bare `1 2 3 … 12` headers are ambiguous and never yet seen in a real file. Ship only if desired; the rest of v2 stands without it.

**Files:**
- Modify: `ingest_recipe.py` — add `_bare_integer_month_row`, call it in `detect_wide_matrix`
- Test: `tests/test_detect_bare_integers.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_detect_bare_integers.py
import os, sys, csv, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ingest_recipe import detect_wide_matrix
F = []
def _check(c, m):
    if not c: F.append(m)
def _csv(rows):
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return p

# 1..12 WITH a month label -> trigger
p = _csv([["Item", "Month", "1", "2", "3", "4", "5", "6"], ["A", "", 1, 2, 3, 4, 5, 6]])
_check(detect_wide_matrix(p) is True, "labelled 1..N run should trigger"); os.remove(p)

# 1..12 with NO month label -> must NOT trigger (too ambiguous)
p = _csv([["Rank", "1", "2", "3", "4", "5", "6"], ["A", 1, 2, 3, 4, 5, 6]])
_check(detect_wide_matrix(p) is False, "unlabelled ints must not trigger"); os.remove(p)

if F:
    print("FAILED:"); [print("  -", m) for m in F]; sys.exit(1)
print("All bare-integer detection tests passed.")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python tests/test_detect_bare_integers.py`
Expected: FAIL — labelled run returns False.

- [ ] **Step 3: Implement + wire the guard**

Add to `ingest_recipe.py`:

```python
def _bare_integer_month_row(row) -> bool:
    """Guarded bare-integer months: a consecutive run of ints starting at 1
    (>= MIN_MONTH_COLS long) AND a 'month'/'period' label somewhere in the row.
    Anything else is too ambiguous to call a month grid."""
    if not any(re.search(r"MONTH|PERIOD", str(c or ""), re.IGNORECASE) for c in row):
        return False
    present = set()
    for c in row:
        if isinstance(c, bool):
            continue
        if isinstance(c, int):
            present.add(c)
        else:
            s = str(c or "").strip()
            if s.isdigit():
                present.add(int(s))
    run, n = 0, 1
    while n in present:
        run += 1; n += 1
    return run >= MIN_MONTH_COLS
```

In `detect_wide_matrix`, inside the row loop, after the `>= MIN_MONTH_COLS` check add:

```python
        if _bare_integer_month_row(r):
            return True
```

- [ ] **Step 4: Run it, verify it passes**

Run: `python tests/test_detect_bare_integers.py`
Expected: `All bare-integer detection tests passed.`

- [ ] **Step 5: Commit**

```powershell
git add ingest_recipe.py tests/test_detect_bare_integers.py
git commit -m "Detect guarded bare-integer month grids"
```

---

### Task 4: Upgrade the mapper prompt with dialect examples

**Files:**
- Modify: `agents/ingest_mapper.py` — `_SYSTEM`
- Test: `tests/test_mapper_prompt_dialects.py` (create)

The mapper still outputs layout only; the JSON schema is unchanged. The prompt just teaches it the new header shapes and the messy-grid quirks so it maps them correctly. The test locks that the guidance is present (a full-accuracy check needs a live API call — out of scope for the suite).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mapper_prompt_dialects.py
"""The mapper system prompt teaches the new header dialects + grid quirks."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from agents.ingest_mapper import _SYSTEM
F = []
def _check(c, m):
    if not c: F.append(m)
low = _SYSTEM.lower()
for token in ("m1", "jan-26", "2026-01", "merged", "first row"):
    _check(token in low, f"prompt should mention {token!r}")
# still layout-only, still the wide_matrix contract
_check("wide_matrix" in _SYSTEM, "wide_matrix contract must remain")
_check("json" in low, "must still ask for JSON")
if F:
    print("FAILED:"); [print("  -", m) for m in F]; sys.exit(1)
print("All mapper-prompt dialect tests passed.")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python tests/test_mapper_prompt_dialects.py`
Expected: FAIL — tokens like `m1`, `jan-26`, `merged` absent.

- [ ] **Step 3: Extend `_SYSTEM`**

In `agents/ingest_mapper.py`, insert this block into the `_SYSTEM` string (before the final "Rules:" line):

```python
    "Month columns may be labelled several ways — treat all as months:\n"
    "  names: Jan, February; codes: M1..M12; dates: Jan-26, 2026-01, 01/2026.\n"
    "Common quirks to expect: a merged 'TOTAL' header can sit over the first\n"
    "month column; supplier and lead-time values are often filled only on the\n"
    "first row of each supplier block; a lead-time column can appear inside a\n"
    "sales report. Map the month NUMBER (1-12) regardless of the label style.\n"
```

- [ ] **Step 4: Run it, verify it passes**

Run: `python tests/test_mapper_prompt_dialects.py`
Expected: `All mapper-prompt dialect tests passed.`

- [ ] **Step 5: Commit**

```powershell
git add agents/ingest_mapper.py tests/test_mapper_prompt_dialects.py
git commit -m "Teach the mapper the new month-header dialects and grid quirks"
```

---

### Task 5: Tier classifier — type-sanity + borderline rescue in `execute_recipe`

**Files:**
- Modify: `ingest_recipe.py` — `execute_recipe` (add findings, tier, question); add `_boundary_is_borderline` helper + `CONFIDENT_PROJECTION`, `MIXED_TEXT_SHARE` constants
- Test: `tests/test_ingest_tiers.py` (create)

Behaviour:
- **Borderline rescue:** after `_split_projection_tail` returns `(kept, dropped)`, if `dropped` is non-empty and `boundary = min(dropped)` with `boundary+1` also mapped, compute the flat fraction (items where `boundary == boundary+1`, among items positive in both). If that fraction `< CONFIDENT_PROJECTION` (0.9), MOVE `boundary` from dropped to kept and record it as the borderline month.
- **Type-sanity:** a month column with `numeric_n>0` and `text_n >= MIXED_TEXT_SHARE*(numeric_n+text_n)` (0.2) is a mixed column.
- **tier:** `"degraded"` if a borderline month OR any mixed column exists, else `"confident"`. Build a one-line `question` string. Readback gains `tier` and `question`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_tiers.py
"""execute_recipe stamps a confident/degraded tier + a question on the read-back."""
import os, sys, csv, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ingest_recipe import execute_recipe, validate_recipe
from datetime import date
F = []
def _check(c, m):
    if not c: F.append(m)

TODAY = date(2026, 7, 1)

def _grid(rows):
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
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
rows = [hdr] + [[f"I{i}", 10+i, 20+i, 30+i, 40+i, 50+i, 60+i] for i in range(8)]
p = _grid(rows)
rec = _recipe(7, {2:1, 3:2, 4:3, 5:4, 6:5, 7:6})
_, rb = execute_recipe(p, rec, today=TODAY); os.remove(p)
_check(rb["tier"] == "confident", f"clean grid should be confident, got {rb['tier']}")
_check(rb.get("question") in (None, ""), "confident has no question")

# --- Degraded: last real month copied forward into a flat projection tail ---
# Jan..May real & varied; Jun real (own number) but == Jul..Dec for MOST items
# (copied forward), so the flat-tail rule drops Jun; a minority differ -> rescue.
hdr = ["Item"] + ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
rows = [hdr]
# 8 items where Jun==Jul..Dec (looks flat) ...
for i in range(8):
    v = 100 + i
    rows.append([f"F{i}", 1,2,3,4,5, v, v,v,v,v,v,v])
# 4 items where Jun differs from the flat Jul..Dec tail (Jun is its own real number)
for i in range(4):
    rows.append([f"D{i}", 1,2,3,4,5, 999, 7,7,7,7,7,7])
p = _grid(rows)
rec = _recipe(13, {c: c-1 for c in range(2, 14)})  # cols 2..13 -> months 1..12
_, rb = execute_recipe(p, rec, today=TODAY); os.remove(p)
_check(rb["tier"] == "degraded", f"copied-forward boundary should degrade, got {rb['tier']}")
_check(6 in rb["months_kept"], "borderline June must be KEPT by default")
_check(7 not in rb["months_kept"], "clear projection July stays dropped")
_check(rb.get("question"), "degraded must carry a question")

# --- Degraded: a month column that is mostly numeric but part text ---
hdr = ["Item", "Jan", "Feb", "Mar", "Apr", "May", "Jun"]
rows = [hdr]
for i in range(8):
    rows.append([f"I{i}", 10, 20, 30, 40, 50, 60])
# make Jun ~30% text
rows[1][6] = "n/a"; rows[2][6] = "tbd"
p = _grid(rows)
rec = _recipe(7, {2:1, 3:2, 4:3, 5:4, 6:5, 7:6})
_, rb = execute_recipe(p, rec, today=TODAY); os.remove(p)
_check(rb["tier"] == "degraded", f"mixed text column should degrade, got {rb['tier']}")
_check(rb.get("question"), "mixed-column degrade must carry a question")

if F:
    print("FAILED:"); [print("  -", m) for m in F]; sys.exit(1)
print("All ingest-tier tests passed.")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python tests/test_ingest_tiers.py`
Expected: FAIL — `KeyError: 'tier'` (readback has no tier yet).

- [ ] **Step 3: Implement the classifier**

In `ingest_recipe.py` add the constants near the other thresholds:

```python
CONFIDENT_PROJECTION = 0.9   # boundary month stays dropped only if this-flat vs next
MIXED_TEXT_SHARE     = 0.2   # a month column this-text-or-more is "mixed" -> degrade
```

Add the helper (place after `_split_projection_tail`):

```python
def _boundary_is_borderline(data_rows, boundary_m, next_m, cols_by_month, item_col_1based):
    """The earliest dropped month is borderline when a real minority of items
    carry their OWN value there (boundary != next), i.e. it was likely a real
    month copied forward to seed the projections. Returns True to KEEP + ask."""
    if boundary_m not in cols_by_month or next_m not in cols_by_month:
        return False
    bc = cols_by_month[boundary_m] - 1
    nc = cols_by_month[next_m] - 1
    item_i = item_col_1based - 1
    flat = total = 0
    for r in data_rows:
        name = str((r[item_i] if item_i < len(r) else "") or "").strip()
        if not name or _is_total_row(name):
            continue
        b = _num(r[bc]) if bc < len(r) else None
        n = _num(r[nc]) if nc < len(r) else None
        if b is None or n is None or b <= 0:
            continue
        total += 1
        if b == n:
            flat += 1
    return total > 0 and (flat / total) < CONFIDENT_PROJECTION
```

In `execute_recipe`, after the line
`kept_months, flat_dropped = _split_projection_tail(rows[hdr:], live_cols, recipe["item_col"])`
and before `dropped = sorted(...)`, insert:

```python
    cols_by_month = {m: c for c, m in month_cols}
    borderline_m = None
    flat_dropped = list(flat_dropped)
    if flat_dropped:
        boundary = min(flat_dropped)
        if _boundary_is_borderline(rows[hdr:], boundary, boundary + 1,
                                   cols_by_month, recipe["item_col"]):
            flat_dropped.remove(boundary)
            kept_months = sorted(set(kept_months) | {boundary})
            borderline_m = boundary
```

Compute mixed columns — right after the existing `empty_months` line
(`empty_months = {m for _c, m in month_cols if numeric_n[m] == 0}`) add:

```python
    mixed_months = {m for _c, m in month_cols
                    if numeric_n[m] > 0 and text_n[m] > 0
                    and text_n[m] >= MIXED_TEXT_SHARE * (numeric_n[m] + text_n[m])}
```

Finally, build tier + question and add them to the `readback` dict (replace the
`readback = {...}` literal's tail):

```python
    tier = "degraded" if (borderline_m is not None or mixed_months) else "confident"
    parts = []
    if borderline_m is not None:
        nm = _MONTH_LABEL[borderline_m]
        parts.append(f"We kept {nm} as real sales, but the months after it look "
                     f"like typed projections. If {nm} is also a projection, "
                     f"replace the file without it.")
    if mixed_months:
        names = ", ".join(_MONTH_LABEL[m] for m in sorted(mixed_months))
        parts.append(f"The {names} column had some non-number cells — "
                     f"double-check it read correctly.")
    readback = {
        "items": items,
        "months_kept": sorted(kept_months),
        "months_dropped": sorted(dropped),
        "assumed_year": year,
        "total_units": round(sum(r[2] for r in out), 1),
        "tier": tier,
        "question": " ".join(parts) if parts else None,
    }
    return csv_path, readback
```

Add a month-label list near `MONTH_NAMES` (used by the question text):

```python
_MONTH_LABEL = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]
```

Note: `kept_months` is now recomputed before the row loop that stamps `year`
and emits rows — verify the borderline block sits ABOVE
`year = _assumed_year(max(kept_months), today) ...` and the emit loop, so the
rescued month is emitted and counted. The independent verification pass already
sums over `kept_months`, so it stays self-consistent.

- [ ] **Step 4: Run it, verify it passes**

Run: `python tests/test_ingest_tiers.py`
Expected: `All ingest-tier tests passed.`
Then `python run_tests.py` — the whole suite green (existing execute_recipe tests must still pass; they don't assert on `tier`).

- [ ] **Step 5: Commit**

```powershell
git add ingest_recipe.py tests/test_ingest_tiers.py
git commit -m "Classify conversions confident/degraded with type-sanity + borderline rescue"
```

---

### Task 6: Wire the degraded tier into app.py (needs_confirm + confirm endpoint)

**Files:**
- Modify: `app.py` — `_start_processing` sales branch (~line 2053); add a `confirm_readback` route near `upload_status` (~line 2080)
- Test: `tests/test_confirm_readback_route.py` (create)

`maybe_convert_sales` still returns `("converted", readback)`; the tier lives in
`readback["tier"]`. A degraded conversion is stored as status `needs_confirm`
(NOT `done`), so the Continue button stays locked. A tiny authenticated POST
flips the slot to `done`, preserving the existing rows + readback.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_confirm_readback_route.py
"""POST /upload/confirm-readback flips a needs_confirm sales slot to done."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import database as db
from app import app
F = []
def _check(c, m):
    if not c: F.append(m)

# The route must exist and be login-gated (302/401 when logged out), never 404.
client = app.test_client()
r = client.post("/upload/confirm-readback", json={"slot": "sales", "session_id": 1})
_check(r.status_code != 404, f"route must exist, got {r.status_code}")

# set_conversion_status merge behaviour the route relies on: writing 'done'
# keeps the prior readback when re-passed.
if F:
    print("FAILED:"); [print("  -", m) for m in F]; sys.exit(1)
print("All confirm-readback route tests passed.")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python tests/test_confirm_readback_route.py`
Expected: FAIL — 404 (route not defined yet).

- [ ] **Step 3a: Branch the sales conversion on tier**

In `app.py` `_start_processing`, replace the `if state == "converted":` block:

```python
                    if state == "converted":
                        readback = payload
                        rows_n = db.query(
                            f'SELECT COUNT(*) AS n FROM "sales_{int(session_id)}"')[0]["n"]
                        if _stale():
                            return
                        # Degraded conversions hold the slot out of "done" until
                        # the user taps once — the paid run stays blocked.
                        new_status = "needs_confirm" if readback.get("tier") == "degraded" else "done"
                        db.set_conversion_status(session_id, slot, new_status,
                                                 rows_count=rows_n, readback=readback)
                        _record_file_name(session_id, slot, orig_name)
                        return
```

(If a `_record_file_name` helper does not exist, inline the two lines that
currently update `file_names_json` for the slot instead — keep it identical to
the existing code path.)

Leave the general (non-sales) `done` write below untouched for other slots.

- [ ] **Step 3b: Add the confirm route**

Near `upload_status` add:

```python
@app.route("/upload/confirm-readback", methods=["POST"])
@login_required
def confirm_readback():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    slot = data.get("slot")
    if not isinstance(session_id, int) or slot not in ("sales",):
        return jsonify({"ok": False, "error": "bad request"}), 400
    _verify_session_owner(session_id)
    cur = db.get_conversion_status(session_id).get(slot, {})
    if cur.get("status") != "needs_confirm":
        return jsonify({"ok": False, "error": "nothing to confirm"}), 409
    db.set_conversion_status(session_id, slot, "done",
                             rows_count=cur.get("rows", 0),
                             readback=cur.get("readback"))
    return jsonify({"ok": True})
```

- [ ] **Step 4: Run it, verify it passes**

Run: `python tests/test_confirm_readback_route.py`
Expected: `All confirm-readback route tests passed.`
Then `python run_tests.py` — full suite green.

- [ ] **Step 5: Commit**

```powershell
git add app.py tests/test_confirm_readback_route.py
git commit -m "Gate degraded sales conversions behind a one-tap confirm"
```

---

### Task 7: Upload-page UI for the degraded question

**Files:**
- Modify: `templates/upload.html` — add a `needs_confirm` branch (template + `_pollConversionStatus` + `uploadedSlots` init)
- Test: `tests/test_upload_needs_confirm_render.py` (create)

The degraded read-back reuses the calm `<details>` styling but adds the
`question` line and a single "Yes — use this file" button that POSTs to
`/upload/confirm-readback` then reloads. `needs_confirm` must NOT count as
`done` anywhere the Continue button is computed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upload_needs_confirm_render.py
"""A needs_confirm sales slot renders the question + a confirm button, and does
NOT enable Continue."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app import app
F = []
def _check(c, m):
    if not c: F.append(m)

conv = {"sales": {"status": "needs_confirm", "rows": 500,
                  "readback": {"items": 149, "months_kept": [1,2,3,4,5,6],
                               "months_dropped": [7,8,9,10,11,12],
                               "assumed_year": 2026, "total_units": 4015509.0,
                               "tier": "degraded",
                               "question": "We kept June as real sales..."}},
        "inventory": {"status": "done", "rows": 10}}
with app.test_request_context():
    from flask import render_template
    html = render_template("upload.html", session_id=1, conversion_status=conv,
                           tables={"sales": True, "inventory": True},
                           file_names={"sales": "s.xlsx", "inventory": "i.xlsx"})

_check("We kept June as real sales" in html, "question text must render")
_check("confirm-readback" in html or "confirmReadback" in html, "confirm action must be wired")
# Continue must be disabled: sales is needs_confirm, not done.
_check("required_done" not in html, "template var should not leak")
_check('id="continue-btn"' in html, "continue button present")
# crude: the disabled attribute should be on the continue button block
_check("disabled" in html, "continue button should be disabled while needs_confirm")

if F:
    print("FAILED:"); [print("  -", m) for m in F]; sys.exit(1)
print("All needs_confirm render tests passed.")
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python tests/test_upload_needs_confirm_render.py`
Expected: FAIL — question text / confirm action absent.

- [ ] **Step 3: Render the needs_confirm branch**

In `templates/upload.html`:

1. Add to the per-slot status vars (~line 40):
```jinja
    {% set needs_confirm = (conv_status == 'needs_confirm') %}
```
2. In the slot state class (~line 42), treat needs_confirm like a non-error
   attention state (reuse the `uploading`/neutral look — do NOT use `done`):
   leave the class list as-is (it already only sets done/uploading/error), so
   needs_confirm renders neutral. Good.
3. After the `{% if readback %}` calm block, add a degraded branch. Simplest:
   render the degraded panel when `needs_confirm and readback`:
```jinja
        {% if needs_confirm and readback %}
        <div class="slot-readback" style="margin-top:8px;padding:10px 12px;border:1px solid var(--brass);background:var(--paper-2);border-radius:8px;font-size:13px;line-height:1.55;">
          <div style="margin-bottom:6px;">Read as {{ '{:,}'.format(readback['items']) }} items, {{ readback['months_kept']|month_span }} {{ readback['assumed_year'] }} · {{ '{:,.0f}'.format(readback['total_units']) }} units.</div>
          <div style="margin-bottom:8px;color:var(--text);">{{ readback['question'] }}</div>
          <button class="btn btn-primary btn-sm" onclick="confirmReadback('{{ slot }}','{{ slotId }}')">Yes — use this file</button>
        </div>
        {% endif %}
```
   Guard the calm `{% if readback %}` block so it does not ALSO show for
   needs_confirm: change its opener to `{% if readback and not needs_confirm %}`.
4. Continue button: the `required_done` computation keys off status == 'done',
   so needs_confirm already leaves Continue disabled. No change needed there.
5. `uploadedSlots` init + `_resumeSlots`: needs_confirm must keep the slot
   pollable and NOT mark uploaded. The init already only sets `true` for
   `== 'done'`, so needs_confirm → false. Add `needs_confirm` to the resume
   condition so a page refresh keeps polling:
```jinja
    {% if cs == 'converting' or cs == 'needs_confirm' or (tables.get(slot) and not cs) %}
```
6. Add the confirm JS (near `removeFile`):
```javascript
async function confirmReadback(slot, slotId) {
  try {
    const res = await fetch('/upload/confirm-readback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ slot, session_id: sessionId })
    });
    const data = await res.json();
    if (data.ok) { window.location.reload(); }
    else { showToast('Could not confirm. Please try again.'); }
  } catch(e) { showToast('Network error. Please try again.'); }
}
```
7. Poll handler: in `_pollConversionStatus`, add a branch so a conversion that
   resolves to needs_confirm reloads to show the question:
```javascript
      } else if (info.status === 'needs_confirm') {
        clearInterval(interval);
        _pollingSlots.delete(slot);
        window.location.reload();
```

- [ ] **Step 4: Run it, verify it passes**

Run: `python tests/test_upload_needs_confirm_render.py`
Expected: `All needs_confirm render tests passed.`
Then `python run_tests.py` — full suite green.

- [ ] **Step 5: Commit**

```powershell
git add templates/upload.html tests/test_upload_needs_confirm_render.py
git commit -m "Show the degraded read-back question with a one-tap confirm"
```

---

### Task 8: End-to-end proof on the real pilot file + security review

**Files:** none (verification only)

- [ ] **Step 1: Re-run the real file through the pipeline**

Use the scratchpad prove-script pattern (detect → validate_recipe → execute_recipe)
on `exports for yh/cool-link-data/sales report.xlsx`. Confirm: `tier == "degraded"`,
June (month 6) is now in `months_kept`, and the per-month totals for Jan–Jun match
the hand CSV `sales_converted_2026_m1-6.csv` to the decimal. This is the proof the
borderline rescue fixed the live undersize. (Scratchpad only — never commit client data.)

- [ ] **Step 2: Full suite**

Run: `python run_tests.py`
Expected: every test green, including the pre-existing ingestion tests.

- [ ] **Step 3: security-reviewer pass (MANDATORY — upload path + client data)**

Dispatch the `security-reviewer` agent over the diff (app.py route, ingest_recipe.py,
templates/upload.html). Address anything it flags before deploy.

- [ ] **Step 4: Final commit / push prompt**

Provide the paste-ready PowerShell to push the branch once the user approves.

---

## Self-review notes

- **Spec coverage:** dialects (Tasks 1–3), mapper prompt (4), three-tier + type-sanity + borderline (5), needs_confirm wiring + confirm (6), UI (7), verification + security (8). The "grid files never fall back to naive read" rule is already enforced by v1's `maybe_convert_sales` (`_refuse` clears the table; converted paths replace it) — v2 adds only the degraded branch, which also never naive-falls-back. No gap.
- **Deferred (Phase 2, per spec):** in-place keep/remove toggle for the borderline month; quarters. Task 3 (bare integers) is optional.
- **Type consistency:** readback keys `tier` (str) and `question` (str|None) are produced in Task 5 and consumed in Tasks 6–7; status string `needs_confirm` is written in Task 6 and read in Tasks 6–7. `_cell_month` (Task 1) is the single recognizer used by Tasks 2–3.
