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

# ---------------------------------------------------------------------------
# monthly_pattern_stats: deterministic row cap — a capped read must drop the
# sliced boundary item (flags absent), never classify it from partial months
# ---------------------------------------------------------------------------
import agents.shared as shared

SID3 = 913
db.execute(f'CREATE TABLE IF NOT EXISTS sales_{SID3} '
           '("item_description" TEXT, "invoice_date" TEXT, "qty" TEXT)')
for r in [
    ("AAA BROOKVALE MILK", "2026-01-15", "10"),
    ("AAA BROOKVALE MILK", "2026-02-15", "20"),
    ("AAA BROOKVALE MILK", "2026-03-15", "30"),
    ("ZZZ PADIMAS RICE", "2026-01-20", "5"),
    ("ZZZ PADIMAS RICE", "2026-02-20", "5"),
    ("ZZZ PADIMAS RICE", "2026-03-20", "5"),
]:
    db.execute(f'INSERT INTO sales_{SID3} VALUES (?,?,?)', r)

AAA = normalise_match_key("AAA BROOKVALE MILK")
ZZZ = normalise_match_key("ZZZ PADIMAS RICE")

# 1. Under the cap: both items get stats.
full = monthly_pattern_stats(SID3)
_check("cap: under the cap both items present", AAA in full and ZZZ in full)
aaa_mean = full[AAA]["mean"] if AAA in full else None

_orig_cap = getattr(shared, "_PATTERN_ROW_CAP", 200000)

# 2. Cap hit mid-item (AAA's 3 rows + 1 of ZZZ's): boundary item dropped,
# surviving item's stats identical to the uncapped run.
try:
    shared._PATTERN_ROW_CAP = 4
    capped = monthly_pattern_stats(SID3)
    _check("cap: sliced boundary item dropped", ZZZ not in capped)
    _check("cap: surviving item kept with unchanged mean",
           AAA in capped and capped[AAA]["mean"] == aaa_mean)
finally:
    shared._PATTERN_ROW_CAP = _orig_cap

# 3. Exact-fit cap (all 6 rows) is indistinguishable from truncation — the
# accepted ceiling is that the last item MAY be dropped; the first item must
# still be present and correct.
try:
    shared._PATTERN_ROW_CAP = 6
    exact = monthly_pattern_stats(SID3)
    _check("cap: exact-fit keeps first item correct",
           AAA in exact and exact[AAA]["mean"] == aaa_mean)
finally:
    shared._PATTERN_ROW_CAP = _orig_cap

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

print()
if _FAILED:
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: ALL OK")
