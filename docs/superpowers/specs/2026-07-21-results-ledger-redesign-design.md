# Results page — ledger redesign

**Date:** 2026-07-21
**Status:** Approved (brainstormed with mockups; row style "B", page layout "C", actions "A")
**Supersedes:** parts of `2026-07-06-results-compact-rows-design.md` (see "Why this reverses a 15-day-old decision")

## Problem

Two weeks of live use surfaced three faults in the compact-row layout.

**1. The item name is unreadable.** `.rec-row-item` is the only flexible element in a
flex row where every sibling is `white-space: nowrap`, so the name absorbs the entire
space shortfall. Observed live: names rendered as `AMM…` and `PAR…` — three characters.
The failure scales the wrong way: a row carrying more badges (low confidence, approved,
overdue) shows *less* of its name, so the rows that most need scrutiny are the least
identifiable.

**2. The overview duplicates the filters.** The sidebar shows Critical / Low / Spoilage /
Approved as stat boxes, then immediately below offers Critical / Low as filter chips.
Worse, the stat counts are computed from `inventory` while the list below renders
`recommendations` — two different populations, so the number never matched the list.

**3. Every row shouts.** A resting row carries approve, dismiss, note, expand, an
outcome question, and a sales sentence ("One tap — it's how berthcast proves it's saving
you money") repeated once per recommendation. At 50+ rows the actual decision data
(what, how much, by when) is outnumbered by chrome.

## Decisions made

| Question | Decision |
|---|---|
| Row structure | Fixed-column ledger with headings (mockup B) — replaces the flex row |
| Item name | Never truncates; wraps to a second line |
| Page layout | Sidebar removed; counted filter chips in a sticky bar above the ledger (mockup C) |
| Row separation | A dividing line under every row |
| Actions at rest | Visible but dimmed; full contrast on hover/focus (mockup A) |
| Outcome prompt | Removed from the web page entirely |
| Print sheet | Gains a pen checkbox, a write-in column, and now prints all recommendations |
| Tab scope | All three tabs restyled, via a results-scoped class (see Constraints) |
| Rollout | Two commits: recommendations ledger first, the two tables second |

## Constraints

`.data-table` is shared by `suppliers.html`, `diff.html`, `guide.html`, `settings.html`,
`admin.html`, and `dashboard.html`. Restyling it would silently change six unrelated
pages. The Inventory Health and Dead SKUs tables therefore get a **new results-scoped
class**; global `.data-table` is not touched.

## Filter bar

Replaces `.mc-sidebar` entirely. Sticky beneath the nav, full page width.

Counted chips, grouped and separated by hairline dividers:

- **Status** — Critical *n* · Low *n*
- **Supplier type** — Import *n* · Local *n* · Other *n*
- **Confidence** — High *n* · Medium *n* · Low *n*
- **State** — All · Pending *n* · Approved *n* · Dismissed *n*

Plus the search field, the keyboard-shortcut legend, and the CSV / Print buttons.

**Counts are computed from the rendered recommendation rows, not from `inventory`.**
This fixes the population mismatch. They are computed once at page load from each card's
`data-status` / `data-suptype` / `data-confidence` attributes and are totals, not
live-filtered subsets — except **Approved**, which continues to update as rows are
approved (today's `#approved-counter` behaviour, moved onto the chip).

**Spoilage leaves this bar.** It is an inventory property, not an order property, and it
never filtered anything. It moves to the Inventory Health tab.

Clicking a chip filters exactly as the current chips do — same `applyFilters()`, same
multi-select-within-a-dimension semantics, same `Esc` to clear.

## The ledger

Supplier grouping is retained: buyers order per supplier, so the grouping is real work
structure, not decoration.

**Group header** — slim rule, no card, no large button:

```
Nordvik Foods · Import · 27 items · 19 critical · reliability 55/100     approve all 27
```

`approve all 27` demotes from the current large gold primary button to a quiet text
link. Prominence should match consequence, and one click currently approves 27
low-confidence recommendations at once. The existing confirm dialogue, undo, and
filtered-subset label logic are unchanged.

**Columns** — CSS grid, fixed widths, headings in small caps:

```
  #   ITEM                                      ORDER      BY       TYPE
  1   BROOKVALE PRAWN MEAT 400G 12X  low conf     240    14 Aug    Import    ✓  ✕  note
  ──────────────────────────────────────────────────────────────────────────────────────
  2   PADIMAS JASMINE RICE 5KG                     60     2 Sep    Local     ✓  ✕  note
  ──────────────────────────────────────────────────────────────────────────────────────
```

- **Item** takes all remaining width and never truncates — long names wrap to a second
  line and the row grows. No `text-overflow: ellipsis` on this column.
- **low conf** renders inline after the name and wraps with it, so it can never steal the
  name's width. HIGH / MEDIUM show nothing, as today.
- **Order** and **By** are right-aligned tabular numerals. Order stays gold; By stays
  salmon and bold when overdue or urgent.
- **Type** replaces the supplier name, which would duplicate the group header.
- No supplier column.
- Criticality colour stays on the row's left edge (CRITICAL red, LOW amber), in every
  state, as today.
- **A 1px divider under every row**, including inside a supplier group.

**Row density:** item text stays at its current size and rows keep generous vertical
padding. The width freed by removing the sidebar goes to the item name, not to fitting
more rows on screen — a denser grid reads as "54 things to process" to a user who is not
yet convinced, which is the opposite of the goal.

**Actions:** approve / dismiss / note sit in a fixed-width right column. `note` is not an
inline field on the row — it expands the row and focuses the note input inside the
panel. All three are present at rest
at reduced contrast and at full contrast on hover or keyboard focus. Opacity floors at
0.55 — below that a control reads as disabled rather than quiet. Keyboard focus gets its
own visible state; it must not depend on hover. Approved rows dim slightly and show a
moss ✓, as today. Viewer role renders the column with no buttons in it — the cell must
still exist or every row's columns shift out of alignment.

## Expanded panel

Unchanged in content and behaviour from the 6 July spec — meta line, quantity/supplier
edit grid with save-on-blur, full reasoning, consequence boxes, supplier-risk line — with
one addition: the **note field moves here** from the resting row.

Note remains a free-text field saved on blur via `/recommend/edit`. No backend change.

## What is deleted

- **"Did you place this order?" / "Was the stockout avoided?"** and their buttons are
  removed from `results.html`. The `/recommend/outcome` endpoint and the
  `order_placed` / `outcome_status` columns are **kept and untouched** — hidden, not
  dropped, so a later automated proof mechanism can populate them without a migration.
- **"One tap — it's how berthcast proves it's saving you money."** — product marketing
  inside a customer's working tool, repeated once per row.

## Print sheet (`print_order.html`)

- Prints **all** recommendations, with approved ones marked, instead of approved only.
- Gains an **Ordered ☐** column — an empty box sized for a pen tick.
- Gains a **write-in column** with enough ruled space to hand-write a PO number and an
  ETA, which is what the pilot client's purchaser records against each line today.
- Stays light-themed; it is a print artefact.

## Inventory Health and Dead SKUs tabs

Same visual language via the results-scoped class: small-caps column headings, a divider
under every row, right-aligned tabular numerals for Stock and Days of supply. Existing
badges, the dead-count link, and all copy are unchanged. Spoilage-risk count from the old
sidebar surfaces here.

## Unchanged

Filters, search, urgency sort within groups, group ordering, approve/dismiss saving,
approve-all and undo, keyboard shortcuts (j/k move, a/d act, Enter or → expand, / search,
Esc clear), CSV export, the clarity box, supplier reliability scores, the recommendation-
failure amber panel, viewer-role read-only behaviour, and the XSS-safe pattern where
buttons carry `data-` attributes only and no user text is ever spliced into inline JS.

No route, query, model prompt, or database change. Layout and CSS only.

## Why this reverses a 15-day-old decision

The 6 July spec explicitly accepted "item name (bold, truncates)" and deliberately raised
the outcome prompt's visibility as the proof loop. Both are reversed here, so both need a
reason:

- **Truncation:** accepted on the assumption it would clip a few trailing characters. In
  production it clipped to three (`AMM…`), because every sibling in the flex row is
  unshrinkable. The decision was sound; the implementation could not deliver it.
- **Outcome prompt:** it is being removed, not relocated, and that leaves berthcast blind
  to whether recommendations become orders. This is only acceptable because self-reporting
  is being replaced by measurement — matching recommended items against the purchase-order
  file in the client's *next* upload. **That replacement is a separate, unbuilt spec.**
  Until it ships, there is no adoption signal at all. That is a known, accepted cost.

## Files touched

| File | Change |
|---|---|
| `templates/results.html` | Sidebar → sticky filter bar; row markup → grid ledger; outcome prompt and sales line removed; note field moves into the expanded panel; chip counts computed from rendered rows |
| `templates/print_order.html` | All recommendations instead of approved-only; Ordered checkbox column; write-in column |
| `static/style.css` | New ledger + filter-bar styles; `.mc-sidebar` / `.mc-side-stat` / old `.rec-row-*` flex styles deleted; results-scoped table class added; global `.data-table` untouched |
| `tests/` | Update render tests to the ledger markup; add the assertions below |

## Testing

- `python run_tests.py` green.
- Offline Jinja render of `results.html` in every state: pending / approved / dismissed /
  expanded / empty / recommendation-failure / viewer role.
- New assertions: ledger column headings present; one row per recommendation; a long item
  name renders in full with no ellipsis class; note input present inside the expanded
  panel; no "Did you place this order?" string anywhere in the output; filter chip counts
  equal the recommendation counts, not the inventory counts.
- Offline render of `print_order.html`: checkbox column present, write-in column present,
  unapproved recommendations included and approved ones marked.
- Print preview eyeballed after the column change — a fixed-column grid is the most
  likely thing to break in print.
- Record today's baseline before deploying: approvals per session and notes written. It is
  the only honest way to judge whether this helped.

## Out of scope

Why quantities render as "Verify with team", why 27 items carry supplier "Unknown", and
why every recommendation is low confidence — a data/pipeline problem with its own risk
profile, tracked separately. Also out: PO-matching for automated proof, pagination or
virtualisation, and any change to recommendation generation or CSV format.

## Known risks

- **This makes an unusable report legible, not usable.** If the underlying run has no
  quantities and no supplier names, a better layout changes nothing for the user. The data
  thread is the higher-value work.
- **No user asked for this.** The truncation is objectively broken and defensible on its
  own, but the rest is inferred from a screenshot, not from a complaint. Scope should not
  extend further on inference.
- **Below ~900px the ledger must stack.** The pilot client's staff are desktop-only, but
  berthcast.com is public.
