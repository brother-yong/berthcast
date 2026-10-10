"""Cover shorter than lead time: stock that runs out before a sales-sheet
delivery could land is CRITICAL, and an order Python sized for it is never
saved as a wait-and-see action.

Only model calls are canned. Real inventory, verifier and recommendation paths
run with synthetic products and a temporary database.
"""
import json
import logging
import os
import sys
import tempfile
import types
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.TemporaryDirectory(prefix="berthcast_cover_lead_", ignore_cleanup_errors=True)
os.environ["DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_tmp.name, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
_stub = types.ModuleType("anthropic")
_stub.Anthropic = lambda *a, **k: None
_stub.AnthropicError = Exception
sys.modules["anthropic"] = _stub

import database as db
import agents.shared as shared
import agents.inventory as inv
import agents.recommendation as rec
from agents.verifier import expected_status, verify_inventory_report
from quantity import parse_quantity

_failed = False
_inv_status = {}   # item -> status the canned health model returns (default CRITICAL)
_rec_reply = {}    # item -> (recommended_action, reason) the canned advisor returns
_DEFAULT_REPLY = ("MONITOR", "Over a month of cover. Watch sales and order when the trend is clearer.")
_inv_prompts, _inv_systems, _verify_inputs = [], [], []


def _check(name, cond, detail=""):
    global _failed
    print(("ok: " if cond else "FAIL: ") + name + (f" [{detail}]" if not cond else ""))
    _failed |= not bool(cond)


def _fake_inventory(model, system, user, **kwargs):
    _inv_systems.append(system)
    _inv_prompts.append(user)
    rows = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            name = line.split(" | ")[0][6:]
            rows.append({"item": name, "stock": 0, "category": "GENERAL",
                         "status": _inv_status.get(name, "CRITICAL"), "days_of_supply": 0,
                         "spoilage_risk": "NONE", "observation": "test"})
    return json.dumps(rows)


def _fake_recommendation(model, system, user, **kwargs):
    rows = []
    for block in user.split("---"):
        name = next((l[6:].strip() for l in block.splitlines() if l.startswith("Item: ")), None)
        if name:
            action, reason = _rec_reply.get(name, _DEFAULT_REPLY)
            rows.append({"item": name, "recommended_action": action, "reason": reason,
                         "suggested_quantity": None, "confidence": "MEDIUM", "supplier": "Unknown",
                         "supplier_risk": "LOW", "flags": []})
    return json.dumps(rows)


_real_verify = inv.verify_inventory_report


def _capture_verify(report, inputs_by_key):
    _verify_inputs.append(inputs_by_key)
    return _real_verify(report, inputs_by_key)


shared._call_claude = lambda *a, **k: "{}"
shared.sg_today = lambda: date(2026, 10, 9)
inv._call_claude = _fake_inventory
rec._call_claude = _fake_recommendation
inv.verify_inventory_report = _capture_verify
db.init_db()
_sid = 19300


def _seed(item, on_hand, lead, po_supplier=None):
    global _sid
    _sid += 1
    db.execute("INSERT INTO upload_sessions (id,user_id,org_name,status,scope,context_json) "
               "VALUES (?,?,?,?,?,?)", (_sid, 1, "SyntheticCo", "complete", "all", "{}"))
    db.execute(f'CREATE TABLE inventory_{_sid} (description TEXT, qty_on_hand TEXT, uom TEXT)')
    db.execute(f'INSERT INTO inventory_{_sid} VALUES (?,?,?)', (item, str(on_hand), "CTN"))
    db.execute(f'CREATE TABLE sales_{_sid} (date TEXT,item_description TEXT,qty_sold TEXT,supplier TEXT,lead_time_days TEXT)')
    for m in (6, 7, 8):
        db.execute(f'INSERT INTO sales_{_sid} VALUES (?,?,?,?,?)',
                   (f"2026-{m:02}-15", item, "60", "", str(lead)))
    if po_supplier:
        db.execute(f'CREATE TABLE purchase_orders_{_sid} (item_description TEXT, supplier_name TEXT)')
        db.execute(f'INSERT INTO purchase_orders_{_sid} VALUES (?,?)', (item, po_supplier))
    return _sid


def _run(item, on_hand, lead, po_supplier=None, force_low=False):
    sid = _seed(item, on_hand, lead, po_supplier)
    _inv_prompts.clear()
    _inv_systems.clear()
    _verify_inputs.clear()
    result = inv.run_inventory_agent(sid, "test", [], {})
    _check(f"inventory {sid} succeeds", "error" not in result, str(result))
    report = result.get("report", [])
    status = (report or [{}])[0].get("status")
    if force_low:
        report = [dict(r, status="LOW") for r in report]
    emits = []
    recs = rec.run_recommendation_agent(sid, "test", report, {}, progress_emit=emits.append,
                                        row_numbers=result.get("row_numbers"), confirmed_groups=[])
    return status, (recs or [{}])[0], sid, emits


def _line(item):
    return next((l for p in _inv_prompts for l in p.splitlines() if l.startswith("Item: " + item + " |")), "")


def _outcome(sid, item):
    rows = db.query("SELECT action_recommended FROM recommendation_outcomes WHERE session_id=? AND item=?",
                    (sid, item))
    return rows[0]["action_recommended"] if rows else None


# 1-6. expected_status: the narrow sheet-lead rule.
_check("supply under the sheet lead is CRITICAL", expected_status(3.5, None, 210, 180, 4.2) == "CRITICAL")
_check("supply equal to the sheet lead is not CRITICAL",
       expected_status(4.2, None, 252, 180, 4.2) == "HEALTHY" and expected_status(2.0, None, 120, 180, 2.0) == "LOW")
_check("sheet lead never moves the LOW band", expected_status(5.0, None, 300, 180, 4.2) == "HEALTHY")
_check("both leads: sheet rule on top of the PO bands",
       expected_status(3.5, 1.9, 210, 180, 4.2) == "CRITICAL"
       and expected_status(4.5, 1.9, 270, 180, 4.2) == "HEALTHY"
       and expected_status(3.0, 4.0, 1, 1, 2.0) == "CRITICAL"
       and expected_status(5.0, 4.0, 1, 1, 2.0) == "LOW")
_check("old 4-arg call unchanged", expected_status(3.5, None, 210, 180) == "HEALTHY")
_check("unreadable stock with sales stays LOW", expected_status(1.0, None, None, 50, 4.2) == "LOW")
_check("unreadable stock without sales stays HEALTHY", expected_status(1.0, None, None, 0, 4.2) == "HEALTHY")
_check("no sales data at zero stock stays REVIEW", expected_status(1.0, None, 0, None, 4.2) == "REVIEW")
_check("no sales data with stock stays HEALTHY", expected_status(1.0, None, 5, None, 4.2) == "HEALTHY")
_check("no velocity stays a judgement call", expected_status(None, None, 100, 0, 4.2) is None)
_check("zero stock with sales stays CRITICAL", expected_status(0.0, None, 0, 50, 4.2) == "CRITICAL")

# 7. verify_inventory_report reads the new key.
_corn = "BROOKVALE CORN 400G"
_key = shared.normalise_match_key(_corn)
_inputs = {"months_supply": 3.5, "lt_months": None, "stock": 210, "total_sold": 180, "unit_clash": None}
_rep = [{"item": _corn, "status": "HEALTHY", "days_of_supply": 105, "spoilage_risk": "NONE"}]
_n = verify_inventory_report(_rep, {_key: dict(_inputs, sheet_lt_months=4.2)})
_check("verifier corrects HEALTHY to CRITICAL under the sheet lead",
       _rep[0]["status"] == "CRITICAL" and _rep[0].get("_status_corrected") is True and _n[0] == 1, str((_rep, _n)))
_rep = [{"item": _corn, "status": "DEAD", "spoilage_risk": "NONE"}]
verify_inventory_report(_rep, {_key: dict(_inputs, months_supply=None, stock=40, total_sold=0, sheet_lt_months=4.2)})
_check("DEAD judgement kept with a sheet lead", _rep[0]["status"] == "DEAD", str(_rep))
_rep = [{"item": _corn, "status": "CRITICAL", "days_of_supply": 105, "spoilage_risk": "NONE"}]
verify_inventory_report(_rep, {_key: dict(_inputs, unit_clash="BAG, CTN", sheet_lt_months=4.2)})
_check("unit clash stays REVIEW with a sheet lead", _rep[0]["status"] == "REVIEW", str(_rep))
_rep = [{"item": _corn, "status": "LOW", "days_of_supply": 105, "spoilage_risk": "NONE"}]
verify_inventory_report(_rep, {_key: dict(_inputs)})
_check("old input shape without the key keeps the old result", _rep[0]["status"] == "HEALTHY", str(_rep))

# 8. Scenario A: 25 days of cover against a 112-day sheet lead.
peach = "BROOKVALE PEACH HALVES 820G"
status, a, sid, emits = _run(peach, 50, 112)
calc = a.get("order_calc") or {}
_check("A: status CRITICAL", status == "CRITICAL", status)
_check("A: MONITOR saved as REORDER", a.get("recommended_action") == "REORDER", str(a.get("recommended_action")))
_check("A: Python reason replaces the model's", a.get("reason") == getattr(rec, "_COVER_SHORT_REASON", None), str(a.get("reason")))
_check("A: quantity is the sized order",
       a.get("suggested_quantity") == "264 CTN" and parse_quantity(a.get("suggested_quantity")) == calc.get("order"),
       str(a.get("suggested_quantity")))
_check("A: order state on the sheet lead", calc.get("state") == "order" and a.get("lead_time_days") == 112, str(calc))
_check("A: outcome stub stores REORDER", _outcome(sid, peach) == "REORDER", str(_outcome(sid, peach)))
_fix_lines = [e for e in emits if e.startswith("Safety check: changed 1 ")]
_check("A: one action-fix progress line", len(_fix_lines) == 1, str(_fix_lines))
_fix_line = _fix_lines[0] if _fix_lines else ""

# 9. Scenario B: 105 days of cover against a 126-day sheet lead, no PO lead.
mackerel = "NORDVIK SMOKED MACKEREL 200G"
status, b, _, _ = _run(mackerel, 210, 126)
line = _line(mackerel)
system = _inv_systems[0] if _inv_systems else ""
_check("B: model CRITICAL kept", status == "CRITICAL", status)
_check("B: order sized and saved as REORDER",
       (b.get("order_calc") or {}).get("state") == "order" and b.get("suggested_quantity") == "132 CTN"
       and b.get("recommended_action") == "REORDER", str(b))
_check("B: prompt shows the sheet lead under its own label",
       "| Months of supply: 3.5 | Sales-sheet lead time: 126d (4.2mo)" in line, line)
_check("B: prompt never calls it Lead time", " | Lead time: " not in line, line)
_check("B: verifier gets the printed sheet lead",
       bool(_verify_inputs) and (_verify_inputs[0].get(shared.normalise_match_key(mackerel)) or {}).get("sheet_lt_months") == 4.2,
       str(_verify_inputs))
_inv_status[mackerel] = "HEALTHY"
status, _, _, _ = _run(mackerel, 210, 126)
_inv_status.clear()
_check("B: model HEALTHY corrected to CRITICAL", status == "CRITICAL", status)

# 10. Both leads: PO lead sets the bands, the sheet lead adds the CRITICAL rule.
oats = "KESTREL OATS 1KG"
_inv_status[oats] = "HEALTHY"
status, k, _, _ = _run(oats, 210, 126, po_supplier="ALDERMOOR TRADING")
_inv_status.clear()
line = _line(oats)
_check("both leads: both tags shown", "| Lead time: 56d (1.9mo) | Sales-sheet lead time: 126d (4.2mo)" in line, line)
_check("both leads: model HEALTHY corrected to CRITICAL", status == "CRITICAL", status)
_check("both leads: order sized on the sheet lead",
       k.get("lead_time_days") == 126 and k.get("suggested_quantity") == "132 CTN", str(k))

# 11. System prompt: one new rule, the existing bands untouched.
_check("system prompt describes the sheet lead field", "'Sales-sheet lead time' (the delivery time stated on the sales sheet)" in system)
_check("system prompt has the narrow rule",
       "SALES-SHEET LEAD TIME is a separate field" in system
       and "CRITICAL also when Months of supply < the Sales-sheet lead time in months" in system)
_check("system prompt keeps the lead-time and fixed bands",
       "When Lead time IS provided for an item" in system and "  - LOW: Months of supply between 1 and 3.\n" in system)

# 12-14. Model actions on scenario A numbers.
_rec_reply[peach] = ("ESCALATE", "model reason")
_, r, _, _ = _run(peach, 50, 112)
_check("ESCALATE kept with its reason and quantity",
       r.get("recommended_action") == "ESCALATE" and r.get("reason") == "model reason"
       and r.get("suggested_quantity") == "264 CTN", str(r))
_rec_reply[peach] = ("REORDER", "model reason")
_, r, _, _ = _run(peach, 50, 112)
_check("REORDER keeps the model reason", r.get("recommended_action") == "REORDER" and r.get("reason") == "model reason", str(r))
_rec_reply[peach] = (" hold ", "model reason")
_, r, _, _ = _run(peach, 50, 112)
_check("padded lower-case HOLD flips",
       r.get("recommended_action") == "REORDER" and r.get("reason") == getattr(rec, "_COVER_SHORT_REASON", None), str(r))
_rec_reply.clear()

# 15. Unknown lead: the 2-month sizing default is not evidence.
jam = "PADIMAS JAM 450G"
_, j, sid, emits = _run(jam, 50, "")
calc = j.get("order_calc") or {}
_check("unknown lead: order on the sizing default",
       calc.get("state") == "order" and j.get("suggested_quantity") == "160 CTN" and calc.get("lead_known") is False, str(calc))
_check("unknown lead: MONITOR and its reason kept",
       j.get("recommended_action") == "MONITOR" and j.get("reason") == _DEFAULT_REPLY[1], str(j))
_check("unknown lead: no action-fix line", not any(e.startswith("Safety check: changed") for e in emits),
       str([e for e in emits if e.startswith("Safety check")]))
_check("unknown lead: outcome stub says MONITOR", _outcome(sid, jam) == "MONITOR", str(_outcome(sid, jam)))

# 16-17. Cover at or past the lead (buffer zone, equal boundary) stays as the model said.
beans = "NORDVIK BEANS 1KG"
status, n, _, _ = _run(beans, 130, 60)
_check("buffer zone: status LOW", status == "LOW", status)
_check("buffer zone: 65d cover on a 60d lead stays MONITOR",
       (n.get("order_calc") or {}).get("state") == "order" and n.get("suggested_quantity") == "80 CTN"
       and n.get("recommended_action") == "MONITOR", str(n))
rice = "VANMARK RICE 5KG"
status, v, _, _ = _run(rice, 120, 60)
_check("equal boundary: status LOW", status == "LOW", status)
_check("equal boundary: 60d cover on a 60d lead stays MONITOR",
       (v.get("order_calc") or {}).get("state") == "order" and v.get("suggested_quantity") == "90 CTN"
       and v.get("recommended_action") == "MONITOR", str(v))

# 18. Covered: no order, so nothing to contradict.
flour = "KESTREL FLOUR 1KG"
_, f, _, _ = _run(flour, 500, 112, force_low=True)
_check("covered: empty quantity", (f.get("order_calc") or {}).get("state") == "covered" and f.get("suggested_quantity") == "", str(f))
_check("covered: MONITOR and its reason kept",
       f.get("recommended_action") == "MONITOR" and f.get("reason") == _DEFAULT_REPLY[1], str(f))

# 18b. Irregular seller: Python itself flags "Verify with your team before
# ordering", so a short-cover MONITOR is not flipped to "order now".
_sid += 1
lumpy, ctrl = "PADIMAS COCONUT MILK 400ML", "PADIMAS CONTROL ITEM 1KG"
db.execute("INSERT INTO upload_sessions (id,user_id,org_name,status,scope,context_json) "
           "VALUES (?,?,?,?,?,?)", (_sid, 1, "SyntheticCo", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{_sid} (description TEXT, qty_on_hand TEXT, uom TEXT)')
db.execute(f'INSERT INTO inventory_{_sid} VALUES (?,?,?)', (lumpy, "20", "CTN"))
db.execute(f'INSERT INTO inventory_{_sid} VALUES (?,?,?)', (ctrl, "500", "CTN"))
db.execute(f'CREATE TABLE sales_{_sid} (date TEXT,item_description TEXT,qty_sold TEXT,supplier TEXT,lead_time_days TEXT)')
for m in range(3, 9):
    db.execute(f'INSERT INTO sales_{_sid} VALUES (?,?,?,?,?)', (f"2026-{m:02}-15", ctrl, "60", "", "112"))
    if m in (3, 8):
        db.execute(f'INSERT INTO sales_{_sid} VALUES (?,?,?,?,?)', (f"2026-{m:02}-15", lumpy, "90", "", "112"))
_r = inv.run_inventory_agent(_sid, "test", [], {})
_emits = []
_recs = rec.run_recommendation_agent(_sid, "test", _r.get("report", []), {}, progress_emit=_emits.append,
                                     row_numbers=_r.get("row_numbers"), confirmed_groups=[])
lu = next((x for x in _recs if x.get("item") == lumpy), {})
_check("lumpy: scenario is an order with the irregular-seller flag",
       (lu.get("order_calc") or {}).get("state") == "order"
       and any("Verify with your team before ordering" in str(f) for f in lu.get("flags") or []), ascii(lu))
_check("lumpy: MONITOR and its reason kept", lu.get("recommended_action") == "MONITOR"
       and lu.get("reason") == _DEFAULT_REPLY[1], ascii(lu))

# 18b-2. Stopped sales on a long steady history (not lumpy): Python's own
# "Check before ordering" flag stands, so MONITOR is not flipped either.
_sid += 1
gone, ctrl2 = "NORDVIK RYE CRACKERS 200G", "NORDVIK CONTROL ITEM 1KG"
db.execute("INSERT INTO upload_sessions (id,user_id,org_name,status,scope,context_json) "
           "VALUES (?,?,?,?,?,?)", (_sid, 1, "SyntheticCo", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{_sid} (description TEXT, qty_on_hand TEXT, uom TEXT)')
db.execute(f'INSERT INTO inventory_{_sid} VALUES (?,?,?)', (gone, "5", "CTN"))
db.execute(f'INSERT INTO inventory_{_sid} VALUES (?,?,?)', (ctrl2, "500", "CTN"))
db.execute(f'CREATE TABLE sales_{_sid} (date TEXT,item_description TEXT,qty_sold TEXT,supplier TEXT,lead_time_days TEXT)')
for y, m in [(2025, mm) for mm in range(9, 13)] + [(2026, mm) for mm in range(1, 9)]:
    db.execute(f'INSERT INTO sales_{_sid} VALUES (?,?,?,?,?)', (f"{y}-{m:02}-15", ctrl2, "60", "", "112"))
    if (y, m) < (2026, 6):
        db.execute(f'INSERT INTO sales_{_sid} VALUES (?,?,?,?,?)', (f"{y}-{m:02}-15", gone, "60", "", "112"))
_r = inv.run_inventory_agent(_sid, "test", [], {})
_recs = rec.run_recommendation_agent(_sid, "test", _r.get("report", []), {}, progress_emit=[].append,
                                     row_numbers=_r.get("row_numbers"), confirmed_groups=[])
gn = next((x for x in _recs if x.get("item") == gone), {})
_check("stopped, not lumpy: scenario is an order with the stopped flag and no irregular flag",
       (gn.get("order_calc") or {}).get("state") == "order" and (gn.get("order_calc") or {}).get("stopped_since")
       and not any("Irregular seller" in str(f) for f in gn.get("flags") or []), ascii(gn))
_check("stopped, not lumpy: MONITOR and its reason kept", gn.get("recommended_action") == "MONITOR"
       and gn.get("reason") == _DEFAULT_REPLY[1], ascii(gn))

# 18c. Junk-typed model fields elsewhere in the rec must not cost the run its
# outcome stubs (one bad bind fails the whole bulk insert).
_plain_rec = rec._call_claude


def _junk_rec(*a, **k):
    rows = json.loads(_plain_rec(*a, **k))
    return json.dumps([dict(r, supplier=["Unknown"], confidence={"x": 1}) for r in rows])


rec._call_claude = _junk_rec
try:
    _, j, jsid, _ = _run("BROOKVALE APRICOT HALVES 820G", 50, 112)
finally:
    rec._call_claude = _plain_rec
_check("junk supplier/confidence: flip still saved", j.get("recommended_action") == "REORDER", ascii(j))
_check("junk supplier/confidence: outcome stub saved as REORDER",
       _outcome(jsid, "BROOKVALE APRICOT HALVES 820G") == "REORDER", str(_outcome(jsid, "BROOKVALE APRICOT HALVES 820G")))

# 19. No em dash in any new staff- or model-facing text.
_start = system.find("SALES-SHEET LEAD TIME")
_block = system[_start:system.find("DEAD rules")] if _start >= 0 else ""
_check("no em dash in the Python reason", "\u2014" not in getattr(rec, "_COVER_SHORT_REASON", "\u2014"))
_check("no em dash in the progress line", bool(_fix_line) and "\u2014" not in _fix_line, ascii(_fix_line))
_check("no em dash in the new prompt rule", bool(_block) and "\u2014" not in _block, ascii(_block))

logging.shutdown()
_tmp.cleanup()
if _failed:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll cover-shorter-than-lead tests passed.")
