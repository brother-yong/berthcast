# Sales Volatility Detection + Warnings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deterministically classify each item's monthly sales pattern (stable / spiky / volatile / lumpy), size spiky items on their typical month instead of the inflated mean, and warn staff — so one bulk-order month can never again produce a confidently-wrong 9× recommendation.

**Architecture:** One pure classifier + one SQL wrapper in `agents/shared.py` compute per-item monthly stats from the dated sales table. Both agents substitute the median for spiky items on the totals÷months path only. A deterministic post-pass (never trusting the model to self-report) stamps `sales_pattern`, appends a plain-English flag, and caps confidence at MEDIUM. Clarity box counts flagged items.

**Tech Stack:** Python stdlib only (`statistics`), SQLite, project-style plain-script tests.

**Spec:** `docs/superpowers/specs/2026-07-10-sales-volatility-warnings-design.md`

**Repo conventions that override generic practice:**
- Owner commits via a paste-ready PowerShell guide at the END (stage by NAME, never `git add .`). Tasks have NO git steps.
- Public repo: invented names only, everywhere including tests.
- Straight to `main`, no branches.

---

### Task 1: Pure classifier `classify_monthly_pattern` (TDD)

**Files:**
- Test: `tests/test_sales_volatility.py` (create)
- Modify: `agents/shared.py` (append constants + function near the date helpers)

- [x] **Step 1: Write the failing test**

Create `tests/test_sales_volatility.py`:

```python
"""Sales volatility: classifier, SQL wrapper, and rec post-pass.

A flat average lies when one month dominates (spike -> 9x over-order), when
months swing (offshore/festive cycles), or when an item sells in rare bursts.
These tests pin the deterministic classification rules and the post-pass that
flags recs + caps confidence. No network; throwaway DB for the SQL wrapper.
All names invented.
"""
import os
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="berth_volatility_test_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.shared import classify_monthly_pattern

_FAILED = False


def _check(name, cond):
    global _FAILED
    if cond:
        print(f"ok: {name}")
    else:
        print(f"FAIL: {name}")
        _FAILED = True


# ---------------------------------------------------------------------------
# classify_monthly_pattern(monthly) -> (pattern, corrected_avg_or_None)
# ---------------------------------------------------------------------------
# The cheese example: 11 x 100 + one 10,000 month -> spiky, sized on 100.
pat, corr = classify_monthly_pattern([100.0] * 11 + [10000.0])
_check("cheese spike -> spiky", pat == "spiky")
_check("cheese spike -> corrected to median 100", corr == 100.0)

# Boundary: mean exactly 2x median is spiky ([100]*11 + [1300] -> mean 200, med 100).
pat, corr = classify_monthly_pattern([100.0] * 11 + [1300.0])
_check("mean exactly 2x median -> spiky", pat == "spiky")

# Just under the boundary -> not spiky; steady positive months -> stable.
pat, corr = classify_monthly_pattern([100.0] * 11 + [1200.0])
_check("just under 2x -> not spiky", pat != "spiky")

# Flat sales -> stable, no correction.
pat, corr = classify_monthly_pattern([100.0] * 12)
_check("flat -> stable", pat == "stable" and corr is None)

# Offshore-style swings (no zeros): 9 x 100 + 3 x 20 -> volatile (max>=3x min).
pat, corr = classify_monthly_pattern([100.0] * 9 + [20.0] * 3)
_check("big swings -> volatile", pat == "volatile")
_check("volatile never corrects", corr is None)

# One literal-zero month (fewer than half) -> volatile via the zero-month rule.
pat, corr = classify_monthly_pattern([100.0] * 11 + [0.0])
_check("single zero month -> volatile", pat == "volatile")

# Bursts: >= half the months zero -> lumpy (checked before spiky/volatile).
pat, corr = classify_monthly_pattern([0, 0, 0, 0, 0, 0, 100, 0, 200, 0, 0, 0])
_check("half-plus zero months -> lumpy", pat == "lumpy")
_check("lumpy never corrects", corr is None)

# Too little history: < 4 covered months -> stable (silent), even if wild.
pat, corr = classify_monthly_pattern([100.0, 200.0, 9000.0])
_check("<4 months -> stable no matter what", pat == "stable" and corr is None)

# All-zero vector -> stable (nothing to say).
pat, corr = classify_monthly_pattern([0.0, 0.0, 0.0, 0.0])
_check("all zeros -> stable", pat == "stable")

print()
if _FAILED:
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: ALL OK")
```

- [x] **Step 2: Run test to verify it fails**

```powershell
python tests/test_sales_volatility.py
```
Expected: `ImportError: cannot import name 'classify_monthly_pattern'`.

- [x] **Step 3: Implement in `agents/shared.py`**

Append after the `count_sales_months` section (near the other date/velocity
helpers). Thresholds are named constants per the spec's maintenance note:

```python
# ── Sales-pattern classification (spec 2026-07-10) ──────────────────────────
# A flat average lies three ways: a one-off bulk month inflates it (spiky),
# recurring swings smear it (volatile), and rare-burst items make it
# meaningless (lumpy). Detection is pure Python over per-month totals —
# thresholds live here so they can be tuned from real client data later.
PATTERN_MIN_MONTHS = 4        # fewer covered months -> too little signal, stay silent
PATTERN_SPIKY_MEAN_X = 2.0    # spiky when mean >= 2x median
PATTERN_VOLATILE_MAXMIN_X = 3.0  # volatile when max >= 3x min (positive months)


def classify_monthly_pattern(monthly):
    """Classify one item's per-month sales totals.

    Returns (pattern, corrected_avg):
      pattern       'stable' | 'spiky' | 'volatile' | 'lumpy'
      corrected_avg the median, ONLY for 'spiky' (the safer sizing number);
                    None otherwise.

    Rules in precedence order (first match wins), per the 2026-07-10 spec:
      lumpy    >= half the covered months are zero, but some sales exist
      spiky    median > 0 and mean >= PATTERN_SPIKY_MEAN_X * median
      volatile median > 0 and (max >= PATTERN_VOLATILE_MAXMIN_X * min over
               positive months, or 1..half-1 covered months are zero — an
               item that vanishes some months is swinging even if its
               selling months are steady)
      stable   everything else, or < PATTERN_MIN_MONTHS covered months
    """
    import statistics as _stats

    vals = [float(v or 0) for v in (monthly or [])]
    if len(vals) < PATTERN_MIN_MONTHS:
        return "stable", None
    total = sum(vals)
    if total <= 0:
        return "stable", None

    zeros = sum(1 for v in vals if v <= 0)
    med = _stats.median(vals)
    mean = total / len(vals)

    if zeros * 2 >= len(vals):
        return "lumpy", None
    if med > 0 and mean >= PATTERN_SPIKY_MEAN_X * med:
        return "spiky", round(med, 1)
    positive = [v for v in vals if v > 0]
    if med > 0 and positive and (
            max(positive) >= PATTERN_VOLATILE_MAXMIN_X * min(positive)
            or zeros >= 1):
        return "volatile", None
    return "stable", None
```

- [x] **Step 4: Run test to verify it passes**

```powershell
python tests/test_sales_volatility.py
```
Expected: all `ok:`, `RESULT: ALL OK`.

---

### Task 2: SQL wrapper `monthly_pattern_stats` (TDD)

**Files:**
- Modify: `agents/shared.py` (month-key helper + wrapper, right after Task 1's code)
- Test: append to `tests/test_sales_volatility.py`

- [x] **Step 1: Append the failing test**

Append to `tests/test_sales_volatility.py` just above the final `print()` block:

```python
# ---------------------------------------------------------------------------
# monthly_pattern_stats: real table, DD/MM/YYYY dates, day-level rows
# ---------------------------------------------------------------------------
import database as db
from agents.shared import monthly_pattern_stats, normalise_match_key

db.init_db()
SID = 990001
db.execute(f'CREATE TABLE IF NOT EXISTS sales_{SID} '
           '("item_description" TEXT, "invoice_date" TEXT, "qty" TEXT)')
rows = []
# Steady seller: 10/month across 12 months of 2025 (two 5-unit sales each).
for m in range(1, 13):
    rows += [("Brookvale UHT Milk", f"05/{m:02d}/2025", "5"),
             ("Brookvale UHT Milk", f"19/{m:02d}/2025", "5")]
# Spiky seller: 100/month, but March holds a single 10,000 bulk order.
for m in range(1, 13):
    rows.append(("Greenfjord Cheddar", f"10/{m:02d}/2025", "100"))
rows.append(("Greenfjord Cheddar", "20/03/2025", "9900"))
for r in rows:
    db.execute(f'INSERT INTO sales_{SID} VALUES (?,?,?)', r)

stats = monthly_pattern_stats(SID)
milk = stats.get(normalise_match_key("Brookvale UHT Milk"))
ched = stats.get(normalise_match_key("Greenfjord Cheddar"))

_check("wrapper: steady item found + stable", milk is not None and milk["pattern"] == "stable")
_check("wrapper: 12 covered months", milk is not None and milk["months"] == 12)
_check("wrapper: spiky item classified", ched is not None and ched["pattern"] == "spiky")
_check("wrapper: spiky corrected to typical month (100)",
       ched is not None and ched["corrected_avg"] == 100.0)
_check("wrapper: mean reflects the spike (~925)",
       ched is not None and 900 <= ched["mean"] <= 950)

# No date column -> empty dict, never raises.
SID2 = 990002
db.execute(f'CREATE TABLE IF NOT EXISTS sales_{SID2} ("item_description" TEXT, "qty" TEXT)')
db.execute(f'INSERT INTO sales_{SID2} VALUES ("Padimas Rice", "7")')
_check("wrapper: dateless table -> empty stats", monthly_pattern_stats(SID2) == {})
```

- [x] **Step 2: Run to verify it fails**

```powershell
python tests/test_sales_volatility.py
```
Expected: `ImportError: cannot import name 'monthly_pattern_stats'`.

- [x] **Step 3: Implement in `agents/shared.py`**

Append below `classify_monthly_pattern`. Reuses the module's existing date
regexes (`_ISO_DATE_RE`, `_NUM_DATE_RE`, `_TXT_DATE_RE`, `_MONTH_NAMES`,
`_norm_year`) and the column-level day-first convention from
`count_sales_months` (deliberately NOT refactoring that tested function):

```python
def _column_day_first(vals):
    """Column-level D/M vs M/D decision — same convention as count_sales_months."""
    day_first = True
    saw_month_first = False
    for s in vals:
        m = _NUM_DATE_RE.match(s)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a >= 1000:
            continue
        if a > 12:
            return True
        if b > 12:
            saw_month_first = True
    return not saw_month_first if saw_month_first else day_first


def _month_key(s, day_first):
    """One date string -> (year, month) or None. Mirrors count_sales_months'
    accepted formats: ISO, numeric D/M/Y-or-M/D/Y, textual 15-Jun-26, and
    Excel serial numbers."""
    import datetime as _dt
    s = str(s or "").strip()
    if not s:
        return None
    m = _ISO_DATE_RE.match(s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return (y, mo) if 1 <= mo <= 12 and 1900 <= y <= 2200 else None
    m = _NUM_DATE_RE.match(s)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a >= 1000:                       # 2026/06/15
            y, mo = a, b
        elif day_first:
            y, mo = _norm_year(c), b
        else:
            y, mo = _norm_year(c), a
        return (y, mo) if 1 <= mo <= 12 and 1900 <= y <= 2200 else None
    m = _TXT_DATE_RE.match(s)
    if m:
        mo = _MONTH_NAMES.get(m.group(2)[:4].lower().rstrip("."), None) \
             or _MONTH_NAMES.get(m.group(2)[:3].lower())
        y = _norm_year(int(m.group(3)))
        return (y, mo) if mo and 1900 <= y <= 2200 else None
    try:                                    # Excel serial (days since 1899-12-30)
        n = float(s)
        if 20000 <= n <= 80000:
            d = _dt.date(1899, 12, 30) + _dt.timedelta(days=int(n))
            return (d.year, d.month)
    except (ValueError, OverflowError):
        pass
    return None


def monthly_pattern_stats(session_id):
    """Per-item monthly totals + pattern classification from sales_<sid>.

    Returns {normalise_match_key(item): {"months": int, "mean": float,
    "median": float, "pattern": str, "corrected_avg": float|None,
    "min": float, "max": float}} — empty dict when the table, a date column,
    a qty column, or parseable dates are missing (wide-matrix summary sheets
    land here by design; spec: silently stable).
    """
    import statistics as _stats
    out = {}
    try:
        tbl = f"sales_{session_id}"
        sample = query(f"SELECT * FROM {tbl} LIMIT 1")
        if not sample:
            return out
        cols = list(sample[0].keys())
        desc = next((c for c in cols if c in ("inventory_desc", "item_description", "description", "product_name")), None)
        if not desc:
            desc = next((c for c in cols if any(k in c.lower() for k in ("desc", "item_name", "product_name", "item")) and "supplier" not in c.lower()), None)
        qty = next((c for c in cols if c in ("billing_qty", "qty", "quantity", "billing_quantity")), None)
        if not qty:
            qty = next((c for c in cols if any(k in c.lower() for k in ("qty", "quantity")) and "allocated" not in c.lower()), None)
        _DATE_EXACT = ("date", "invoice_date", "order_date", "transaction_date",
                       "sales_date", "po_date", "doc_date", "posting_date")
        dcol = next((c for c in cols if c.lower() in _DATE_EXACT), None)
        if not dcol:
            dcol = next((c for c in cols if "date" in c.lower()), None)
        if not (desc and qty and dcol):
            return out

        # Day-level pre-aggregation keeps the row count bounded (items x days,
        # not raw transactions); substr(1,10) trims time-of-day stamps.
        rows = query(
            f'SELECT "{desc}" AS item, substr("{dcol}", 1, 10) AS d, '
            f'SUM({_num_sql(qty)}) AS q FROM {tbl} '
            f'WHERE "{desc}" IS NOT NULL '
            f'GROUP BY "{desc}", substr("{dcol}", 1, 10) LIMIT 200000')
        if not rows:
            return out

        day_first = _column_day_first([str(r["d"] or "") for r in rows[:500]])
        per_item = {}          # key -> {(y,m): qty}
        covered = set()        # all months the file covers, across items
        for r in rows:
            mk = _month_key(r["d"], day_first)
            if not mk:
                continue
            covered.add(mk)
            key = normalise_match_key(str(r["item"]))
            if not key:
                continue
            bucket = per_item.setdefault(key, {})
            bucket[mk] = bucket.get(mk, 0.0) + float(r["q"] or 0)
        if not covered or not per_item:
            return out
        months_sorted = sorted(covered)[-24:]   # cap the window: latest 24 months

        for key, bucket in per_item.items():
            vec = [bucket.get(mk, 0.0) for mk in months_sorted]
            pattern, corrected = classify_monthly_pattern(vec)
            positive_or_all = [v for v in vec] or [0.0]
            out[key] = {
                "months": len(months_sorted),
                "mean": round(sum(vec) / len(vec), 1) if vec else 0.0,
                "median": round(_stats.median(vec), 1) if vec else 0.0,
                "min": round(min(positive_or_all), 1),
                "max": round(max(positive_or_all), 1),
                "pattern": pattern,
                "corrected_avg": corrected,
            }
    except Exception:
        return {}
    return out
```

- [x] **Step 4: Run to verify it passes**

```powershell
python tests/test_sales_volatility.py
```
Expected: all `ok:`, `RESULT: ALL OK`.

---

### Task 3: Post-pass `apply_sales_pattern_flags` (TDD)

**Files:**
- Modify: `agents/shared.py` (append after Task 2's code)
- Test: append to `tests/test_sales_volatility.py`

- [x] **Step 1: Append the failing test**

Append above the final `print()` block:

```python
# ---------------------------------------------------------------------------
# apply_sales_pattern_flags: deterministic flag + confidence cap on recs
# ---------------------------------------------------------------------------
from agents.shared import apply_sales_pattern_flags

fake_stats = {
    normalise_match_key("Greenfjord Cheddar"): {
        "months": 12, "mean": 925.0, "median": 100.0, "min": 100.0,
        "max": 10000.0, "pattern": "spiky", "corrected_avg": 100.0},
    normalise_match_key("Oakfield Eggs"): {
        "months": 12, "mean": 80.0, "median": 100.0, "min": 20.0,
        "max": 100.0, "pattern": "volatile", "corrected_avg": None},
    normalise_match_key("Marikita Coconut Milk"): {
        "months": 12, "mean": 25.0, "median": 0.0, "min": 0.0,
        "max": 200.0, "pattern": "lumpy", "corrected_avg": None},
    normalise_match_key("Brookvale UHT Milk"): {
        "months": 12, "mean": 10.0, "median": 10.0, "min": 10.0,
        "max": 10.0, "pattern": "stable", "corrected_avg": None},
}
recs = [
    {"item": "Greenfjord Cheddar", "confidence": "HIGH", "flags": []},
    {"item": "Oakfield Eggs", "confidence": "MEDIUM"},          # no flags key
    {"item": "Marikita Coconut Milk", "confidence": "LOW", "flags": ["existing note"]},
    {"item": "Brookvale UHT Milk", "confidence": "HIGH", "flags": []},
    {"error": "batch failed"},                                   # must be skipped
]
counts = apply_sales_pattern_flags(recs, fake_stats)

_check("post-pass: spiky rec stamped", recs[0].get("sales_pattern") == "spiky")
_check("post-pass: spiky HIGH capped to MEDIUM", recs[0]["confidence"] == "MEDIUM")
_check("post-pass: spiky flag text mentions typical month",
       any("typical month" in f for f in recs[0]["flags"]))
_check("post-pass: volatile rec gets flags list created",
       any("swing" in f.lower() for f in recs[1].get("flags", [])))
_check("post-pass: volatile MEDIUM stays MEDIUM", recs[1]["confidence"] == "MEDIUM")
_check("post-pass: volatile flag points at the context form",
       any("context" in f.lower() for f in recs[1]["flags"]))
_check("post-pass: lumpy keeps LOW confidence", recs[2]["confidence"] == "LOW")
_check("post-pass: lumpy keeps existing flags too", "existing note" in recs[2]["flags"])
_check("post-pass: stable rec untouched",
       "sales_pattern" not in recs[3] and recs[3]["confidence"] == "HIGH")
_check("post-pass: counts", counts == {"spiky": 1, "volatile": 1, "lumpy": 1})
```

- [x] **Step 2: Run to verify it fails**

```powershell
python tests/test_sales_volatility.py
```
Expected: `ImportError: cannot import name 'apply_sales_pattern_flags'`.

- [x] **Step 3: Implement in `agents/shared.py`**

```python
def apply_sales_pattern_flags(recs, pattern_stats):
    """Deterministic post-pass over parsed recommendations (same philosophy
    as the quantity sanitizer: never trust the model to self-report a
    warning). Stamps rec["sales_pattern"], appends a plain-English flag, and
    caps confidence at MEDIUM (HIGH -> MEDIUM; lower values untouched).
    Returns {"spiky": n, "volatile": n, "lumpy": n} counts."""
    counts = {"spiky": 0, "volatile": 0, "lumpy": 0}
    if not pattern_stats:
        return counts
    for rec in recs:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        st = pattern_stats.get(normalise_match_key(str(rec.get("item") or "")))
        if not st or st["pattern"] == "stable":
            continue
        pattern = st["pattern"]
        counts[pattern] += 1
        rec["sales_pattern"] = pattern
        if pattern == "spiky":
            flag = (f"Spiky sales history — one month dominates the average; "
                    f"quantity sized on the typical month ({st['corrected_avg']}/mo, "
                    f"raw average {st['mean']}/mo).")
        elif pattern == "volatile":
            flag = (f"Sales swing a lot month to month ({st['min']}–{st['max']}). "
                    f"If this is a known cycle (offshore periods, festive season), "
                    f"mention it in the analysis context next run.")
        else:
            flag = ("Irregular seller — bursts with quiet months. "
                    "Verify with your team before ordering.")
        flags = rec.get("flags")
        if not isinstance(flags, list):
            flags = []
        flags.append(flag)
        rec["flags"] = flags
        if str(rec.get("confidence", "")).upper() == "HIGH":
            rec["confidence"] = "MEDIUM"
    return counts
```

- [x] **Step 4: Run to verify it passes**

```powershell
python tests/test_sales_volatility.py
```
Expected: all `ok:`, `RESULT: ALL OK`.

---

### Task 4: Wire both agents

**Files:**
- Modify: `agents/inventory.py` (~line 509 velocity block)
- Modify: `agents/recommendation.py` (velocity rows ~169, prompt lines ~283, post-pass ~443)

- [x] **Step 1: Inventory agent — spike-corrected months-of-supply**

In `agents/inventory.py`, the import block at the top already pulls from
`.shared` — add `monthly_pattern_stats` and `normalise_match_key` to that
import list (check for existing `normalise_match_key` import first; add only
what's missing).

Directly BEFORE the line `inv_summary_lines = []` (after the months_of_data
resolution), insert:

```python
    # Sales-pattern stats (spec 2026-07-10): spiky items get their velocity
    # replaced by the typical month so one bulk order can't fake a CRITICAL.
    pattern_stats = monthly_pattern_stats(session_id)
```

Then replace:

```python
        _avg_direct = (sales_info or {}).get("avg_monthly_direct") or 0
        if _avg_direct > 0:
            avg_monthly = _avg_direct
        else:
            avg_monthly = total_sold / months_of_data if total_sold > 0 else 0
```

with:

```python
        _avg_direct = (sales_info or {}).get("avg_monthly_direct") or 0
        if _avg_direct > 0:
            avg_monthly = _avg_direct
        else:
            avg_monthly = total_sold / months_of_data if total_sold > 0 else 0
            # Spiky items: size on the typical month (median), never the
            # spike-inflated mean. Only on this derived path — a sheet-stated
            # average is the customer's own number and is never overridden.
            _pat = pattern_stats.get(normalise_match_key(canonical)) \
                   or pattern_stats.get(normalise_match_key(desc))
            if _pat and _pat["pattern"] == "spiky" and _pat["corrected_avg"]:
                avg_monthly = _pat["corrected_avg"]
```

- [x] **Step 2: Recommendation agent — velocity substitution**

In `agents/recommendation.py`, extend the `.shared` import list with
`monthly_pattern_stats` and `apply_sales_pattern_flags` (`normalise_match_key`
is already imported — verify).

Directly before the `try:` that opens the sales-velocity block (the line
`sal_table_r = f"sales_{session_id}"` sits inside it), insert:

```python
    pattern_stats = monthly_pattern_stats(session_id)
```

Then replace:

```python
                for r in stat_rows:
                    _avg_d = r["avg_direct"] or 0
                    _tot   = r["total_qty"] or 0
                    vel_rows.append({
                        "item": r["item"],
                        "avg_monthly": _avg_d if _avg_d > 0
                                       else (_tot / months_r if _tot > 0 else 0),
                    })
```

with:

```python
                for r in stat_rows:
                    _avg_d = r["avg_direct"] or 0
                    _tot   = r["total_qty"] or 0
                    _avg_m = _avg_d if _avg_d > 0 \
                             else (_tot / months_r if _tot > 0 else 0)
                    if _avg_d <= 0:
                        # Spiky: size on the typical month, not the spike-
                        # inflated mean (sheet-stated averages left alone).
                        _pat = pattern_stats.get(normalise_match_key(str(r["item"] or "")))
                        if _pat and _pat["pattern"] == "spiky" and _pat["corrected_avg"]:
                            _avg_m = _pat["corrected_avg"]
                    vel_rows.append({
                        "item": r["item"],
                        "avg_monthly": _avg_m,
                    })
```

- [x] **Step 3: Recommendation agent — pattern line in the per-item prompt block**

In the `enriched_lines.append(...)` block (the f-string starting `f"---\n"`
around line 283), the line
`f"Avg monthly sales: {avg_monthly}{uom_label}\n"` gains a pattern note
right after it. First, immediately before `enriched_lines.append(`, insert:

```python
        _pat = pattern_stats.get(normalise_match_key(iname))
        if _pat and _pat["pattern"] == "spiky":
            pattern_line = (f"Sales pattern: SPIKY — one month dominates; typical month "
                            f"(median) = {_pat['corrected_avg']}, raw average = {_pat['mean']}. "
                            f"Quantities are sized on the typical month.\n")
        elif _pat and _pat["pattern"] == "volatile":
            pattern_line = (f"Sales pattern: VOLATILE — monthly sales swing between "
                            f"{_pat['min']} and {_pat['max']}. The average may mislead; "
                            f"flag this for the buyer.\n")
        elif _pat and _pat["pattern"] == "lumpy":
            pattern_line = "Sales pattern: IRREGULAR — sells in bursts with many zero months.\n"
        else:
            pattern_line = ""
```

Then inside the f-string, insert `f"{pattern_line}"` on the line directly
after `f"Avg monthly sales: {avg_monthly}{uom_label}\n"`.

- [x] **Step 4: Recommendation agent — post-pass + progress line**

Directly after the `if qty_corrections:` `_emit(...)` block (around line
446), insert:

```python
        pat_counts = apply_sales_pattern_flags(recs, pattern_stats)
        _n_pat = sum(pat_counts.values())
        if _n_pat:
            _emit(progress_emit,
                  f"Safety check: {_n_pat} item{'s' if _n_pat != 1 else ''} with unusual "
                  f"sales patterns ({pat_counts['spiky']} spiky, "
                  f"{pat_counts['volatile']} swingy, {pat_counts['lumpy']} irregular)")
```

- [x] **Step 5: Full suite**

```powershell
python run_tests.py
```
Expected: all green (53 files after Task 1 created the new one). If
`test_agents_refactor.py` or `test_forecast_accuracy_audit.py` fail, read the
failure — their canned data may have accidentally tripped a pattern; fix by
adjusting THIS feature's thresholds only if the canned data genuinely
represents a false positive, otherwise update the fixture expectations with a
comment.

---

### Task 5: Clarity-box line

**Files:**
- Modify: `rec_logic.py` (`clarity_gaps`, after the `no_qty` gap block)
- Test: extend `tests/test_clarity_gaps.py`

- [x] **Step 1: Extend the clarity test (failing first)**

Open `tests/test_clarity_gaps.py`, find its existing checks, and append a new
case following the file's local style (it builds rec dicts and asserts on
`clarity_gaps(...)` output):

```python
# Sales-pattern warnings surface as a counted gap (spec 2026-07-10).
recs_pat = [
    {"item": "A", "sales_pattern": "spiky"},
    {"item": "B", "sales_pattern": "volatile"},
    {"item": "C"},  # stable/no pattern
]
gaps_pat = clarity_gaps(recs_pat)
labels = [g["label"] for g in gaps_pat]
assert any("unusual sales pattern" in l for l in labels), labels
pat_gap = next(g for g in gaps_pat if "unusual sales pattern" in g["label"])
assert pat_gap["count"] == 2, pat_gap
```

(Adapt the assertion style — `_check(...)` vs bare `assert` — to whatever the
file already uses; keep its conventions.)

- [x] **Step 2: Run to verify it fails**

```powershell
python tests/test_clarity_gaps.py
```
Expected: assertion failure (no such gap yet).

- [x] **Step 3: Implement in `rec_logic.py`**

In `clarity_gaps`, after the `no_qty` count and before `gaps = []`, add:

```python
    pattern_items = sum(1 for r in valid
                        if r.get("sales_pattern") in ("spiky", "volatile", "lumpy"))
```

And after the existing `if no_qty:` block's append, add:

```python
    if pattern_items:
        gaps.append({
            "count": pattern_items,
            "label": "unusual sales pattern",
            "why": "check their flags before approving",
        })
```

- [x] **Step 4: Run test + full suite**

```powershell
python tests/test_clarity_gaps.py
python run_tests.py
```
Expected: both green.

---

### Task 6: Security review (standing rule — new SQL construction)

- [x] **Step 1: Run the security-reviewer agent** on the diff of
`agents/shared.py`, `agents/inventory.py`, `agents/recommendation.py`,
`rec_logic.py`, `tests/test_sales_volatility.py`, `tests/test_clarity_gaps.py`.
Focus: the new `monthly_pattern_stats` query (column names come from the same
keyword-detection idiom as the neighbouring queries; `_num_sql` wraps the qty
column; table name is `sales_{int}`), no cross-org leak (session-scoped
table), flag text reaching templates (flags render via existing escaping).
Findings must be clean or fixed before handover.

---

### Task 7: Handover

- [x] **Step 1: Commit guide** (volatility commit, separate from the legal one):

```powershell
cd c:\BerthAI\BerthAI
git add agents/shared.py agents/inventory.py agents/recommendation.py rec_logic.py tests/test_sales_volatility.py tests/test_clarity_gaps.py docs/superpowers/specs/2026-07-10-sales-volatility-warnings-design.md docs/superpowers/plans/2026-07-10-sales-volatility-warnings.md
git commit -m "Flag spiky/volatile/irregular sales patterns, size spiky items on typical month"
git push
```

- [ ] **Step 2: Live verification note** — after deploy, run one real analysis;
if any item has a bulk-order month, its rec should carry the spike flag, a
MEDIUM confidence chip, and a sane quantity; progress screen shows the
"Safety check: N items with unusual sales patterns" line when N>0.

- [x] **Step 3: Update repo-root MEMORY.md** — feature summary, thresholds
location, the "only corrects the derived path, never sheet-stated averages"
rule, suite count.

---

## Self-review (done at plan time)

- **Spec coverage:** classifier rules → Task 1; monthly stats + guard rails
  (≥4 months, no-dates → empty, 24-month cap) → Task 2; spiky substitution in
  both agents (derived path only) → Task 4 Steps 1-2; prompt lines → Task 4
  Step 3; post-pass flags + confidence cap + progress line → Tasks 3, 4
  Step 4; clarity box → Task 5; named constants → Task 1; tests → Tasks 1/2/3/5.
  Out-of-scope items need no tasks. No gaps.
- **Placeholders:** none; every code step shows the code.
- **Type consistency:** `classify_monthly_pattern(list) -> (str, float|None)`;
  `monthly_pattern_stats(int) -> dict[key, dict]` with fields months/mean/
  median/min/max/pattern/corrected_avg used identically in Tasks 2/3/4;
  `apply_sales_pattern_flags(recs, stats) -> counts dict` consistent between
  Task 3 test and Task 4 call site.
- **Known judgment call:** `_column_day_first`/`_month_key` duplicate ~15
  lines of `count_sales_months`' parsing rather than refactoring that
  heavily-tested function — deliberate (stability > DRY here); noted in code
  comment.
