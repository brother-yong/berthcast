# Data Safety Net — design

**Date:** 2026-06-13
**Status:** Draft for review

## Why

berthcast is about to meet messy, unseen files: prospect demos, and a free tier
where strangers upload whatever they have. There is no chance to fix anything on
the spot.

The danger is not crashes. A crash is visible and recoverable. The danger is a
clean, confident report built on a misread file: wrong velocity, a stockout
shown as healthy, the client trusts it, orders wrong, and we lose them for good.

We cannot guarantee correct output on infinite real-world files. No software can.
So that is not the goal.

## The guarantee we CAN make

**berthcast never shows a trusted report built on data it could not read
correctly.** Every analysis ends in one of three states:

- **OK** — checks passed; report shown normally.
- **WARN** — report shown, plus a plain-English caveat naming exactly what was
  assumed or might be wrong.
- **BLOCK** — the file is unusable (wrong document, empty, corrupt, or no
  readable stock at all); no report; a clear explanation of what is wrong and
  what to upload instead.

"Fail loud and honest" beats "fail silently wrong." That is the whole promise
this protects, and unlike "always correct," it is something we can actually test
and prove.

## How it works — two parts

### Part 1 — Confidence gate (the product change)

A checking layer that runs on every analysis, after the file is read and columns
are mapped, before the report is treated as trustworthy. It returns a list of
findings, each with a level (OK / WARN / BLOCK), a short code, and a
plain-English message.

It extends what we already built: `data_notes` is already saved on
`analysis_results` and shown as a banner on the results page. We add levels to it
and add BLOCK handling. A BLOCK stops the pipeline before recommendations run and
the results page shows the block instead of a half-finished report.

### Part 2 — Broken-file corpus (the proof)

A library of small, deliberately broken files under `tests/corpus/`, each paired
with the gate outcome it must produce. A harness runs every file through the real
ingestion and the real gate and asserts the outcome.

This is how we get as close to "certain" as software allows. We can't test the
infinite real world, but we can prove every *category* of mess produces a safe
state, and keep it that way every time the code changes.

## The checks (first slice)

| Failure mode | How we detect it | Outcome |
|---|---|---|
| Wrong / empty / unreadable file (no item column or no stock column) | ingestion finds no usable columns | BLOCK |
| No name overlap between inventory and sales (codes in one, names in the other) | matched names well below inventory size | WARN — runs on stock alone |
| Suspected non-US number format ("1.200,50") | a sample of numeric cells fits the dot-thousand / comma-decimal shape | WARN |
| Unit mismatch between inventory and sales | the two files' unit columns disagree for matched items | WARN |
| Which stock column was read | always state it; flag if it looks like on-order / allocated / sold | note, or WARN if suspicious |
| Sales period assumed (no dates, no avg column) | already built | WARN — fold existing banner into the gate |
| Very large file (slow + costly + truncation risk) | row count over a threshold | WARN + suggest narrowing scope |

Each row gets at least one corpus file and one test.

## Out of scope — parked, fixed when a real client needs it

The gate WARNS about these now; the real fix waits for a trigger:

- Correctly *parsing* European / Indonesian decimals — trigger: first client
  whose exports use that format.
- Full unit conversion (cartons ↔ pieces ↔ kg) — trigger: first client who needs
  cross-unit math.
- Non-English header handling — trigger: first non-English-export client.

This matches the existing parked-work plan and keeps this slice shippable.

## Testing / verification

- `tests/corpus/` files plus `tests/test_data_safety_net.py`: every corpus file
  asserts its required gate outcome (right number, specific WARN, or BLOCK).
- Regression: Cool Link's real files still produce a clean expected run.
- Full existing suite stays green.

## Files (estimate)

- **New:** a confidence-gate module (e.g. `data_quality.py`), `tests/corpus/*`,
  `tests/test_data_safety_net.py`.
- **Touch:** `agents/inventory.py` / `agents/orchestrator.py` (emit findings),
  `app.py` (handle BLOCK on the results route), `templates/results.html` (show
  WARN / BLOCK), `database.py` (store finding levels if needed).

## Why this approach over the alternatives

- **Fix everything correctly now** — too big, too slow, and you can't prove it's
  done. Rejected.
- **Manual pre-flight only (you run each file before a demo)** — doesn't help the
  free tier, where strangers upload unattended. Rejected earlier for that reason.
- **Detect-and-warn gate + corpus (this)** — shippable, provable, and protects
  the unattended free tier. Chosen.
