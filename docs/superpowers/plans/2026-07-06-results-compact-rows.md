# Results Page Compact Rows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the card-per-recommendation layout on the results page with compact two-line rows (grouped by supplier, urgency-sorted within groups) that expand inline to full detail, with inline notes and criticality colour edges.

**Architecture:** Keep the existing DOM contract (`.rec-card` container class, `rec-N` / `qty-N` / `sup-N` / `note-N` / `outcome-N` ids, `data-*` attributes, `.rec-card-header` class on line 1) so filters, approve-all, keyboard nav, and edit-save JS survive with minimal changes. New: urgency sort in `rec_logic`, `note` param on `/recommend/edit`, expand/collapse JS, outcome prompt rendered on row line 2. Spec: `docs/superpowers/specs/2026-07-06-results-compact-rows-design.md`.

**Tech Stack:** Flask/Jinja2, vanilla JS, plain CSS (dark green + gold theme), Python stdlib tests via `run_tests.py`.

**House rules (override skill defaults):**
- Work directly on `main` in the main working tree — NO worktree, NO feature branch (user preference; user cannot preview branches).
- Claude does NOT run git commits — the user commits himself. Commit steps below = "report checkpoint"; a single PowerShell commit guide is given at the end, staging files BY NAME (never `git add .`).
- Repo is PUBLIC: no real client names, suppliers, financials, or staff emails in any tracked file. Test data uses invented brands.
- Full suite must be green (`python run_tests.py`) and the user's explicit requirement met: prove end-to-end with dummy data that the page renders and that **CSV export and Print/PDF still work**, before reporting done.

---

## File map

| File | Action |
|---|---|
| `rec_logic.py` | Add `_urgency_sort_key()`; sort each group's recs in `_group_recs_by_supplier` |
| `tests/test_urgency_sort.py` | Create — unit test for the sort |
| `app.py` | `/recommend/edit` accepts optional `note` (~6 lines) |
| `tests/test_rec_note_save.py` | Create — note save/clear via `/recommend/edit`, persistence to results page + CSV |
| `templates/results.html` | Replace card markup with rows + panels; rework JS (expand, note blur, outcome on line 2); delete confidence-popover JS |
| `static/style.css` | New row/panel styles; retire dead card/ring/popover selectors |
| `tests/verify_results_render.py` | Extend checks to new markup (keeps existing `rec-stakes`/`rec-qty-basis`/`rec-mitigation` checks working) |
| `tests/verify_export_regression.py` | Create — end-to-end: approve via POST, CSV content, print page 200, note round-trip, Edge print-to-PDF |

Class names `rec-stakes`, `rec-qty-basis`, `rec-mitigation`, `rec-note` are reused inside the new markup on purpose — `tests/verify_results_render.py` and `tests/test_summary_sheet_velocity.py` assert on them.

---

### Task 1: Urgency sort within supplier groups

**Files:**
- Modify: `rec_logic.py` (after `_compute_order_by`, before `_group_recs_by_supplier`)
- Test: `tests/test_urgency_sort.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_urgency_sort.py`:

```python
"""Rows inside each supplier group must be urgency-sorted:
overdue first (most overdue first), then urgent, then ok by ascending buffer;
recs with no order-by date sort CRITICAL before LOW before the rest, last.

Run: python tests/test_urgency_sort.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rec_logic import _group_recs_by_supplier  # noqa: E402

# All from one supplier so they land in one group. days_of_supply - lead_time_days
# = buffer: negative = overdue, 0-7 = urgent, >7 = ok, missing = unknown.
recs = [
    {"item": "OK item",        "supplier": "S", "days_of_supply": 60, "lead_time_days": 10},   # buffer 50, ok
    {"item": "No-date LOW",    "supplier": "S"},                                               # unknown
    {"item": "Overdue small",  "supplier": "S", "days_of_supply": 10, "lead_time_days": 20},   # buffer -10
    {"item": "No-date CRIT",   "supplier": "S"},                                               # unknown
    {"item": "Urgent item",    "supplier": "S", "days_of_supply": 25, "lead_time_days": 20},   # buffer 5, urgent
    {"item": "Overdue big",    "supplier": "S", "days_of_supply": 10, "lead_time_days": 66},   # buffer -56
]
status_by_item = {
    "No-date CRIT": "CRITICAL",
    "No-date LOW":  "LOW",
    "Overdue big":  "CRITICAL",
    "Overdue small": "CRITICAL",
    "Urgent item":  "LOW",
    "OK item":      "HEALTHY",
}

groups = _group_recs_by_supplier(recs, status_by_item)
assert len(groups) == 1, f"expected 1 group, got {len(groups)}"
order = [r["item"] for r in groups[0]["recs"]]
expected = ["Overdue big", "Overdue small", "Urgent item", "OK item",
            "No-date CRIT", "No-date LOW"]
assert order == expected, f"wrong order:\n  got      {order}\n  expected {expected}"

print("All urgency-sort tests passed.")
```

- [ ] **Step 2: Run it — must FAIL**

Run: `python tests/test_urgency_sort.py`
Expected: `AssertionError: wrong order` (current code keeps insertion order).

- [ ] **Step 3: Implement the sort in `rec_logic.py`**

Add after `_compute_order_by` (around line 90):

```python
_STATUS_RANK = {"overdue": 0, "urgent": 1, "ok": 2, "unknown": 3}
_CRIT_RANK   = {"CRITICAL": 0, "LOW": 1}


def _urgency_sort_key(rec, status_by_item):
    """Sort key for rows inside a supplier group: most urgent first.
    Overdue (most negative buffer first), then urgent, then ok by ascending
    buffer; date-less recs last, CRITICAL before LOW before the rest."""
    ob = rec.get("_order_by") or _compute_order_by(rec)
    rank = _STATUS_RANK.get(ob.get("status"), 3)
    buffer_days = ob.get("buffer_days")
    buffer_key = buffer_days if buffer_days is not None else float("inf")
    item_status = status_by_item.get(str(rec.get("item", "")), "")
    return (rank, buffer_key, _CRIT_RANK.get(item_status, 2))
```

Then inside `_group_recs_by_supplier`, immediately before the `ordered = sorted(` line, add:

```python
    for g in groups.values():
        g["recs"].sort(key=lambda r: _urgency_sort_key(r, status_by_item))
```

Note: `app.py results()` stamps `_order_by` on each rec before grouping, so the
`rec.get("_order_by")` branch is the hot path; `_compute_order_by` fallback keeps
the function pure/standalone.

- [ ] **Step 4: Run test — must PASS**

Run: `python tests/test_urgency_sort.py`
Expected: `All urgency-sort tests passed.`

- [ ] **Step 5: Full suite still green**

Run: `python run_tests.py`
Expected: all tests pass (`test_rec_logic_stakes.py` and `test_supplier_lead_time.py` exercise `_group_recs_by_supplier` — confirm no regression).

- [ ] **Step 6: Report checkpoint** (no git action — user commits at the end)

---

### Task 2: `note` param on `/recommend/edit`

**Files:**
- Modify: `app.py:3071-3118` (`recommend_edit`)
- Test: `tests/test_rec_note_save.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rec_note_save.py`. Follow the exact bootstrap pattern of `tests/verify_results_render.py` (temp DB env var + anthropic stub BEFORE importing app — copy lines 7-42 of that file verbatim, changing the DB filename to `berthcast_note_save.db`), then:

```python
# ── Seed user + session + one rec ──────────────────────────────────────────────
db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role, analyses_used, chat_messages_used) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    ("note@test.com", generate_password_hash("x"), "NoteOrg",
     "claude-haiku-4-5-20251001", "enterprise", 1, "admin", 0, 0),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("note@test.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "NoteOrg", "complete"),
)
recs = [{"item": "Brookvale UHT Milk 1L", "supplier": "Nordvik Dairy",
         "supplier_type": "import", "suggested_quantity": "1570 CTN",
         "confidence": "HIGH", "reason": "Out of stock.", "flags": []}]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, json.dumps([]), json.dumps(recs)),
)

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid; s["email"] = "note@test.com"; s["org_name"] = "NoteOrg"
    s["model"] = "claude-haiku-4-5-20251001"; s["is_admin"] = False
    s["tier"] = "enterprise"; s["role"] = "admin"

def _saved_note():
    row = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (sid,))
    return json.loads(row[0]["recommendations_json"])[0].get("note")

checks = {}

# 1. Note saves WITHOUT any approve/dismiss action
r = client.post("/recommend/edit", json={
    "session_id": sid, "item": "Brookvale UHT Milk 1L", "note": "called supplier"})
checks["edit-with-note returns ok"] = r.status_code == 200 and r.get_json()["ok"]
checks["note persisted"] = _saved_note() == "called supplier"

# 2. Action state untouched by a note-only save
rec0 = json.loads(db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (sid,))[0]["recommendations_json"])[0]
checks["note save does not approve"] = not rec0.get("approved") and not rec0.get("dismissed")

# 3. Blank note clears it
r = client.post("/recommend/edit", json={
    "session_id": sid, "item": "Brookvale UHT Milk 1L", "note": "   "})
checks["blank note clears"] = r.status_code == 200 and _saved_note() is None

# 4. Omitted note leaves an existing note alone (qty-only edit)
client.post("/recommend/edit", json={
    "session_id": sid, "item": "Brookvale UHT Milk 1L", "note": "keep me"})
client.post("/recommend/edit", json={
    "session_id": sid, "item": "Brookvale UHT Milk 1L", "edited_quantity": "1600 CTN"})
checks["omitted note untouched"] = _saved_note() == "keep me"

failed = [n for n, ok in checks.items() if not ok]
for n, ok in checks.items():
    print(f"{'ok ' if ok else 'FAIL'}: {n}")
if failed:
    sys.exit(1)
print("\nAll note-save tests passed.")
```

(Include the same imports as verify_results_render.py: `json`, `database as db`, `app as appmod`, `generate_password_hash`, `flask_app = appmod.app`.)

- [ ] **Step 2: Run it — must FAIL**

Run: `python tests/test_rec_note_save.py`
Expected: `FAIL: note persisted` (route currently ignores `note`).

- [ ] **Step 3: Implement in `app.py` `recommend_edit`**

After `edited_supplier = data.get("edited_supplier", None)` add:

```python
    note            = data.get("note", None)
```

Inside `_mutate`, after the `edited_supplier` block and before `return {"updated": True}` add:

```python
                if note is not None:
                    ns = str(note).strip()
                    if ns:
                        rec["note"] = ns[:500]
                    else:
                        rec.pop("note", None)
```

(500-char cap mirrors defensive caps elsewhere; `/recommend/action`'s note behaviour is unchanged.)

Also update the route docstring first line to: `"""Save the user's edited quantity, supplier and/or note without changing the approve/dismiss state. ..."""`

- [ ] **Step 4: Run test — must PASS**

Run: `python tests/test_rec_note_save.py`
Expected: `All note-save tests passed.`

- [ ] **Step 5: Full suite green**

Run: `python run_tests.py`

- [ ] **Step 6: Report checkpoint**

---

### Task 3: Row + panel markup in `templates/results.html`

**Files:**
- Modify: `templates/results.html:271-493` (the `rec-supplier-cards` loop) and the `<style>` block at 594-645

The supplier group header (lines 225-270) is untouched. Replace everything from `<div class="rec-supplier-cards">` through its closing `</div>` (the per-rec card loop, lines 271-491) with:

- [ ] **Step 1: Write the new markup**

```html
          <div class="rec-supplier-cards">
        {% for rec in group.recs %}
        {% if not rec.error %}
          {% set item_status = status_by_item.get(rec.item, '') %}
          {% set low_conf = rec.confidence in ('LOW', 'INSUFFICIENT_DATA') %}
          <div class="rec-card {% if rec.approved %}approved{% elif rec.dismissed %}dismissed{% endif %}"
               id="rec-{{ rec._card_idx }}"
               data-status="{{ item_status }}"
               data-suptype="{{ rec.supplier_type or 'other' }}"
               data-confidence="{{ rec.confidence }}"
               data-supplier="{{ rec.supplier or '' }}"
               data-item="{{ rec.item }}">

            <!-- Line 1: name · qty · date · actions · chevron -->
            <div class="rec-card-header rec-row-main" onclick="rowMainClick(event, {{ rec._card_idx }})">
              <span class="rec-row-num" aria-hidden="true">#{{ rec._display_num }}</span>
              <span class="rec-row-item">{{ rec.item }}</span>
              {% if low_conf %}<span class="rec-row-lowconf">low confidence</span>{% endif %}
              {% if rec.approved %}
                <span class="badge badge-healthy">✓ Approved</span>
              {% elif rec.dismissed %}
                <span class="badge badge-dead">Dismissed</span>
              {% endif %}
              <span class="rec-row-qty">{{ rec.edited_quantity or rec.suggested_quantity or '—' }}</span>
              {% if rec._order_by and rec._order_by.order_by_date %}
              <span class="rec-row-date rec-row-date-{{ rec._order_by.status }}">
                {% if rec._order_by.status == 'overdue' %}overdue {{ rec._order_by.buffer_days | abs }}d
                {% else %}by {{ rec._order_by.order_by_date }}{% endif %}
              </span>
              {% else %}
              <span class="rec-row-date">no date</span>
              {% endif %}
              {% if user_role != 'viewer' %}
              <span class="rec-row-act no-print">
                <button type="button" class="rec-row-btn rec-row-btn-ok" title="Approve"
                        onclick="takeAction({{ rec._card_idx }}, '{{ rec.item | replace("'", "\\'") }}', 'approve')">✓</button>
                <button type="button" class="rec-row-btn" title="Dismiss"
                        onclick="takeAction({{ rec._card_idx }}, '{{ rec.item | replace("'", "\\'") }}', 'dismiss')">✕</button>
              </span>
              {% endif %}
              <span class="rec-row-chev" id="chev-{{ rec._card_idx }}">▾</span>
            </div>

            <!-- Line 2: reason snippet OR outcome prompt · note -->
            <div class="rec-row-sub">
              <span class="rec-row-snippet" id="snippet-{{ rec._card_idx }}"
                    {% if rec.approved and user_role != 'viewer' %}style="display:none;"{% endif %}>
                {{ ((rec.reason or '').split('.') | first ~ '.') if rec.reason else '' }}
              </span>
              {% if user_role != 'viewer' %}
              <span class="rec-row-outcome" id="outcome-{{ rec._card_idx }}"
                    {% if not rec.approved %}style="display:none;"{% endif %}>
                {% if rec.approved %}
                  {% if not rec.order_placed %}
                  <span class="outcome-q">Did you place this order?</span>
                  <button class="btn btn-outline btn-sm outcome-btn"
                          onclick="recordOutcome({{ rec._card_idx }}, '{{ rec.item | replace("'", "\\'") }}', 'order_placed', true)">Yes, ordered</button>
                  <button class="btn btn-ghost btn-sm outcome-btn"
                          onclick="recordOutcome({{ rec._card_idx }}, '{{ rec.item | replace("'", "\\'") }}', 'order_placed', false)">Not yet</button>
                  {% elif not rec.outcome_status %}
                  <span class="outcome-check">Order placed</span>
                  <span class="outcome-q outcome-q-secondary">Was the stockout avoided?</span>
                  <button class="btn btn-success btn-sm outcome-btn"
                          onclick="recordOutcome({{ rec._card_idx }}, '{{ rec.item | replace("'", "\\'") }}', 'outcome_status', 'stockout_avoided')">Yes, avoided</button>
                  <button class="btn btn-muted btn-sm outcome-btn"
                          onclick="recordOutcome({{ rec._card_idx }}, '{{ rec.item | replace("'", "\\'") }}', 'outcome_status', 'stockout_happened')">No, stockout</button>
                  {% elif rec.outcome_status == 'stockout_avoided' %}
                  <span class="outcome-check">Order placed</span>
                  <span class="outcome-result outcome-good">Stockout avoided</span>
                  {% else %}
                  <span class="outcome-check">Order placed</span>
                  <span class="outcome-result outcome-bad">Stockout occurred</span>
                  {% endif %}
                {% endif %}
              </span>
              <input type="text" class="rec-note"
                     id="note-{{ rec._card_idx }}"
                     data-item="{{ rec.item }}"
                     placeholder="add note…"
                     value="{{ rec.note or '' }}">
              {% endif %}
            </div>

            <!-- Expanded panel: full detail (hidden until expand) -->
            <div class="rec-row-panel" id="panel-{{ rec._card_idx }}" hidden>
              <div class="rec-panel-meta">
                {% if rec._order_by and rec._order_by.order_by_date %}
                <strong>Order by {{ rec._order_by.order_by_date }}</strong>
                <span class="rec-order-by-meta">
                  {% if rec._order_by.status == 'overdue' %}— already overdue by {{ rec._order_by.buffer_days | abs }} day{{ 's' if rec._order_by.buffer_days|abs != 1 }}
                  {% elif rec._order_by.status == 'urgent' %}— only {{ rec._order_by.buffer_days }} day{{ 's' if rec._order_by.buffer_days != 1 }} of buffer
                  {% else %}— {{ rec._order_by.buffer_days }} day{{ 's' if rec._order_by.buffer_days != 1 }} of buffer{% endif %}
                </span>
                <span class="rec-panel-sep">·</span>
                {% endif %}
                {% if rec.days_of_supply %}
                stock lasts about {{ (rec.days_of_supply / 30) | round(1) }} more month{{ 's' if (rec.days_of_supply / 30) | round(1) != 1 }}
                <span class="rec-panel-sep">·</span>
                {% endif %}
                <span class="rec-conf-chip rec-conf-{{ rec.confidence | lower }}">
                  {{ 'needs more data' if rec.confidence == 'INSUFFICIENT_DATA' else (rec.confidence | lower ~ ' confidence') }}
                </span>
              </div>
              {% if rec._conf_reasons %}
              <div class="rec-panel-why">Why: {{ rec._conf_reasons | join(' · ') }}</div>
              {% endif %}

              <div class="rec-edit-grid">
                <div class="rec-edit-field">
                  <label for="qty-{{ rec._card_idx }}">Quantity to order</label>
                  <input type="text" class="rec-edit-input" id="qty-{{ rec._card_idx }}"
                         data-original="{{ rec.suggested_quantity or '' }}"
                         data-field="quantity" data-item="{{ rec.item }}"
                         value="{{ rec.edited_quantity or rec.suggested_quantity or '' }}">
                  {% if rec.edited_quantity and rec.edited_quantity != rec.suggested_quantity %}
                  <span class="rec-edit-hint" id="qty-hint-{{ rec._card_idx }}">
                    AI suggested: {{ rec.suggested_quantity }}
                    <button type="button" class="rec-edit-revert" onclick="revertField({{ rec._card_idx }}, 'quantity')">revert</button>
                  </span>
                  {% else %}
                  <span class="rec-edit-hint rec-edit-hint-muted" id="qty-hint-{{ rec._card_idx }}">
                    AI suggestion — adjust if you want to order more or less.
                  </span>
                  {% endif %}
                </div>
                <div class="rec-edit-field">
                  <label for="sup-{{ rec._card_idx }}">Supplier</label>
                  <input type="text" class="rec-edit-input" id="sup-{{ rec._card_idx }}"
                         data-original="{{ rec.supplier or '' }}"
                         data-field="supplier" data-item="{{ rec.item }}"
                         value="{{ rec.edited_supplier or rec.supplier or '' }}">
                  {% if rec.edited_supplier and rec.edited_supplier != rec.supplier %}
                  <span class="rec-edit-hint" id="sup-hint-{{ rec._card_idx }}">
                    AI suggested: {{ rec.supplier }} ({{ rec.supplier_type }})
                    <button type="button" class="rec-edit-revert" onclick="revertField({{ rec._card_idx }}, 'supplier')">revert</button>
                  </span>
                  {% else %}
                  <span class="rec-edit-hint rec-edit-hint-muted" id="sup-hint-{{ rec._card_idx }}">
                    {{ rec.supplier_type }} supplier on file.
                  </span>
                  {% endif %}
                </div>
              </div>

              {% if rec._quantity_basis %}
              <p class="rec-qty-basis">{{ rec._quantity_basis }}</p>
              {% endif %}
              <p class="rec-panel-reason">{{ rec.reason }}</p>

              {% if rec._has_stakes %}
              <div class="rec-stakes">
                {% if rec.consequence_if_not_acting %}
                <div class="rec-stake is-negative">
                  <div class="rec-stake-label">If you don't order</div>
                  <div class="rec-stake-text">{{ rec.consequence_if_not_acting }}</div>
                </div>
                {% endif %}
                {% if rec.consequence_if_acting %}
                <div class="rec-stake is-positive">
                  <div class="rec-stake-label">If you order</div>
                  <div class="rec-stake-text">{{ rec.consequence_if_acting }}</div>
                </div>
                {% endif %}
              </div>
              {% endif %}
              {% if rec.supplier_risk == 'HIGH' and rec.mitigation %}
              <div class="rec-mitigation">
                <span class="rec-mitigation-label">What to do about the supplier risk:</span>
                {{ rec.mitigation }}
              </div>
              {% endif %}
              {% if user_role == 'viewer' %}
              <p style="font-size:13px; color:var(--muted); margin-top:10px;">View-only access</p>
              {% endif %}
            </div>
          </div>
        {% endif %}
        {% endfor %}
          </div>
```

Details that matter:
- **The `{% if user_role != 'viewer' %}` around line 2's outcome+note block** — viewers see only the snippet (mirrors today's behaviour where viewers get no note input, no action buttons, no outcome strip).
- `rec-row-qty` shows the **effective** qty (edited wins) — same expression as the old input's `value`.
- The old confidence ring, `conf-popover` markup, and the old `.rec-card-actions` / `.outcome-strip` blocks are **gone** — do not carry them over.
- Snippet = first sentence of `rec.reason` via `split('.') | first`.

- [ ] **Step 2: Offline render sanity check**

Run: `python tests/verify_results_render.py`
Expected: PASS — the stakes/basis/mitigation class checks still find exactly one of each (they now live in the panel). If it fails, fix markup before proceeding.

- [ ] **Step 3: Report checkpoint**

---

### Task 4: JS rework in `templates/results.html`

**Files:**
- Modify: `templates/results.html` `<script>` block (was lines 647-1316)

- [ ] **Step 1: Add expand/collapse + note-blur JS, rewire outcome rendering**

**(a) Add after the `showTab` function:**

```js
// ── Row expand / collapse ──
function toggleRow(idx) {
  const panel = document.getElementById('panel-' + idx);
  const chev  = document.getElementById('chev-' + idx);
  if (!panel) return;
  panel.hidden = !panel.hidden;
  if (chev) chev.textContent = panel.hidden ? '▾' : '▴';
}

// Line-1 click expands, unless the click was on a button/input/link.
function rowMainClick(e, idx) {
  if (e.target.closest('button, input, a')) return;
  toggleRow(idx);
}
```

**(b) Keyboard block:** in the existing `keydown` listener, after the `d` handler add:

```js
  // Enter or → toggles the expanded panel on the active row
  if (e.key === 'Enter' || e.key === 'ArrowRight') {
    const d = _activeCardData();
    if (!d) return;
    e.preventDefault();
    toggleRow(d.domIdx);
    return;
  }
```

Also update the hint bar markup (template, line ~213) to add: `<span class="kb-key">↵</span> expand &nbsp;` before the `/` entry.

**(c) Delete the whole "── Confidence popover ──" section** (`toggleConfidencePopover` + its document click listener) — the popover no longer exists.

**(d) Note save on blur** — add after the edit-input wiring block:

```js
// Notes save on blur — no approve needed.
document.querySelectorAll('.rec-note').forEach(input => {
  input.addEventListener('blur', () => {
    _saveEdit(input.dataset.item, { note: input.value });
  });
});
```

(`_saveEdit` already POSTs to `/recommend/edit`; Task 2 made the route accept `note`.)

**(e) `takeAction` approve branch** — replace the whole `if (action === 'approve') { ... appendChild(strip) ... }` block at the end of the success path with:

```js
    // Approve → show the outcome prompt on line 2 (replacing the snippet).
    // Dismiss → restore the snippet.
    const snippet = document.getElementById('snippet-' + idx);
    const outcome = document.getElementById('outcome-' + idx);
    if (action === 'approve' && outcome) {
      if (snippet) snippet.style.display = 'none';
      outcome.style.display = '';
      if (!outcome.dataset.progressed) {
        outcome.innerHTML = `
          <span class="outcome-q">Did you place this order?</span>
          <button class="btn btn-outline btn-sm outcome-btn"
                  onclick="recordOutcome(${idx}, '${item.replace(/'/g,"\\'")}', 'order_placed', true)">Yes, ordered</button>
          <button class="btn btn-ghost btn-sm outcome-btn"
                  onclick="recordOutcome(${idx}, '${item.replace(/'/g,"\\'")}', 'order_placed', false)">Not yet</button>`;
      }
    } else if (action === 'dismiss') {
      if (outcome) outcome.style.display = 'none';
      if (snippet) snippet.style.display = '';
    }
```

**(f) `recordOutcome`** — change every `const strip = document.getElementById('outcome-' + idx);` write target to the same `outcome-` span (it already uses that id — only the wrapper markup changes). Replace the three `strip.innerHTML = \`...\`` payloads with the same content minus the `<div class="outcome-prompt">`/`<div class="outcome-done">` wrappers (the span is already a flex row via CSS), and in the `order_placed && value` branch set `strip.dataset.progressed = "1"` first. Payloads:

```js
  if (field === 'order_placed' && value) {
    strip.dataset.progressed = "1";
    strip.innerHTML = `
        <span class="outcome-check">Order placed</span>
        <span class="outcome-q outcome-q-secondary">Was the stockout avoided?</span>
        <button class="btn btn-success btn-sm outcome-btn"
                onclick="recordOutcome(${idx}, '${item.replace(/'/g,"\\'")}', 'outcome_status', 'stockout_avoided')">Yes, avoided</button>
        <button class="btn btn-muted btn-sm outcome-btn"
                onclick="recordOutcome(${idx}, '${item.replace(/'/g,"\\'")}', 'outcome_status', 'stockout_happened')">No, stockout</button>`;
  } else if (field === 'order_placed' && !value) {
    strip.dataset.progressed = "1";
    strip.innerHTML = `<span style="font-size:12px;color:var(--muted);">Not ordered</span>`;
  } else if (field === 'outcome_status') {
    strip.dataset.progressed = "1";
    const isGood = value === 'stockout_avoided';
    strip.innerHTML = `
        <span class="outcome-check">Order placed</span>
        <span class="outcome-result ${isGood ? 'outcome-good' : 'outcome-bad'}">
          ${isGood ? 'Stockout avoided' : 'Stockout occurred'}
        </span>`;
  }
```

**(g) Badge handling in `takeAction`, `approveSupplier`, `approveAll`, `undoApproveAll`:** unchanged — they append/remove badges on `.rec-card-header`, which is now line 1. Verify the selector `card.querySelector('.rec-card-header')` still matches (it does — line 1 carries the class).

**Untouched JS:** tabs, filters/chips/search, `applyFilters` (targets `.rec-card` = the row), `approveSupplier`, `approveAll`/`undoApproveAll`, `_updateApproveBtn`, keyboard j/k/a/d, `_getEditValues`, `_hintForField`, `escapeHtmlForHint`, `_saveEdit`, `revertField`, edit-input blur wiring.

- [ ] **Step 2: Render + suite check**

Run: `python tests/verify_results_render.py && python run_tests.py`
Expected: both pass.

- [ ] **Step 3: Report checkpoint**

---

### Task 5: CSS — row styles in `static/style.css`

**Files:**
- Modify: `static/style.css` (card block ~449-533, popover ~633-671, ring ~1007-1055; check the DARK THEME override block at the end for `.rec-card` overrides too)

- [ ] **Step 1: Replace the `.rec-card` visual block**

Replace the existing `.rec-card`, `.rec-card-header`, `.rec-card-title`, `.rec-card-body`, `.rec-card-flags`, `.rec-card-actions` rules (KEEP `.rec-card.kb-active`, `.kb-hint-bar`, `.kb-key`, `.rec-note` base, `.rec-edit-*`, `.rec-order-by-meta`) with:

```css
/* ── Compact recommendation rows (2026-07 redesign) ── */
.rec-card {
  background: var(--paper-2);
  border: 1px solid var(--border);
  border-left: 4px solid transparent;
  border-radius: 10px;
  margin-bottom: 8px;
  transition: border-color 120ms ease;
}
.rec-card[data-status="CRITICAL"] { border-left-color: #C0392B; background: rgba(192,57,43,0.10); }
.rec-card[data-status="LOW"]      { border-left-color: var(--warning); background: rgba(230,180,80,0.07); }
.rec-card:hover { border-color: var(--brass); }
.rec-card.approved .rec-row-main { opacity: 0.92; }
.rec-card.dismissed { opacity: 0.5; }

.rec-card-header.rec-row-main {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; cursor: pointer; position: relative;
}
.rec-row-num { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.rec-row-item {
  flex: 1; min-width: 0; font-weight: 600; font-size: 14px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rec-row-lowconf {
  font-size: 10px; padding: 2px 7px; border-radius: 8px;
  background: rgba(232,154,146,0.14); color: var(--crit, #E89A92);
  font-weight: 600; white-space: nowrap;
}
.rec-row-qty {
  font-variant-numeric: tabular-nums; font-weight: 700;
  color: var(--brass-2, #DCBE63); font-size: 14px; white-space: nowrap;
}
.rec-row-date { font-size: 12px; color: var(--muted); white-space: nowrap; }
.rec-row-date-overdue, .rec-row-date-urgent { color: var(--crit, #E89A92); font-weight: 600; }
.rec-row-act { display: flex; gap: 5px; }
.rec-row-btn {
  width: 26px; height: 26px; border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border); background: transparent;
  color: var(--text); font-size: 13px; line-height: 1;
}
.rec-row-btn:hover { border-color: var(--brass); }
.rec-row-btn-ok { border-color: rgba(143,188,143,0.5); color: var(--success); }
.rec-row-chev { color: var(--muted); font-size: 11px; width: 14px; text-align: center; }

.rec-row-sub {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 0 12px 9px 12px; margin-top: -3px;
}
.rec-row-snippet {
  flex: 1; min-width: 0; font-size: 12px; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.rec-row-outcome { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1; }
.rec-row-sub .rec-note {
  width: 190px; margin: 0; padding: 3px 2px; font-size: 12px;
  background: transparent; border: none;
  border-bottom: 1px dashed rgba(243,239,223,0.28); border-radius: 0;
  color: var(--text);
}
.rec-row-sub .rec-note:focus { outline: none; border-bottom-color: var(--brass); }

.rec-row-panel {
  border-top: 1px solid var(--border);
  padding: 13px 16px 14px;
  font-size: 13px;
}
.rec-panel-meta { font-size: 12.5px; color: var(--muted); }
.rec-panel-meta strong { color: var(--text); }
.rec-panel-sep { margin: 0 6px; color: var(--border); }
.rec-panel-why { font-size: 11.5px; color: var(--muted); margin-top: 5px; }
.rec-panel-reason { margin-top: 10px; line-height: 1.5; }
.rec-conf-chip {
  display: inline-block; font-size: 10.5px; padding: 2px 9px; border-radius: 9px;
  font-weight: 700; letter-spacing: 0.02em;
}
.rec-conf-high   { background: rgba(143,188,143,0.15); color: var(--success); }
.rec-conf-medium { background: rgba(201,162,39,0.15);  color: var(--brass-2, #DCBE63); }
.rec-conf-low, .rec-conf-insufficient_data { background: rgba(232,154,146,0.14); color: var(--crit, #E89A92); }
.rec-edit-grid { margin-top: 12px; }
```

- [ ] **Step 2: Delete dead selectors**

Remove: `.rec-card-title`, `.rec-card-body` (+ its `strong` rule), `.rec-card-flags`, `.rec-card-actions`, `.conf-popover` (+ `.open`, `.conf-pop-title`, `.conf-pop-list` rules), `.confidence-ring` (+ svg/ring-bg/ring-fill), `.confidence-high/medium/low/insufficient` ring+lbl rules, `.confidence-label`, `.rec-order-by`, `.rec-order-by-urgent`, `.rec-order-by-overdue` (keep `.rec-order-by-meta` — the panel uses it). Grep first to confirm each selector is referenced nowhere else:

Run: `grep -rn "confidence-ring\|conf-popover\|rec-card-body\|rec-card-title\|rec-card-actions\|rec-order-by" templates/ static/`
Expected after template rework: hits only in `static/style.css` (the rules being deleted) and `rec-order-by-meta` in results.html.

Also check the **DARK THEME override block** at the bottom of style.css and the results.html inline `<style>` blocks for `.rec-card` / `.outcome-strip` rules that need the same cleanup: the `.outcome-strip` CSS in results.html's inline style block (lines ~595-598) is dead (class no longer rendered) — delete it, keep `.outcome-prompt/q/check/result/done` rules (line-2 spans reuse them).

- [ ] **Step 3: Render + suite check**

Run: `python tests/verify_results_render.py && python run_tests.py`
Expected: pass.

- [ ] **Step 4: Report checkpoint**

---

### Task 6: Extend `tests/verify_results_render.py` for the new markup

**Files:**
- Modify: `tests/verify_results_render.py`

- [ ] **Step 1: Add checks + a third approved rec**

In the seeded `recs` list add a third rec (approved, order placed, to exercise the line-2 outcome markup):

```python
    {   # approved with outcome in progress: line 2 must show the outcome prompt
        "item": "Padimas Jasmine Rice 5kg", "supplier": "Local Co", "supplier_type": "local",
        "lead_time_days": 21, "days_of_supply": 9, "recommended_action": "REORDER",
        "suggested_quantity": "80 BAG", "confidence": "MEDIUM",
        "supplier_risk": "None", "flags": [], "reason": "Stock low against steady sales.",
        "approved": True, "order_placed": True, "note": "told team already",
    },
```

And a matching inventory row:

```python
    {"item": "Padimas Jasmine Rice 5kg", "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 9, "category": "DRY", "stock": "6 BAG", "observation": "low"},
```

Add to `checks`:

```python
    # ── compact-row markup ──
    "three row containers": html.count('rec-row-main') == 3,
    "three hidden panels": html.count('class="rec-row-panel"') == 3,
    "note inputs on rows": html.count('class="rec-note"') == 3,
    "saved note rendered": 'told team already' in html,
    "critical row colour hook present": 'data-status="CRITICAL"' in html,
    "low-confidence tag shown": 'rec-row-lowconf' in html,          # crackers rec is INSUFFICIENT_DATA
    "reason snippet on line 2": 'Tight stock with a slow import supplier.' in html,
    "approved row shows outcome question": 'Was the stockout avoided?' in html,
    "qty on line 1": '160 CTN' in html,
    "confidence ring is gone": 'confidence-ring' not in html,
    "popover is gone": 'conf-popover' not in html,
```

- [ ] **Step 2: Run — must PASS**

Run: `python tests/verify_results_render.py`
Expected: `All render checks passed.` (Existing stakes/basis/mitigation counts still assert exactly one each — the new third rec has no consequences/mitigation/avg_monthly_sales so it adds none.)

- [ ] **Step 3: Report checkpoint**

---

### Task 7: End-to-end export regression proof (CSV + Print/PDF) — the user's explicit requirement

**Files:**
- Create: `tests/verify_export_regression.py`

- [ ] **Step 1: Write the script**

Same bootstrap as `verify_results_render.py` (temp DB `berthcast_verify_export.db`, anthropic stub), seed the same 2-rec dataset (salmon + crackers, invented data), then drive the REAL flow through the test client:

```python
# 1. Approve one rec WITH a note and an edited qty via the real endpoint
r = client.post("/recommend/action", json={
    "session_id": sid, "item": "Frozen Salmon 1kg", "action": "approve",
    "note": "call to confirm ETA", "edited_quantity": "170 CTN",
    "edited_supplier": None})
checks["action approve ok"] = r.status_code == 200 and r.get_json()["ok"]

# 2. Note-only save on the OTHER rec via /recommend/edit (new path)
r = client.post("/recommend/edit", json={
    "session_id": sid, "item": "Plain Crackers", "note": "check with team"})
checks["note-only edit ok"] = r.status_code == 200 and r.get_json()["ok"]

# 3. Results page renders with both
r = client.get(f"/results/{sid}")
html = r.get_data(as_text=True)
checks["results 200"] = r.status_code == 200
checks["note A on page"] = "call to confirm ETA" in html
checks["note B on page"] = "check with team" in html

# 4. CSV export: approved rec present with edited qty + AI original + note
r = client.get(f"/results/{sid}/export.csv")
csv_text = r.get_data(as_text=True)
checks["csv 200"] = r.status_code == 200
checks["csv content-type"] = r.mimetype == "text/csv"
checks["csv header row"] = csv_text.splitlines()[0].startswith("Item,On Hand,Qty To Order")
checks["csv has approved item"] = "Frozen Salmon 1kg" in csv_text
checks["csv shows edited qty with AI original"] = "170 CTN (AI: 160 CTN)" in csv_text
checks["csv carries the note"] = "call to confirm ETA" in csv_text
checks["csv excludes unapproved"] = "Plain Crackers" not in csv_text

# 5. Print page (the Print/PDF flow) renders the approved order sheet
r = client.get(f"/results/{sid}/print")
print_html = r.get_data(as_text=True)
checks["print 200"] = r.status_code == 200
checks["print has approved item"] = "Frozen Salmon 1kg" in print_html
checks["print shows qty"] = "170 CTN" in print_html
checks["print excludes unapproved"] = "Plain Crackers" not in print_html

# 6. Save print HTML and produce an actual PDF via headless Edge —
#    proves the browser print-to-PDF path emits a real file.
import subprocess, pathlib
out_dir = pathlib.Path(tempfile.gettempdir()) / "berthcast_export_proof"
out_dir.mkdir(exist_ok=True)
print_file = out_dir / "print_page.html"
print_file.write_text(print_html, encoding="utf-8")
pdf_file = out_dir / "order_sheet.pdf"
edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
try:
    subprocess.run([edge, "--headless", "--disable-gpu",
                    f"--print-to-pdf={pdf_file}", print_file.as_uri()],
                   timeout=60, check=False)
    checks["pdf produced (>1KB)"] = pdf_file.exists() and pdf_file.stat().st_size > 1024
except FileNotFoundError:
    checks["pdf produced (>1KB)"] = None   # Edge missing — report SKIP, don't fail
```

End with the same pass/fail print loop as `verify_results_render.py`, treating `None` as `SKIP`. **Name it `verify_*` on purpose** — the suite (`run_tests.py`) only picks up `test_*.py`, and this script shells out to Edge, which does not belong in CI. It runs manually here and before future results-page changes.

- [ ] **Step 2: Run — must PASS**

Run: `python tests/verify_export_regression.py`
Expected: all checks `ok`, PDF produced. This is the user's demanded proof — paste the actual output in the final report.

- [ ] **Step 3: Report checkpoint**

---

### Task 8: Visual verification (headless screenshots, dark theme)

- [ ] **Step 1: Screenshot the real rendered page**

Reuse the established pattern (offline Flask test-client render → rewrite `static/` URLs to `file://` → headless Edge screenshot; same approach as `render_landing.py` in the scratchpad). Write the harness into the scratchpad (NOT the repo), rendering the verify_results_render dataset. Capture:

1. Default state — rows collapsed, red/amber edges visible
2. One row expanded (inject `panel.hidden=false` via a `--run-all-compositor-stages-before-draw` + small JS snippet, or simply strip `hidden` from the saved HTML for the shot)
3. The approved row showing the outcome prompt on line 2

Run for each: `msedge --headless --disable-gpu --screenshot=<path> --window-size=1440,1100 <file-url>`

- [ ] **Step 2: Eyeball every screenshot**

Read each PNG. Check: colour edges land on the correct rows, gold qty legible, note field visible but quiet, panel typography consistent with the app's dark theme, nothing white/unstyled (the classic dark-theme regression), badges not overlapping.

- [ ] **Step 3: Full suite one last time**

Run: `python run_tests.py`
Expected: all green.

---

### Task 9: Report + commit guide

- [ ] **Step 1: Report to the user**

Show: what changed per file, the verify_export_regression output verbatim (his explicit ask), screenshot descriptions, suite count. Explain in one plain sentence why CSV/PDF were never at risk (they read the database, not the page) but were proven anyway.

- [ ] **Step 2: Give the PowerShell commit guide** (exact form, stage by name, short message):

```powershell
cd c:\BerthAI\BerthAI
git add rec_logic.py app.py templates/results.html static/style.css tests/test_urgency_sort.py tests/test_rec_note_save.py tests/verify_results_render.py tests/verify_export_regression.py docs/superpowers/specs/2026-07-06-results-compact-rows-design.md docs/superpowers/plans/2026-07-06-results-compact-rows.md .gitignore
git commit -m "Compact expandable rows on results page"
git push
```

- [ ] **Step 3: Remind post-deploy check**

One real analysis on the live site after Render deploys: rows render, expand works, a note saves, CSV downloads, Print/PDF opens. Update MEMORY.md when pushed.

---

## Self-review notes

- **Spec coverage:** collapsed row (T3), urgency sort (T1), expanded panel (T3), outcome on line 2 (T3+T4), notes on blur (T2+T4), colour edges + states (T5), unchanged surfaces guarded (T4 "untouched JS" + T7 CSV/print + suite), testing section (T6-T8). Toggle-free replacement: old markup deleted (T3), old CSS deleted (T5).
- **Type consistency:** ids `rec-N/qty-N/sup-N/note-N/outcome-N/snippet-N/panel-N/chev-N` used consistently across T3 markup and T4 JS. `_urgency_sort_key(rec, status_by_item)` signature matches its call site.
- **Known trade-offs:** `rec-row-qty` on line 1 does not live-update when the panel qty input is edited (page reload shows it; acceptable, matches current card where the input IS the display). Approved-row badge duplication guard relies on existing badge-removal JS (unchanged behaviour).
