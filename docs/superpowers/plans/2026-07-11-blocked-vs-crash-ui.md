# Blocked-vs-Crash Failure UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a data-fixable file refusal (blocked) render as calm amber guidance, visually distinct from a real crash (red), and stop showing raw technical crash text to staff.

**Architecture:** The backend already stores a `blocked` flag in the in-memory `analysis_progress` dict at failure time but never sends it to the page. Two small backend changes in the `analysis_status` route (pass the flag; scrub raw crash text) plus a branch in the template's error handler. No new files except the test.

**Tech Stack:** Flask (app.py), vanilla JS in a Jinja template, plain-script tests (`_check()` + exit code, auto-run by `run_tests.py`).

**Spec:** `docs/superpowers/specs/2026-07-11-blocked-vs-crash-ui-design.md`

---

## Context for a zero-context engineer

- An analysis runs in a background thread; its progress lives in the
  module-level dict `analysis_progress` (app.py:211), guarded by
  `analysis_progress_lock` (app.py:212).
- On failure, the runner sets `analysis_progress[sid]["status"] = "error"`,
  `["error"] = <message>`, and `["blocked"] = True/False` (app.py, the
  `"error" in result` branch of `run_analysis`). `blocked=True` means the
  data safety net refused the file — the message is plain English and
  already contains the fix. `blocked=False` (or missing) means a real crash
  and the message may be raw technical text.
- The page polls `GET /analysis_status/<sid>` (app.py:2638) every ~1.2s.
  That route builds a JSON payload from the in-memory entry — currently
  WITHOUT the `blocked` flag — or falls back to the DB when the in-memory
  entry is gone (worker restarted).
- The frontend error branch is in `templates/analysis_progress.html`
  (`if (data.status === 'error')`, ~line 517): one red banner for
  everything.
- Test conventions: plain scripts `tests/test_*.py`, no pytest. Each
  defines `_check(name, cond, detail)` and exits non-zero on failure.
  `tests/test_stuck_analysis.py` is the model — it already tests this exact
  route with a Flask test client and a stubbed `anthropic` module.

### Files

- Modify: `app.py` (constant near line 244; `analysis_status` route ~2638–2703)
- Modify: `templates/analysis_progress.html` (CSS ~line 211; `setCardStatus` ~line 333; error branch ~line 517)
- Create: `tests/test_analysis_status_blocked.py`

---

### Task 1: Backend — pass `blocked`, scrub crash text (TDD)

**Files:**
- Test: `tests/test_analysis_status_blocked.py` (create)
- Modify: `app.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_analysis_status_blocked.py` with exactly:

```python
"""Blocked-vs-crash split in /analysis_status (spec 2026-07-11).

A refused file (blocked) and a real crash used to reach the page as the same
red failure. The route must now (a) pass the ``blocked`` flag through, (b)
keep a blocked run's guidance message verbatim (it contains the fix), and
(c) replace raw crash text with a friendly generic — raw errors are for
logs and ALERT_EMAIL, never for staff.

Dependency-free:  python tests/test_analysis_status_blocked.py
"""
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_blocked.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db          # noqa: E402
import app as appmod           # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _mk_session(status, started_at=None):
    return db.execute(
        "INSERT INTO upload_sessions (user_id, org_name, status, analysis_started_at) VALUES (?,?,?,?)",
        (1, "Test Org", status, started_at)
    )


def _mem_entry(sid, **extra):
    entry = {"status": "running", "log": [], "started_at": time.time(),
             "agents": {}, "stats": {}, "current_agent": "inventory"}
    entry.update(extra)
    with appmod.analysis_progress_lock:
        appmod.analysis_progress[sid] = entry


appmod.app.config["TESTING"] = True
client = appmod.app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = 1
    sess["org_name"] = "Test Org"
    sess["model"] = "claude-sonnet-4-6"
    sess["is_admin"] = False
    sess["tier"] = "enterprise"
    sess["role"] = "admin"

GUIDANCE = ("We couldn't find both an item-name column and a current-stock "
            "column in your Inventory Report. Make sure one column lists "
            "product names and one lists how much is in stock now.")
RAW_CRASH = "No inventory rows. desc_col=None, qty_col=None, rows=0"

# ── 1. Blocked run: flag passes through, guidance kept verbatim ──────────────
s_blocked = _mk_session("failed")
_mem_entry(s_blocked, status="error", error=GUIDANCE, blocked=True)
body = client.get(f"/analysis_status/{s_blocked}").get_json()
_check("blocked run has blocked=true", body.get("blocked") is True, str(body))
_check("blocked run keeps its guidance verbatim", body.get("error") == GUIDANCE,
       str(body.get("error")))

# ── 2. Crash: raw text scrubbed, friendly generic shown ──────────────────────
s_crash = _mk_session("failed")
_mem_entry(s_crash, status="error", error=RAW_CRASH, blocked=False)
resp = client.get(f"/analysis_status/{s_crash}")
body = resp.get_json()
_check("crash has blocked=false", body.get("blocked") is False, str(body))
_check("crash error replaced by friendly generic",
       body.get("error") == appmod.CRASH_FRIENDLY_ERROR, str(body.get("error")))
_check("raw crash text absent from the whole payload",
       RAW_CRASH not in resp.get_data(as_text=True))

# ── 3. Missing blocked key (old-style entry) defaults to crash ───────────────
s_legacy = _mk_session("failed")
_mem_entry(s_legacy, status="error", error=RAW_CRASH)   # no 'blocked' key
body = client.get(f"/analysis_status/{s_legacy}").get_json()
_check("missing flag defaults to blocked=false", body.get("blocked") is False, str(body))
_check("missing flag still scrubs raw text",
       body.get("error") == appmod.CRASH_FRIENDLY_ERROR, str(body.get("error")))

# ── 4. Running entry untouched (scrub only applies to errors) ────────────────
s_running = _mk_session("analyzing", datetime.utcnow().isoformat())
_mem_entry(s_running)   # status='running', no error
body = client.get(f"/analysis_status/{s_running}").get_json()
_check("running entry not scrubbed", body.get("error") is None, str(body.get("error")))
_check("running entry carries blocked=false", body.get("blocked") is False, str(body))

# ── 5. DB fallback (worker died): friendly message, blocked=false ─────────────
s_dead = _mk_session("failed", (datetime.utcnow() - timedelta(hours=1)).isoformat())
body = client.get(f"/analysis_status/{s_dead}").get_json()
_check("worker-died fallback reports error", body.get("status") == "error", str(body))
_check("worker-died fallback has blocked=false", body.get("blocked") is False, str(body))
_check("worker-died fallback keeps its own friendly message",
       "run it again" in (body.get("error") or ""), str(body.get("error")))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll blocked-vs-crash payload tests passed.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_analysis_status_blocked.py`
Expected: FAILs — `blocked` is missing from payloads (`blocked=true` check fails with `None`) and `appmod.CRASH_FRIENDLY_ERROR` raises `AttributeError`. Either failure mode proves red.

- [ ] **Step 3: Implement the backend changes in `app.py`**

3a. Near `STUCK_ANALYSIS_SECONDS = 120` (line ~244), add below it:

```python
# Shown to staff in place of a raw crash message. The raw text still reaches
# the operator via logs + ALERT_EMAIL — it is scrubbed only from the page.
# Blocked refusals keep their message: it is written for the user and
# contains the fix. The bold title lives in the template.
CRASH_FRIENDLY_ERROR = ("We've been alerted and we're looking at it. "
                        "Please try again in a moment.")
```

3b. In `analysis_status` (route ~2638), the in-memory payload dict gains one key. Change:

```python
            payload = {
                "status":        entry["status"],
                "log":           list(entry["log"]),
                "elapsed":       round(time.time() - entry["started_at"], 1),
                "error":         entry.get("error"),
                "current_agent": entry.get("current_agent"),
                "agents":        {k: dict(v) for k, v in entry.get("agents", {}).items()},
                "stats":         dict(entry.get("stats") or {}),
            }
```

to:

```python
            payload = {
                "status":        entry["status"],
                "log":           list(entry["log"]),
                "elapsed":       round(time.time() - entry["started_at"], 1),
                "error":         entry.get("error"),
                "blocked":       bool(entry.get("blocked")),
                "current_agent": entry.get("current_agent"),
                "agents":        {k: dict(v) for k, v in entry.get("agents", {}).items()},
                "stats":         dict(entry.get("stats") or {}),
            }
```

3c. Immediately after the lock block, change:

```python
    if payload is not None:
        return jsonify(payload)
```

to:

```python
    if payload is not None:
        # A real crash must never show raw technical text to staff. A blocked
        # run keeps its message — the safety net wrote it for the user.
        if payload["status"] == "error" and not payload["blocked"]:
            payload["error"] = CRASH_FRIENDLY_ERROR
        return jsonify(payload)
```

3d. The `_interrupted` fallback dict (same route, ~line 2675) gains the flag. Change:

```python
    _interrupted = {
        "status": "error",
        "error": "The analysis stopped unexpectedly — the server may have restarted. Please run it again.",
        "log": [], "elapsed": 0, "agents": {}, "current_agent": None,
    }
```

to:

```python
    _interrupted = {
        "status": "error",
        "error": "The analysis stopped unexpectedly — the server may have restarted. Please run it again.",
        "blocked": False,
        "log": [], "elapsed": 0, "agents": {}, "current_agent": None,
    }
```

(The `complete`/`running`/`not_found` fallbacks need no flag — the frontend
only reads `blocked` in the error branch and treats a missing flag as a
crash.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_analysis_status_blocked.py`
Expected: all checks `ok:`, exit 0, final line `All blocked-vs-crash payload tests passed.`

- [ ] **Step 5: Run the neighbouring route test to prove no regression**

Run: `python tests/test_stuck_analysis.py`
Expected: `All stuck-analysis tests passed.`

- [ ] **Step 6: Commit**

```powershell
git add app.py tests/test_analysis_status_blocked.py
git commit -m "Pass blocked flag through analysis_status, scrub raw crash text"
```

---

### Task 2: Frontend — amber blocked banner, red crash banner

**Files:**
- Modify: `templates/analysis_progress.html`

All values below use the existing CSS variables: `--warning: #E6B450`
(static/style.css:36) → rgb(230,180,80); `--danger` and `--paper` are
already used on this page.

- [ ] **Step 1: Add blocked CSS states**

After the line (~211):

```css
.status-error   .agent-status-dot  { background: var(--danger); }
```

insert:

```css
/* Blocked = the safety net refused the file (user-fixable). Amber, not red —
   visually distinct from a real crash so staff know which side must act. */
.agent-card.status-blocked { border-color: var(--warning); background: rgba(230,180,80,0.08); }
.status-blocked .agent-num { background: var(--warning); border-color: var(--warning); color: var(--paper); }
.status-blocked .agent-status-pill { background: rgba(230,180,80,0.12); border-color: rgba(230,180,80,0.35); color: var(--warning); }
.status-blocked .agent-status-dot  { background: var(--warning); }
```

And after the line (~125):

```css
.overall-dot.error { background: var(--danger);  animation: none; }
```

insert:

```css
.overall-dot.blocked { background: var(--warning); animation: none; }
```

- [ ] **Step 2: Teach `setCardStatus` the blocked state**

In `setCardStatus` (~line 333), change the class-removal line:

```js
  card.classList.remove('status-pending', 'status-running', 'status-done', 'status-error');
```

to:

```js
  card.classList.remove('status-pending', 'status-running', 'status-done', 'status-error', 'status-blocked');
```

and the pill-text ternary:

```js
    pillText.textContent = (
      status === 'running' ? 'Running' :
      status === 'done'    ? 'Done'    :
      status === 'error'   ? 'Failed'  :
                             'Pending'
    );
```

to:

```js
    pillText.textContent = (
      status === 'running' ? 'Running' :
      status === 'done'    ? 'Done'    :
      status === 'error'   ? 'Failed'  :
      status === 'blocked' ? 'Needs a fix' :
                             'Pending'
    );
```

- [ ] **Step 3: Branch the error handler on `data.blocked`**

Replace the whole `if (data.status === 'error') { ... }` block (~lines 517–539):

```js
    if (data.status === 'error') {
      polling = false;
      const blocked = !!data.blocked;
      // Whatever agent was running, mark it — amber when the file was
      // refused (user-fixable), red when something actually broke.
      const active = data.current_agent;
      if (active && agentState[active]) {
        setCardStatus(active, blocked ? 'blocked' : 'error');
        setSummary(active, blocked
          ? "Can't use this file — see the reason below"
          : 'Error: ' + (data.error || 'Unknown error'));
      }
      document.getElementById('overall-dot').classList.add(blocked ? 'blocked' : 'error');
      document.getElementById('overall-label').textContent =
        blocked ? 'File needs a fix' : 'Analysis failed';
      // Surface the reason in a banner so the user actually sees it instead
      // of having to spot a tiny pill on whichever card was running.
      const banner = document.createElement('div');
      if (blocked) {
        // Recoverable: the message contains the fix. No "Try again" link —
        // re-running the same file fails the same way, so the action is
        // re-upload.
        banner.style.cssText =
          'margin-top:16px;padding:14px 18px;border:1px solid var(--warning);' +
          'background:rgba(230,180,80,0.10);color:var(--warning);border-radius:12px;font-size:14px;line-height:1.5;';
        banner.innerHTML =
          "<strong>We couldn't read your file.</strong> " + escapeHtml(data.error || '') +
          '<br><a href="/dashboard" style="color:var(--warning);font-weight:600;">Go back and re-upload</a>.';
      } else {
        banner.style.cssText =
          'margin-top:16px;padding:14px 18px;border:1px solid var(--danger);' +
          'background:rgba(232,154,146,0.12);color:var(--danger);border-radius:12px;font-size:14px;line-height:1.5;';
        banner.innerHTML =
          '<strong>Something broke on our end.</strong> ' + escapeHtml(data.error || 'Please try again in a moment.') +
          '<br><a href="' + window.location.pathname + '" style="color:var(--danger);font-weight:600;">Try again</a>' +
          ' or <a href="/dashboard" style="color:var(--danger);font-weight:600;">go back to dashboard</a>.';
      }
      document.querySelector('.page-wrap').appendChild(banner);
      return;
    }
```

- [ ] **Step 4: Sanity-check the template still parses**

Run: `python -c "import os, types, sys, tempfile; os.environ['DB_PATH']=tempfile.mktemp(); os.environ.setdefault('ANTHROPIC_API_KEY','x'); import app; app.app.jinja_env.get_template('analysis_progress.html'); print('template ok')"`
Expected: `template ok`

- [ ] **Step 5: Run the full suite**

Run: `python run_tests.py`
Expected: all tests pass (was 53 + the new file from Task 1).

- [ ] **Step 6: Commit**

```powershell
git add templates/analysis_progress.html
git commit -m "Amber 'fix your file' banner for blocked runs, friendly crash banner"
```

---

### Task 3: Verification and pre-deploy review

- [ ] **Step 1: Security review**

Dispatch the `security-reviewer` agent over the diff (app.py route payload
change + template JS). Expected: CLEAN — no auth/SQL/upload surface changed;
confirm the scrub does not leak the raw error anywhere new and that
`escapeHtml` still wraps every user-visible string.

- [ ] **Step 2: Manual visual check (operator, post-deploy)**

Not automatable here (JS in a template, no JS harness). After deploy:
upload a sheet with no stock column → expect the amber "We couldn't read
your file" banner with the column guidance and a re-upload link. Any real
crash → red "Something broke on our end" with no technical text.

- [ ] **Step 3: Update MEMORY.md** (local, never committed) with the commits and what changed.
