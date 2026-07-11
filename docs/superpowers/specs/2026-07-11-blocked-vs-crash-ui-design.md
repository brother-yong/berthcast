# Blocked-vs-Crash Failure UI Design

**Date:** 11 July 2026
**Status:** Approved

## Problem

When an analysis fails, the progress page shows the same red "Analysis failed"
banner for two very different situations:

1. **Blocked** — the data safety net politely refused a file it can't read
   (missing columns, empty stock column, non-numeric stock, empty file). This
   is recoverable: the user fixes their file and re-uploads. The backend
   already writes plain-English guidance with a concrete fix into the error
   message, and sets `blocked: True` on the result.
2. **Crash** — a real failure on our side (agent exception, worker died,
   no usable JSON). Not the user's fault, nothing they can fix. The raw
   technical error string (e.g. `No inventory rows. desc_col=None, ...`)
   is shown to staff verbatim.

The `blocked` flag never reaches the frontend: `analysis_status` (app.py)
returns only `status` and `error`, so the page renders both cases
identically — a scary red failure. Staff can't tell "fix my file" from
"the tool is broken", which erodes trust and makes them stop using it.

## Goal

- A blocked refusal reads as calm, actionable guidance (amber), visually
  distinct from a real crash (red).
- A real crash never shows raw technical text to staff; it shows a friendly
  "our side, we've been alerted" message. The raw error still goes to the
  operator via the existing ALERT_EMAIL path and logs, unchanged.

## Design

### Backend — `analysis_status` route (app.py)

Two changes to the JSON payload built from the in-memory
`analysis_progress` entry:

1. **Pass the flag through.** Add `"blocked": bool(entry.get("blocked"))`
   to the payload. The flag is already stored in `analysis_progress` at
   failure time (app.py, the `"error" in result` branch); it is just not
   sent.
2. **Scrub crash text.** When `status == "error"` and `blocked` is falsy,
   replace the `error` field with a fixed friendly string:
   `"Something broke on our end. We've been alerted and we're looking at
   it. Please try again in a moment."`
   The raw error is untouched everywhere else — logs, ALERT_EMAIL, the DB.
   Blocked errors are sent through **unmodified** (they already contain the
   fix guidance).

The DB-fallback paths in `analysis_status` (worker died / stuck run) have
no `blocked` flag and already return a friendly message — they gain
`"blocked": false` and land in crash styling. Safe default: anything
unknown renders as a crash, never as "your file's fault".

### Frontend — error branch in `templates/analysis_progress.html`

Branch on `data.blocked` in the existing `status === 'error'` handler:

**Blocked (amber):**
- Title: **We couldn't read your file**
- Body: the full backend message, verbatim (it contains the fix).
- Action: single link **Go back and re-upload** → dashboard.
  No "Try again" link — re-running the same file fails the same way.
- Style: the existing `var(--warning)` CSS variable (already used on this
  page for the `.fstat.warn` stat), not the danger red.
- The overall status label reads "File needs a fix" instead of
  "Analysis failed", and the agent card pill follows the same tone.

**Crash (red, current styling):**
- Title: **Something broke on our end**
- Body: the friendly scrubbed message from the backend.
- Actions: **Try again** (re-run link, current behaviour) or dashboard.

Missing/absent `blocked` field → crash branch (default).

### Out of scope

- No changes to the blocked message texts in `agents/inventory.py` or
  `data_quality.py` — all verified to already include a concrete fix.
- No changes to alerting (`_alert_failure`), logging, or the results page.
- No new notification channels.

## Testing

The real logic seam is the backend payload. One plain-script test in the
project style (`tests/test_analysis_status_blocked.py`, `_check()` + exit
code):

- Blocked run → payload has `blocked: true` and the original guidance
  message unmodified.
- Crashed run → payload has `blocked: false`, the friendly generic string,
  and the raw error text absent from the payload.
- DB-fallback (worker died) → `blocked: false`, friendly message.

Frontend branch is JS in a template (no JS harness in this repo) — covered
by manual verification: force one blocked run and one crash locally,
screenshot both banners.
