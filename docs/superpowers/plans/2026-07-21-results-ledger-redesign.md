# Results Ledger Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the purchase-recommendations list into a fixed-column ledger whose item names never truncate, replace the duplicated sidebar overview with a counted filter bar, and move the pen-and-paper ordering loop onto the print sheet.

**Architecture:** Layout and CSS only. `.rec-row-main` changes from a flex row (where the item name was the only shrinkable element) to a CSS grid with fixed columns. The `.mc-sidebar` is deleted and its filters move into a full-width bar whose counts are computed in JavaScript from the rendered recommendation rows instead of from the inventory report. No route, query, prompt, or schema change in Phase 1; Phase 2 changes one route's filter and one print template.

**Tech Stack:** Flask + Jinja2 templates, vanilla JavaScript (no framework, no build step), plain CSS with custom properties. Tests are standalone Python scripts, not pytest.

---

## Context you need before starting

**This is a live pilot.** One real client uses this page. Prefer the smallest change that works. Do not refactor anything this plan does not name.

**Read these first:**
- `docs/superpowers/specs/2026-07-21-results-ledger-redesign-design.md` — the spec this implements
- `docs/superpowers/specs/2026-07-06-results-compact-rows-design.md` — the layout being replaced, so you know what was deliberate

**Repo conventions that apply here:**

1. **Tests are standalone scripts, not pytest.** `run_tests.py` discovers `tests/test_*.py` only — a file named `verify_*.py` is NOT in the suite and only runs when invoked by hand. Each test sets `os.environ["DB_PATH"]` to a temp file *before* importing any project module, stubs the `anthropic` module into `sys.modules`, runs checks through a dict of `name: bool`, prints `ok:` / `FAIL:` per check, and calls `sys.exit(1)` if any failed. Copy `tests/verify_results_render.py` as your template — it already does the Flask-render setup you need.
2. **Test data uses invented brands only.** BROOKVALE, NORDVIK, PADIMAS style. Never a real product, supplier, or client name — this repo is public.
3. **Model output is untrusted.** Item names come from a client's uploaded file via Claude. They must never be spliced into inline JS or `innerHTML`. The existing pattern — buttons carry `data-` attributes only, and one delegated listener reads the item name from the card's `data-item` — is a security control. Preserve it exactly.
4. **Comments explain constraints the code can't show**, matched to surrounding density. Don't narrate what the code already says.

**Verification command used throughout:**

```powershell
python run_tests.py
```

Expected on success, as the last line: `All N tests passed` with `0 failed`.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `templates/results.html` | Recommendation ledger markup, filter bar, page JS | 1 |
| `static/style.css` | Ledger grid, filter bar, results-scoped table skin | 1 and 2 |
| `tests/test_results_ledger_render.py` | **New.** Ledger render assertions, runs in CI | 1 |
| `tests/verify_results_render.py` | Existing manual render check — has assertions that this work breaks | 1 |
| `app.py` | `print_results()` route — which recommendations reach the print sheet | 2 |
| `templates/print_order.html` | Print sheet: checkbox column, write-in column, landscape | 2 |
| `tests/test_print_order_sheet.py` | **New.** Print sheet assertions | 2 |

---

# PHASE 1 — The recommendations ledger

## Task 1: Failing test for the ledger markup

**Files:**
- Create: `tests/test_results_ledger_render.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_results_ledger_render.py`:

```python
"""Render /results through Flask and assert the ledger markup (2026-07 redesign).

Guards the three faults the redesign fixes:
  1. the item name must never be truncated by CSS,
  2. filter chip counts must come from recommendations, not inventory,
  3. the outcome prompt and its sales line must be gone from the page.

Run with: python tests/test_results_ledger_render.py
Uses a throwaway temp DB so it never touches real data.
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_ledger_render.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

import types  # noqa: E402
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
from werkzeug.security import generate_password_hash  # noqa: E402

flask_app = appmod.app

db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role, analyses_used, chat_messages_used) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    ("ledger@test.com", generate_password_hash("x"), "LedgerOrg",
     "claude-haiku-4-5-20251001", "enterprise", 1, "admin", 0, 0),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("ledger@test.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "LedgerOrg", "complete"),
)

# A deliberately long name — the fault this redesign exists to fix.
LONG_NAME = "BROOKVALE AMMONIA CHILLED PRAWN MEAT PEELED DEVEINED 400G 12X CARTON"

recs = [
    {"item": LONG_NAME, "supplier": "Nordvik Foods", "supplier_type": "import",
     "lead_time_days": 105, "days_of_supply": 12, "recommended_action": "REORDER",
     "suggested_quantity": "240 CTN", "confidence": "LOW", "supplier_risk": "None",
     "flags": [], "reason": "Tight stock against a long import lead time.",
     "avg_monthly_sales": 60, "uom_label": " CTN"},
    {"item": "PADIMAS JASMINE RICE 5KG", "supplier": "Kessington Trading",
     "supplier_type": "local", "lead_time_days": 21, "days_of_supply": 9,
     "recommended_action": "REORDER", "suggested_quantity": "60 BAG",
     "confidence": "MEDIUM", "supplier_risk": "None", "flags": [],
     "reason": "Stock low against steady sales.",
     "approved": True, "note": "called supplier"},
    {"item": "NORDVIK COD FILLET SKIN-ON 1KG", "supplier": "Nordvik Foods",
     "supplier_type": "import", "lead_time_days": 98, "days_of_supply": 40,
     "recommended_action": "MONITOR", "suggested_quantity": "18 CTN",
     "confidence": "HIGH", "supplier_risk": "None", "flags": [],
     "reason": "Comfortable for now."},
]

# Inventory deliberately carries MORE criticals than the recommendations do.
# If a filter chip count is read off inventory, it reads 4; off recommendations, 1.
inv = [
    {"item": LONG_NAME, "status": "CRITICAL", "spoilage_risk": "HIGH",
     "days_of_supply": 12, "category": "FROZEN", "stock": "38 CTN", "observation": "low"},
    {"item": "PADIMAS JASMINE RICE 5KG", "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 9, "category": "DRY", "stock": "6 BAG", "observation": "low"},
    {"item": "NORDVIK COD FILLET SKIN-ON 1KG", "status": "HEALTHY", "spoilage_risk": "NONE",
     "days_of_supply": 40, "category": "FROZEN", "stock": "90 CTN", "observation": "ok"},
    {"item": "ASTELLA CHICKPEA 400G 24X", "status": "CRITICAL", "spoilage_risk": "NONE",
     "days_of_supply": 3, "category": "DRY", "stock": "2 CTN", "observation": "low"},
    {"item": "MERIDYNE OLIVE OIL 1L", "status": "CRITICAL", "spoilage_risk": "NONE",
     "days_of_supply": 4, "category": "DRY", "stock": "5 CTN", "observation": "low"},
    {"item": "HAVLUND TUNA CHUNK 185G", "status": "CRITICAL", "spoilage_risk": "NONE",
     "days_of_supply": 2, "category": "DRY", "stock": "1 CTN", "observation": "low"},
]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, json.dumps(inv), json.dumps(recs)),
)

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "ledger@test.com"
    s["org_name"] = "LedgerOrg"
    s["model"] = "claude-haiku-4-5-20251001"
    s["is_admin"] = False
    s["tier"] = "enterprise"
    s["role"] = "admin"

resp = client.get(f"/results/{sid}")
html = resp.get_data(as_text=True)

checks = {
    "page returns 200": resp.status_code == 200,

    # ── Fault 1: the item name must survive intact ──
    "long item name renders in full": LONG_NAME in html,
    "name sits in its own grid cell": html.count('class="rec-row-namecell"') == 3,
    "ledger column headings render once per supplier group": html.count('class="rec-ledger-head"') == 2,
    "supplier type shows on every row": html.count('class="rec-row-type"') == 3,

    # ── Fault 2: chip counts come from recommendations, not inventory ──
    "chip count placeholders exist": html.count('class="mc-chip-count"') >= 9,
    "sidebar is gone": 'class="mc-sidebar"' not in html,
    "filter bar replaces it": 'class="mc-filterbar"' in html,
    "stat boxes are gone": 'mc-side-stat' not in html,
    "spoilage left the recommendation filters": 'data-val="SPOILAGE"' not in html,

    # ── Fault 3: the outcome prompt and its sales line are gone ──
    "order-placed question removed": 'Did you place this order?' not in html,
    "stockout question removed": 'Was the stockout avoided?' not in html,
    "sales line removed": "proves it's saving you money" not in html,
    "outcome span removed": 'class="rec-row-outcome"' not in html,

    # ── Note moved into the expanded panel ──
    "one note input per row": html.count('class="rec-note"') == 3,
    "note inputs live inside panels": html.count('rec-panel-note') == 3,
    "saved note still renders": 'called supplier' in html,
    "note button in the action column": html.count('data-rec-action="note"') == 3,

    # ── Preserved behaviour ──
    "three rows rendered": html.count('rec-row-main') == 3,
    "three expandable panels": html.count('class="rec-row-panel"') == 3,
    "critical colour hook intact": 'data-status="CRITICAL"' in html,
    "low-confidence tag intact": 'rec-row-lowconf' in html,
    "approve-all demoted to a link": 'rec-supplier-approve-link' in html,
    "keyboard hint bar intact": 'kb-hint-bar' in html,
    "quantity still rendered": '240 CTN' in html,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"{'ok ' if ok else 'FAIL'}: {name}")

if resp.status_code != 200:
    print("\n--- first 800 chars of response (for debugging) ---")
    print(html[:800])

if failed:
    print(f"\n{len(failed)} check(s) failed.")
    sys.exit(1)
print("\nAll ledger render checks passed.")
```

- [ ] **Step 2: Run it and confirm it fails**

```powershell
python tests/test_results_ledger_render.py
```

Expected: many `FAIL:` lines (`rec-row-namecell`, `mc-filterbar`, the outcome-prompt removals) and a non-zero exit. `page returns 200` and `three rows rendered` should already pass — if they don't, stop: something unrelated is broken and this plan assumes a working page.

---

## Task 2: Item name gets its own grid cell

The name currently sits as a bare `<span>` among seven unshrinkable siblings. Wrapping it with its tags into one cell is what makes a fixed-column grid possible.

**Files:**
- Modify: `templates/results.html:285-316`

- [ ] **Step 1: Replace the header row markup**

Find this block (starts at line 285):

```html
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
```

Replace it with:

```html
            <!-- Ledger row: # · name+tags · qty · order-by · type · actions.
                 Name and its tags share ONE grid cell so the tags can never
                 steal width from the name (the 2026-07-06 layout let them). -->
            <div class="rec-card-header rec-row-main" onclick="rowMainClick(event, {{ rec._card_idx }})">
              <span class="rec-row-num" aria-hidden="true">{{ rec._display_num }}</span>
              <span class="rec-row-namecell">
                <span class="rec-row-item">{{ rec.item }}</span>
                {% if low_conf %}<span class="rec-row-lowconf">low confidence</span>{% endif %}
                {% if rec.approved %}
                  <span class="badge badge-healthy">✓ Approved</span>
                {% elif rec.dismissed %}
                  <span class="badge badge-dead">Dismissed</span>
                {% endif %}
              </span>
              <span class="rec-row-qty">{{ rec.edited_quantity or rec.suggested_quantity or '—' }}</span>
```

- [ ] **Step 2: Add the supplier-type cell and wrap the actions**

Find this block (currently lines 296-316, immediately after the qty span):

```html
              {% if rec._order_by and rec._order_by.order_by_date %}
              <span class="rec-row-date rec-row-date-{{ rec._order_by.status }}">
                {% if rec._order_by.status == 'overdue' %}overdue {{ rec._order_by.buffer_days | abs }}d
                {% else %}by {{ rec._order_by.order_by_date }}{% endif %}
              </span>
              {% else %}
              <span class="rec-row-date">no date</span>
              {% endif %}
              {% if user_role != 'viewer' %}
              {# Buttons carry data attrs only — the delegated listener reads the
                 item from the card's data-item, so no user text is ever spliced
                 into inline JS (XSS-safe by construction). #}
              <span class="rec-row-act no-print">
                <button type="button" class="rec-row-btn rec-row-btn-ok" title="Approve"
                        data-rec-action="approve">✓</button>
                <button type="button" class="rec-row-btn" title="Dismiss"
                        data-rec-action="dismiss">✕</button>
              </span>
              {% endif %}
              <span class="rec-row-chev" id="chev-{{ rec._card_idx }}">▾</span>
            </div>
```

Replace it with:

```html
              {% if rec._order_by and rec._order_by.order_by_date %}
              <span class="rec-row-date rec-row-date-{{ rec._order_by.status }}">
                {% if rec._order_by.status == 'overdue' %}overdue {{ rec._order_by.buffer_days | abs }}d
                {% else %}{{ rec._order_by.order_by_date }}{% endif %}
              </span>
              {% else %}
              <span class="rec-row-date">—</span>
              {% endif %}
              <span class="rec-row-type">{{ (rec.supplier_type or 'other') | title }}</span>
              {# Buttons carry data attrs only — the delegated listener reads the
                 item from the card's data-item, so no user text is ever spliced
                 into inline JS (XSS-safe by construction). #}
              <span class="rec-row-act no-print">
                {% if user_role != 'viewer' %}
                <button type="button" class="rec-row-btn rec-row-btn-ok" title="Approve"
                        data-rec-action="approve">✓</button>
                <button type="button" class="rec-row-btn" title="Dismiss"
                        data-rec-action="dismiss">✕</button>
                <button type="button" class="rec-row-btn rec-row-btn-note" title="Add a note"
                        data-rec-action="note">note</button>
                {% endif %}
                <span class="rec-row-chev" id="chev-{{ rec._card_idx }}">▾</span>
              </span>
            </div>
```

Three things changed and each matters: the `#` prefix is gone from the row number (the column heading carries that meaning now), `by ` is dropped from the date (the `BY` column heading carries it), and the chevron moved *inside* `.rec-row-act` so the grid has exactly six children in every role — a viewer still gets the action cell, just with only the chevron in it.

- [ ] **Step 3: Add the column-heading row to each supplier group**

Find line 272:

```html
          <div class="rec-supplier-cards">
```

Replace with:

```html
          <div class="rec-supplier-cards">
          <div class="rec-ledger-head" aria-hidden="true">
            <span></span><span>Item</span><span>Order</span><span>By</span><span>Type</span><span></span>
          </div>
```

- [ ] **Step 4: Verify the markup assertions now pass**

```powershell
python tests/test_results_ledger_render.py
```

Expected: `ok` on `long item name renders in full`, `name sits in its own grid cell`, `ledger column headings render once per supplier group`, `supplier type shows on every row`, `note button in the action column`. Filter-bar and outcome-removal checks still `FAIL` — later tasks.

---

## Task 3: Grid columns and row dividers

**Files:**
- Modify: `static/style.css:471-506`

- [ ] **Step 1: Replace the flex row with a grid**

Find this block (lines 471-506):

```css
/* Line 1 */
.rec-row-main {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  cursor: pointer;
}
.rec-row-num { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.rec-row-item {
  flex: 1;
  min-width: 0;
  font-weight: 600;
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

Replace with:

```css
/* Ledger row. Fixed columns, name column takes the slack.
   NEVER give .rec-row-item ellipsis/nowrap again: in a flex row it was the only
   shrinkable child, so every badge stole from the name and live rows rendered
   as "AMM…" (21 Jul 2026). The grid makes that structurally impossible. */
.rec-ledger-head,
.rec-row-main {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 82px 92px 74px 124px;
  gap: 14px;
  align-items: center;
}
.rec-ledger-head {
  font-family: var(--font-display);
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.13em;
  color: var(--muted);
  padding: 9px 12px 7px;
}
.rec-ledger-head span:nth-child(3),
.rec-ledger-head span:nth-child(4) { text-align: right; }
.rec-row-main {
  padding: 11px 12px;
  cursor: pointer;
}
.rec-row-num { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; }
.rec-row-namecell {
  min-width: 0;
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 7px;
}
.rec-row-item {
  font-weight: 600;
  font-size: 14px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.rec-row-type {
  font-size: 11.5px;
  color: var(--muted);
  white-space: nowrap;
}
```

- [ ] **Step 2: Right-align the numeric columns and add the divider**

Find these two rules (lines 498-506 in the original file):

```css
.rec-row-qty {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  color: var(--brass-2, var(--brass));
  font-size: 14px;
  white-space: nowrap;
}
.rec-row-date { font-size: 12px; color: var(--muted); white-space: nowrap; }
```

Replace with:

```css
.rec-row-qty {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  color: var(--brass-2, var(--brass));
  font-size: 14px;
  white-space: nowrap;
  text-align: right;
}
.rec-row-date { font-size: 12px; color: var(--muted); white-space: nowrap; text-align: right; }
```

- [ ] **Step 3: Give every row a dividing line**

Find this rule (around line 460):

```css
.rec-card:hover { border-color: var(--brass); }
```

Add immediately after it:

```css
/* A line under every row — asked for explicitly: at 50+ rows the eye needs a
   rail to track along. Last row keeps it too so the group reads as closed. */
.rec-supplier-cards .rec-card { border-bottom: 1px solid var(--border); }
```

- [ ] **Step 4: Confirm the suite still passes**

```powershell
python run_tests.py
```

Expected: `0 failed`.

---

## Task 4: Quiet actions, loud on hover and focus

**Files:**
- Modify: `static/style.css:507-521`

- [ ] **Step 1: Replace the action-button styles**

Find this block (lines 507-521):

```css
.rec-row-act { display: flex; gap: 5px; }
.rec-row-btn {
  width: 26px;
  height: 26px;
  border-radius: 7px;
  cursor: pointer;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  font-size: 13px;
  line-height: 1;
}
.rec-row-btn:hover { border-color: var(--brass); }
.rec-row-btn-ok { border-color: rgba(143,188,143,0.5); color: var(--success); }
.rec-row-chev { color: var(--muted); font-size: 15px; width: 16px; text-align: center; line-height: 1; }
```

Replace with:

```css
/* Actions rest at reduced contrast so the numbers stay loud, and come up on
   hover or keyboard focus. 0.55 is a floor, not a preference — below it a
   control reads as disabled, and hiding them entirely means a non-power user
   never finds the only action on the page. */
.rec-row-act {
  display: flex;
  gap: 5px;
  align-items: center;
  justify-content: flex-end;
  opacity: 0.55;
  transition: opacity 140ms var(--ease-out);
}
.rec-card:hover .rec-row-act,
.rec-card.kb-active .rec-row-act,
.rec-row-act:focus-within { opacity: 1; }
.rec-row-btn {
  height: 24px;
  min-width: 24px;
  padding: 0 6px;
  border-radius: 7px;
  cursor: pointer;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  font-size: 12.5px;
  line-height: 1;
}
.rec-row-btn:hover { border-color: var(--brass); }
.rec-row-btn:focus-visible { outline: 2px solid var(--brass); outline-offset: 1px; }
.rec-row-btn-ok { border-color: rgba(143,188,143,0.5); color: var(--success); }
.rec-row-btn-note { font-size: 11px; color: var(--muted); }
.rec-row-chev { color: var(--muted); font-size: 15px; width: 16px; text-align: center; line-height: 1; }
```

- [ ] **Step 2: Confirm the suite still passes**

```powershell
python run_tests.py
```

Expected: `0 failed`.

---

## Task 5: Remove the outcome prompt, move the note into the panel

**Files:**
- Modify: `templates/results.html:318-358` (line 2 of the row)
- Modify: `templates/results.html:361-363` (top of the expanded panel)
- Modify: `templates/results.html:1172-1212` (the JS that injects the prompt)
- Modify: `static/style.css:541-542`, `static/style.css:594-607`

- [ ] **Step 1: Strip line 2 down to the reason snippet**

Find the whole block from `<!-- Line 2: reason snippet OR outcome prompt · note -->` (line 318) through its closing `</div>` (line 358) and replace it with:

```html
            <!-- Line 2: reason snippet only. The outcome prompt and the note
                 field both moved out — the prompt is gone from the product
                 (paper checkbox replaces it), the note lives in the panel. -->
            <div class="rec-row-sub">
              <span class="rec-row-snippet" id="snippet-{{ rec._card_idx }}">
                {{ ((rec.reason or '').split('.') | first ~ '.') if rec.reason else '' }}
              </span>
            </div>
```

- [ ] **Step 2: Put the note field at the top of the expanded panel**

Find lines 360-362:

```html
            <!-- Expanded panel: full detail (hidden until expand) -->
            <div class="rec-row-panel" id="panel-{{ rec._card_idx }}" hidden>
              <div class="rec-panel-meta">
```

Replace with:

```html
            <!-- Expanded panel: full detail (hidden until expand) -->
            <div class="rec-row-panel" id="panel-{{ rec._card_idx }}" hidden>
              {% if user_role != 'viewer' %}
              <div class="rec-panel-note">
                <label for="note-{{ rec._card_idx }}">Note</label>
                <input type="text" class="rec-note"
                       id="note-{{ rec._card_idx }}"
                       data-item="{{ rec.item }}"
                       placeholder="e.g. PO number, ETA, called supplier"
                       value="{{ rec.note or '' }}">
              </div>
              {% endif %}
              <div class="rec-panel-meta">
```

- [ ] **Step 3: Delete the prompt-injecting JavaScript**

Find the whole `_showOutcomePrompt` function (lines 1172-1192, from the comment `// Show the outcome question on a row's line 2...` through its closing `}`) and delete it entirely.

Then find the delegated listener (lines 1194-1212) and replace it with:

```javascript
// One delegated listener for every row button. Reads idx/item from the row's
// own attributes, so an untrusted item name never reaches an inline handler.
document.getElementById('recs-container')?.addEventListener('click', e => {
  const btn = e.target.closest('[data-rec-action]');
  if (!btn) return;
  const card = btn.closest('.rec-card');
  if (!card) return;
  const idx  = parseInt(card.id.replace('rec-', ''), 10);
  const item = card.dataset.item;
  const act  = btn.dataset.recAction;
  if (act === 'approve' || act === 'dismiss') {
    takeAction(idx, item, act);
  } else if (act === 'note') {
    openRowNote(idx);
  }
});

// The `note` button expands the row (if collapsed) and drops the cursor in.
function openRowNote(idx) {
  const panel = document.getElementById('panel-' + idx);
  if (panel && panel.hidden) toggleRow(idx);
  document.getElementById('note-' + idx)?.focus();
}
```

**Do not touch** the `/recommend/outcome` route in `app.py`, the `recordOutcome` function, or the `order_placed` / `outcome_status` database columns. They stay so a later automated proof mechanism can write to them without a migration.

- [ ] **Step 4: Find and remove any remaining call to the deleted function**

```powershell
Select-String -Path templates/results.html -Pattern "_showOutcomePrompt"
```

Expected: no output. If any line is returned, delete that call — it now references a function that no longer exists and would throw at runtime.

- [ ] **Step 5: Fix the badge insertion point**

`_setBadge` inserts the Approved/Dismissed badge before `.rec-row-qty`, which after Task 2 puts it in the wrong grid cell. Find this block (around line 1216):

```javascript
  const nb = document.createElement('span');
  nb.className = kind === 'approve' ? 'badge badge-healthy' : 'badge badge-dead';
  nb.textContent = kind === 'approve' ? '✓ Approved' : 'Dismissed';
  const qty = header.querySelector('.rec-row-qty');
  if (qty) header.insertBefore(nb, qty); else header.appendChild(nb);
```

Replace with:

```javascript
  const nb = document.createElement('span');
  nb.className = kind === 'approve' ? 'badge badge-healthy' : 'badge badge-dead';
  nb.textContent = kind === 'approve' ? '✓ Approved' : 'Dismissed';
  // Badge belongs in the name cell — the grid has no spare column for it.
  const cell = header.querySelector('.rec-row-namecell');
  (cell || header).appendChild(nb);
```

- [ ] **Step 6: Update the CSS for the moved note and the deleted outcome**

Find and delete these two lines (541-542):

```css
.rec-row-outcome { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1; }
.rec-row-outcome .outcome-why { font-size: 11.5px; }
```

Then find the note styles (lines 594-607) and replace them with:

```css
/* Note now lives in the expanded panel, so it can be a real labelled field
   instead of a 190px sliver squeezed onto a row. */
.rec-panel-note {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding-bottom: 11px;
  border-bottom: 1px dashed var(--border);
}
.rec-panel-note label {
  font-family: var(--font-display);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--muted);
}
.rec-note {
  flex: 1;
  margin: 0;
  padding: 5px 2px !important;
  font-size: 13px !important;
  background: transparent !important;
  border: none !important;
  border-bottom: 1px dashed rgba(243,239,223,0.28) !important;
  border-radius: 0 !important;
  box-shadow: none !important;
  color: var(--text);
}
.rec-note:focus { outline: none; border-bottom-color: var(--brass) !important; }
```

- [ ] **Step 7: Verify the removal assertions pass**

```powershell
python tests/test_results_ledger_render.py
```

Expected: `ok` on all four Fault-3 checks and all four note checks. Filter-bar checks still `FAIL`.

---

## Task 6: Filter bar replaces the sidebar

**Files:**
- Modify: `templates/results.html:125-185`
- Modify: `static/style.css:1047-1101`, `static/style.css:1178-1185`
- Modify: `templates/results.html` — add `initChipCounts()` near `applyFilters()`

- [ ] **Step 1: Replace the sidebar markup with a filter bar**

Find the block from `<div class="mc-layout">` (line 125) through `</aside>` (line 184) and replace the whole thing with:

```html
    <div class="mc-layout">

      <!-- Counted filter bar. Counts are filled in by initChipCounts() from the
           rendered rows — the old sidebar counted `inventory` while the list
           below showed `recommendations`, so its numbers never matched. -->
      <div class="mc-filterbar">
        <div class="mc-filter-group">
          <div class="mc-filter-heading">Status</div>
          <div class="mc-chips" data-filter="status">
            <span class="mc-chip" data-val="CRITICAL">Critical <b class="mc-chip-count">0</b></span>
            <span class="mc-chip" data-val="LOW">Low <b class="mc-chip-count">0</b></span>
          </div>
        </div>

        <div class="mc-filter-group">
          <div class="mc-filter-heading">Supplier type</div>
          <div class="mc-chips" data-filter="suptype">
            <span class="mc-chip" data-val="import">Import <b class="mc-chip-count">0</b></span>
            <span class="mc-chip" data-val="local">Local <b class="mc-chip-count">0</b></span>
            <span class="mc-chip" data-val="other">Other <b class="mc-chip-count">0</b></span>
          </div>
        </div>

        <div class="mc-filter-group">
          <div class="mc-filter-heading">Confidence</div>
          <div class="mc-chips" data-filter="confidence">
            <span class="mc-chip" data-val="HIGH">High <b class="mc-chip-count">0</b></span>
            <span class="mc-chip" data-val="MEDIUM">Medium <b class="mc-chip-count">0</b></span>
            <span class="mc-chip" data-val="LOW">Low <b class="mc-chip-count">0</b></span>
          </div>
        </div>

        <div class="mc-filter-group">
          <div class="mc-filter-heading">State</div>
          <div class="mc-chips" data-filter="action">
            <span class="mc-chip is-active" data-val="all">All <b class="mc-chip-count">0</b></span>
            <span class="mc-chip" data-val="pending">Pending <b class="mc-chip-count">0</b></span>
            <span class="mc-chip" data-val="approved">Approved <b class="mc-chip-count" id="approved-counter">0</b></span>
            <span class="mc-chip" data-val="dismissed">Dismissed <b class="mc-chip-count">0</b></span>
          </div>
        </div>
      </div>
```

The four Jinja `{% set %}` lines above (`crit_count`, `low_count`, `high_spoil`, `approved_init`, lines 120-123) are now unused by this section — **leave them in place**, the Inventory Health tab and the summary bar still read them.

- [ ] **Step 2: Add the count function**

In the `<script>` block, immediately before `function applyFilters() {` (line 698), insert:

```javascript
// Fill the chip counts from the rendered recommendation rows. Totals, not
// live-filtered subsets — a count that shrank as you filtered would be
// useless for deciding what to filter to next.
function initChipCounts() {
  const cards = document.querySelectorAll('.rec-card');
  const tally = { status: {}, suptype: {}, confidence: {} };
  cards.forEach(c => {
    const s = c.dataset.status || '';
    const t = (c.dataset.suptype || '').toLowerCase();
    const f = c.dataset.confidence || '';
    tally.status[s]      = (tally.status[s]      || 0) + 1;
    tally.suptype[t]     = (tally.suptype[t]     || 0) + 1;
    tally.confidence[f]  = (tally.confidence[f]  || 0) + 1;
  });
  const approved  = document.querySelectorAll('.rec-card.approved').length;
  const dismissed = document.querySelectorAll('.rec-card.dismissed').length;
  document.querySelectorAll('.mc-chip[data-val]').forEach(chip => {
    const dim = chip.closest('.mc-chips')?.dataset.filter;
    const out = chip.querySelector('.mc-chip-count');
    if (!dim || !out) return;
    if (dim === 'action') {
      const v = chip.dataset.val;
      out.textContent = v === 'all'       ? cards.length
                      : v === 'approved'  ? approved
                      : v === 'dismissed' ? dismissed
                      : cards.length - approved - dismissed;
    } else {
      out.textContent = tally[dim][chip.dataset.val] || 0;
    }
  });
}
initChipCounts();
```

- [ ] **Step 3: Keep the counts fresh after an approve or dismiss**

In `takeAction`, find these lines (around 1258):

```javascript
    const counter = document.getElementById('approved-counter');
    if (counter) {
      const approvedNow = document.querySelectorAll('.rec-card.approved').length;
      const old = parseInt(counter.textContent, 10) || 0;
      if (approvedNow !== old) {
        counter.textContent = approvedNow;
        counter.style.animation = 'none';
        counter.offsetHeight;
        counter.style.animation = 'numberFlash 600ms ease-out';
      }
    }
```

Replace with:

```javascript
    const counter = document.getElementById('approved-counter');
    const approvedNow = document.querySelectorAll('.rec-card.approved').length;
    const old = parseInt(counter?.textContent, 10) || 0;
    initChipCounts();
    if (counter && approvedNow !== old) {
      counter.style.animation = 'none';
      counter.offsetHeight;
      counter.style.animation = 'numberFlash 600ms ease-out';
    }
```

- [ ] **Step 4: Replace the layout and sidebar CSS**

Find the block from `.mc-layout {` (line 1047) through the closing brace of `.mc-side-stat.is-warn .v` (line 1101) and replace it with:

```css
/* ─────────────────── Mission control results page ─────────────────── */
.mc-layout {
  margin-top: 24px;
}
.mc-filterbar {
  display: flex;
  flex-wrap: wrap;
  gap: 26px;
  align-items: flex-start;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 18px;
  margin-bottom: 18px;
  position: sticky;
  top: 78px;
  z-index: 5;
  box-shadow: var(--shadow);
}
.mc-chip-count {
  font-variant-numeric: tabular-nums;
  color: var(--text);
  font-weight: 700;
  margin-left: 3px;
}
.mc-chip.is-active .mc-chip-count { color: #fff; }
```

- [ ] **Step 5: Fix the responsive rule that references the dead sidebar**

Find (lines 1178-1185):

```css
@media (max-width: 960px) {
  .mc-layout {
    grid-template-columns: 1fr;
  }
  .mc-sidebar {
    position: static;
  }
}
```

Replace with:

```css
/* Below ~900px the fixed ledger columns stop fitting, so rows fall back to a
   wrapping flex line. Pilot staff are desktop-only; this is for the public site. */
@media (max-width: 900px) {
  .mc-filterbar { position: static; gap: 16px; }
  .rec-ledger-head { display: none; }
  .rec-row-main {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 10px;
  }
  .rec-row-namecell { flex: 1 1 100%; }
  .rec-row-act { margin-left: auto; }
}
```

- [ ] **Step 6: Verify every ledger check now passes**

```powershell
python tests/test_results_ledger_render.py
```

Expected: every line `ok`, ending `All ledger render checks passed.`

---

## Task 7: Demote the approve-all button to a link

**Files:**
- Modify: `templates/results.html:263-270`
- Modify: `templates/results.html` — the label-rewriting code inside `applyFilters()` (lines 749-760)
- Modify: `static/style.css` — add the link style

- [ ] **Step 1: Change the button's class**

Find (lines 263-270):

```html
            {% if user_role != 'viewer' %}
            <button type="button" class="btn btn-primary btn-sm no-print rec-supplier-approve"
                    data-group-key="{{ group.key }}"
                    data-items='{{ group.item_names | tojson }}'
                    onclick="approveSupplier(this)">
              ✓ Approve all {{ group.count }} from this supplier
            </button>
            {% endif %}
```

Replace with:

```html
            {% if user_role != 'viewer' %}
            {# Demoted from a gold primary button: one click approves a whole
               supplier's worth of orders, so prominence should match consequence. #}
            <button type="button" class="rec-supplier-approve rec-supplier-approve-link no-print"
                    data-group-key="{{ group.key }}"
                    data-items='{{ group.item_names | tojson }}'
                    onclick="approveSupplier(this)">
              approve all {{ group.count }}
            </button>
            {% endif %}
```

- [ ] **Step 2: Match the shortened label in the filter code**

Inside `applyFilters()`, find (lines 757-759):

```javascript
      approveBtn.textContent = pendingVisible === 0
        ? '✓ All approved or dismissed'
        : '✓ Approve all ' + pendingVisible + ' from this supplier';
```

Replace with:

```javascript
      approveBtn.textContent = pendingVisible === 0
        ? 'all done'
        : 'approve all ' + pendingVisible;
```

- [ ] **Step 3: Style the link**

Add at the end of `static/style.css`:

```css
.rec-supplier-approve-link {
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--muted);
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}
.rec-supplier-approve-link:hover:not(:disabled) { color: var(--brass); }
.rec-supplier-approve-link:disabled { cursor: not-allowed; text-decoration: none; }
```

The confirm dialogue in `approveSupplier()` is untouched and still fires. It is the only guard on a bulk approve — do not remove it.

- [ ] **Step 4: Run everything**

```powershell
python tests/test_results_ledger_render.py
python run_tests.py
```

Expected: ledger checks all `ok`; suite `0 failed`.

---

## Task 8: Repair the old manual render check

`tests/verify_results_render.py` asserts markup this redesign deletes. It is not in the CI suite, so it will rot silently unless fixed now.

**Files:**
- Modify: `tests/verify_results_render.py:128-139`

- [ ] **Step 1: Replace the stale compact-row assertions**

Find this block (lines 128-139):

```python
    # ── compact-row markup (2026-07 redesign) ──
    "three row containers": html.count('rec-row-main') == 3,
    "three hidden panels": html.count('class="rec-row-panel"') == 3,
    "note inputs on rows": html.count('class="rec-note"') == 3,
    "saved note rendered": 'told team already' in html,
    "critical row colour hook present": 'data-status="CRITICAL"' in html,
    "low-confidence tag shown": 'rec-row-lowconf' in html,
    "reason snippet on line 2": 'Tight stock with a slow import supplier.' in html,
    "approved row shows outcome question": 'Was the stockout avoided?' in html,
    "qty on line 1": '160 CTN' in html,
    "confidence ring is gone": 'confidence-ring' not in html,
    "popover is gone": 'conf-popover' not in html,
```

Replace with:

```python
    # ── ledger markup (2026-07-21 redesign) ──
    "three row containers": html.count('rec-row-main') == 3,
    "three hidden panels": html.count('class="rec-row-panel"') == 3,
    "note inputs in panels": html.count('class="rec-note"') == 3,
    "saved note rendered": 'told team already' in html,
    "critical row colour hook present": 'data-status="CRITICAL"' in html,
    "low-confidence tag shown": 'rec-row-lowconf' in html,
    "reason snippet on line 2": 'Tight stock with a slow import supplier.' in html,
    "outcome question is gone": 'Was the stockout avoided?' not in html,
    "qty in the order column": '160 CTN' in html,
    "confidence ring is gone": 'confidence-ring' not in html,
    "popover is gone": 'conf-popover' not in html,
```

- [ ] **Step 2: Run it**

```powershell
python tests/verify_results_render.py
```

Expected: `All render checks passed.`

---

## Task 9: Review, then Phase 1 commit

- [ ] **Step 1: Run the full suite one more time**

```powershell
python run_tests.py
python tests/verify_results_render.py
```

Expected: `0 failed`, then `All render checks passed.`

- [ ] **Step 2: Review the diff**

This is a multi-file diff, so the repo rule is a `cavecrew-reviewer` pass before handing over commit commands. Fix anything it rates CRITICAL or HIGH.

```powershell
git diff --stat
```

Expected files: `templates/results.html`, `static/style.css`, `tests/verify_results_render.py`, plus the new `tests/test_results_ledger_render.py`.

- [ ] **Step 3: Check for client identifiers**

```powershell
git diff | Select-String -Pattern "Cool Link|coollink|Synergix"
```

Expected: no output. The repo is public.

- [ ] **Step 4: Commit**

```powershell
cd c:\BerthAI\BerthAI
git add templates/results.html static/style.css tests/test_results_ledger_render.py tests/verify_results_render.py docs/superpowers/specs/2026-07-21-results-ledger-redesign-design.md docs/superpowers/plans/2026-07-21-results-ledger-redesign.md
git commit -m "Results page: ledger layout, counted filter bar, no outcome prompt"
git push
```

- [ ] **Step 5: Check it live after Render finishes (2-5 min)**

Open a completed analysis and confirm: full item names, a line under every row, columns aligned under their headings, chip counts matching the list, no "Did you place this order?" anywhere.

---

# PHASE 2 — Print sheet and the other two tabs

## Task 10: Print sheet prints everything, with a pen checkbox and a write-in column

**Files:**
- Modify: `app.py:3043-3071`
- Modify: `templates/print_order.html`
- Create: `tests/test_print_order_sheet.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_print_order_sheet.py`:

```python
"""The print sheet is the paper half of the ordering loop: staff carry it,
tick what they ordered, and write the PO number and ETA on it by hand.

Asserts it prints EVERY recommendation (not just approved ones), marks the
approved ones, and provides both the tick box and the write-in space.

Run with: python tests/test_print_order_sheet.py
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_print_sheet.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

import types  # noqa: E402
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
from werkzeug.security import generate_password_hash  # noqa: E402

flask_app = appmod.app

db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role, analyses_used, chat_messages_used) "
    "VALUES (?,?,?,?,?,?,?,?,?)",
    ("printsheet@test.com", generate_password_hash("x"), "PrintOrg",
     "claude-haiku-4-5-20251001", "enterprise", 1, "admin", 0, 0),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("printsheet@test.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "PrintOrg", "complete"),
)

recs = [
    {"item": "BROOKVALE PRAWN MEAT 400G 12X", "supplier": "Nordvik Foods",
     "supplier_type": "import", "lead_time_days": 105, "days_of_supply": 12,
     "recommended_action": "REORDER", "suggested_quantity": "240 CTN",
     "confidence": "MEDIUM", "supplier_risk": "None", "flags": [],
     "reason": "Tight stock.", "approved": True},
    {"item": "PADIMAS JASMINE RICE 5KG", "supplier": "Kessington Trading",
     "supplier_type": "local", "lead_time_days": 21, "days_of_supply": 9,
     "recommended_action": "REORDER", "suggested_quantity": "60 BAG",
     "confidence": "MEDIUM", "supplier_risk": "None", "flags": [],
     "reason": "Steady sales."},
    {"error": "recommendation agent failed"},
]
inv = [
    {"item": "BROOKVALE PRAWN MEAT 400G 12X", "status": "CRITICAL", "spoilage_risk": "NONE",
     "days_of_supply": 12, "category": "FROZEN", "stock": "38 CTN", "observation": "low"},
    {"item": "PADIMAS JASMINE RICE 5KG", "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 9, "category": "DRY", "stock": "6 BAG", "observation": "low"},
]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, json.dumps(inv), json.dumps(recs)),
)

client = flask_app.test_client()
with client.session_transaction() as s:
    s["user_id"] = uid
    s["email"] = "printsheet@test.com"
    s["org_name"] = "PrintOrg"
    s["model"] = "claude-haiku-4-5-20251001"
    s["is_admin"] = False
    s["tier"] = "enterprise"
    s["role"] = "admin"

resp = client.get(f"/results/{sid}/print")
html = resp.get_data(as_text=True)

checks = {
    "page returns 200": resp.status_code == 200,
    "approved item printed": "BROOKVALE PRAWN MEAT 400G 12X" in html,
    "UNapproved item printed too": "PADIMAS JASMINE RICE 5KG" in html,
    "failed rec never printed": "recommendation agent failed" not in html,
    "approved rows are marked": html.count("approved-tag") == 1,
    "tick-box column header": "Ordered" in html,
    "one tick box per item": html.count('class="tickbox"') == 2,
    "write-in column header": "PO no. / ETA" in html,
    "one write-in cell per item": html.count('class="writein"') == 2,
    "landscape page rule present": "landscape" in html,
    "both suppliers grouped": html.count("group-head") >= 2,
}

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"{'ok ' if ok else 'FAIL'}: {name}")

if failed:
    print(f"\n{len(failed)} check(s) failed.")
    sys.exit(1)
print("\nAll print sheet checks passed.")
```

- [ ] **Step 2: Run it and confirm it fails**

```powershell
python tests/test_print_order_sheet.py
```

Expected: `FAIL` on `UNapproved item printed too`, `approved rows are marked`, both tick-box checks, both write-in checks, and `landscape page rule present`.

- [ ] **Step 3: Change the route to pass every printable recommendation**

In `app.py`, find lines 3045-3071 (the body of `print_results`) and replace from the docstring through the `render_template` call with:

```python
    """Render a print-ready sheet of every recommendation, approved ones marked.

    Approved-only until 21 Jul 2026: staff decide on paper with a pen, so the
    sheet has to carry the items they haven't approved on screen yet.
    """
    _verify_session_owner(upload_session_id)
    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (upload_session_id,))
    if not ar:
        flash("No results found.", "error")
        return redirect(url_for("dashboard"))
    try:
        recommendations = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        recommendations = []
    printable = [r for r in recommendations if not r.get("error")]
    # Current stock (qty on hand) isn't on the rec — join it from the inventory report.
    stock_map = _stock_on_hand_map(upload_session_id)
    # Enrich with effective values + order-by so the print template can stay simple.
    for r in printable:
        _normalise_confidence(r)
        r["_effective_qty"]      = _effective_qty(r)
        r["_effective_supplier"] = _effective_supplier(r)
        r["_order_by"]           = _compute_order_by(r)
        r["_current_stock"]      = stock_map.get(str(r.get("item", "")).strip())
        r["_order_covers"]       = _order_covers_months(r)
    # Group by supplier so each block prints as one hand-over-ready PO, same
    # grouping/order the on-screen results page uses.
    groups = _group_recs_by_supplier(printable, _status_by_item_map(upload_session_id))
    return render_template("print_order.html", groups=groups, total=len(printable),
                           approved_count=sum(1 for r in printable if r.get("approved")),
                           org_name=session["org_name"])
```

- [ ] **Step 4: Add the tick box and write-in column to the template**

In `templates/print_order.html`, find this rule inside `<style>` (line 21):

```css
    @media print { body { margin: 16px; } }
```

Replace with:

```css
    /* Two extra columns (tick box + write-in) don't fit portrait. */
    @page { size: A4 landscape; margin: 12mm; }
    .tickbox { display: inline-block; width: 16px; height: 16px;
               border: 1.5px solid #444; border-radius: 2px; }
    .writein { min-width: 150px; border-bottom: 1px solid #bbb; height: 22px; }
    .approved-tag { background: #dcfce7; color: #166534; padding: 1px 7px;
                    border-radius: 10px; font-size: 11px; margin-left: 6px; }
    @media print { body { margin: 8px; } }
```

- [ ] **Step 5: Update the header line**

Find (line 29):

```html
    &nbsp;|&nbsp; {{ total }} item(s) approved &nbsp;|&nbsp; {{ groups | length }} supplier(s)
```

Replace with:

```html
    &nbsp;|&nbsp; {{ total }} item(s), {{ approved_count }} approved on screen
    &nbsp;|&nbsp; {{ groups | length }} supplier(s)
```

- [ ] **Step 6: Add the two columns to the table**

Find the `<thead>` row (lines 36-46) and replace with:

```html
      <tr>
        <th>#</th>
        <th>Ordered</th>
        <th>Item</th>
        <th>On hand</th>
        <th>Qty to order</th>
        <th>Supplier</th>
        <th>Order by</th>
        <th>Current stock lasts</th>
        <th>This order lasts</th>
        <th>Notes</th>
        <th>PO no. / ETA</th>
      </tr>
```

Find the group header cell (line 51) and change its colspan:

```html
        <td colspan="9">
```

to:

```html
        <td colspan="11">
```

Find the first two body cells (lines 59-60):

```html
        <td>{{ ns.n }}</td>
        <td><strong>{{ r.item }}</strong></td>
```

Replace with:

```html
        <td>{{ ns.n }}</td>
        <td><span class="tickbox"></span></td>
        <td>
          <strong>{{ r.item }}</strong>
          {% if r.approved %}<span class="approved-tag">approved</span>{% endif %}
        </td>
```

Find the final Notes cell (lines 88-90):

```html
        <td>
          {%- if r.note -%}{{ r.note }}{%- else -%}—{%- endif -%}
        </td>
```

Replace with:

```html
        <td>
          {%- if r.note -%}{{ r.note }}{%- else -%}—{%- endif -%}
        </td>
        <td><div class="writein"></div></td>
```

- [ ] **Step 7: Update the empty-state message**

Find (line 97):

```html
  <p style="color:#6b7280; margin-top:24px;">No approved orders to print. Go back and approve recommendations first.</p>
```

Replace with:

```html
  <p style="color:#6b7280; margin-top:24px;">No recommendations to print for this analysis.</p>
```

- [ ] **Step 8: Verify**

```powershell
python tests/test_print_order_sheet.py
python run_tests.py
```

Expected: `All print sheet checks passed.` then `0 failed`.

- [ ] **Step 9: Eyeball the print preview**

Open a completed analysis, click Print / PDF, and check in the browser's preview that all eleven columns fit on landscape A4 and the write-in cell is wide enough to hand-write a PO number and a date. If a column overflows, reduce `td`/`th` padding from `10px 12px` to `6px 8px` — do not drop a column.

---

## Task 11: Results-scoped skin for the Inventory Health and Dead SKUs tables

`.data-table` is shared with `suppliers.html`, `diff.html`, `guide.html`, `settings.html`, `admin.html`, and `dashboard.html`. Restyling it directly would change six pages nobody asked about.

**Files:**
- Modify: `templates/results.html:503`, `templates/results.html:556`
- Modify: `static/style.css` — append the scoped class

- [ ] **Step 1: Add the scoped class to both tables**

In `templates/results.html`, find line 503:

```html
      <table class="data-table">
```

There are two matches — one in the Inventory Health tab (line 503) and one in the Dead SKUs tab (line 556). Change **both** to:

```html
      <table class="data-table data-table-ledger">
```

- [ ] **Step 2: Add the scoped styles**

Append to `static/style.css`:

```css
/* Ledger skin for the results-page tables only. Scoped on purpose: .data-table
   alone is shared with suppliers/diff/guide/settings/admin/dashboard. */
.data-table-ledger th {
  font-family: var(--font-display);
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.13em;
  color: var(--muted);
  font-weight: 600;
}
.data-table-ledger td { border-bottom: 1px solid var(--border); }
.data-table-ledger td:nth-child(3),
.data-table-ledger td:nth-child(6),
.data-table-ledger th:nth-child(3),
.data-table-ledger th:nth-child(6) {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
```

Columns 3 and 6 are Stock and Days of supply on the Inventory Health table. The Dead SKUs table has only four columns, so its column 3 (Stock on hand) is right-aligned too and column 6 does not exist — both correct.

- [ ] **Step 3: Give the spoilage count its new home**

Spoilage left the recommendation filter bar in Task 6 because it never filtered anything there. It belongs on this tab.

In `templates/results.html`, find line 501-502:

```html
    {% if live_items %}
    <div class="card" style="overflow-x:auto;">
```

Replace with:

```html
    {% if live_items %}
    {% if high_sp %}
    <p class="inv-spoilage-note">{{ high_sp }} item{{ 's' if high_sp != 1 }} at high spoilage risk.</p>
    {% endif %}
    <div class="card" style="overflow-x:auto;">
```

`high_sp` is already computed at line 122 and was previously only used by the deleted sidebar, so no new Jinja is needed.

Append to `static/style.css`:

```css
.inv-spoilage-note {
  font-size: 12.5px;
  color: var(--danger);
  margin-bottom: 12px;
}
```

- [ ] **Step 4: Verify**

```powershell
python run_tests.py
```

Expected: `0 failed`.

- [ ] **Step 5: Confirm no other page moved**

```powershell
git diff static/style.css | Select-String -Pattern "^\-.*\.data-table[^-]"
```

Expected: no output — meaning no existing `.data-table` rule was deleted or edited, only new `.data-table-ledger` rules added.

---

## Task 12: Review, then Phase 2 commit

- [ ] **Step 1: Full verification**

```powershell
python run_tests.py
python tests/verify_results_render.py
```

Expected: `0 failed`, then `All render checks passed.`

- [ ] **Step 2: Reviewer pass**

Multi-file diff again — run the `cavecrew-reviewer` agent on it and fix CRITICAL/HIGH findings before handing over commit commands.

Note for the reviewer's benefit: `print_results` is a client-data route, so a `security-reviewer` pass is also required by repo rule before this ships. The route's `_verify_session_owner(upload_session_id)` ownership check is unchanged and must stay the first statement in the function body.

- [ ] **Step 3: Client-identifier sweep**

```powershell
git diff | Select-String -Pattern "Cool Link|coollink|Synergix"
```

Expected: no output.

- [ ] **Step 4: Commit**

```powershell
cd c:\BerthAI\BerthAI
git add app.py templates/print_order.html templates/results.html static/style.css tests/test_print_order_sheet.py
git commit -m "Print sheet: all items with tick box and write-in column; ledger skin for results tables"
git push
```

---

## Stop conditions

Stop and report back instead of improvising if:

- `python run_tests.py` fails on a test this plan never touches — something else is broken and this work must not land on top of it.
- The grid columns don't line up after Task 3 because a row has a different number of children in some state (dismissed, viewer role, missing order-by). The fix is to make the *markup* always emit six cells, never to add grid rules per state.
- The `approved-counter` element ends up in two places at once. There must be exactly one `id="approved-counter"` on the page — `takeAction` looks it up by id.
- Task 10's route change makes the print sheet show recommendations from a different session. That would be an ownership-check failure and is a security bug — stop immediately and report it.

## Maintenance notes

- **Never restore `text-overflow: ellipsis` on `.rec-row-item`.** That is the fault this whole plan exists to fix, and it looks harmless in a diff.
- **Adding a column to the ledger means three edits**, not one: `grid-template-columns` in `.rec-ledger-head, .rec-row-main`, the heading `<span>`s in `.rec-ledger-head`, and a cell in every row state. Miss one and every row silently shifts a column.
- **`order_placed` and `outcome_status` are live columns with no UI.** They are deliberately orphaned, waiting on the PO-matching work. Do not "clean them up".
- **The `showConfirm` in `approveSupplier()` is a safety control**, not a nicety — it is the only thing standing between one click and a whole supplier's orders being approved.
