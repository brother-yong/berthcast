# Supplier Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the supplier list out of the client's own uploaded files so flagging a supplier unreliable is one click on a name guaranteed to match the data, and merge the duplicate spellings that would otherwise split one supplier into three.

**Architecture:** A new `supplier_directory` table caches every supplier name seen across an org's completed upload sessions, refreshed at the end of each analysis run. `/suppliers` becomes the single supplier page (Settings loses its supplier card), rendering that table with an unreliable checkbox, an archive-as-delete action, and a duplicate-suggestion banner. Duplicate detection is two deterministic passes — suffix-strip and `difflib` fuzzy — computed at rebuild time and stored, never at page load. Merges write `merged_into` on the directory row, which `get_supplier_profile` and `update_supplier_scores` resolve so a merge actually changes what the pipeline sees.

**Tech Stack:** Python 3.11 (prod) / 3.14 (local), Flask, SQLite, Jinja templates. `difflib` from the stdlib — no new dependency.

**Spec:** `docs/superpowers/specs/2026-07-26-supplier-directory-design.md`

---

## Repo conventions that override the defaults in the writing-plans skill

Read these before starting. Getting them wrong wastes a full cycle.

- **Tests are plain scripts, NOT pytest.** No `pytest`, no fixtures, no `assert` framework. Copy the shape of `tests/test_supplier_accuracy.py`: set `os.environ["DB_PATH"]` to a temp file BEFORE importing any project module, stub `anthropic` into `sys.modules`, define `_check(name, cond)` printing `ok:` / `FAIL:`, and `sys.exit(1)` at the end if anything failed. Run a single file with `python tests/test_x.py`; run everything with `python run_tests.py`, which auto-discovers `tests/test_*.py`.
- **Do NOT commit per task.** A git pre-commit hook (`.claude/hooks/check_security_review.py`) blocks staging `app.py`, `database.py` or anything in `agents/` until a security review is recorded. Committing after each task means running the security-reviewer agent six times. Build the whole feature, run the gates in Task 8, then hand the user ONE commit command. Between tasks, the checkpoint is "tests pass", not "commit".
- **The user runs git himself.** Never run `git commit` or `git push`. Produce paste-ready PowerShell, staging files BY NAME (`git add .` is forbidden and hook-blocked).
- **All SQL lives in `database.py`.** `supplier_directory.py` must contain zero SQL — it takes lists of names and returns groups.
- **Test data uses invented brands only** (BROOKVALE, NORDVIK, PADIMAS, KESTREL). Never a real product, supplier or client name — the repo is public.
- **Parameterized queries always.** The only permitted interpolation into SQL is an int session id in a table name (`f"inventory_{session_id}"`), and the int must come from the DB, never from a request.
- `python app.py` is not run locally by the user. Verification is the test suite plus `smoke_live.py`.

---

## File Structure

**Create:**
- `supplier_directory.py` — pure functions: name keys, suffix-strip pass, fuzzy pass, group union. No SQL, no Flask, no I/O. Sits at repo root beside the existing extracted-logic modules (`rec_logic.py`, `chat_logic.py`, `data_quality.py`).
- `tests/test_supplier_directory.py` — one script covering pure grouping, rebuild, merge resolution, archive, and cross-org isolation.

**Modify:**
- `database.py` — three new tables + one column in the `init_db()` migration list; rebuild/read/merge/archive helpers; `get_supplier_profile` alias resolution; `update_supplier_scores` alias folding; `get_supplier_scores` archived filter.
- `app.py` — rebuild call after `status='complete'` (line 2659); `/suppliers` route rewrite; four new POST routes; delete the `save_supplier` / `delete_supplier` branches from `user_settings` (lines 1460-1482).
- `templates/suppliers.html` — the merged page.
- `templates/settings.html` — remove the supplier-profiles card, keep the default-lead-times card.

---

### Task 1: Schema

**Files:**
- Modify: `database.py:255-259` (end of the `init_db()` migration list)

- [ ] **Step 1: Add the migrations**

In `database.py`, the `init_db()` function ends its migration list with the `recommendation_outcomes ADD COLUMN supplier` entry at line 258, followed by `]:` and a try/except that swallows failures (that is what makes them idempotent). Insert these entries immediately BEFORE the closing `]:`

```python
        # ── Supplier directory ────────────────────────────────────────────
        # The list of supplier names the client has actually uploaded. The
        # profile machinery (delay rate → high-risk → safety buffer) shipped
        # long ago but sat unused, because the list started empty and had to be
        # typed by hand — and get_supplier_profile matches names EXACTLY, so a
        # hand-typed name silently missed the spelling used in the files.
        # Building the list from the uploads makes the names match by
        # construction. merged_into points at the canonical name after the user
        # confirms two spellings are one supplier.
        """CREATE TABLE IF NOT EXISTS supplier_directory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_name TEXT NOT NULL,
            supplier_name TEXT NOT NULL,
            source TEXT,
            merged_into TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_name, supplier_name)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_supplier_directory_org ON supplier_directory(org_name)",
        # Duplicate suggestions are recomputed at rebuild time and stored, never
        # derived on page load: 431 names is 92,665 pairs, measured at 0.74s —
        # free once per run, unacceptable on every view of a single-worker box.
        """CREATE TABLE IF NOT EXISTS supplier_merge_suggestions (
            org_name TEXT NOT NULL,
            group_key TEXT NOT NULL,
            supplier_name TEXT NOT NULL,
            reason TEXT,
            UNIQUE(org_name, group_key, supplier_name)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_supplier_merge_sugg_org ON supplier_merge_suggestions(org_name)",
        # "Not the same" has to stick, or the rejected group returns on every
        # page load and the user learns to ignore the banner.
        """CREATE TABLE IF NOT EXISTS supplier_merge_ignored (
            org_name TEXT NOT NULL,
            group_key TEXT NOT NULL,
            UNIQUE(org_name, group_key)
        )""",
        # Delete = hide. Archiving must NOT suppress recommendations: a delete
        # button that silently stops reorder advice is how a client stops
        # trusting the product.
        "ALTER TABLE supplier_profiles ADD COLUMN archived INTEGER DEFAULT 0",
```

- [ ] **Step 2: Verify the migrations apply to a fresh DB**

Run:
```
python -c "import os,tempfile; os.environ['DB_PATH']=os.path.join(tempfile.gettempdir(),'sd_check.db'); import database as db; db.init_db(); print([r['name'] for r in db.query(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'supplier%'\")]); print([c['name'] for c in db.query('PRAGMA table_info(supplier_profiles)') if c['name']=='archived'])"
```

Expected output contains `supplier_directory`, `supplier_merge_suggestions`, `supplier_merge_ignored`, `supplier_profiles`, and `['archived']`.

- [ ] **Step 3: Verify the migrations are idempotent**

Run the same command a second time. Expected: identical output, no exception. (Re-running `init_db()` on an existing DB is the normal case on every boot.)

---

### Task 2: Pure duplicate detection

Written first and tested offline because it is the only genuinely tricky logic in the feature, and it needs no database at all.

**Files:**
- Create: `supplier_directory.py`
- Create: `tests/test_supplier_directory.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_supplier_directory.py`:

```python
"""Supplier directory: duplicate detection, rebuild, merge resolution, archive.

The supplier-profile machinery (delay rate -> high-risk flag -> safety buffer)
shipped long ago but sat unused: the list started empty and had to be typed by
hand, and get_supplier_profile matches names EXACTLY, so a hand-typed profile
silently missed the spelling in the client's files. This covers the fix --
building the list from the uploads -- and the duplicate handling on top of it.

Run: python tests/test_supplier_directory.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_supdir.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db          # noqa: E402
import supplier_directory as sd  # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _group_of(groups, name):
    """Return the member->reason dict of the group containing `name`, or None."""
    for g in groups:
        if name in g["members"]:
            return g
    return None


# ---------------------------------------------------------------------------
# Pass 1 — suffix strip
# ---------------------------------------------------------------------------
groups = sd.detect_duplicate_groups(
    ["NORDVIK", "NORDVIK TRADING PTE LTD", "BROOKVALE FOODS"]
)
g = _group_of(groups, "NORDVIK")
_check("suffix pass groups NORDVIK with NORDVIK TRADING PTE LTD",
       g is not None and "NORDVIK TRADING PTE LTD" in g["members"])
_check("unrelated name is not pulled into the group",
       g is not None and "BROOKVALE FOODS" not in g["members"])
_check("suffix group carries a reason",
       g is not None and bool(g["members"]["NORDVIK"]))

# ---------------------------------------------------------------------------
# Pass 2 — fuzzy (typos the suffix rule is blind to)
# ---------------------------------------------------------------------------
groups = sd.detect_duplicate_groups(["BROOKVALE", "BROKVALE", "PADIMAS RICE"])
g = _group_of(groups, "BROOKVALE")
_check("fuzzy pass groups BROOKVALE with BROKVALE",
       g is not None and "BROKVALE" in g["members"])
_check("fuzzy pass leaves an unrelated name alone",
       g is not None and "PADIMAS RICE" not in g["members"])

# ---------------------------------------------------------------------------
# Names that merely share a first word must NOT fuzzy-match. They DO collide
# under the suffix pass (both strip to "padimas") -- that is a known, accepted
# false suggestion, which is exactly why a merge is always human-confirmed.
# ---------------------------------------------------------------------------
_check("fuzzy ratio keeps PADIMAS TRADING and PADIMAS ENTERPRISE apart",
       not sd._fuzzy_pairs(["padimastrading", "padimasenterprise"]))

# ---------------------------------------------------------------------------
# A group found by BOTH passes appears once, not twice
# ---------------------------------------------------------------------------
groups = sd.detect_duplicate_groups(
    ["NORDVIK TRADING", "NORDVIK TRAIDNG", "NORDVIK"]
)
_check("overlapping passes produce exactly one group", len(groups) == 1, f"got {len(groups)}")
_check("the single group holds all three spellings",
       len(groups) == 1 and len(groups[0]["members"]) == 3)

# ---------------------------------------------------------------------------
# Solo names produce no group; every group has a stable key
# ---------------------------------------------------------------------------
_check("no group for unrelated names",
       sd.detect_duplicate_groups(["NORDVIK", "BROOKVALE", "KESTREL"]) == [])
g1 = sd.detect_duplicate_groups(["NORDVIK", "NORDVIK TRADING"])
g2 = sd.detect_duplicate_groups(["NORDVIK TRADING", "NORDVIK"])
_check("group_key does not depend on input order",
       g1[0]["group_key"] == g2[0]["group_key"])

if _FAILED:
    sys.exit(1)
print("\nAll supplier-directory checks passed.")
```

- [ ] **Step 2: Run it and confirm it fails for the right reason**

Run: `python tests/test_supplier_directory.py`
Expected: `ModuleNotFoundError: No module named 'supplier_directory'`

- [ ] **Step 3: Write `supplier_directory.py`**

Create `supplier_directory.py`:

```python
"""Duplicate detection over supplier names.

Pure functions only -- no SQL, no Flask. All database work for the supplier
directory lives in database.py, per the repo convention.

Two passes, because neither catches the other's cases. Measured on real-shaped
names:

    nordvik / nordvic              difflib 0.857   fuzzy only
    brookvale / brokvale           difflib 0.941   fuzzy only
    padimastrading / padimas       difflib 0.667   suffix-strip only

Pass 1 also groups companies that merely share a first word (PADIMAS TRADING vs
PADIMAS ENTERPRISE both strip to "padimas"). That false positive is accepted on
purpose: it is the price of catching NORDVIK / NORDVIK TRADING, and it is why a
merge is only ever SUGGESTED and never applied without the user confirming.
"""
import difflib
import re

from agents.shared import normalise_match_key

# Legal-form and generic trade words that distinguish nothing between two
# spellings of the same trader. Kept deliberately short: every word added here
# widens the net for false groups as well as real ones.
_SUFFIX_WORDS = {
    "pte", "ltd", "llp", "co", "sdn", "bhd",
    "trading", "enterprise", "enterprises",
    "import", "export", "imports", "exports",
    "intl", "international", "sons", "and",
}

_WORD_RE = re.compile(r"[^a-z0-9]+")

# 0.85 catches single-character typos (0.857-0.941 measured) while leaving
# same-first-word companies apart (0.645-0.667 measured).
_FUZZY_THRESHOLD = 0.85


def _words(name):
    return [w for w in _WORD_RE.split(str(name).casefold()) if w]


def _stripped_key(name):
    """Normalised key with legal/trade words removed.

    Falls back to the full key when a name is nothing BUT suffix words, so
    e.g. a supplier literally called "Trading Co" still keys to something.
    """
    kept = [w for w in _words(name) if w not in _SUFFIX_WORDS]
    return "".join(kept) or normalise_match_key(name)


def _removed_words(name):
    return [w for w in _words(name) if w in _SUFFIX_WORDS]


def _suffix_groups(names):
    """{stripped_key: [names]} for keys shared by 2+ distinct names."""
    buckets = {}
    for n in names:
        buckets.setdefault(_stripped_key(n), []).append(n)
    return {k: v for k, v in buckets.items() if len(v) > 1}


def _fuzzy_pairs(keys):
    """[(key_a, key_b)] for normalised keys similar enough to be a typo.

    quick_ratio() is an upper bound on ratio() and much cheaper, so it screens
    out the overwhelming majority of pairs before the real comparison.
    """
    keys = sorted(set(keys))
    out = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            sm = difflib.SequenceMatcher(None, a, b)
            if sm.quick_ratio() < _FUZZY_THRESHOLD:
                continue
            if sm.ratio() >= _FUZZY_THRESHOLD:
                out.append((a, b))
    return out


def detect_duplicate_groups(names):
    """Return [{"group_key": str, "members": {name: reason}}].

    Groups from both passes are unioned: any two groups sharing a member become
    one group, so a set of spellings caught by both passes is offered once.
    """
    names = [n for n in dict.fromkeys(n for n in names if str(n).strip())]
    if len(names) < 2:
        return []

    # parent[name] -> representative name (plain union-find, no ranking; the
    # lists here are hundreds of names, not millions)
    parent = {n: n for n in names}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    reasons = {}

    # Pass 1 — suffix strip
    for _key, members in _suffix_groups(names).items():
        dropped = sorted({w for m in members for w in _removed_words(m)})
        reason = ("same name after removing " + ", ".join(w.upper() for w in dropped)
                  if dropped else "same name ignoring spacing and punctuation")
        for m in members:
            reasons.setdefault(m, reason)
            _union(members[0], m)

    # Pass 2 — fuzzy
    by_key = {}
    for n in names:
        by_key.setdefault(normalise_match_key(n), []).append(n)
    for key_a, key_b in _fuzzy_pairs(list(by_key)):
        for a in by_key[key_a]:
            for b in by_key[key_b]:
                reasons.setdefault(a, "spelled almost the same")
                reasons.setdefault(b, "spelled almost the same")
                _union(a, b)

    clusters = {}
    for n in names:
        if n in reasons:
            clusters.setdefault(_find(n), []).append(n)

    groups = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        groups.append({
            # Keyed off the alphabetically-first member so the key is stable
            # across rebuilds and input orderings -- the ignore list depends on
            # that stability to keep a dismissed group dismissed.
            "group_key": normalise_match_key(members[0]),
            "members": {m: reasons[m] for m in members},
        })
    return sorted(groups, key=lambda g: g["group_key"])
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python tests/test_supplier_directory.py`
Expected: every line starts `ok:`, ending with `All supplier-directory checks passed.`

If "overlapping passes produce exactly one group" fails with 2 groups, the union step is not linking the passes — check that pass 2 unions against the same `parent` dict pass 1 populated.

---

### Task 3: Directory rebuild

**Files:**
- Modify: `database.py` (append after `get_supplier_scores`, which ends at line 1235)
- Modify: `tests/test_supplier_directory.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_supplier_directory.py`, immediately BEFORE the closing `if _FAILED:` block:

```python
# ---------------------------------------------------------------------------
# Rebuild: names come out of the org's own uploaded files
# ---------------------------------------------------------------------------
db.init_db()

SID_A, SID_B = 8801, 8802   # OrgA, OrgB
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_A, 1, "OrgA", "complete", "all", "{}"))
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_B, 2, "OrgB", "complete", "all", "{}"))

db.execute(f"CREATE TABLE suppliers_{SID_A} (supplier_name TEXT, supplier_type TEXT)")
db.execute(f"INSERT INTO suppliers_{SID_A} VALUES ('KESTREL COLD CHAIN','import')")

db.execute(f"CREATE TABLE purchase_orders_{SID_A} (inventory_desc TEXT, supplier_name TEXT)")
for _item, _sup in [("Brookvale UHT Milk", "NORDVIK TRADING PTE LTD"),
                    ("Padimas Rice 5kg", "NORDVIK"),
                    ("Vanmark Butter", "KESTREL COLD CHAIN")]:
    db.execute(f"INSERT INTO purchase_orders_{SID_A} VALUES (?,?)", (_item, _sup))

# Sales sheet: blocky supplier column (one contiguous run per name, first name
# in the first two rows) -- the plan-008 shape that is safe to read.
db.execute(f"CREATE TABLE sales_{SID_A} (description TEXT, supplier TEXT)")
for _item, _sup in [("Aldermoor Oats", "ALDERMOOR SUPPLY"),
                    ("Aldermoor Oats 1kg", "ALDERMOOR SUPPLY"),
                    ("Brookvale UHT Milk", "NORDVIK TRADING PTE LTD")]:
    db.execute(f"INSERT INTO sales_{SID_A} VALUES (?,?)", (_item, _sup))

# OrgB has its own supplier that OrgA must never see.
db.execute(f"CREATE TABLE purchase_orders_{SID_B} (inventory_desc TEXT, supplier_name TEXT)")
db.execute(f"INSERT INTO purchase_orders_{SID_B} VALUES ('Secret Item','OTHERORG TRADING')")

db.rebuild_supplier_directory("OrgA")
db.rebuild_supplier_directory("OrgB")

rows = db.get_supplier_directory("OrgA")
names = {r["supplier_name"]: r for r in rows}

_check("PO supplier is discovered", "NORDVIK TRADING PTE LTD" in names)
_check("supplier-listing name is discovered", "KESTREL COLD CHAIN" in names)
_check("sales-sheet name is discovered", "ALDERMOOR SUPPLY" in names)
_check("listing beats po as the recorded source",
       names.get("KESTREL COLD CHAIN", {}).get("source") == "listing")
_check("sales-only name is tagged as sales",
       names.get("ALDERMOOR SUPPLY", {}).get("source") == "sales")
_check("a name in both PO and sales is tagged po, not sales",
       names.get("NORDVIK TRADING PTE LTD", {}).get("source") == "po")
_check("OrgA cannot see OrgB's supplier", "OTHERORG TRADING" not in names)
_check("OrgB sees only its own",
       {r["supplier_name"] for r in db.get_supplier_directory("OrgB")} == {"OTHERORG TRADING"})

# Rebuild is idempotent -- running it twice must not duplicate rows.
_before = len(db.get_supplier_directory("OrgA"))
db.rebuild_supplier_directory("OrgA")
_check("rebuild is idempotent", len(db.get_supplier_directory("OrgA")) == _before)

# Pure spelling drift collapses silently -- no suggestion, no confirmation.
# "KESTREL COLD CHAIN" (listing) and "kestrel cold  chain" (PO) are one row.
db.execute(f"INSERT INTO purchase_orders_{SID_A} VALUES (?,?)",
           ("Vanmark Butter 250g", "kestrel cold  chain"))
db.rebuild_supplier_directory("OrgA")
_kestrel = [r for r in db.get_supplier_directory("OrgA")
            if r["supplier_name"].lower().replace(" ", "") == "kestrelcoldchain"]
_check("case and spacing drift collapses to one directory row",
       len(_kestrel) == 1, f"got {[r['supplier_name'] for r in _kestrel]}")
_check("the longer, more complete spelling is the one kept",
       len(_kestrel) == 1 and _kestrel[0]["supplier_name"] == "KESTREL COLD CHAIN")

# Suggestions were computed and stored by the rebuild.
sugg = db.get_merge_suggestions("OrgA")
_nord = [g for g in sugg if any("NORDVIK" in m for m in g["members"])]
_check("rebuild stored a NORDVIK suggestion group", len(_nord) == 1, f"got {len(_nord)}")
_check("suggestion members carry reasons",
       bool(_nord) and all(v for v in _nord[0]["members"].values()))

# A non-blocky sales supplier column must be ignored (plan-008 gate): the same
# name reappears in separate runs, which is a transaction dump, not a merged
# export.
SID_C = 8803
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_C, 3, "OrgC", "complete", "all", "{}"))
db.execute(f"CREATE TABLE sales_{SID_C} (description TEXT, supplier TEXT)")
for _item, _sup in [("A", "SCATTER ONE"), ("B", "SCATTER TWO"),
                    ("C", "SCATTER ONE"), ("D", "SCATTER TWO")]:
    db.execute(f"INSERT INTO sales_{SID_C} VALUES (?,?)", (_item, _sup))
db.rebuild_supplier_directory("OrgC")
_check("non-blocky sales column is rejected",
       db.get_supplier_directory("OrgC") == [])

# Dismissed groups stay dismissed across rebuilds.
_key = _nord[0]["group_key"]
db.ignore_merge_group("OrgA", _key)
db.rebuild_supplier_directory("OrgA")
_check("a dismissed group does not come back",
       all(g["group_key"] != _key for g in db.get_merge_suggestions("OrgA")))
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python tests/test_supplier_directory.py`
Expected: `AttributeError: module 'database' has no attribute 'rebuild_supplier_directory'`

- [ ] **Step 3: Implement the rebuild in `database.py`**

Append to `database.py` after `get_supplier_scores` (ends line 1235):

```python
# ---------------------------------------------------------------------------
# Supplier directory — the supplier list built from the client's own uploads
# ---------------------------------------------------------------------------

# A name confirmed by a better source is upgraded, never downgraded: a supplier
# first seen on the sales sheet and later found on a real PO becomes "po".
_SUPPLIER_SOURCE_RANK = {"sales": 0, "po": 1, "listing": 2}

_DIRECTORY_MAX_SESSIONS = 25      # bounds the scan on a long-lived org
_DIRECTORY_MAX_ROWS     = 5000    # per table, per session — 512 MB worker


def _sales_supplier_names(session_id: int) -> set:
    """Supplier names off the sales sheet, but only when the column is a
    merged-cell export (plan 008).

    A blocky column has each name in ONE contiguous run and the first name
    within the first two rows — the shape a grouped report has. A column where
    names recur in scattered runs is a transaction dump whose supplier field
    means something else, and reading it paints stray names across the file.
    (This repeats ~6 lines of the check in agents/recommendation.py:233-237 on
    purpose: that guard is live on a real client and extracting it would put a
    shipped safety net at risk for no functional gain.)
    """
    table = f"sales_{session_id}"
    try:
        sample = query(f"SELECT * FROM {table} LIMIT 1")
        if not sample:
            return set()
        cols = list(sample[0].keys())
        sup_col = next((c for c in cols if "supplier" in c), None)
        if not sup_col:
            return set()
        rows = query(f'SELECT "{sup_col}" AS s FROM {table} LIMIT ?',
                     (_DIRECTORY_MAX_ROWS,))
        raw = [str(r["s"]).strip() if r["s"] is not None else "" for r in rows]
        seq = [v for v in raw if v]
        if not seq:
            return set()
        first_idx = next((i for i, v in enumerate(raw) if v), None)
        runs = 1 + sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        is_blocky = runs == len(set(seq)) and first_idx is not None and first_idx <= 1
        return set(seq) if is_blocky else set()
    except Exception:
        return set()


def _po_supplier_names(session_id: int) -> set:
    table = f"purchase_orders_{session_id}"
    try:
        sample = query(f"SELECT * FROM {table} LIMIT 1")
        if not sample:
            return set()
        cols = list(sample[0].keys())
        sup_col = (next((c for c in cols if "supplier" in c and "name" in c), None)
                   or next((c for c in cols if "supplier" in c), None))
        if not sup_col:
            return set()
        rows = query(
            f'SELECT DISTINCT "{sup_col}" AS s FROM {table} '
            f'WHERE "{sup_col}" IS NOT NULL LIMIT ?', (_DIRECTORY_MAX_ROWS,))
        return {str(r["s"]).strip() for r in rows if str(r["s"] or "").strip()}
    except Exception:
        return set()


def _listing_supplier_names(session_id: int) -> set:
    table = f"suppliers_{session_id}"
    try:
        sample = query(f"SELECT * FROM {table} LIMIT 1")
        if not sample:
            return set()
        cols = list(sample[0].keys())
        name_col = next((c for c in cols if "name" in c or "supplier" in c), None)
        if not name_col:
            return set()
        rows = query(
            f'SELECT DISTINCT "{name_col}" AS s FROM {table} '
            f'WHERE "{name_col}" IS NOT NULL LIMIT ?', (_DIRECTORY_MAX_ROWS,))
        return {str(r["s"]).strip() for r in rows if str(r["s"] or "").strip()}
    except Exception:
        return set()


def rebuild_supplier_directory(org_name: str):
    """Refresh an org's supplier directory and duplicate suggestions.

    Called at the end of an analysis run, and lazily on first view of an org
    with an empty directory. Never called on every page load — the fuzzy pass
    is O(n^2) and this reads per-session tables.
    """
    from agents.shared import normalise_match_key

    sessions = query(
        "SELECT id FROM upload_sessions WHERE org_name=? AND status='complete' "
        "ORDER BY created_at DESC LIMIT ?",
        (org_name, _DIRECTORY_MAX_SESSIONS)
    )

    listing, po, sales = set(), set(), set()
    for row in sessions:
        # int() straight off the DB — the only interpolation into a table name
        # the conventions allow, and never request-supplied.
        sid = int(row["id"])
        listing |= _listing_supplier_names(sid)
        po      |= _po_supplier_names(sid)
        sales   |= _sales_supplier_names(sid)

    # Highest-trust source written last so it wins.
    found = {}
    for name in sales:
        found[name] = "sales"
    for name in po:
        found[name] = "po"
    for name in listing:
        found[name] = "listing"

    # Collapse pure spelling drift ("NORDVIK" / "nordvik " / "Nord-vik") to one
    # row, keeping the longest spelling as the one worth showing. This is the
    # deterministic half of dedup — no confirmation needed for it.
    by_key = {}
    for name, source in found.items():
        key = normalise_match_key(name)
        prev = by_key.get(key)
        if prev is None or len(name) > len(prev[0]):
            by_key[key] = (name, source)
        elif _SUPPLIER_SOURCE_RANK.get(source, 0) > _SUPPLIER_SOURCE_RANK.get(prev[1], 0):
            by_key[key] = (prev[0], source)
    found = {name: source for name, source in by_key.values()}

    existing = {r["supplier_name"]: r["source"]
                for r in query("SELECT supplier_name, source FROM supplier_directory "
                               "WHERE org_name=?", (org_name,))}

    # One connection for the whole write — `execute()` opens and closes a
    # connection per call, and an org can have hundreds of suppliers.
    conn = get_db()
    try:
        c = conn.cursor()
        for name, source in found.items():
            if name not in existing:
                c.execute(
                    "INSERT OR IGNORE INTO supplier_directory "
                    "(org_name, supplier_name, source) VALUES (?,?,?)",
                    (org_name, name, source))
            elif (_SUPPLIER_SOURCE_RANK.get(source, 0)
                  > _SUPPLIER_SOURCE_RANK.get(existing[name], 0)):
                c.execute(
                    "UPDATE supplier_directory SET source=?, last_seen=CURRENT_TIMESTAMP "
                    "WHERE org_name=? AND supplier_name=?", (source, org_name, name))
            else:
                c.execute(
                    "UPDATE supplier_directory SET last_seen=CURRENT_TIMESTAMP "
                    "WHERE org_name=? AND supplier_name=?", (org_name, name))
        conn.commit()
    finally:
        conn.close()

    _rebuild_merge_suggestions(org_name)


def _rebuild_merge_suggestions(org_name: str):
    """Recompute and store duplicate suggestions for an org.

    Already-merged names are excluded (the question is settled) and so are
    groups the user dismissed.
    """
    from supplier_directory import detect_duplicate_groups

    names = [r["supplier_name"] for r in query(
        "SELECT supplier_name FROM supplier_directory "
        "WHERE org_name=? AND merged_into IS NULL", (org_name,))]
    ignored = {r["group_key"] for r in query(
        "SELECT group_key FROM supplier_merge_ignored WHERE org_name=?", (org_name,))}

    groups = [g for g in detect_duplicate_groups(names)
              if g["group_key"] not in ignored]

    conn = get_db()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM supplier_merge_suggestions WHERE org_name=?", (org_name,))
        for g in groups:
            for name, reason in g["members"].items():
                c.execute(
                    "INSERT OR IGNORE INTO supplier_merge_suggestions "
                    "(org_name, group_key, supplier_name, reason) VALUES (?,?,?,?)",
                    (org_name, g["group_key"], name, reason))
        conn.commit()
    finally:
        conn.close()


def get_supplier_directory(org_name: str) -> list:
    return query(
        "SELECT supplier_name, source, merged_into, first_seen, last_seen "
        "FROM supplier_directory WHERE org_name=? ORDER BY supplier_name",
        (org_name,))


def get_merge_suggestions(org_name: str) -> list:
    """[{"group_key": str, "members": {name: reason}}] — page-load cheap."""
    rows = query(
        "SELECT group_key, supplier_name, reason FROM supplier_merge_suggestions "
        "WHERE org_name=? ORDER BY group_key, supplier_name", (org_name,))
    groups = {}
    for r in rows:
        groups.setdefault(r["group_key"], {})[r["supplier_name"]] = r["reason"]
    return [{"group_key": k, "members": v} for k, v in groups.items()]


def ignore_merge_group(org_name: str, group_key: str):
    execute("INSERT OR IGNORE INTO supplier_merge_ignored (org_name, group_key) "
            "VALUES (?,?)", (org_name, group_key))
    execute("DELETE FROM supplier_merge_suggestions WHERE org_name=? AND group_key=?",
            (org_name, group_key))
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python tests/test_supplier_directory.py`
Expected: all `ok:`.

If "non-blocky sales column is rejected" fails, the run-counting is wrong — `runs == len(set(seq))` must be False for `SCATTER ONE, SCATTER TWO, SCATTER ONE, SCATTER TWO` (4 runs, 2 distinct).

---

### Task 4: Merge resolution reaches the pipeline

A merge that only changes the list is decoration. This is the task that makes it change what the AI sees.

**Files:**
- Modify: `database.py:972-990` (`get_supplier_profile`), `database.py:1156-1222` (`update_supplier_scores`), append `merge_suppliers` / `unmerge_supplier`
- Modify: `tests/test_supplier_directory.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_supplier_directory.py`, before the closing `if _FAILED:` block:

```python
# ---------------------------------------------------------------------------
# Merge: the canonical profile must answer for every merged spelling
# ---------------------------------------------------------------------------
db.upsert_supplier_profile("OrgA", "NORDVIK TRADING PTE LTD",
                           avg_lead_time_days=99, delay_probability=0.5,
                           notes="slow on chilled")

_p = db.get_supplier_profile("OrgA", "NORDVIK")
_check("before merge, the short spelling gets defaults",
       _p.get("avg_lead_time_days") != 99)

db.merge_suppliers("OrgA", "NORDVIK TRADING PTE LTD", ["NORDVIK"])

_p = db.get_supplier_profile("OrgA", "NORDVIK")
_check("after merge, the short spelling resolves to the canonical profile",
       _p.get("avg_lead_time_days") == 99, str(_p.get("avg_lead_time_days")))
_check("merged spelling inherits the unreliable delay rate",
       abs(float(_p.get("delay_probability") or 0) - 0.5) < 1e-9)
_check("canonical name still resolves to itself",
       db.get_supplier_profile("OrgA", "NORDVIK TRADING PTE LTD").get("avg_lead_time_days") == 99)
_check("merging does not leak across orgs",
       db.get_supplier_profile("OrgB", "NORDVIK").get("avg_lead_time_days") != 99)

# Merge is reversible.
db.unmerge_supplier("OrgA", "NORDVIK")
_check("unmerge restores the standalone lookup",
       db.get_supplier_profile("OrgA", "NORDVIK").get("avg_lead_time_days") != 99)
db.merge_suppliers("OrgA", "NORDVIK TRADING PTE LTD", ["NORDVIK"])

# Scores fold together instead of splitting across spellings.
import json as _json  # noqa: E402
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_json, recommendations_json) "
    "VALUES (?,?,?)",
    (SID_A, "{}", _json.dumps([
        {"item": "Brookvale UHT Milk", "supplier": "NORDVIK TRADING PTE LTD",
         "approved": True, "order_placed": True, "outcome_status": "stockout_avoided"},
        {"item": "Padimas Rice 5kg", "supplier": "NORDVIK",
         "approved": True, "order_placed": True, "outcome_status": "stockout_avoided"},
    ])))
db.update_supplier_scores("OrgA")
_scores = {r["supplier_name"]: r for r in db.get_supplier_scores("OrgA")}
_check("merged spellings score as one supplier",
       _scores.get("NORDVIK TRADING PTE LTD", {}).get("total_recs") == 2,
       str(_scores.get("NORDVIK TRADING PTE LTD", {}).get("total_recs")))
_check("the merged-away spelling gets no separate score row",
       "NORDVIK" not in _scores)

# A merge must survive the next upload, or it silently undoes itself.
db.rebuild_supplier_directory("OrgA")
_check("merge survives a rebuild",
       db.get_supplier_profile("OrgA", "NORDVIK").get("avg_lead_time_days") == 99)
_check("a merged name is not re-offered as a suggestion",
       all("NORDVIK" not in g["members"] for g in db.get_merge_suggestions("OrgA")))

# Manual merge: two names no pass would ever group, merged by hand from the
# list. This is what makes detection quality a convenience, not a dependency.
db.merge_suppliers("OrgA", "KESTREL COLD CHAIN", ["ALDERMOOR SUPPLY"])
db.upsert_supplier_profile("OrgA", "KESTREL COLD CHAIN", avg_lead_time_days=21)
_check("hand-merged name resolves to the chosen canonical profile",
       db.get_supplier_profile("OrgA", "ALDERMOOR SUPPLY").get("avg_lead_time_days") == 21)
db.rebuild_supplier_directory("OrgA")
_check("hand merge survives a rebuild",
       db.get_supplier_profile("OrgA", "ALDERMOOR SUPPLY").get("avg_lead_time_days") == 21)
db.unmerge_supplier("OrgA", "ALDERMOOR SUPPLY")

# The unreliable flag has to land ABOVE the threshold that selects the
# 2.5-month safety buffer (agents/recommendation.py:356-361) and the high-risk
# flag (agents/shared.py:870). Asserting the boundary, not re-implementing the
# banding: this fails if the flag value is ever changed without checking it
# still clears both.
db.upsert_supplier_profile("OrgA", "BROOKVALE DAIRY", delay_probability=0.5)
_flagged = float(db.get_supplier_profile("OrgA", "BROOKVALE DAIRY")["delay_probability"])
_check("unreliable flag clears the 2.5-month buffer threshold", _flagged > 0.35)
_check("unreliable flag clears the high-risk threshold", _flagged > 0.30)
db.upsert_supplier_profile("OrgA", "BROOKVALE DAIRY", delay_probability=0.2)
_unflagged = float(db.get_supplier_profile("OrgA", "BROOKVALE DAIRY")["delay_probability"])
_check("cleared flag sits in the middle buffer band, not the lowest",
       0.15 < _unflagged <= 0.35)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python tests/test_supplier_directory.py`
Expected: `AttributeError: module 'database' has no attribute 'merge_suppliers'`

- [ ] **Step 3: Rewrite `get_supplier_profile` to resolve aliases**

In `database.py`, replace the body of `get_supplier_profile` (lines 972-990) with:

```python
def get_supplier_profile(org_name: str, supplier_name: str) -> dict:
    # One statement, not two: resolve the name through supplier_directory's
    # merged_into if the user merged it, else match the name as given. This
    # lookup used to be a raw exact-string match, so a profile spelled even
    # slightly differently from the client's file silently fell back to
    # defaults with no error anywhere.
    rows = query(
        "SELECT p.* FROM supplier_profiles p "
        "WHERE p.org_name=? AND p.supplier_name = COALESCE(("
        "    SELECT d.merged_into FROM supplier_directory d "
        "    WHERE d.org_name=? AND d.supplier_name=? AND d.merged_into IS NOT NULL"
        "), ?)",
        (org_name, org_name, supplier_name, supplier_name)
    )
    if rows:
        return dict(rows[0])
    return {
        "org_name": org_name,
        "supplier_name": supplier_name,
        "delay_probability": 0.2,
        "avg_lead_time_days": None,
        "data_quality_score": 0.3,  # Low — unknown supplier
        "notes": "No profile. Using defaults.",
    }
```

Keep the existing default-dict keys exactly as they are — `agents/shared.py` and `agents/recommendation.py` read `delay_probability`, `avg_lead_time_days`, `data_quality_score` and `notes` off this return value, and a missing key changes pipeline behaviour.

- [ ] **Step 4: Fold merged names together in `update_supplier_scores`**

In `database.py:1156`, inside `update_supplier_scores`, add the alias map immediately after the `supplier_stats = {}` line (line 1171):

```python
    # Merged spellings score as one supplier. Loaded once — this loop runs over
    # every rec in every session.
    aliases = {r["supplier_name"]: r["merged_into"] for r in query(
        "SELECT supplier_name, merged_into FROM supplier_directory "
        "WHERE org_name=? AND merged_into IS NOT NULL", (org_name,))}
```

Then change the supplier-extraction line (currently line 1180) from:

```python
            sup = (r.get("edited_supplier") or r.get("supplier") or "Unknown").strip()
```

to:

```python
            sup = (r.get("edited_supplier") or r.get("supplier") or "Unknown").strip()
            sup = aliases.get(sup, sup)
```

- [ ] **Step 5: Add the merge helpers**

Append to `database.py`, after `ignore_merge_group`:

```python
def merge_suppliers(org_name: str, canonical: str, variants: list):
    """Point each variant at `canonical` so every lookup resolves to one profile.

    Reversible — nothing in the client's uploaded files is touched, only how
    names resolve. A variant with no directory row yet gets one, so a merge
    initiated by hand from the list works on a name that has not been rebuilt.
    """
    conn = get_db()
    try:
        c = conn.cursor()
        for v in variants:
            if not v or v == canonical:
                continue
            c.execute(
                "INSERT OR IGNORE INTO supplier_directory (org_name, supplier_name, source) "
                "VALUES (?,?,?)", (org_name, v, "manual"))
            c.execute(
                "UPDATE supplier_directory SET merged_into=? "
                "WHERE org_name=? AND supplier_name=?", (canonical, org_name, v))
            # A canonical name must never itself be merged away, or lookups
            # would need to follow a chain. Collapse one level instead.
            c.execute(
                "UPDATE supplier_directory SET merged_into=? "
                "WHERE org_name=? AND merged_into=?", (canonical, org_name, v))
        c.execute(
            "UPDATE supplier_directory SET merged_into=NULL "
            "WHERE org_name=? AND supplier_name=?", (org_name, canonical))
        conn.commit()
    finally:
        conn.close()
    _rebuild_merge_suggestions(org_name)


def unmerge_supplier(org_name: str, supplier_name: str):
    execute("UPDATE supplier_directory SET merged_into=NULL "
            "WHERE org_name=? AND supplier_name=?", (org_name, supplier_name))
    _rebuild_merge_suggestions(org_name)
```

- [ ] **Step 6: Run the test and confirm it passes**

Run: `python tests/test_supplier_directory.py`
Expected: all `ok:`.

- [ ] **Step 7: Confirm nothing else broke**

Run: `python run_tests.py`
Expected: `0 failed`. `tests/test_supplier_accuracy.py`, `tests/test_supplier_lead_time.py` and `tests/test_supplier_attribution_guard.py` all exercise `get_supplier_profile` — if any now fails, the rewritten lookup changed behaviour for an unmerged name, which it must not.

---

### Task 5: Archive (delete = hide)

**Files:**
- Modify: `database.py:1225-1235` (`get_supplier_scores`), append `archive_supplier`
- Modify: `tests/test_supplier_directory.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_supplier_directory.py`, before the closing `if _FAILED:` block:

```python
# ---------------------------------------------------------------------------
# Archive: hide from the list, WITHOUT changing what gets recommended
# ---------------------------------------------------------------------------
db.upsert_supplier_profile("OrgA", "KESTREL COLD CHAIN",
                           avg_lead_time_days=30, delay_probability=0.5)
db.archive_supplier("OrgA", "KESTREL COLD CHAIN", True)

_visible = {r["supplier_name"] for r in db.get_supplier_scores("OrgA")}
_check("archived supplier is hidden from the list",
       "KESTREL COLD CHAIN" not in _visible)
_check("archived supplier is listed when hidden are requested",
       "KESTREL COLD CHAIN" in {r["supplier_name"]
                                for r in db.get_supplier_scores("OrgA", include_archived=True)})

_p = db.get_supplier_profile("OrgA", "KESTREL COLD CHAIN")
_check("archiving does NOT change the profile the pipeline reads",
       _p.get("avg_lead_time_days") == 30
       and abs(float(_p.get("delay_probability") or 0) - 0.5) < 1e-9)

db.archive_supplier("OrgA", "KESTREL COLD CHAIN", False)
_check("un-archiving restores the row",
       "KESTREL COLD CHAIN" in {r["supplier_name"] for r in db.get_supplier_scores("OrgA")})

# Archiving a supplier with no profile row yet must still work.
db.archive_supplier("OrgA", "ALDERMOOR SUPPLY", True)
_check("archiving a profile-less supplier creates the row",
       "ALDERMOOR SUPPLY" in {r["supplier_name"]
                              for r in db.get_supplier_scores("OrgA", include_archived=True)})
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python tests/test_supplier_directory.py`
Expected: `AttributeError: module 'database' has no attribute 'archive_supplier'`

- [ ] **Step 3: Implement**

In `database.py`, replace `get_supplier_scores` (lines 1225-1235) with:

```python
def get_supplier_scores(org_name: str, include_archived: bool = False) -> list:
    """Return supplier profiles with scores for an org, best score first.

    Archived suppliers are hidden from the list but keep their profile — the
    pipeline still reads their lead time and delay rate. Hiding is list
    cleanup; it must never silently change what gets recommended.
    """
    sql = (
        "SELECT supplier_name, supplier_type, reliability_score, "
        "       total_recs, orders_placed, stockouts_avoided, stockouts_happened, "
        "       delay_probability, avg_lead_time_days, last_scored_at, notes, "
        "       COALESCE(archived, 0) AS archived "
        "FROM supplier_profiles WHERE org_name=? "
    )
    if not include_archived:
        sql += "AND COALESCE(archived, 0) = 0 "
    sql += "ORDER BY reliability_score DESC, supplier_name ASC"
    return query(sql, (org_name,))
```

Append after `unmerge_supplier`:

```python
def archive_supplier(org_name: str, supplier_name: str, archived: bool = True):
    """Hide (or restore) a supplier row. Creates the profile if absent, so a
    supplier discovered from an upload can be hidden before it is ever edited."""
    upsert_supplier_profile(org_name, supplier_name)
    execute("UPDATE supplier_profiles SET archived=? WHERE org_name=? AND supplier_name=?",
            (1 if archived else 0, org_name, supplier_name))
```

`upsert_supplier_profile` with no keyword arguments inserts a row with just `org_name` and `supplier_name` when none exists, and updates nothing when one does — check that the `sets` string it builds is not empty for the update branch. If `fields` is empty the UPDATE becomes `SET , updated_at=...` and raises. Guard it by changing the update branch of `upsert_supplier_profile` (line 1001) from:

```python
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=CURRENT_TIMESTAMP"
```

to:

```python
        # fields can be empty (archive_supplier calls this purely to ensure the
        # row exists) — an empty SET list is a syntax error.
        sets = "".join(f"{k}=?, " for k in fields) + "updated_at=CURRENT_TIMESTAMP"
```

- [ ] **Step 4: Run the tests**

Run: `python tests/test_supplier_directory.py` → all `ok:`
Run: `python run_tests.py` → `0 failed`

---

### Task 6: Routes

**Files:**
- Modify: `app.py:1460-1482` (delete the two supplier branches from `user_settings`)
- Modify: `app.py:2659` (rebuild after a run completes)
- Modify: `app.py:3502-3510` (`/suppliers`)
- Modify: `tests/test_supplier_directory.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_supplier_directory.py`, before the closing `if _FAILED:` block:

```python
# ---------------------------------------------------------------------------
# Routes: every one org-scoped. Org B must never touch Org A's suppliers.
# ---------------------------------------------------------------------------
os.environ.setdefault("SECRET_KEY", "test-secret-not-used-in-prod")
import app as flask_app  # noqa: E402

flask_app.app.config["WTF_CSRF_ENABLED"] = False
flask_app.app.config["TESTING"] = True

db.execute("INSERT OR IGNORE INTO users (id, email, password_hash, org_name, role, "
           "email_verified, tier, session_version) VALUES (?,?,?,?,?,?,?,?)",
           (7001, "a@example.test", "x", "OrgA", "user", 1, "enterprise", 0))
db.execute("INSERT OR IGNORE INTO users (id, email, password_hash, org_name, role, "
           "email_verified, tier, session_version) VALUES (?,?,?,?,?,?,?,?)",
           (7002, "b@example.test", "x", "OrgB", "user", 1, "enterprise", 0))


def _client_for(user_id, org):
    c = flask_app.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = user_id
        s["org_name"] = org
        s["role"] = "user"
        s["sv"] = 0
    return c


_ca = _client_for(7001, "OrgA")
_cb = _client_for(7002, "OrgB")

_r = _ca.get("/suppliers")
_check("owner can load /suppliers", _r.status_code == 200, str(_r.status_code))

# OrgB tries to flag OrgA's supplier as unreliable.
_cb.post("/suppliers/flag", data={"supplier_name": "NORDVIK TRADING PTE LTD",
                                  "unreliable": "1"})
_check("cross-org flag does not touch OrgA's profile",
       abs(float(db.get_supplier_profile(
           "OrgA", "NORDVIK TRADING PTE LTD").get("delay_probability") or 0) - 0.5) < 1e-9)

# OrgB tries to archive OrgA's supplier.
_cb.post("/suppliers/archive", data={"supplier_name": "NORDVIK TRADING PTE LTD",
                                     "archived": "1"})
_check("cross-org archive leaves OrgA's list intact",
       "NORDVIK TRADING PTE LTD" in {r["supplier_name"]
                                     for r in db.get_supplier_scores("OrgA")})

# OrgB tries to merge OrgA's suppliers.
_cb.post("/suppliers/merge", data={"canonical": "NORDVIK TRADING PTE LTD",
                                   "variants": "KESTREL COLD CHAIN"})
_check("cross-org merge does not link OrgA's suppliers",
       not [r for r in db.get_supplier_directory("OrgA")
            if r["supplier_name"] == "KESTREL COLD CHAIN" and r["merged_into"]])

# The owner's own flag works, and lands on the value the buffer logic reads.
_ca.post("/suppliers/flag", data={"supplier_name": "ALDERMOOR SUPPLY", "unreliable": "1"})
_check("owner's unreliable flag writes 0.5",
       abs(float(db.get_supplier_profile(
           "OrgA", "ALDERMOOR SUPPLY").get("delay_probability") or 0) - 0.5) < 1e-9)
_ca.post("/suppliers/flag", data={"supplier_name": "ALDERMOOR SUPPLY"})
_check("clearing the flag writes 0.2",
       abs(float(db.get_supplier_profile(
           "OrgA", "ALDERMOOR SUPPLY").get("delay_probability") or 0) - 0.2) < 1e-9)

# Settings must no longer accept supplier writes — that moved to /suppliers.
_ca.post("/settings", data={"action": "delete_supplier",
                            "supplier_name": "NORDVIK TRADING PTE LTD"})
_check("settings no longer deletes suppliers",
       db.query("SELECT 1 FROM supplier_profiles WHERE org_name=? AND supplier_name=?",
                ("OrgA", "NORDVIK TRADING PTE LTD")) != [])
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python tests/test_supplier_directory.py`
Expected: the cross-org checks fail or the POSTs 404, because the routes do not exist yet.

- [ ] **Step 3: Rebuild the directory when a run completes**

In `app.py`, after line 2659:

```python
            db.execute("UPDATE upload_sessions SET status='complete' WHERE id=?", (upload_session_id,))
```

insert:

```python
            # Refresh the supplier list from what was just ingested. Its own
            # try/except: a directory failure must never fail a completed run.
            try:
                _sess_org = db.query(
                    "SELECT org_name FROM upload_sessions WHERE id=?", (upload_session_id,))
                if _sess_org:
                    db.rebuild_supplier_directory(_sess_org[0]["org_name"])
            except Exception:
                logger.warning("Supplier-directory rebuild failed for session %s",
                               upload_session_id, exc_info=True)
```

- [ ] **Step 4: Delete the supplier branches from `user_settings`**

In `app.py`, delete lines 1460-1482 entirely — the `if action == "save_supplier":` and `elif action == "delete_supplier":` blocks. The next branch, `elif action == "change_password":`, becomes the first branch and must be changed to `if action == "change_password":`.

Also remove `profiles = db.get_supplier_profiles(org)` (line 1636) and the `profiles=profiles` argument from that route's `render_template` call, since the template no longer renders them.

- [ ] **Step 5: Replace the `/suppliers` route**

In `app.py`, replace lines 3502-3510 with:

```python
@app.route("/suppliers")
@login_required
def suppliers_page():
    org = session["org_name"]
    # Lazy backfill: an org that has not run an analysis since this shipped has
    # an empty directory. Rebuild once, here, rather than on every page view —
    # the fuzzy pass is O(n^2) over the name list.
    if not db.get_supplier_directory(org):
        try:
            db.rebuild_supplier_directory(org)
        except Exception:
            logger.warning("Supplier-directory backfill failed for org %s", org, exc_info=True)

    show_hidden = request.args.get("hidden") == "1"
    scores = {r["supplier_name"]: r for r in db.get_supplier_scores(org, include_archived=True)}
    directory = db.get_supplier_directory(org)

    # Names folded into a canonical row, so that row can list them and offer undo.
    merged_by_canonical = {}
    for d in directory:
        if d["merged_into"]:
            merged_by_canonical.setdefault(d["merged_into"], []).append(d["supplier_name"])

    rows, hidden_count = [], 0
    seen = set()
    for d in directory:
        if d["merged_into"]:
            continue  # folded into its canonical row
        name = d["supplier_name"]
        seen.add(name)
        prof = scores.get(name, {})
        if prof.get("archived"):
            hidden_count += 1
            if not show_hidden:
                continue
        rows.append(_supplier_row(name, d["source"], prof,
                                  merged_by_canonical.get(name, [])))

    # Profiles with no directory entry: typed by hand and matching nothing in
    # any upload, so they have been doing nothing. Surface them instead of
    # hiding them — this is the silent failure the directory exists to end.
    for name, prof in scores.items():
        if name in seen or name == "Unknown":
            continue
        if prof.get("archived"):
            hidden_count += 1
            if not show_hidden:
                continue
        rows.append(_supplier_row(name, None, prof, merged_by_canonical.get(name, [])))

    # Most-used first: the handful that matter sit above the long tail.
    rows.sort(key=lambda r: (-(r["total_recs"] or 0), r["supplier_name"].lower()))

    return render_template(
        "suppliers.html",
        suppliers=rows,
        suggestions=db.get_merge_suggestions(org),
        outcome_stats=db.get_outcome_stats(org),
        hidden_count=hidden_count,
        show_hidden=show_hidden,
        org_name=org,
    )


def _supplier_row(name, source, prof, merged_names=()):
    """One render-ready supplier row. `source` is None for a profile that no
    upload has ever mentioned."""
    delay = prof.get("delay_probability")
    return {
        "supplier_name":      name,
        "source":             source,
        "merged_names":       list(merged_names),
        "reliability_score":  prof.get("reliability_score"),
        "total_recs":         prof.get("total_recs") or 0,
        "orders_placed":      prof.get("orders_placed") or 0,
        "stockouts_avoided":  prof.get("stockouts_avoided") or 0,
        "stockouts_happened": prof.get("stockouts_happened") or 0,
        "avg_lead_time_days": prof.get("avg_lead_time_days"),
        "supplier_type":      prof.get("supplier_type"),
        "notes":              prof.get("notes"),
        "delay_probability":  delay,
        # Same threshold that selects the 2.5-month safety buffer, so a raw
        # value set by hand always displays consistently with what it does.
        "unreliable":         float(delay) > 0.35 if delay is not None else False,
        "archived":           bool(prof.get("archived")),
    }


@app.route("/suppliers/flag", methods=["POST"])
@login_required
def suppliers_flag():
    org = session["org_name"]
    name = request.form.get("supplier_name", "").strip()
    if name:
        unreliable = request.form.get("unreliable") == "1"
        db.upsert_supplier_profile(org, name,
                                   delay_probability=0.5 if unreliable else 0.2,
                                   data_quality_score=0.8)  # user-stated = high confidence
    return redirect(url_for("suppliers_page"))


@app.route("/suppliers/save", methods=["POST"])
@login_required
def suppliers_save():
    org = session["org_name"]
    name = request.form.get("supplier_name", "").strip()
    if name:
        try:
            db.upsert_supplier_profile(
                org, name,
                supplier_type      = request.form.get("supplier_type", "other"),
                avg_lead_time_days = int(request.form.get("avg_lead_time_days", 56)),
                delay_probability  = float(request.form.get("delay_probability", 0.2)),
                data_quality_score = 0.8,
                notes              = request.form.get("notes", ""),
            )
            flash(f"Saved {name}.", "success")
        except (ValueError, TypeError):
            flash("Lead time must be a whole number and delay rate a number between 0 and 1.", "error")
    return redirect(url_for("suppliers_page"))


@app.route("/suppliers/archive", methods=["POST"])
@login_required
def suppliers_archive():
    org = session["org_name"]
    name = request.form.get("supplier_name", "").strip()
    archived = request.form.get("archived", "1") == "1"
    if name:
        db.archive_supplier(org, name, archived)
        flash(f"{'Hid' if archived else 'Restored'} {name}. "
              "Items from this supplier are still recommended.", "success")
    # After restoring, stay on the hidden view — the user is working through it.
    return redirect(url_for("suppliers_page", hidden=None if archived else "1"))


@app.route("/suppliers/merge", methods=["POST"])
@login_required
def suppliers_merge():
    """Serves both the suggestion banner and manual selection from the list.

    Both post the same two fields, so there is one code path: `canonical` picks
    the name to keep, `variants` lists every name in the merge (the canonical
    included — merge_suppliers skips it).
    """
    org = session["org_name"]
    canonical = request.form.get("canonical", "").strip()
    variants = [v.strip() for v in request.form.getlist("variants") if v.strip()]
    others = [v for v in variants if v != canonical]
    if not canonical or not others:
        flash("Pick at least two suppliers and choose which name to keep.", "error")
        return redirect(url_for("suppliers_page"))
    db.merge_suppliers(org, canonical, others)
    flash(f"Merged {len(others)} name(s) into {canonical}. Undo from the row's Details panel.",
          "success")
    return redirect(url_for("suppliers_page"))


@app.route("/suppliers/unmerge", methods=["POST"])
@login_required
def suppliers_unmerge():
    """Undo one merge. A wrong merge is a real risk — the suffix pass groups
    same-first-word companies on purpose — so this must be reachable from the UI,
    not only the database."""
    org = session["org_name"]
    name = request.form.get("supplier_name", "").strip()
    if name:
        db.unmerge_supplier(org, name)
        flash(f"{name} is a separate supplier again.", "success")
    return redirect(url_for("suppliers_page"))


@app.route("/suppliers/ignore_group", methods=["POST"])
@login_required
def suppliers_ignore_group():
    org = session["org_name"]
    key = request.form.get("group_key", "").strip()
    if key:
        db.ignore_merge_group(org, key)
    return redirect(url_for("suppliers_page"))
```

Every route reads `session["org_name"]` and never a form-supplied org — that is what makes the cross-org tests pass. Do not add an `org` form field.

- [ ] **Step 6: Run the tests**

Run: `python tests/test_supplier_directory.py` → all `ok:`
Run: `python run_tests.py` → `0 failed`

If `tests/test_cross_org_untested_routes.py` or `tests/test_ops_guards.py` fails, a route was added without `@login_required` or the settings edit removed something still referenced.

---

### Task 7: Templates

**Files:**
- Modify: `templates/suppliers.html` (full rewrite of the table + new banner)
- Modify: `templates/settings.html` (remove the supplier-profiles card)

- [ ] **Step 1: Rewrite the `/suppliers` table**

Two columns are added for manual merge: a radio (`canonical`) and a checkbox (`variants`). Both carry `form="mergeForm"`, an HTML5 attribute that binds an input to a form it is not nested inside — necessary because the per-row Hide and flag forms already live in the row, and HTML forbids nested forms. No JavaScript.

In `templates/suppliers.html`, replace the `<table class="data-table">` block (lines 27-81) with:

```html
      <thead>
        <tr>
          <th title="Merge: tick the duplicates, then pick which name to keep">Merge</th>
          <th>Keep</th>
          <th>Supplier</th>
          <th>Reliability</th>
          <th title="Adds a month of extra cover to this supplier's orders">Unreliable</th>
          <th>Recs</th>
          <th>Lead time</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for s in suppliers %}
        <tr>
          <td><input type="checkbox" form="mergeForm" name="variants" value="{{ s.supplier_name }}"
                     aria-label="Select {{ s.supplier_name }} to merge"></td>
          <td><input type="radio" form="mergeForm" name="canonical" value="{{ s.supplier_name }}"
                     aria-label="Keep the name {{ s.supplier_name }}"></td>
          <td style="font-weight:500;">
            {{ s.supplier_name }}
            {% if s.source == 'sales' %}
            <span class="sup-tag">from sales file — unconfirmed</span>
            {% elif s.source is none %}
            <span class="sup-tag">not seen in any upload</span>
            {% endif %}
            {% for m in s.merged_names %}
            <span class="sup-tag">
              also "{{ m }}"
              <form method="POST" action="{{ url_for('suppliers_unmerge') }}" style="display:inline;">
                <input type="hidden" name="supplier_name" value="{{ m }}">
                <button type="submit" class="link-btn" style="font-size:11px;">undo</button>
              </form>
            </span>
            {% endfor %}
          </td>
          <td>
            {% set score = s.reliability_score or 50 %}
            <span class="sup-score
              {% if score >= 70 %}sup-score-good
              {% elif score >= 40 %}sup-score-ok
              {% else %}sup-score-bad{% endif %}">{{ score | round(0) | int }}</span>
            <span class="sup-score-bar">
              <span class="sup-score-fill
                {% if score >= 70 %}fill-good
                {% elif score >= 40 %}fill-ok
                {% else %}fill-bad{% endif %}" style="width: {{ score }}%;"></span>
            </span>
          </td>
          <td>
            <form method="POST" action="{{ url_for('suppliers_flag') }}">
              <input type="hidden" name="supplier_name" value="{{ s.supplier_name }}">
              <input type="hidden" name="unreliable" value="{{ '0' if s.unreliable else '1' }}">
              <button type="submit" class="sup-flag {% if s.unreliable %}sup-flag-on{% endif %}">
                {{ 'Unreliable' if s.unreliable else 'Mark' }}
              </button>
            </form>
          </td>
          <td>{{ s.total_recs }}</td>
          <td>{{ s.avg_lead_time_days or '—' }}{% if s.avg_lead_time_days %}d{% endif %}</td>
          <td style="display:flex; gap:8px;">
            <button type="button" class="btn btn-muted btn-sm"
                    onclick="prefillEdit('{{ s.supplier_name }}',
                      '{{ s.supplier_type or '' }}',
                      {{ s.avg_lead_time_days or 56 }},
                      {{ s.delay_probability or 0.2 }},
                      '{{ (s.notes or '')|replace("'", "\\'")|replace('"','&quot;') }}')">
              Edit
            </button>
            <form method="POST" action="{{ url_for('suppliers_archive') }}"
                  data-confirm="Hide {{ s.supplier_name }}? Its items are still recommended."
                  data-confirm-label="Hide">
              <input type="hidden" name="supplier_name" value="{{ s.supplier_name }}">
              <input type="hidden" name="archived" value="{{ '0' if s.archived else '1' }}">
              <button type="submit" class="btn btn-muted btn-sm">{{ 'Restore' if s.archived else 'Hide' }}</button>
            </form>
          </td>
        </tr>
        {% endfor %}
        <tr id="supplierNoMatch" style="display:none;">
          <td colspan="8" style="text-align:center; color:var(--muted); padding:24px;">No suppliers match your search.</td>
        </tr>
      </tbody>
```

The search script at the bottom of the file must now read the NAME column, which moved from cell 0 to cell 2. Change `r.cells[0]` to `r.cells[2]` in that script.

- [ ] **Step 1b: Add the manual-merge bar**

Immediately AFTER the closing `</div>` of the card holding the table, insert:

```html
  <form method="POST" action="{{ url_for('suppliers_merge') }}" id="mergeForm"
        style="margin-top:12px; display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
    <button type="submit" class="btn btn-sm">Merge selected</button>
    <span style="color:var(--muted); font-size:13px;">
      Tick every spelling of the same supplier, choose which name to keep, then merge.
      Your uploaded files are not changed.
    </span>
  </form>
```

This is the guarantee that the automatic passes do not have to be good: anything they miss is two ticks and a click, and it stays fixed.

- [ ] **Step 2: Add the duplicate banner**

In `templates/suppliers.html`, immediately after the `<section class="hero">` block (after line 16), insert:

```html
  {% if suggestions %}
  <div class="card" style="margin-top:14px; border-color:var(--brass);">
    <div class="card-title" style="font-size:14px;">
      {{ suggestions | length }} possible duplicate{{ '' if suggestions | length == 1 else 's' }}
    </div>
    <p style="color:var(--muted); font-size:13px; margin-bottom:14px;">
      These look like one supplier written more than one way. Merging makes them share one
      profile and one reliability score. Nothing in your uploaded files changes.
    </p>
    {% for g in suggestions %}
    <form method="POST" action="{{ url_for('suppliers_merge') }}" class="dup-group">
      {% for name, reason in g.members.items() %}
      <label class="dup-row">
        <input type="radio" name="canonical" value="{{ name }}" {% if loop.first %}checked{% endif %}>
        <span class="dup-name">{{ name }}</span>
        <span class="sup-tag">{{ reason }}</span>
        <input type="hidden" name="variants" value="{{ name }}">
      </label>
      {% endfor %}
      <div style="margin-top:8px; display:flex; gap:10px; align-items:center;">
        <button type="submit" class="btn btn-sm">Merge — keep the selected name</button>
      </div>
    </form>
    <form method="POST" action="{{ url_for('suppliers_ignore_group') }}" style="margin:-6px 0 18px;">
      <input type="hidden" name="group_key" value="{{ g.group_key }}">
      <button type="submit" class="link-btn">Not the same</button>
    </form>
    {% endfor %}
  </div>
  {% endif %}
```

`variants` is submitted for every member including the chosen canonical; `merge_suppliers` skips `v == canonical`, so no extra client-side work is needed.

- [ ] **Step 3: Add the hidden-suppliers link**

In `templates/suppliers.html`, immediately after the search-box `<div>` (line 25), insert:

```html
  {% if hidden_count %}
  <p style="margin-top:10px; font-size:13px;">
    <a href="{{ url_for('suppliers_page', hidden='0' if show_hidden else '1') }}" class="link-btn">
      {{ 'Back to visible suppliers' if show_hidden else 'Show hidden (' ~ hidden_count ~ ')' }}
    </a>
  </p>
  {% endif %}
```

- [ ] **Step 4: Add the styles**

In `templates/suppliers.html`, inside the existing `<style>` block, append:

```css
.sup-tag {
  font-size: 11px;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 99px;
  padding: 1px 7px;
  margin-left: 6px;
  white-space: nowrap;
}
.sup-flag {
  background: none;
  border: 1px solid var(--border);
  border-radius: 99px;
  color: var(--muted);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  padding: 4px 9px;
}
.sup-flag-on { color: var(--danger, #8B2C2C); border-color: var(--danger, #8B2C2C); }
.dup-group { border-left: 2px solid var(--border); padding-left: 12px; margin-bottom: 6px; }
.dup-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; cursor: pointer; }
.dup-name { font-weight: 500; }
```

- [ ] **Step 4b: Move the add/edit form onto the suppliers page**

The Edit button calls `prefillEdit`, which already exists and works in `templates/settings.html`. Move it rather than write a new panel — this is the raw delay-rate box the checkbox cannot express, and it is where the 0.5-month buffer band (`delay_probability <= 0.15`) stays reachable.

In `templates/suppliers.html`, immediately after the merge bar from Step 1b, insert:

```html
  <div class="card" style="margin-top:20px;">
    <form method="POST" action="{{ url_for('suppliers_save') }}" id="supplierForm">
      <div style="font-size:12px; font-weight:600; color:var(--muted); text-transform:uppercase;
                  letter-spacing:0.07em; margin-bottom:14px;" id="formLabel">Add supplier</div>
      <div style="display:grid; grid-template-columns:2fr 1fr 1fr 1fr 3fr auto; gap:12px; align-items:end;">
        <div class="form-group" style="margin:0;">
          <label style="font-size:13px;">Supplier name</label>
          <input type="text" name="supplier_name" id="inp-name" required placeholder="e.g. Nordvik Trading">
        </div>
        <div class="form-group" style="margin:0;">
          <label style="font-size:13px;">Type</label>
          <select name="supplier_type" id="inp-type">
            <option value="import">Import</option>
            <option value="local">Local</option>
            <option value="other">Other</option>
          </select>
        </div>
        <div class="form-group" style="margin:0;">
          <label style="font-size:13px;">Lead time (days)</label>
          <input type="number" name="avg_lead_time_days" id="inp-lead" step="1" min="1" max="365" value="56">
        </div>
        <div class="form-group" style="margin:0;">
          <label style="font-size:13px;">Delay rate
            <span class="hint">0 to 1. Above 0.35 orders an extra month of cover.</span>
          </label>
          <input type="number" name="delay_probability" id="inp-delay" step="0.05" min="0" max="1" value="0.2">
        </div>
        <div class="form-group" style="margin:0;">
          <label style="font-size:13px;">Notes</label>
          <input type="text" name="notes" id="inp-notes" placeholder="e.g. slow on chilled lines">
        </div>
        <button type="submit" class="btn btn-sm">Save</button>
      </div>
    </form>
  </div>
```

And append to the page's `<script>` block, inside the existing IIFE or after it:

```javascript
function prefillEdit(name, type, lead, delay, notes) {
  document.getElementById('inp-name').value = name;
  document.getElementById('inp-type').value = type || 'other';
  document.getElementById('inp-lead').value = lead;
  document.getElementById('inp-delay').value = delay;
  document.getElementById('inp-notes').value = notes || '';
  document.getElementById('formLabel').textContent = 'Edit ' + name;
  document.getElementById('supplierForm').scrollIntoView({behavior: 'smooth', block: 'center'});
}
```

`prefillEdit` sets `.value` and `.textContent` only — never `innerHTML`. Supplier names are client-uploaded file content, and the 21 Jul 2026 XSS fix exists because a supplier name reached an HTML sink. Do not regress that.

- [ ] **Step 5: Remove the supplier card from Settings**

In `templates/settings.html`, delete the whole supplier-profiles card — from `<!-- Supplier profiles -->` (line 11) through the closing `</div>` of that card (just before `<!-- Default lead times -->` around line 113). Keep everything from the default-lead-times card onward.

Delete the `prefillEdit` JavaScript function and the `#supplierForm` handling from the page's `<script>` block, and update the page's intro line (line 8) from:

```html
    <p>Manage supplier profiles for {{ session.org_name }}. These inform how the AI assesses risk and lead times in every analysis.</p>
```

to:

```html
    <p>Default lead times and account settings for {{ session.org_name }}. Suppliers are managed on the <a href="{{ url_for('suppliers_page') }}">Suppliers</a> page.</p>
```

Also delete the "How the AI uses these profiles" explainer card (lines 298-306) — it describes the form that just moved.

- [ ] **Step 6: Verify the templates render**

Run:
```
python tests/verify_results_render.py
```
Expected: PASS (it renders templates through the Flask test client and catches Jinja errors).

Run: `python run_tests.py`
Expected: `0 failed`.

- [ ] **Step 7: Screenshot the page**

Create `tools_shot_suppliers.py` in the scratch directory (NOT in the repo) and run it:

```python
import os, re, sys, tempfile
ROOT = r"C:\BerthAI\BerthAI"
sys.path.insert(0, ROOT)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "berthcast_supdir.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
os.environ.setdefault("SECRET_KEY", "shot-only")
import app as flask_app

flask_app.app.config["TESTING"] = True
c = flask_app.app.test_client()
with c.session_transaction() as s:
    s["user_id"], s["org_name"], s["role"], s["sv"] = 7001, "OrgA", "user", 0
html = c.get("/suppliers").get_data(as_text=True)

# Absolute CSS/JS URLs will not resolve from file:// — strip them and inline
# nothing; the page's own <style> block is what we are checking.
html = re.sub(r'<link[^>]+rel="stylesheet"[^>]*>', "", html)
out = os.path.join(os.environ.get("CLAUDE_JOB_DIR", tempfile.gettempdir()), "suppliers.html")
open(out, "w", encoding="utf-8").write(html)
print(out)
```

Run the test file first so the temp DB holds the seeded OrgA data (`python tests/test_supplier_directory.py`), then:

```
msedge.exe --headless --disable-gpu --virtual-time-budget=9000 --window-size=1400,1000 --screenshot=<ABSOLUTE>\suppliers.png "file:///<ABSOLUTE>/suppliers.html"
```

The screenshot path MUST be absolute. Read the PNG and check: the Merge/Keep columns read as controls rather than noise, the duplicate banner does not dominate the page, and the "from sales file — unconfirmed" tag reads as secondary to the supplier name.

---

### Task 8: Gates and handoff

No commits happen before this task. The pre-commit hook blocks `app.py` / `database.py` until a security review is recorded, so the review must be genuinely run here.

- [ ] **Step 1: Full suite**

Run: `python run_tests.py`
Expected: `0 failed`. Paste the tail of the output — a claim is not evidence.

- [ ] **Step 2: Security review**

Run the `security-reviewer` agent over the diff. It must specifically confirm:
- every new route is scoped by `session["org_name"]` and never a form value
- the only SQL interpolation is the int session id in per-session table names, and those ints come from `upload_sessions`, never a request
- supplier names (client-uploaded, untrusted) reach the page through Jinja autoescape and never an `innerHTML` sink
- merge/archive/flag are POST-only and covered by the global CSRF

Show the verdict. Fix anything CRITICAL or HIGH, then record it:
```
python .claude/hooks/check_security_review.py --approve
```

- [ ] **Step 3: Multi-file review**

Run the `cavecrew-reviewer` agent on the diff (6 files — well past the single-file threshold). Fix CRITICAL/HIGH findings. Report in one line what it flagged and what was done.

- [ ] **Step 4: Live smoke**

`database.py` changed, and the suite stubs Claude, so the suite passing is not sufficient.

Run: `python smoke_live.py`
Expected: `PASSED`. Show the output. Roughly 3 minutes and US$0.10-0.40 of real API usage.

- [ ] **Step 5: Redaction sweep**

Re-read the full diff. Confirm no client name, real supplier name, staff email, financials, or line-of-business detail appears in any tracked file or in the commit message. Test data must be invented brands only.

- [ ] **Step 6: Hand the user the commit**

Do NOT run these. Output them for the user to paste:

```powershell
cd c:\BerthAI\BerthAI
git add supplier_directory.py database.py app.py templates/suppliers.html templates/settings.html tests/test_supplier_directory.py docs/superpowers/plans/2026-07-26-supplier-directory.md
git commit -m "Supplier list built from uploads, with duplicate merge"
git push
```

- [ ] **Step 7: Update MEMORY.md**

Add a dated entry recording what shipped, the accepted false-positive in the suffix pass, and the deliberate duplication of the plan-008 blocky check in `database.py`. `MEMORY.md` is gitignored — never stage it.

---

## Known limits, accepted on purpose

- **The suffix pass suggests some wrong groups.** `PADIMAS TRADING` and `PADIMAS ENTERPRISE` both strip to `padimas`. Accepted: it is the same rule that catches `NORDVIK` / `NORDVIK TRADING`, no merge applies without confirmation, and the reason line makes the mistake obvious in one read.
- **The blocky-column check is duplicated** between `agents/recommendation.py:233-237` and `database.py._sales_supplier_names`. Extracting it would put a shipped guard on a live pilot at risk for no functional gain.
- **Archived suppliers still influence recommendations.** Their profile is still read. Documented in the UI copy on the Hide confirmation.
- **A drift rename can orphan a profile.** When a later upload spells a supplier more completely (`Nordvik` → `Nord-vik`, same normalised key), the directory row is renamed. A `supplier_profiles` row saved under the old spelling then no longer joins to it, so the page shows the supplier once without its flag and once tagged "not seen in any upload". Visible and fixable by merging the two — not silent — so it is accepted for v1 rather than adding profile-rename cascade logic.
- **Merge chains collapse one level only.** Merging B into A when C is already merged into B repoints C at A at merge time; there is no recursive resolution at lookup time, deliberately, so the pipeline lookup stays one query.
- **No Claude call for duplicate detection.** Deferred until the deterministic passes are measured against the real supplier list. `agents/normalization.py` is the pattern to copy if that measurement justifies it.
