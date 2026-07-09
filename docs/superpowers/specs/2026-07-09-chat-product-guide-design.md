# Chat product guide + prompt fence — design

**Date:** 9 July 2026
**Status:** Approved direction; spec for implementation planning.

## Problem

The in-app chat assistant receives the org's live analysis data in its system
prompt, but knows nothing about the product itself. Asked "how do I export a
PDF?" or "where do I see my suppliers?", it improvises from general knowledge
and can confidently give wrong steps. Worst case is a brand-new user with no
data: the prompt tells the assistant to "guide them through uploading" but
gives it no facts to guide with.

Separately, a July 2026 security review flagged the chat path as the one
remaining Claude prompt site where spreadsheet-derived text (item names,
observations, supplier notes) is embedded **unfenced** — rated HIGH because
chat output goes verbatim to a logged-in user. All four other prompt sites
already use `UNTRUSTED_GUARD` + `wrap_untrusted()` from `agents/shared.py`.

Both changes touch the same code, so they ship together.

## Decisions (agreed 9 July 2026)

1. **Guide scope: staff daily-use only.** Upload wizard, results page,
   outcome logging, Print/PDF + CSV, dashboard history/compare, suppliers
   page, chat capabilities, settings. **No admin/operator content** (account
   creation, trials, model switching, usage page) — staff would be told about
   pages they cannot open, and it leaks operator workflow.
2. **Bundle the chat fence fix** (the flagged HIGH) into this change.

## Design

### 1. `PRODUCT_GUIDE` constant (new, `chat_logic.py`)

A single module-level string, plain English, ~10 short sections. Content
must match the real UI labels exactly (verified against templates):
nav links **Dashboard / New Analysis / Suppliers / Chat**; results buttons
**"✓ Approve all"**, **"Print / PDF"**, **"CSV"**; outcome prompt
**"Did you place this order?"**; chat toggle **"Analysis context"**.

Draft text (final wording lives in the constant; edits during implementation
are fine as long as facts stay accurate):

> **What berthcast is:** AI inventory forecasting for distributors. Upload
> your stock and sales exports; it tells you what to reorder, how much, and
> what's at risk. It recommends — it never places orders.
>
> **Files to upload:** inventory (stock on hand) and sales history are
> required. A supplier list and purchase-order history are optional but
> improve supplier detection and lead-time awareness. Excel (.xlsx) or CSV,
> exported straight from your system — no reformatting needed; berthcast
> detects the right columns itself.
>
> **Running an analysis:** click "New Analysis" in the top navigation →
> upload files → fill the short context form (anything unusual this period)
> → review possible duplicate items → run. Takes a minute or two; a live
> progress screen shows findings as they appear.
>
> **Results page:** recommendations grouped by supplier, most urgent first.
> Red left edge = critical, amber = low. Each row shows item, quantity,
> order-by date (red if overdue). Click a row to expand full detail — the
> reasoning, confidence, and what happens if you don't act. ✓ approves,
> ✕ dismisses. In the expanded panel you can edit quantity or supplier
> before approving. The dashed note field saves automatically.
>
> **Logging outcomes (important):** after approving, the row asks "Did you
> place this order?" — later, mark whether the stockout was avoided or
> happened. This builds supplier reliability scores and the ROI numbers;
> if nobody logs outcomes, those stay empty.
>
> **Getting the order sheet out:** "Print / PDF" prints approved items
> (choose "Save as PDF" in the print dialog for a PDF file). "CSV" downloads
> a spreadsheet for Excel.
>
> **Dashboard:** past analyses, most recent first. Open any old run, or
> compare two runs to see what changed between them.
>
> **Suppliers page:** every detected supplier with a 0–100 reliability
> score. Everyone starts at 50; scores move as outcomes are logged. The
> search box filters the table.
>
> **Chat (this assistant):** sees the latest completed analysis only — not
> older runs. The "Analysis context" toggle gives it more detail (low-stock
> lists, dead SKUs, supplier profiles). It cannot place orders or change
> data.
>
> **Settings:** edit supplier profiles — lead time in days and delay
> likelihood. Filling these in sharpens reorder timing, especially for slow
> import suppliers.

### 2. `build_chat_system_prompt()` (new, `chat_logic.py`)

Move system-prompt assembly out of the `chat_api` route into one pure
function so tests can exercise it without Flask or the Anthropic SDK:

```python
def build_chat_system_prompt(org_name: str, chat_ctx: dict,
                             features: list[str]) -> str
```

Assembly order:

1. Persona + RULES (existing `base_system` text, unchanged) +
   `UNTRUSTED_GUARD` (imported from `agents.shared`).
2. `PRODUCT_GUIDE` — **outside** the fence; it is trusted instructions.
3. `wrap_untrusted(summary_text)` — when present.
4. `wrap_untrusted(detailed_text)` — when present.
5. The existing no-data onboarding line — when `has_data` is false.
6. The existing feature add-ons (`show_reasoning`, `detailed`) — moved here
   from `generate()` so the whole prompt is built in one place.

`app.py` `chat_api` shrinks to a single
`build_chat_system_prompt(session["org_name"], chat_ctx, features_snapshot)`
call. No other behaviour change; streaming, rate limits, lanes untouched.

### 3. Fence details

- `summary_text` and `detailed_text` are wrapped **whole** — same pattern
  as the four agent prompt sites (headers and counts are trusted, but
  fencing the whole block wholesale matches the established precedent and
  keeps the diff tiny).
- The guard instruction (`UNTRUSTED_GUARD`) rides in section 1 so it always
  precedes the fenced blocks.
- User chat messages are NOT fenced — they are the normal user channel, out
  of scope per the original review finding (which was about spreadsheet-
  derived text in the *system* prompt).

### 4. Cost

Guide ≈ 600–700 tokens, always included → well under half a cent per chat
message on the org models. No gating/keyword detection (rejected: fragile,
complexity for pennies).

## Testing

New `tests/test_chat_system_prompt.py`, driving `build_chat_system_prompt`
directly with hand-built `chat_ctx` dicts:

1. Guide present (assert a distinctive guide phrase).
2. `UNTRUSTED_GUARD` present.
3. Planted attack string in an item observation lands **inside**
   `<untrusted_data>` fencing; guide text stays outside.
4. Fence tags balanced (equal open/close, attack's fake tags stripped).
5. No-data variant: guide + onboarding line present, no fence blocks.
6. Feature add-ons appear when requested.

Full suite must stay green. Security-reviewer agent runs on the diff before
the commit guide is handed over (touches prompt construction — standing
rule).

**Live verification after deploy:** ask the chat "how do I save a PDF of my
order sheet?" — answer must describe the real "Print / PDF" button flow.

## Out of scope

- Admin/operator documentation in the guide (decision 1).
- Fencing user-typed chat messages.
- Keyword-gated guide inclusion.
- Any UI change.

## Maintenance note

The guide is static text. When a staff-facing flow or button label changes,
update `PRODUCT_GUIDE` in the same commit — stale instructions are worse
than none.
