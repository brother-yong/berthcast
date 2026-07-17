# Minimal Landing + Remember-Me Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in 30-day "Keep me signed in" to login, and rebuild the landing page as a minimal pure-text-hero page (~70% fewer words), per `docs/superpowers/specs/2026-07-17-minimal-landing-remember-me-design.md`.

**Architecture:** Remember-me is Flask's built-in permanent-session mechanism — a checkbox sets `session.permanent`, config sets the 30-day lifetime; no new tokens or tables. The landing page is a standalone template (does not extend base.html); it gets rewritten in place keeping the same colour system, fonts, head metadata, and footer.

**Tech Stack:** Flask, Jinja2, plain-script tests (`_check()` + `sys.exit(1)`, run by `run_tests.py`).

**Test style note:** Tests are plain Python scripts, NOT pytest. Run each with `python tests/<file>.py`; exit code 0 = pass. `run_tests.py` auto-discovers `tests/test_*.py`.

---

### Task 1: Remember-me — failing test

**Files:**
- Test: `tests/test_remember_me.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""'Keep me signed in' on the login page.

Ticked  -> permanent session cookie, ~30-day expiry survives browser restart.
Unticked -> plain browser-session cookie (today's behaviour, no Expires).
Logout  -> session gone either way.

Throwaway temp DB + stubbed anthropic; CSRF disabled for the test client only.
Run: python tests/test_remember_me.py
"""
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_remember_me.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")
    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass
    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                                   # noqa: E402
import app as appmod                                    # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
flask_app = appmod.app

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


db.execute("INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
           ("staff@example.com.sg", generate_password_hash("correct-horse-9"),
            "a regional food distributor", "claude-sonnet-4-6"))


def _session_cookie(resp):
    """The Set-Cookie line for the Flask session, or None."""
    for c in resp.headers.getlist("Set-Cookie"):
        if c.startswith("session="):
            return c
    return None


# 1) remember ticked -> permanent cookie with ~30-day expiry
c1 = flask_app.test_client()
r = c1.post("/login", data={"email": "staff@example.com.sg",
                            "password": "correct-horse-9", "remember": "on"})
_check("remembered login redirects", r.status_code == 302, detail=str(r.status_code))
ck = _session_cookie(r)
_check("session cookie set", ck is not None)
_check("remembered cookie has an expiry", ck is not None and "Expires=" in ck, detail=str(ck))
if ck and "Expires=" in ck:
    raw = [p for p in ck.split("; ") if p.startswith("Expires=")][0][len("Expires="):]
    exp = parsedate_to_datetime(raw)
    days = (exp - datetime.now(timezone.utc)).days
    _check("expiry is ~30 days out", 28 <= days <= 31, detail=f"{days} days")

# 2) remember NOT ticked -> browser-session cookie, no expiry
c2 = flask_app.test_client()
r = c2.post("/login", data={"email": "staff@example.com.sg",
                            "password": "correct-horse-9"})
_check("plain login redirects", r.status_code == 302, detail=str(r.status_code))
ck = _session_cookie(r)
_check("plain cookie has NO expiry", ck is not None and "Expires=" not in ck and "Max-Age=" not in ck,
       detail=str(ck))

# 3) logout after a remembered login -> protected page bounces to login
r = c1.get("/logout", follow_redirects=False)
_check("logout redirects", r.status_code == 302, detail=str(r.status_code))
r = c1.get("/dashboard", follow_redirects=False)
_check("protected page redirects to login after logout",
       r.status_code == 302 and "/login" in r.headers.get("Location", ""),
       detail=f"{r.status_code} -> {r.headers.get('Location')}")

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll remember-me tests passed.")
```

- [ ] **Step 2: Run it — must FAIL**

Run: `python tests/test_remember_me.py`
Expected: FAIL on "remembered cookie has an expiry" (no checkbox handling yet, cookie has no Expires).

---

### Task 2: Remember-me — implementation

**Files:**
- Modify: `app.py:9` (import), `app.py:127-131` (config), `app.py:756-758` (login route)
- Modify: `templates/login.html:26-31`

- [ ] **Step 1: Import timedelta**

`app.py` line 9, change:

```python
from datetime import datetime
```
to:
```python
from datetime import datetime, timedelta
```

- [ ] **Step 2: Add the 30-day lifetime to the cookie config block**

`app.py` (~line 127), change:

```python
app.config.update(
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
```
to:
```python
app.config.update(
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Only applies when a login opts in via "Keep me signed in" — the default
    # session stays a browser-session cookie (clears on close).
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
```

Also update the comment above the block: the sentence "Session length is left as a browser-session cookie (clears on close)." becomes "Sessions default to browser-session cookies; 'Keep me signed in' opts into a 30-day permanent cookie."

- [ ] **Step 3: Set session.permanent on successful login**

`app.py` login route — immediately after `rate_limit.clear(...)` and BEFORE the `session["user_id"] = ...` block (must come first so Flask writes the expiry onto the same cookie):

```python
            rate_limit.clear(ip)
            if acct_key:
                rate_limit.clear(acct_key)
            # "Keep me signed in" — opt-in 30-day cookie (PERMANENT_SESSION_LIFETIME).
            # Unticked keeps the default browser-session cookie.
            session.permanent = bool(request.form.get("remember"))
            session["user_id"]  = u["id"]
```

- [ ] **Step 4: Add the checkbox to the login form**

`templates/login.html` — replace the "Forgot password?" paragraph (lines 26-31):

```html
      <p style="text-align:right; margin-top:10px; margin-bottom:0;">
        <a href="{{ url_for('forgot_password') }}"
           style="font-size:13px; color:var(--muted); text-decoration:none;">
          Forgot password?
        </a>
      </p>
```
with:
```html
      <div style="display:flex; justify-content:space-between; align-items:center; margin-top:10px;">
        <label style="display:flex; align-items:center; gap:7px; font-size:13px; color:var(--muted); cursor:pointer; margin:0;">
          <input type="checkbox" name="remember" value="on"
                 style="width:15px; height:15px; accent-color:var(--brass);">
          Keep me signed in
        </label>
        <a href="{{ url_for('forgot_password') }}"
           style="font-size:13px; color:var(--muted); text-decoration:none;">
          Forgot password?
        </a>
      </div>
```

- [ ] **Step 5: Run the test — must PASS**

Run: `python tests/test_remember_me.py`
Expected: `All remember-me tests passed.`

- [ ] **Step 6: Run the full suite**

Run: `python run_tests.py`
Expected: all tests pass (68 before this plan; 69 after).

- [ ] **Step 7: Commit**

```powershell
git add tests/test_remember_me.py app.py templates/login.html
git commit -m "Add opt-in 30-day 'Keep me signed in' to login"
```

---

### Task 3: Minimal landing — failing test

**Files:**
- Test: `tests/test_landing_minimal.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""The landing page is the minimal version: pure-text hero + 3 trimmed
sections. Locks in what was deleted (features grid, screenshots, stats strip,
problem section, hero report card + animation scripts) so it can't creep back.

Run: python tests/test_landing_minimal.py
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")
    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass
    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import app as appmod  # noqa: E402

appmod.app.config["TESTING"] = True
client = appmod.app.test_client()

F = []


def _check(c, m):
    print(("ok: " if c else "FAIL: ") + m)
    if not c:
        F.append(m)


r = client.get("/")
html = r.get_data(as_text=True)
_check(r.status_code == 200, "landing returns 200")

# what stays
_check("Stop losing revenue" in html, "hero headline kept")
_check("berthcast reads your ERP exports and writes the order you should place." in html,
       "one-line hero sub")
_check("How it works" in html, "how-it-works section kept")
_check("1,570" in html, "worked example number kept")
_check("Get in touch" in html, "primary CTA kept")

# what must be GONE
_check("feat-grid" not in html, "features grid deleted")
_check("screenshot-inventory" not in html, "screenshots section deleted")
_check("strip-inner" not in html, "stats strip deleted")
_check("running-head" not in html, "running head deleted")
_check("pullquote" not in html, "problem section deleted")
_check("snapQty" not in html, "hero report card + count-up script deleted")
_check("stampIn" not in html, "card animations deleted")

# nav: exactly the kept links
_check('href="#how"' in html, "nav links to #how")
_check("#features" not in html, "features nav link gone")

if F:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll minimal-landing tests passed.")
```

- [ ] **Step 2: Run it — must FAIL**

Run: `python tests/test_landing_minimal.py`
Expected: FAIL on "features grid deleted" (and the other must-be-gone checks) — the old page still has them.

---

### Task 4: Minimal landing — rewrite the template

**Files:**
- Modify: `templates/landing.html` (full rewrite — replace the whole file)

- [ ] **Step 1: Replace `templates/landing.html` with exactly this**

Keep in mind: `<head>` metadata is identical to the old file; the CSS keeps the same variables and component styles for what survives; everything about the deleted sections is removed.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>berthcast — Inventory intelligence for food distributors</title>
  <meta name="description" content="berthcast reads your inventory, purchase, and sales exports, flags what's about to stock out, and writes the order you should place — before it costs you a sale.">
  <link rel="canonical" href="https://berthcast.com/">
  <link rel="icon" type="image/png" href="{{ url_for('static', filename='favicon.png') }}">

  <meta property="og:type" content="website">
  <meta property="og:site_name" content="berthcast">
  <meta property="og:title" content="berthcast — Inventory intelligence for food distributors">
  <meta property="og:description" content="Stop losing revenue to stockouts you could have seen coming. berthcast flags what's going critical and writes the order you should place.">
  <meta property="og:url" content="https://berthcast.com/">
  <meta property="og:image" content="https://berthcast.com/static/logo.png">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="berthcast — Inventory intelligence for food distributors">
  <meta name="twitter:description" content="Stop losing revenue to stockouts you could have seen coming. berthcast flags what's going critical and writes the order you should place.">
  <meta name="twitter:image" content="https://berthcast.com/static/logo.png">

  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
  :root{
    --green-deep:#05140A; --green-900:#081C0F; --green-800:#0D2818; --green-700:#12351F;
    --gold:#C9A227; --gold-light:#DcBE63;
    --ivory:#F3EFDF; --ivory-dim:rgba(243,239,223,.66); --ivory-faint:rgba(243,239,223,.42);
    --moss:#8FBC8F; --crit:#E89A92;
    --gold-line:rgba(201,162,39,.22); --hair:rgba(243,239,223,.12);
    --serif:'Cormorant Garamond', Georgia, serif;
    --sans:'Inter', system-ui, sans-serif;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{ background:var(--green-deep); color:var(--ivory); font-family:var(--sans); font-weight:400;
        line-height:1.6; -webkit-font-smoothing:antialiased; position:relative; overflow-x:hidden; font-size:16px; }
  img{max-width:100%;display:block}
  /* the page's only decoration now: one warm dawn-glow behind the hero text,
     one cool counterweight low-left, and the grain */
  body::before{ content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
    background:
      radial-gradient(ellipse at 76% 14%, rgba(201,162,39,.16), transparent 52%),
      radial-gradient(ellipse at 8% 90%, rgba(26,77,46,.3), transparent 55%); }
  body::after{ content:''; position:fixed; inset:0; pointer-events:none; z-index:0; opacity:.018;
    background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"); }

  .wrap{ position:relative; z-index:2; max-width:1180px; margin:0 auto; padding-left:40px; padding-right:40px; }

  .eyebrow{ font-family:var(--sans); font-size:11.5px; font-weight:600; letter-spacing:.12em; text-transform:uppercase;
            color:var(--gold); display:flex; align-items:center; gap:14px; }
  .eyebrow::before{ content:''; width:30px; height:1px; background:var(--gold); }
  .eyebrow.center{ justify-content:center; } .eyebrow.center::before{ display:none; }
  h2.sec-title{ font-family:var(--serif); font-weight:600; font-size:clamp(34px,4.4vw,52px); line-height:1.04;
                letter-spacing:-.005em; color:var(--ivory); margin-top:16px; }
  h2.sec-title em{ font-style:normal; color:var(--gold); }

  /* nav */
  nav{ position:relative; z-index:3; max-width:1180px; margin:0 auto; padding:14px 40px;
       display:flex; align-items:center; justify-content:space-between; }
  .brand{ display:inline-flex; align-items:center; text-decoration:none; }
  .brand img{ height:44px; width:auto; }
  .nav-right{ display:flex; align-items:center; gap:6px; }
  .nav-link{ font-family:var(--sans); font-size:13px; color:var(--ivory-dim); text-decoration:none; padding:8px 14px; transition:color .2s; }
  .nav-link:hover{ color:var(--gold); }
  .btn{ font-family:var(--sans); font-weight:600; text-decoration:none; cursor:pointer; border-radius:9px;
        display:inline-flex; align-items:center; gap:8px; border:1px solid transparent; transition:all .2s; }
  .btn-nav{ background:transparent; color:var(--ivory); font-size:13px; padding:9px 16px; border-color:var(--gold-line); }
  .btn-nav:hover{ border-color:var(--gold); color:var(--gold); }
  .btn-primary{ background:var(--gold); color:var(--green-deep); font-size:15px; padding:14px 26px; }
  .btn-primary:hover{ background:var(--gold-light); }
  .btn-secondary{ background:transparent; color:var(--ivory); font-size:15px; padding:14px 22px; border-color:var(--gold-line); }
  .btn-secondary:hover{ border-color:var(--gold); }

  /* hero — pure text, fills the first screen (Minds-level minimal) */
  .hero{ position:relative; z-index:2; max-width:1180px; margin:0 auto; padding:0 40px 40px;
         min-height:calc(100vh - 150px); display:flex; flex-direction:column; justify-content:center; }
  .hero .eyebrow{ margin-bottom:26px; }
  h1.hero-title{ font-family:var(--serif); font-weight:600; font-size:clamp(46px,6.4vw,86px); line-height:1.02;
                 letter-spacing:-.008em; color:var(--ivory); max-width:12.5em; }
  h1.hero-title em{ font-style:normal; font-weight:600; color:var(--gold); }
  .hero-sub{ font-size:18px; color:var(--ivory-dim); line-height:1.6; margin-top:24px; max-width:32em; }
  .hero-ctas{ display:flex; gap:14px; flex-wrap:wrap; margin-top:34px; }

  /* how it works */
  .section-head{ text-align:center; max-width:640px; margin:0 auto 56px; }
  .section-head .eyebrow{ margin-bottom:14px; }
  .section-head p{ font-size:16px; color:var(--ivory-dim); margin-top:14px; }
  .how{ padding:90px 0; border-top:1px solid var(--hair); }
  .how-steps{ display:grid; grid-template-columns:repeat(3,1fr); gap:36px; }
  .step__n{ font-family:var(--serif); font-weight:600; font-size:54px; color:var(--gold); line-height:1; }
  .step h3{ font-family:var(--serif); font-weight:600; font-size:24px; color:var(--ivory); margin:14px 0 8px; }
  .step p{ font-size:14px; color:var(--ivory-dim); line-height:1.6; }
  .step__rule{ width:40px; height:1px; background:var(--gold-line); margin:18px 0; }

  /* worked example */
  .demo{ padding:90px 0; border-top:1px solid var(--hair); }
  .demo-grid{ display:grid; grid-template-columns:1fr 1fr; gap:24px; align-items:stretch; }
  .panel{ background:rgba(13,40,24,.5); border:1px solid var(--gold-line); border-radius:10px; padding:28px; }
  .panel.read{ background:linear-gradient(180deg, var(--green-800), var(--green-900)); }
  .panel-tag{ font-family:var(--sans); font-size:10px; font-weight:500; letter-spacing:.14em; text-transform:uppercase; color:var(--ivory-faint); margin-bottom:18px; }
  .sku-name{ font-family:var(--serif); font-weight:600; font-size:24px; color:var(--ivory); }
  .sku-meta{ font-family:var(--sans); font-size:12px; color:var(--ivory-faint); margin-top:4px; }
  .sku-facts{ display:grid; grid-template-columns:1fr 1fr; gap:18px 30px; margin-top:24px; }
  .sku-fact .l{ font-size:10px; letter-spacing:.06em; text-transform:uppercase; color:var(--ivory-faint); }
  .sku-fact .v{ font-family:var(--serif); font-weight:600; font-size:26px; color:var(--ivory); margin-top:3px; font-variant-numeric:tabular-nums; }
  .sku-fact .v span{ font-size:13px; color:var(--ivory-faint); }
  .ex-num{ font-family:var(--serif); font-weight:700; font-size:56px; color:var(--gold); line-height:1; margin:16px 0 2px; font-variant-numeric:tabular-nums; }
  .ex-num span{ font-size:18px; color:var(--ivory-faint); font-weight:600; }
  .pill{ display:inline-flex; align-items:center; gap:6px; font-family:var(--sans); font-size:11px; font-weight:600; letter-spacing:.06em;
         padding:5px 11px; border-radius:100px; background:rgba(232,154,146,.16); color:var(--crit); }
  .read-line{ font-size:13.5px; color:var(--ivory-dim); margin-top:12px; max-width:24em; line-height:1.5; }
  .read-line b{ color:var(--ivory); }
  .read-out{ display:flex; gap:28px; margin-top:24px; padding-top:20px; border-top:1px solid var(--hair); }
  .read-out .l{ font-family:var(--sans); font-size:10px; font-weight:500; letter-spacing:.08em; text-transform:uppercase; color:var(--ivory-faint); }
  .read-out .v{ font-family:var(--serif); font-weight:600; font-size:28px; margin-top:5px; color:var(--gold); }
  .read-out .v.crit{ color:var(--crit); }

  /* cta */
  .cta{ padding:104px 0; border-top:1px solid var(--hair); text-align:center; }
  .cta h2{ font-family:var(--serif); font-weight:600; font-size:clamp(38px,5vw,60px); line-height:1.05; color:var(--ivory); }
  .cta h2 em{ font-style:normal; color:var(--gold); }
  .cta p{ font-size:17px; color:var(--ivory-dim); margin:18px auto 32px; max-width:34em; }
  .cta-btns{ display:flex; gap:14px; justify-content:center; flex-wrap:wrap; }

  /* footer */
  footer.site{ border-top:1px solid var(--hair); padding:40px 0; }
  .foot{ max-width:1180px; margin:0 auto; padding:0 40px; display:flex; align-items:center; justify-content:space-between; gap:20px; flex-wrap:wrap; }
  .foot img{ height:34px; }
  .foot .colophon{ order:3; flex-basis:100%; text-align:center; margin-top:8px; }
  .foot-links{ display:flex; gap:22px; flex-wrap:wrap; }
  .foot-links a{ font-size:13px; color:var(--ivory-dim); text-decoration:none; }
  .foot-links a:hover{ color:var(--gold); }
  .colophon{ font-family:var(--serif); font-style:italic; color:var(--ivory-faint); font-size:14px; }

  @media (max-width:900px){
    .how-steps,.demo-grid{ grid-template-columns:1fr; gap:36px; }
    nav,.wrap,.foot{ padding-left:22px; padding-right:22px; }
    .hero{ padding:0 22px 34px; min-height:calc(100svh - 120px); }
    .nav-link{ display:none; }
    .foot{ flex-direction:column; align-items:flex-start; gap:14px; }
    .foot .colophon{ text-align:left; margin-top:0; }
  }
  @media (prefers-reduced-motion: reduce){ html{ scroll-behavior:auto; } }
  </style>
</head>
<body>

<nav>
  <a class="brand" href="{{ url_for('landing') }}"><img src="{{ url_for('static', filename='logo-dark.png') }}" alt="berthcast"></a>
  <div class="nav-right">
    <a class="nav-link" href="#how">How it works</a>
    <a class="nav-link" href="{{ url_for('pricing') }}">Pricing</a>
    <a class="btn btn-nav" href="{{ url_for('login') }}">Sign in →</a>
  </div>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="eyebrow">AI inventory operations for food distributors</div>
  <h1 class="hero-title">Stop losing revenue to <em>stockouts</em> you could have seen coming.</h1>
  <p class="hero-sub">berthcast reads your ERP exports and writes the order you should place.</p>
  <div class="hero-ctas">
    <a class="btn btn-primary" href="{{ url_for('contact') }}">Get in touch →</a>
    <a class="btn btn-secondary" href="#demo">See a real decision</a>
  </div>
</section>

<!-- HOW -->
<section class="how" id="how"><div class="wrap">
  <div class="section-head"><div class="eyebrow center">How it works</div><h2 class="sec-title">From your exports to a written order</h2></div>
  <div class="how-steps">
    <div class="step"><div class="step__n">01</div><div class="step__rule"></div><h3>Upload your files</h3><p>Drop in your inventory, purchase, and sales exports.</p></div>
    <div class="step"><div class="step__n">02</div><div class="step__rule"></div><h3>It reads and scores</h3><p>Every SKU scored against demand and real lead times.</p></div>
    <div class="step"><div class="step__n">03</div><div class="step__rule"></div><h3>Approve and order</h3><p>Review, adjust, and send the order it writes.</p></div>
  </div>
</div></section>

<!-- WORKED EXAMPLE -->
<section class="demo" id="demo"><div class="wrap">
  <div class="section-head"><div class="eyebrow center">A worked example</div><h2 class="sec-title">Why berthcast says order <em>1,570</em></h2><p>One real line from a live run. The same logic runs on every SKU.</p></div>
  <div class="demo-grid">
    <div class="panel">
      <div class="panel-tag">The line · as berthcast reads it</div>
      <div class="sku-name">Brookvale UHT Milk Full Cream 1L</div>
      <div class="sku-meta">UHT-CH-1L · case of 12 · import supplier</div>
      <div class="sku-facts">
        <div class="sku-fact"><div class="l">On hand</div><div class="v">0</div></div>
        <div class="sku-fact"><div class="l">Days of cover</div><div class="v">0</div></div>
        <div class="sku-fact"><div class="l">Sells</div><div class="v">~300<span> /mo</span></div></div>
        <div class="sku-fact"><div class="l">Lead time</div><div class="v">16<span> wks</span></div></div>
      </div>
    </div>
    <div class="panel read">
      <div class="panel-tag">berthcast's read</div>
      <span class="pill">● Critical</span>
      <div class="read-line" style="margin-top:14px">Nothing on the shelf, next shipment <b>~16 weeks</b> out. Sized to cover demand until stock lands:</div>
      <div class="ex-num">1,570 <span>CTN</span></div>
      <div class="read-out">
        <div><div class="l">Decision</div><div class="v">Order now</div></div>
        <div><div class="l">Wait one week and</div><div class="v crit">~70 CTN walk out</div></div>
      </div>
    </div>
  </div>
</div></section>

<!-- CTA -->
<section class="cta"><div class="wrap">
  <h2>Ready to stop <em>guessing</em> your reorders?</h2>
  <p>Get in touch and we'll run it on your own data.</p>
  <div class="cta-btns"><a class="btn btn-primary" href="{{ url_for('contact') }}">Get in touch →</a></div>
</div></section>

<!-- FOOTER -->
<footer class="site"><div class="foot">
  <img src="{{ url_for('static', filename='logo-dark.png') }}" alt="berthcast">
  <span class="colophon">Inventory operations for food distributors</span>
  <div class="foot-links">
    <a href="{{ url_for('about') }}">About</a><a href="{{ url_for('data_promise') }}">Your data</a>
    <a href="{{ url_for('pricing') }}">Pricing</a><a href="{{ url_for('contact') }}">Contact</a>
    <a href="{{ url_for('terms') }}">Terms</a><a href="{{ url_for('privacy') }}">Privacy</a>
    <a href="{{ url_for('login') }}">Sign in</a>
  </div>
</div></footer>
</body>
</html>
```

- [ ] **Step 2: Run the landing test — must PASS**

Run: `python tests/test_landing_minimal.py`
Expected: `All minimal-landing tests passed.`

- [ ] **Step 3: Eyeball it in a browser**

Run: `python app.py` then open `http://127.0.0.1:5000/`
Check: hero fills the first screen with text only; #how and #demo anchors scroll; nothing visually broken at a ~400px-wide window.

- [ ] **Step 4: Run the full suite**

Run: `python run_tests.py`
Expected: all pass (70 total now).

- [ ] **Step 5: Commit**

```powershell
git add tests/test_landing_minimal.py templates/landing.html
git commit -m "Rebuild landing as minimal pure-text-hero page"
```

---

### Task 5: Security review + ship

- [ ] **Step 1: security-reviewer agent** — the login change touches auth; run it over `app.py` (login route + config) and `templates/login.html` before deploy. Fix anything it flags.

- [ ] **Step 2: Push** (user runs):

```powershell
git push
```

- [ ] **Step 3: Update MEMORY.md** with what shipped (local only, never committed).
