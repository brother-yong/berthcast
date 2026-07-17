# Minimal landing page + "Keep me signed in" — design spec

**Date:** 2026-07-17
**Status:** Remember-me part approved. Landing part written for review.

## Why

Two UX asks:

1. Users must re-enter their password every time the browser closes. Add an
   opt-in "Keep me signed in" on the login page.
2. The landing page is overwhelming — too many words on screen. Rebuild it at
   the simplicity level of a minimal hero-first page: one headline, one line,
   two buttons, then a short trimmed body. Keep the existing dark green + gold
   brand.

---

## Part 1 — "Keep me signed in" (APPROVED)

### Behaviour

- A checkbox labelled **"Keep me signed in"** on the login form, on the same
  row as the "Forgot password?" link (checkbox left, link right).
- **Ticked:** the session cookie is made permanent with a 30-day lifetime.
  User stays signed in across browser restarts for 30 days.
- **Unticked (default):** exactly today's behaviour — session cookie dies when
  the browser closes.
- **Logout** clears the session either way — unchanged.

### Implementation

- `templates/login.html`: add the checkbox inside the existing form,
  `name="remember"`.
- `app.py` login route (POST success path): one line —
  `session.permanent = bool(request.form.get("remember"))` — set **before**
  the `session[...]` assignments so Flask writes the expiry with the same
  cookie.
- `app.py` config: `PERMANENT_SESSION_LIFETIME = timedelta(days=30)`.

### Security notes

- Same signed cookie as today (HttpOnly, SameSite=Lax, Secure in production).
  No new token store, no new DB table, no new attack surface beyond a
  longer-lived cookie that the user explicitly opts into.
- Password change or logout invalidates nothing extra — session.clear() on
  logout already covers the remembered cookie on that browser.
- security-reviewer agent runs before deploy (touches login).

### Tests

`tests/test_remember_me.py` (plain-script style, `_check()` + `sys.exit(1)`):

1. Login POST **with** `remember=on` → `Set-Cookie` for the session includes
   an `Expires`/`Max-Age` ≈ 30 days.
2. Login POST **without** `remember` → `Set-Cookie` has no `Expires`/`Max-Age`
   (browser-session cookie).
3. Logout after a remembered login → session cleared (protected page
   redirects to login).

---

## Part 2 — Minimal landing page (FOR REVIEW)

### Goal

Cut the landing from ~8 scroll-screens of dense copy to ~3 calm screens.
Word count down roughly 70%. Keep the dark green + gold look so the landing
matches the app behind login.

### New structure (top to bottom)

1. **Nav** — logo · "How it works" anchor · "Pricing" link · "Sign in" button.
   (Drop: "Worked example" and "Features" nav links.)
2. **Hero — full viewport, pure text, left-aligned (like the reference):**
   - Serif headline, kept as-is: "Stop losing revenue to *stockouts* you
     could have seen coming."
   - One sub line: "berthcast reads your ERP exports and writes the order
     you should place."
   - Two buttons: **Get in touch →** (primary, gold) · **See a real decision**
     (secondary, anchors to worked example).
   - Background: the existing radial gold glow + noise, slightly richer so the
     empty space feels deliberate. CSS only — no images, no JS.
3. **How it works** — 3 numbered steps, ONE line each
   (upload files → it reads and scores → approve and order).
4. **Worked example** — the two-panel "why order 1,570 CTN" block, kept, with
   copy roughly halved. This is the only product proof left on the page.
5. **CTA** — one headline, one line, one primary button.
6. **Footer** — unchanged (logo, colophon, links).

### Deleted outright

- Running head (top strip of tiny caps text)
- Animated "morning report" hero card + its CSS animations + the count-up
  `<script>`
- Stats/figures strip (16 wks · 400+ SKUs · 3 passes · folio)
- "Problem" section (both paragraphs + pullquote)
- 6-card features grid
- "Proof" screenshots section (both product screenshots)

### Kept untouched

- Colour system, fonts (Cormorant Garamond + Inter), button styles
- All meta/OG/SEO tags in `<head>`
- Footer
- `prefers-reduced-motion` handling (trivially, since animations are gone)

### Accepted trade-off

Features grid and screenshots were the only places the product's face appeared
on the page. A cold visitor now sees less proof. Accepted: current customer
acquisition is direct-contact, not cold web traffic. Fully reversible via git.

### Tests

`tests/test_landing_minimal.py` (plain-script style):

1. Landing route returns 200 and contains hero headline, "How it works",
   the worked example number, and the CTA button.
2. Deleted content is truly gone: no features-grid markup, no screenshots
   section, no stats strip, no count-up script.
3. Nav has exactly the kept links (How it works, Pricing, Sign in).

### Out of scope

- No changes to pricing/about/contact pages.
- No light-theme variant (dark green + gold confirmed).
- No new imagery or illustration work.
