# Pricing + Trial Model — remove public free access, admin-granted time-boxed trials

**Date:** 2026-06-23
**Status:** Design approved (pending spec review)

## Goal

berthcast is a B2B inventory tool that distributors trust with reorder decisions. A
self-serve "$0 forever" tier makes it read like a toy and lets anyone take access
without the founder involved. We want access to be **granted, never taken**: people
either pay, or they are given a time-boxed trial that the operator sets up by hand and
controls. This fits the current stage (every client is hand-sold; the existential goal
is landing paying client #2, not self-serve growth).

This supersedes the earlier note that the public pages would keep a free tier. The
strategy was always "free *trial*, not free *tier*" — this aligns the product with it.

## Decisions (the model)

1. **No public free access.** The pricing page shows paid plans only. There is no
   self-serve sign-up.
2. **Trials are operator-granted.** The operator creates the account in the admin panel
   and optionally stamps a **trial end date** on it.
3. **A trial is a pure time window.** Full product access (same as a paying client) until
   the end date. The clock is *calendar time*, not a usage count — berthcast's value
   proves itself over weeks (a stockout warning has to actually come true), so a
   time window is what lets the "aha" land.
4. **Trials can be made permanent.** One admin action clears the end date → the account
   becomes a normal client with no clock.
5. **Soft lock on expiry (option B).** When the end date passes, the user can still log
   in and view past reports, but running new analyses / uploads / chat is blocked behind
   a "trial ended" notice with a **Contact berthcast** button.
6. **Copy never names a person.** All user-facing copy says "berthcast" / "Contact
   berthcast" (→ Contact page or admin@berthcast.com). Never an individual's name.

## Scope of changes

### A. Pricing page (`templates/pricing.html`)
- Remove the **Free ($0)** card. Grid becomes 2 cards: **Professional** and **Enterprise**
  (both keep the existing "Talk to us" CTA → Contact page).
- Add one quiet line beneath the cards: *"Want to see it on your own data first? Get in
  touch and we'll set you up with a trial."*

### B. Remove public sign-up
- Repoint every "Get started" / "Sign up" link (landing nav + hero, pricing, base nav,
  anywhere else) to the **Contact** page.
- The `/register` route (and `register.html`) is retired. Simplest safe option:
  `/register` redirects to `/contact` so any stale link/bookmark still lands somewhere
  sensible rather than 404-ing. (Implementation plan to confirm there are no remaining
  inbound dependencies.)
- **Login is untouched** — existing users sign in exactly as before.

### C. Trial mechanic (the only real build)
- **DB:** add `users.trial_ends_at TIMESTAMP NULL` via the `init_db()` migration list.
  `NULL` = permanent account (no clock). Non-NULL = trial that soft-locks at that date.
  Existing accounts (incl. a regional food distributor, which is permanent) are unaffected — NULL by default.
- **Create account (admin):** the existing `create_user` form gains an optional
  "Trial ends on [date]" input. Blank = permanent.
- **Per-account admin actions:**
  - **Make permanent** → sets `trial_ends_at = NULL`.
  - **Change trial date** → set/extend/shorten the date (also re-activates an expired
    trial if moved to the future).
- **Session:** stamp `trial_ends_at` into the session on login (alongside tier/role).
- **Countdown banner (`base.html`, all logged-in pages):** while a trial is active, show
  *"Your free trial ends on DD/MM/YYYY (N days left)."* Permanent accounts and the admin
  show nothing.
- **Expiry (soft lock, option B):**
  - Banner switches to *"Your free trial ended on DD/MM/YYYY"* with a **Contact berthcast**
    button (→ Contact page).
  - The value-producing, money-costing actions are blocked with the same message:
    **run analysis, upload, dedup, chat, CSV export**. Reuse the existing tier-gate
    pattern (these routes already flash + redirect / return JSON when a cap is hit).
  - **Read-only stays open:** dashboard, viewing past results, diff, supplier scores.

### D. Tiers
- Trial accounts get **full access** during the trial (treat like the unlimited tier) so
  they experience the real product.
- The old self-serve `free` tier (1 analysis / 20 chat messages) is **retired for new
  accounts** — nothing creates a free account anymore. Existing free-tier code can remain
  dormant; no migration of old rows is required.

## Copy (exact, no names)
- Active trial banner: `Your free trial ends on {DD/MM/YYYY} — {N} days left.`
- Expired banner: `Your free trial ended on {DD/MM/YYYY}.` + button `Contact berthcast`.
- Blocked action: `Your trial has ended. Contact berthcast to keep running analyses.`
- Date format: DD/MM/YYYY (Singapore).

## Edge cases
- Trial date set in the past at creation → account is immediately in the soft-locked state.
- "Make permanent" mid-trial → banner disappears, full access continues.
- Extending a date after expiry → trial re-activates, banner returns to countdown.
- Admin and permanent (NULL-date) accounts never see a trial banner and are never gated.

## Testing
- The expiry gate is effectively pure (compare `now` vs `trial_ends_at`) → unit-testable
  like the existing per-org caps. Add a test that:
  - a past-date account is **blocked** from running an analysis but **can** view results;
  - a future-date account runs analyses normally **and** the banner shows the right date.
- `run_tests.py` must stay green (colour/flow changes never touch the Python pipeline).

## Out of scope / future
- **Self-serve sign-up** — deliberately removed. If self-serve growth is wanted later,
  it's a fresh build (public registration + abuse controls).
- **Payment integration** — still manual (invoice / PayNow), unchanged by this work.
