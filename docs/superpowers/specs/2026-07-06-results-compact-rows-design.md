# Results page — compact rows redesign

**Date:** 2026-07-06
**Status:** Approved (brainstormed with mockups; row style "B", expanded depth "A")

## Problem

The purchase-recommendations tab renders one large card per recommendation. A single
item fills most of a screen. At hundreds of SKUs the page is unmanageable — the user
called it "headache if hundreds of products". The page must become scannable: a
compact list that says *what to order and how much*, with detail on demand.

## Decisions made

| Question | Decision |
|---|---|
| List structure | Keep supplier groups; sort rows by urgency **within** each group |
| Row style | Two-line row with inline note field (mockup option B) |
| Expanded depth | Full detail — everything the old card had (mockup option A) |
| Old card layout | Fully replaced — no list/card toggle (toggle = double maintenance) |

## Collapsed row (default view)

Two-line row inside each supplier group:

- **Line 1:** item name (bold, truncates) · order quantity in gold tabular numerals
  (e.g. `8,191 KG`) · order-by date (salmon + bold when overdue/urgent) ·
  ✓ approve / ✕ dismiss buttons · expand chevron.
- **Line 2:** first sentence of the AI reason (truncated, muted) · dashed inline
  note field.
- **Criticality colour (left edge + faint background tint):**
  - CRITICAL → red edge, `rgba(192,57,43,0.10)` tint
  - LOW → amber edge, `rgba(230,180,80,0.07)` tint
  - other → plain surface
- **States:** approved → moss ✓ badge; dismissed → faded row. Criticality edge stays
  in all states.
- **Confidence:** LOW / INSUFFICIENT_DATA shows a small muted "low confidence" tag on
  line 1. HIGH/MEDIUM show nothing on the row (chip lives in the expanded panel).

**Urgency sort within a group:** overdue first (most overdue first), then urgent,
then ascending buffer days; recs without an order-by date sort CRITICAL before LOW
before the rest. Group ordering on the page is unchanged.

## Expanded panel (click row or chevron)

Opens inline under the row, closes on second click. Contents (all existing card
fields, packed tighter):

1. Meta line: **Order by <date>** + overdue/buffer detail · "stock lasts about
   N months" · confidence chip (with the existing why-this-confidence reasons
   available from it).
2. Edit grid: quantity + supplier inputs — same `/recommend/edit` save-on-blur
   behaviour and AI-suggested/revert hints as today.
3. Full reasoning paragraph (quantity basis + reason).
4. Consequence boxes: "If you don't order" / "If you order" (when present).
5. Supplier-risk mitigation line (when supplier_risk is HIGH).

## Outcome tracking (proof-loop — improved visibility)

After a row is approved, its **line 2 swaps the reason snippet for the outcome
prompt** (the inline note field stays at the end of line 2 in every state),
visible without expanding:

- "Did you place this order? [Yes, ordered] [Not yet]"
- then "Was the stockout avoided? [Yes, avoided] [No, stockout]"
- then the final state ("Order placed · Stockout avoided").

Same `/recommend/outcome` endpoint and payloads — zero backend change here. This is
strictly more visible than the old card strip.

## Notes

- Inline note field on every row, saves **on blur** via `/recommend/edit`, which
  gains an optional `note` param (~6 lines in `app.py`, same mutate pattern as
  edited_quantity). Approve/dismiss continues to send the note exactly as today.
- Placeholder text unchanged ("Add a note (e.g. called supplier, confirmed
  delivery)" — may shorten to fit the line).

## Unchanged

Sidebar overview stats + filter chips, search, Approve-all + undo, per-supplier
approve button, keyboard shortcuts (j/k move row focus, a/d act, / search, Esc
clear; Enter or → toggles expand), CSV export, Print/PDF, clarity box, summary bar,
supplier reliability score in group header, inventory-health tab, not-selling tab,
viewer-role read-only behaviour.

## Files touched

| File | Change |
|---|---|
| `templates/results.html` | Card markup → row + expandable panel; JS reworked (expand/collapse, note save-on-blur, outcome prompt on line 2, keyboard focus on rows) |
| `static/style.css` | New row/panel styles; old `rec-card` styles deleted |
| `app.py` | `/recommend/edit` accepts optional `note`; urgency sort of `group.recs` where `rec_groups` is built |
| `tests/` | Update template-render tests to new markup; add note-save-on-blur test |

## Testing

- Full suite green before handoff.
- Offline Jinja render of results.html in all states: pending / approved /
  dismissed / expanded / empty / viewer role.
- Headless screenshots on the dark theme, eyeballed (row colours, expanded panel,
  outcome prompt).
- Manual: one real analysis after deploy to confirm live.

## Out of scope

Pagination/virtualisation (revisit if a single render of ~1,000+ rows is slow),
mobile layout (staff are desktop-only), changes to rec generation or print/CSV
formats.
