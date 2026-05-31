# Spec: Show the stakes on each recommendation card

**Date:** 2026-05-31
**Status:** Approved (design)
**Scope:** Results page only (`/results/<id>`). No changes to print/CSV/PDF exports this round.

## Problem

The recommendation agent already generates, and the database already stores, three
high-value fields per recommendation:

- `consequence_if_not_acting` — plain-English stakes of skipping the order
- `consequence_if_acting` — plain-English downside of ordering
- `mitigation` — concrete action when the supplier is high-risk

None of these are rendered anywhere the user can see them. The results card shows only
`reason`. The suggested order quantity is shown as a bare number, and the agent is
explicitly told to keep the numeric basis out of `reason` (`agents.py` rule 3). The net
effect: the page asks a buyer to commit money to a number with no visible justification,
and the product's most persuasive content is discarded before it reaches the screen.

## Goal

Make each recommendation card show *why* — so a purchasing manager can trust and act on
it — by surfacing the already-generated reasoning, with a "side-by-side stakes" layout.

## What the user sees (per card)

Below the existing `Reason` line:

1. **Two contrasting boxes, side by side:**
   - Left, red-tinted: heading "If you don't order" + `consequence_if_not_acting`.
   - Right, neutral: heading "If you order" + `consequence_if_acting`.
2. **Amber strip below the boxes**, only when `supplier_risk == "HIGH"` *and* `mitigation`
   is non-empty: shows the mitigation advice.
3. **A small muted line under the Quantity input** describing the quantity basis, e.g.
   *"Based on ~40 CTN/mo sold and a ~3.5-month lead time, plus a safety buffer."*

The quantity line is a **plain description, not a strict equation** (`40 × 3.5 = 160`).
The agent may override the suggested quantity, so a literal equation could fail to add up
and damage trust. We state the inputs and present the suggested quantity as the result.

## Graceful degradation (required)

- Missing one consequence sentence → render only the box that has text (full width).
- Missing both → render no stakes section at all (no empty boxes, no error).
- No usable monthly-sales figure → hide the quantity-basis line entirely.
- Supplier not high-risk, or `mitigation` empty → no amber strip.
- View-only role (`user_role == 'viewer'`) → still sees the stakes (it is information,
  not an action).
- Old analyses created before this change (no saved monthly-sales number) → quantity-basis
  line hidden; consequence boxes still show if those fields exist.

## The one backend change

The page already has `consequence_if_acting`, `consequence_if_not_acting`, `mitigation`,
and `lead_time_days` on each saved recommendation. The only missing input for the
quantity-basis line is **average monthly sales**, which `run_recommendation_agent`
computes (`avg_monthly = sales_velocity.get(iname, 0)`) but does not persist onto the rec.

Change in `agents.py` (`run_recommendation_agent`): after parsing `recs`, attach the
monthly-sales figure (and the unit label) to each rec keyed by item name, using the same
value already used to compute `suggested_quantity`. This keeps the displayed basis
consistent with the displayed quantity. Concretely, store on each rec:

- `avg_monthly_sales`: number (e.g. `40`), `0`/absent when unknown
- `uom_label`: unit string already derived in the loop (e.g. `" CTN"` or `" units"`)

Because the agent's output array order/items may not line up 1:1 with the input, match by
item name (build a `{item_name: (avg_monthly, uom_label)}` map from the enrichment loop and
look each rec up by `rec["item"]`). When `avg_monthly_sales` is `0`/absent (the existing
"insufficient sales data" case), the quantity-basis line is hidden.

## New display helpers (`rec_logic.py`)

Pure functions, no I/O, unit-testable:

- `_quantity_basis(rec)` → returns the muted sentence string, or `None` when there is no
  usable `avg_monthly_sales`. Uses `avg_monthly_sales`, `uom_label`, `lead_time_days`
  (→ months, rounded to 1 dp), and `_effective_qty(rec)`. Omits the lead-time clause when
  `lead_time_days` is missing/None (no "None months").
- `_has_stakes(rec)` → `True` if either consequence field is a non-empty string.

These are enriched onto each rec in the `results()` view (alongside the existing
`_order_by`, `_conf_reasons`, etc.) as `rec["_quantity_basis"]` and `rec["_has_stakes"]`,
so the template stays declarative.

## Template change (`templates/results.html`)

In the `rec-card-body`, after the `Reason` paragraph (~line 328):

- If `rec._quantity_basis`, render it as a muted line directly under the quantity edit
  field (within/after the quantity column of `.rec-edit-grid`).
- If `rec._has_stakes`, render a `.rec-stakes` block: a two-column grid containing
  `.rec-stake.is-negative` (left) and `.rec-stake.is-positive` (right). Each box renders
  only if its consequence text exists.
- If `rec.supplier_risk == 'HIGH'` and `rec.mitigation`, render a `.rec-mitigation` amber
  strip below the grid.

Add a scoped `<style>` block following the existing in-file convention (cf. the
`.outcome-strip` block already in this template). Use existing tokens: `--danger`,
`--muted`, `--border`, `--paper`, brass/amber for mitigation. The two-column grid collapses
to one column under ~560px via a media query so it reads on mobile.

## Testing

Project has no test framework (no pytest, no `tests/`). Add a dependency-free script
`tests/test_rec_logic_stakes.py` runnable with `python tests/test_rec_logic_stakes.py`,
using plain `assert` and a non-zero exit on failure. Cover `_quantity_basis`:

- Normal: `avg_monthly_sales=40`, `uom_label=" CTN"`, `lead_time_days=105`, effective qty
  `160` → sentence contains "40", "CTN", "3.5", "160".
- No sales data: `avg_monthly_sales=0` / missing → returns `None`.
- Missing lead time: `lead_time_days=None` → returns a sentence, omits the lead-time clause,
  no crash, no "None".

And `_has_stakes`: both fields present → `True`; both empty/missing → `False`; one present
→ `True`.

Manual verification (step 3, systematic debug): run the app on real a regional food distributor data, open a
results page, confirm boxes render, the amber strip appears for a high-risk supplier, and
everything hides cleanly when fields are blank.

## Out of scope (this round)

- Print/CSV/PDF exports (deferred).
- The Top-N scope-filter dead code and the dedup-vs-analysis ranking mismatch (separate
  "correctness bugs" track the user deprioritized).
- Fixing the canonical-name vs raw-sales-name matching that can leave `avg_monthly_sales`
  at 0 more often than ideal (pre-existing; we degrade gracefully around it).

## Files touched

- `agents.py` — persist `avg_monthly_sales` + `uom_label` on each rec.
- `rec_logic.py` — add `_quantity_basis`, `_has_stakes`.
- `app.py` — enrich `rec._quantity_basis`, `rec._has_stakes` in `results()`.
- `templates/results.html` — stakes boxes, mitigation strip, quantity-basis line, CSS.
- `tests/test_rec_logic_stakes.py` — new standalone test.
