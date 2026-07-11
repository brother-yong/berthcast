# Smart Sales-File Ingestion ("AI maps, Python converts")

**Date:** 11 July 2026
**Status:** Approved (design discussion 11 Jul 2026)

## Problem

A pilot client's sales report is a planning grid — one row per item, twelve
month columns (JAN–DEC), a merged "TOTAL SALES QTY" header sitting over the
first month column, supplier/lead-time filled only on the first row of each
supplier block, H2 cells hand-typed projections, some cells formulas or
text zeros. Today's ingestion read the January column as the whole year's
sales, assumed 12 months of data, and produced a full-price analysis run
(~$2.68, ~30 min) with 0 recommendations and a false "everything well
stocked" verdict.

Different companies export different shapes. The pipeline needs a front
door that recognises a non-transactional sales file, converts it
deterministically into the canonical shape, and tells the user what was
read BEFORE the paid analysis runs.

## Core principle

**The AI never touches the numbers.** The model reads a ~30-row sample and
outputs a layout *recipe*. Deterministic Python executes the recipe over
the whole file. Every quantity is copied by code and verified against the
source. Any doubt at any stage → loud refusal, never a silent guess.

## Decisions (made 11 Jul 2026)

1. **Trigger — only when needed.** A deterministic detector runs after
   normal ingestion of the SALES slot only. Clean transaction files take
   today's untouched path. v1 detector signature: a header row containing
   ≥6 English month names (JAN/FEB/... full or 3-letter). Nothing else
   triggers the mapper in v1.
2. **UX — non-blocking read-back on the upload page.** After conversion the
   sales slot shows what was read. Proceeding to analysis is NOT gated on
   clicking anything ("Looks right — continue" is an affordance, not a
   gate). Only "This looks wrong" blocks. This is deliberate: zero added
   friction on the happy path.
3. **Failure — refuse with guidance.** Mapper unsure, schema invalid,
   verification mismatch, executor exception → amber "we couldn't read
   your sales file" treatment (same tone as the blocked-vs-crash split
   shipped 11 Jul), with a concrete ask: request an export with one row
   per sale, a date column, item name and quantity. Nothing runs, nothing
   is charged. There is NO fallback to naive ingestion and NO
   "run anyway" override.
4. **Architecture — recipe → canonical CSV → existing ingestion.** The
   executor writes a canonical CSV next to the upload; that CSV is fed
   through the existing `excel_to_sqlite` path unchanged, replacing the
   naive sales table for the session. Reuses all existing sanitisation;
   leaves an on-disk artifact for debugging "this looks wrong" reports.

## Components

### 1. Detector (pure Python, new module `ingest_recipe.py`)

`detect_wide_matrix(filepath) -> bool` — reads only the first
`~15` raw rows (openpyxl read-only for .xlsx, csv module for .csv) and
returns True when any single row contains ≥6 distinct English month names
(case-insensitive, trailing spaces ignored; both `JAN` and `JANUARY`
forms). Runs in the existing upload background thread after
`excel_to_sqlite` succeeds for the sales slot.

### 2. Mapper (new module `agents/ingest_mapper.py`)

- Input: the first ~30 raw rows rendered as plain text with row/column
  coordinates, **wrapped in `wrap_untrusted()`** — spreadsheet content is
  untrusted and may carry prompt injection. System prompt includes
  `UNTRUSTED_GUARD`.
- Model: Haiku (fixed, cheap; this is infrastructure, not an org-facing
  feature — not the per-org chat model). Small `max_tokens`.
- Output: JSON only, validated in Python against a strict schema:

```json
{
  "layout": "wide_matrix",          // only accepted value in v1; anything else -> refuse
  "header_row": 3,                  // 1-based row holding the month names
  "item_col": 1,                    // 1-based column of item names
  "month_cols": {"2": 1, "3": 2},   // column -> month number (1-12)
  "supplier_col": 14,               // or null
  "leadtime_col": 16                // or null
}
```

- Validation (deterministic, in `ingest_recipe.py`): every index within
  the file's real bounds, months 1–12, no duplicate months, ≥6 month
  columns, item_col not a month col. Any violation → refuse. The model's
  output is never trusted structurally.
- The mapper does NOT decide projections, year, or fill-down — those are
  numeric/statistical questions and stay in Python.

### 3. Executor (pure Python, `ingest_recipe.py`)

`execute_recipe(filepath, recipe, today) -> (csv_path, readback dict)` or
raises `RecipeRefusal(reason)`.

- Reads .xlsx with `data_only=True` (cached formula values) or .csv with
  the csv module.
- Rows: skip blank item names; skip rows whose item name contains
  "TOTAL" (case-insensitive).
- Supplier / lead time fill down their block (merged-cell style): a filled
  supplier cell starts a new block and RESETS the carried lead time; a
  filled lead-time cell updates it. Lead time parsed to days:
  `10 WEEKS → 70`, `14 DAYS → 14`, `2 MONTHS → 60`; unparseable → blank.
- **Projection detection (deterministic flat-tail rule):** walking months
  from December backwards, a month belongs to the projection tail while
  ≥50% of items with a positive value in that month have the SAME value
  repeated across the whole remaining tail. The longest such trailing run
  is dropped. Known false positive: a catalogue dominated by fixed
  standing orders (genuinely identical quantities every month) could get
  real months dropped — accepted for v1 because the read-back names the
  dropped months loudly.
- **Year rule (deterministic):** the file states no year. If the last
  kept (actual) month number > the current calendar month number →
  previous calendar year, else current year. Always shown on the
  read-back as "assumed".
- Output rows: `Date` (ISO `YYYY-MM-15` — unambiguous, no day-first
  guessing; verified 11 Jul that `count_sales_months` reads ISO), `Item
  Description`, `Qty Sold`, `Supplier`, `Lead Time Days`. Zero quantities
  are kept (a real no-sales month is data). Non-numeric cells are skipped.
- **Verification (mandatory):** per-month totals recomputed independently
  from the source cells must equal the CSV's per-month totals exactly;
  row and item counts must match expectations. Any mismatch →
  `RecipeRefusal`. (Honest limit: this proves Python followed the recipe,
  not that the recipe is true — the truth check is the human read-back.)
- Readback dict: `{items, months_kept: [..], months_dropped: [..],
  assumed_year, total_units, coverage: {sales_items_matched,
  inventory_items_total}}`. Coverage is computed by comparing distinct
  item names (trimmed, case-insensitive exact match) against the
  session's inventory table when that table exists at conversion time;
  otherwise the coverage line is omitted. This is a rough
  pre-normalisation number and is labelled as approximate in the UI —
  the dedup agent does the real matching later.

### 4. Wiring (app.py `_start_processing`, sales slot only)

```
excel_to_sqlite (unchanged, all slots)
  └─ sales slot AND detect_wide_matrix?
       ├─ no  → done (today's behaviour, byte-identical)
       └─ yes → mapper → validate recipe → executor → canonical CSV
                 ├─ ok      → re-ingest CSV via excel_to_sqlite into the
                 │            same sales_<sid> table (replace), store
                 │            read-back JSON, status "done" + read-back
                 └─ refusal → status "unreadable" (amber), guidance text,
                              sales table for the session cleared so the
                              analysis cannot run on the naive misread
```

- Conversion status gains one state ("unreadable") and a read-back JSON
  payload stored inside the existing per-slot conversion-status structure
  (a new `readback` key next to `rows_count`/`error`) — no new table.
- Concurrency: the existing per-slot conversion-status machinery already
  serialises re-uploads to the same slot; the recipe step lives inside
  the same background thread, so no new races are introduced. A re-upload
  simply restarts the slot's pipeline.

### 5. Upload-page UX (templates/upload.html)

- Converted slot (non-blocking read-back):

```
Sales file ✓ converted
⚠ This wasn't a standard sales export — berthcast read it as:
  • 149 items
  • Jan–Jun (6 months of actual sales, year assumed 2026)
  • Jul–Dec dropped — they look like typed-in projections, not sales
  • 4,015,510 units total
  • covers ~149 of your 1,345 inventory items (approximate) — items
    without sales data will show as "no sales data"
  [Looks right — continue]   [This looks wrong]
```

- "This looks wrong" → clears the slot's converted table, shows the amber
  guidance (what export to request), lets them re-upload. No re-mapping
  loop in v1.
- Refusal state: amber panel, same guidance, re-upload enabled.

## Security

- Spreadsheet sample is fenced with `wrap_untrusted()` + `UNTRUSTED_GUARD`;
  recipe JSON is schema-validated field-by-field — a poisoned file cannot
  reach code execution or another org's data. Residual (accepted): a
  poisoned file could still induce a schema-VALID but wrong recipe; damage
  is bounded to a wrong conversion of the attacker's own upload, surfaced
  by verification totals and the read-back.
- No new SQL surfaces: table writes go through the existing
  `excel_to_sqlite` path. The new state/JSON fields use parameterised
  updates like every neighbour.
- security-reviewer agent run is mandatory before deploy (upload path +
  client data).

## Residual holes — accepted, not solved (recorded 11 Jul 2026)

1. Detector recognises ONE weird shape (English month-name grids). Other
   alien formats fall through to today's naive path with today's warning
   banner. Each new client file teaches the next signature.
2. Non-blocking read-back means hurried staff can proceed without reading
   it. Chosen trade-off (friction vs safety).
3. Coverage: a sales file listing 149 items cannot produce recommendations
   for the other ~1,200 inventory items. The read-back coverage line makes
   this visible before the spend; only a fuller ERP export fixes it.
4. Flat-tail projection rule can drop genuinely-flat real months (fixed
   standing orders) — mitigated by the ≥50%-of-rows threshold and loud
   read-back, not eliminated.

## Testing

All plain-script tests (`_check()` + exit code), synthetic generic fixture
data only — nothing resembling real client names, suppliers, or the real
business (public repo).

- Detector: month-grid header → True; transaction headers → False; month
  names scattered in data rows (not one row) → False; .csv variant.
- Recipe validation: valid recipe accepted; out-of-bounds column,
  duplicate months, unknown layout, missing field → each refused.
- Executor (synthetic wide-matrix fixture): per-month sums equal source;
  supplier/lead-time fill-down and block reset; "10 WEEKS" → 70;
  TOTAL row skipped; text zeros/blank cells; flat-tail drops the
  projection months; standing-order edge documented; year rule both sides
  (month > current → previous year); zero-qty rows kept; ISO dates.
- Verification: a deliberately corrupted recipe (wrong month column) →
  RecipeRefusal, no CSV.
- Fence: a fixture whose cells contain instruction-like text ("ignore all
  previous instructions...") — the mapper prompt string contains it only
  inside the fence (string-level test, like the chat-guide test).
- Wiring: refusal path sets the "unreadable" status and clears the naive
  sales table.

## Out of scope (v1)

- Inventory / purchase-order slot mapping.
- "Include projections anyway" toggle; re-mapping loop after "this looks
  wrong".
- Multi-sheet workbooks (first sheet only, as today), non-English month
  names, quarter/week columns, "M1..M12" style headers.
- Any change to clean-transaction-file ingestion.
