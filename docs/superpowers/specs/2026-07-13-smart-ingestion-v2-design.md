# Smart Ingestion v2 — Wider Front Door, Ask When Unsure

**Date:** 13 July 2026
**Status:** Approved direction (design discussion 13 Jul 2026); spec for review
**Extends:** `2026-07-11-smart-sales-ingestion-design.md` (v1)

## Why

v1 shipped "AI maps, Python counts": for one file shape (an English
month-name grid) an AI reads a ~30-row sample and proposes a layout
*recipe*; deterministic Python copies every quantity itself and verifies it
against the source. Proven on a pilot file — the actual months reproduce a
hand conversion to the decimal.

Two gaps remain, and they are exactly what makes the front door fragile:

1. **The door is too narrow.** Only English month-name grids trigger the
   safe path. A grid whose columns are labelled with month *numbers*,
   *dates* (`Jan-26`), or *codes* (`M1..M12`) slips past the detector and
   falls back to the naive read — the same silent misread that produced a
   wrong, paid analysis run before v1 existed.
2. **The outcome is binary.** Today it is convert-silently or hard-refuse.
   "Unsure" becomes a wall. A user reads a wall as "this tool rejects my
   files" and stops coming back. There is no middle where berthcast converts
   what it is confident about and asks the human about the rest.

v2 widens the door to more grid dialects and replaces the binary outcome
with three tiers, so "unsure" becomes a one-tap question instead of a wall —
without ever letting an AI type a quantity.

## Core principle (unchanged from v1)

**The AI never touches a number.** It reads a sample and outputs a layout
recipe — which columns are months, which is the item, which is supplier /
lead time. Python executes the recipe over the whole file, copies every
quantity itself, and independently re-sums the source cells to verify.
Every number the analysis sees was copied by code straight from the file,
never typed by a model.

(The "translator and typist" split: the AI translator reads the messy file
and writes instructions; the Python typist copies the figures straight from
the original. Same AI understanding of any layout — the money numbers just
never pass through the model.)

## Decisions (13 Jul 2026)

### 1. Wider detector — more grid dialects, same safe executor

The detector triggers the mapper when one header row holds ≥6 cells that
each resolve to a distinct month (1–12). v2 widens what "resolves to a
month" means:

| Dialect | Example header cells | In v1? |
|---|---|---|
| English month names | `Jan Feb …` / `January …` | yes |
| Month-year dates | `Jan-26`, `2026-01`, `01/2026` | new |
| M-codes | `M1 M2 … M12` | new |
| Bare numbers 1–12 | `1 2 3 … 12` | new, guarded |

- **Bare integers 1–12 are guarded.** Many tables number things 1–12, so a
  bare-integer row triggers only when it is a consecutive run starting at 1
  **and** a nearby label cell reads `month`/`period`; otherwise it is
  ignored, to avoid false triggers. Best-effort — the mapper and the
  verification pass are the backstop.
- **Quarters (Q1–Q4) are deferred.** A quarter is not a month; splitting one
  quarter into three months fabricates monthly detail the file does not
  contain. Not worth the risk in Phase 1.
- **Clean transaction files stay on the naive path, untouched** — no AI
  call, no cost, byte-identical to today. The detector only diverts
  grid-shaped files.

### 2. Mapper prompt upgrade — accuracy only

The mapper's system prompt gains worked examples of every dialect above and
of the messy-grid quirks already seen (a merged `TOTAL` header sitting over
the first month column; supplier / lead time filled only on the first row of
each block; lead time hiding in a sales report). Still layout only, still
Haiku, still fenced as untrusted content. **No self-confidence field** — a
model grading its own certainty is noisy; the tier decision below runs on
deterministic evidence instead.

### 3. Three-tier outcome (replaces the binary)

After Python executes and verifies the recipe, the run is classified:

1. **Confident** — every claimed month column is cleanly numeric, projection
   drops are unambiguous, verification totals match. → Convert, show the
   calm collapsed read-back (shipped last session), **no gate**. Zero
   friction on the good path.
2. **Degraded** — converted and verified, but something is a judgment call:
   a claimed month column is mixed numeric/text, or a dropped month is
   borderline (see §4). → Convert the trustworthy part, show the read-back
   **with the specific question**, and require **one tap** to proceed to the
   paid run. Never falls back to the naive read.
3. **Refuse** — verification totals disagree, or zero trustworthy rows come
   out, or the mapper cannot map the file at all. → Amber guidance (as v1),
   naive sales table cleared. This is the floor: you cannot ask someone to
   confirm nothing.

**Iron rule under all three: a file the detector flagged as a grid never
falls back to the naive read.** Convert, confirm, or refuse — never a silent
misread. This closes the step-2 weak spot for every dialect the detector
catches.

### 4. Borderline projection drops become a question

v1's flat-tail rule drops trailing months that repeat one flat value (typed
projections). Failure mode found on the pilot file: when the projection
columns are built by copying the *last real month* forward, that real month
matches the flat run and gets dropped too — silently losing a month of real
sales and undersizing every order.

v2 does not try to out-guess this. When a dropped month is **borderline** (a
meaningful minority of items disagree with the flat value), it is surfaced as
a degraded-tier question — *"Dropped [month] as a projection — keep it?"* —
and one tap restores it. A clear-cut projection tail (all items flat) still
drops silently, as today.

### 5. Verification — one added lever

Keep v1's mandatory independent re-sum (source cells vs output totals must
match exactly, else refuse). Add **type-sanity** as a *degrade* trigger: a
claimed month column that is *mostly* numeric but carries a meaningful share
of text cells is suspect → degraded (ask), not silently trusted. An all-text
claimed month column still hard-refuses (v1); an all-empty one still drops
that month (v1).

## Components (files touched)

- **`ingest_recipe.py`** — widen `detect_wide_matrix` with the dialect
  recognizer; add the tier classifier; add type-sanity and borderline-drop
  detection to `execute_recipe`'s findings; the read-back dict gains `tier`
  and an optional `question`.
- **`agents/ingest_mapper.py`** — prompt upgrade (dialect examples + quirks).
  Output schema and `validate_recipe` are essentially unchanged (no new
  fields).
- **`app.py` `_start_processing`** (sales slot) — handle the new degraded
  state; store `tier` + `question` in the conversion status; gate
  proceed-to-analysis on the tap for **degraded only**.
- **`templates/upload.html`** — degraded read-back with the question and a
  one-tap control that unblocks the run; confident read-back unchanged;
  refuse panel unchanged.

## Data flow

```
upload → naive excel_to_sqlite (all slots)
  └─ sales slot AND detector says grid?
       ├─ no  → done; naive read stands (clean transaction path, unchanged)
       └─ yes → mapper(sample) → validate recipe → execute + verify + classify
                 ├─ confident → replace sales table; status "done" + calm read-back
                 ├─ degraded  → replace sales table; status "needs_confirm"
                 │              + read-back + question; PAID RUN BLOCKED until one tap
                 └─ refuse    → status "unreadable" + guidance; naive sales table cleared
```

## Error handling

- Any executor / verification exception → refuse (never silent). Reason
  logged for the operator; the user sees generic guidance.
- Mapper API failure or timeout → refuse (as v1).
- The v1 stale-thread token guard is unchanged: a removed or re-uploaded slot
  discards the old thread's terminal write. The new degraded state is written
  under the same guard.

## Security

- The spreadsheet sample is still fenced with `wrap_untrusted()` +
  `UNTRUSTED_GUARD`; the recipe is schema-validated field-by-field. A
  poisoned file can at most induce a schema-valid-but-wrong recipe on the
  attacker's own upload — caught by the verification totals and surfaced in
  the read-back.
- No new SQL surfaces: table writes go through `excel_to_sqlite`; the new
  status / JSON fields use the existing parameterised updates.
- Resource caps (raw file size, decompressed xlsx size, row count) from v1
  are unchanged.
- **security-reviewer agent run is mandatory before deploy** (upload path +
  client data).

## Testing (plain scripts, synthetic generic fixtures only — public repo)

- **Detector:** each new dialect (dates, M-codes, guarded bare-integers)
  triggers; a bare `1..12` header with no month label does **not** trigger;
  transaction headers still return False; `.csv` variant.
- **Tier classifier:** confident / degraded / refuse each from a crafted
  fixture — a clean grid (confident); a mixed numeric/text month column
  (degraded); a last-real-month copied into the projection tail (degraded);
  a sums mismatch (refuse); an unmappable file (refuse).
- **Borderline drop:** a fixture where the last real month is copied into the
  projection tail → that month is surfaced as a question, not dropped; a
  fully-flat tail still drops silently.
- **Type-sanity:** mostly-numeric-with-some-text month column → degraded;
  all-text → refuse; all-empty → month dropped.
- **Verification (unchanged):** a corrupted recipe (wrong month column) →
  refuse, no CSV written.
- **Wiring:** degraded state blocks the paid run until the tap; refuse clears
  the naive table; confident path unchanged.

## Out of scope (Phase 1)

- Quarter (Q1–Q4) grids.
- Non-grid / long-format column mapping, pivots, transposed sheets,
  multi-block or nested-header layouts.
- Multi-sheet workbooks (first sheet only, as today).
- Inventory / purchase-order slot mapping.
- Retuning the flat-tail heuristic itself — the one-tap question covers the
  borderline case instead.
- Running the mapper on files the detector did **not** flag (cost; clean
  transaction files stay untouched).

## Residual holes (accepted, not solved)

1. A grid in a dialect even the widened detector misses still falls to the
   naive read. Smaller surface than v1; each new real file teaches the next
   dialect.
2. The degraded tap is one gate, not a form — a hurried user can still tap
   through without reading. Chosen trade-off (one tap beats a wall).
3. Verification proves Python followed the recipe, not that the recipe is
   *true* — the human read-back stays the truth check.
4. Bare-integer detection is best-effort; an unlabelled `1..12` grid may not
   trigger.
