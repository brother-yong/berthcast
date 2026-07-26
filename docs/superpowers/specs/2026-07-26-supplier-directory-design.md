# Supplier directory — design

**Date:** 2026-07-26
**Status:** approved, ready for implementation plan

## Problem

Supplier profiles already exist and already reach the pipeline, but nobody fills them in.

What works today:

- `templates/settings.html` has a supplier-profile card: add / edit / delete, with type, lead time, delay rate and notes.
- `agents/shared.py:870` sets `high_risk = delay_prob > 0.30`.
- `agents/recommendation.py:356-361` sizes the safety buffer from the same number: 0.5 months if `delay_prob <= 0.15`, 1.5 if `<= 0.35`, else 2.5.
- `/suppliers` renders a read-only reliability scoreboard built from recorded outcomes.

Three things break it in practice:

1. **The profile list starts empty and is typed by hand.** A client with hundreds of suppliers will never populate it, so the delay-rate machinery above sits inert.
2. **`get_supplier_profile` (`database.py:972-990`) is a raw exact-string match** on `supplier_name`. It does not go through `NameKeyedDict` (`agents/shared.py:573`), which the item→supplier maps do use. So a profile typed as `Nordvik` against a file spelling `NORDVIK TRADING PTE LTD` silently resolves to defaults, with no error anywhere.
3. **Editable profiles live in Settings, scores live on `/suppliers`.** Two pages, one subject.

## Goal

Make the supplier list build itself out of what the client already uploaded, so flagging an unreliable supplier is one click on a name that is guaranteed to match the files.

## Design

### 1. Supplier directory

New table:

```sql
CREATE TABLE IF NOT EXISTS supplier_directory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_name TEXT NOT NULL,
    supplier_name TEXT NOT NULL,
    source TEXT,              -- 'listing' | 'po' | 'sales'
    merged_into TEXT,         -- canonical supplier_name, NULL when not merged
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_name, supplier_name)
);
CREATE INDEX IF NOT EXISTS idx_supplier_directory_org ON supplier_directory(org_name);
```

One rebuild function scans that org's completed sessions and collects distinct supplier names:

| Source table | `source` | Trust |
|---|---|---|
| `suppliers_<id>` | `listing` | high — the client's own supplier master |
| `purchase_orders_<id>` | `po` | high — a PO row is a real trading relationship |
| sales file | `sales` | low — only if the column passes the plan-008 blocky gate |

Session ids come from `SELECT id FROM upload_sessions WHERE org_name=? AND status='complete'`, so they are ints straight from the DB — the one interpolation into a table name the conventions allow. Cap the session sweep (most recent 25) and each per-table read, per the 512 MB Render ceiling.

`source` records where a name was **first** seen. A name later confirmed by a PO upgrades from `sales` to `po`; it never downgrades.

Called from two places:

- After each analysis run, beside the existing `db.update_supplier_scores(org_name)` call at `app.py:3477-3482`, in the same non-blocking try/except.
- On `/suppliers` load when the org's directory is empty — a lazy backfill so existing clients see their suppliers without waiting for a new upload.

The page then reads one indexed table. It must not scan per-session tables on every view.

### 2. `/suppliers` becomes the only supplier page

Settings drops its supplier-profile card and keeps the default-lead-times card. The `save_supplier` / `delete_supplier` actions move to `/suppliers` routes.

Row: **name · source tag · reliability score · `[ ] unreliable` · lead time · notes · Delete**

- The **unreliable** checkbox writes `delay_probability` — `0.50` checked, `0.20` unchecked — via the existing `upsert_supplier_profile`, which inserts when no profile row exists. It renders as checked when `delay_probability > 0.35`, i.e. the same threshold that selects the 2.5-month buffer band, so a raw value set by hand always displays consistently. No pipeline change is needed: `high_risk` and the safety buffer already read that column.
- Expanding a row reveals lead time, supplier type, notes and the raw delay-rate number. That panel has to exist once Settings loses its form, and it keeps the 0.5-month buffer band (`delay_prob <= 0.15`) reachable, which the checkbox alone cannot express.
- Default sort: most PO rows behind the name first, so the handful that matter sit above the long tail. The existing search box stays.
- Names carrying `source = 'sales'` render a muted "from sales file — unconfirmed" tag. Plan 008 exists because that column mislabelled 39 items in the messy-upload test; the names are worth listing but must not look equal to a PO-confirmed supplier.
- **Profiles with no matching directory entry are listed too**, tagged "not seen in any upload". This is problem 3 above made visible instead of silent — a dead hand-typed profile can finally be spotted and deleted.

### 3. Delete = hide

```sql
ALTER TABLE supplier_profiles ADD COLUMN archived INTEGER DEFAULT 0
```

Additive migration in `init_db()`'s list, matching the existing pattern. Archived suppliers disappear from the list and stay gone across future uploads. A "Show hidden (N)" link restores them.

Archiving deliberately does **not** change recommendations. Items from a hidden supplier still get recommended, and a hidden supplier's unreliable flag still applies. Hiding is list cleanup. A delete button that silently suppresses reorder advice is the failure mode that loses a client's trust.

Deleting a supplier that has no profile row creates one carrying `archived=1`.

### 4. Duplicate detection

Two distinct problems:

**Drift** — `NORDVIK` / `Nordvik ` / `Nord-vik`. `normalise_match_key` (`agents/shared.py:567`) already collapses case, spacing and punctuation. Deterministic and safe: merge these at rebuild time without asking.

**Judgment** — `NORDVIK` / `NORDVIK TRADING` / `NORDVIK TRADING PTE LTD`. Normalised keys still differ. Suggest only, never auto-merge: any rule loose enough to join these also joins `PADIMAS TRADING` and `PADIMAS ENTERPRISE`, which are two different companies.

Detection is pure Python, no Claude call. Strip a suffix list (`PTE LTD`, `LTD`, `LLP`, `SDN BHD`, `CO`, `TRADING`, `ENTERPRISE`, `IMPORT`, `EXPORT`, `INTL`, `& SONS`) from the normalised key, bucket every name by the stripped key, and treat any bucket holding 2+ raw names as a suggestion group. O(n) over the directory, cheap enough to run on page load, nothing stored. An API call here would add cost per page view and a nondeterministic answer to a question the user settles by reading two names — Claude judges, Python does the arithmetic.

UI: a banner — "3 possible duplicates — review" — expanding to each group with a radio to choose the canonical name (default: most PO rows behind it) and two buttons, **Merge** and **Not the same**.

**Merge must reach the pipeline, or it is decoration.** On merge, every variant row gets `merged_into = <canonical>`. Then:

- `get_supplier_profile` gains a single fallback query **on the miss path only**: no exact profile row → look up `merged_into` in `supplier_directory` → retry once against the canonical name. So a PO row reading `NORDVIK` picks up the `NORDVIK TRADING PTE LTD` profile — lead time, unreliable flag, notes.
- `update_supplier_scores` resolves names through the same map before aggregating, so three split reliability scores collapse into one real one.

**"Not the same" must persist**, or the dismissed group reappears on every page load and the banner gets ignored. `group_key` is the suffix-stripped key that formed the bucket, so a new variant of an already-dismissed group stays dismissed:

```sql
CREATE TABLE IF NOT EXISTS supplier_merge_ignored (
    org_name TEXT NOT NULL,
    group_key TEXT NOT NULL,
    UNIQUE(org_name, group_key)
);
```

Merging is reversible — unmerge clears `merged_into`. Nothing in the client's uploaded files is touched; only name resolution changes.

## Security

- Every new route is org-scoped off `session["org_name"]`. Org A must never see or mutate Org B's suppliers.
- Parameterized SQL throughout. The only interpolation is int session ids into per-session table names during the rebuild.
- Supplier names are client-uploaded file content — untrusted. They render through Jinja autoescape and must never be concatenated into an HTML sink; `showToast` / `showConfirm` take text nodes only (fixed 21 Jul 2026, do not regress).
- Merge and archive are POST with CSRF, never GET.

## Testing

New `tests/test_supplier_directory.py`, following the standalone-script pattern (`DB_PATH` env before imports, stub `anthropic`, `_check(name, cond)`, `sys.exit(1)` on failure). Invented brand names only.

Cases:

1. Rebuild discovers names from all three sources with correct `source` values.
2. A non-blocky sales supplier column is rejected (plan-008 gate).
3. Drift variants collapse to one directory row.
4. `NORDVIK` / `NORDVIK TRADING PTE LTD` are suggested as a group; `PADIMAS TRADING` / `PADIMAS ENTERPRISE` are not.
5. A dismissed group does not reappear after rebuild.
6. After merge, `get_supplier_profile("NORDVIK")` returns the canonical profile.
7. The unreliable checkbox writes `0.50`, and a rec for that supplier is sized on a 2.5-month buffer.
8. Archived suppliers are hidden from the list; their items still receive recommendations.
9. Cross-org isolation on every new route.

## Gates before merge

Touches `app.py`, `database.py` and two templates:

- `python run_tests.py` → 0 failed
- security-reviewer (enforced by the pre-commit hook for `app.py` / `database.py`)
- cavecrew-reviewer on the multi-file diff
- `python smoke_live.py` — `database.py` changes, and the suite stubs Claude
- redaction sweep: no client, supplier or product identifiers in the diff or commit message

## Explicitly out of scope

- Any Claude call for duplicate detection.
- Auto-merging judgment-class duplicates.
- Archiving suppressing recommendations.
- Cross-client supplier reliability ("unreliable across N importers") — parked until a second paying client exists.
