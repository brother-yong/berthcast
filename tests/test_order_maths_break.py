"""Independent bad-input checks for Python-owned order maths.

Real pipeline and display helpers run against synthetic uploads; model replies
are deliberately misleading. No real customer data or network calls are used.
"""
import atexit
import json
import logging
import os
import re
import sys
import tempfile
import types
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.TemporaryDirectory(prefix="berthcast_order_break_", ignore_cleanup_errors=True)


def _cleanup():
    logging.shutdown()
    _tmp.cleanup()


atexit.register(_cleanup)
os.environ["DB_PATH"] = os.path.join(_tmp.name, "break.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_tmp.name, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "unused-test-key")
_anthropic = types.ModuleType("anthropic")
_anthropic.Anthropic = lambda *a, **k: None
_anthropic.AnthropicError = Exception
sys.modules["anthropic"] = _anthropic

import database as db
import agents.shared as shared
import agents.inventory as inventory
import agents.recommendation as recommendation
from ingest_recipe import execute_recipe, validate_recipe, RecipeRefusal
from quantity import parse_quantity
from rec_logic import _quantity_basis, _compute_order_by, _tender_split

_failed = False
_sid = 17800
_reply_mutator = None
_rec_prompts = []
_inv_prompts = []


def _check(name, cond, detail=""):
    global _failed
    print(("ok: " if cond else "FAIL: ") + name + (f" [{detail}]" if not cond else ""))
    _failed |= not bool(cond)


def _inventory_reply(model, system, user, **kwargs):
    _inv_prompts.append(user)
    rows = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            rows.append({"item": line[6:].split(" | ")[0], "stock": 0,
                         "status": "CRITICAL", "category": "GENERAL",
                         "spoilage_risk": "NONE", "days_of_supply": 0,
                         "observation": "Synthetic test"})
    return json.dumps(rows)


def _recommendation_reply(model, system, user, **kwargs):
    _rec_prompts.append(user)
    rows = []
    for block in user.split("---"):
        match = re.search(r"^Item: (.+)$", block, re.MULTILINE)
        if not match:
            continue
        rows.append({"item": match.group(1).strip(), "suggested_quantity": "5000 CTN",
                     "recommended_action": "REORDER", "confidence": "HIGH",
                     "supplier": "Unknown", "supplier_risk": "NONE", "flags": []})
    if _reply_mutator:
        rows = _reply_mutator(rows)
    return json.dumps(rows)


shared._call_claude = lambda *a, **k: "{}"
shared.sg_today = lambda: date(2026, 9, 26)
inventory._call_claude = _inventory_reply
recommendation._call_claude = _recommendation_reply
db.init_db()


def _seed(stock, sales, cols=None):
    global _sid
    _sid = max(_sid, db.query("SELECT COALESCE(MAX(id),0) AS largest FROM upload_sessions")[0]["largest"]) + 1
    cols = cols or ["description", "qty_on_hand", "qty_on_order", "ar_inv_back_order", "free_balance", "uom"]
    db.execute("INSERT INTO upload_sessions (id,user_id,org_name,status,scope,context_json) VALUES (?,?,?,?,?,?)",
               (_sid, 1, "SyntheticCo", "complete", "all", "{}"))
    db.execute(f'CREATE TABLE inventory_{_sid} (' + ",".join(f'"{c}" TEXT' for c in cols) + ')')
    db.execute(f'CREATE TABLE sales_{_sid} (date TEXT,item_description TEXT,qty_sold TEXT,supplier TEXT,lead_time_days TEXT)')
    for row in stock:
        db.execute(f'INSERT INTO inventory_{_sid} VALUES (' + ",".join("?" for _ in cols) + ')',
                   tuple(row.get(c, "") for c in cols))
    for row in sales:
        db.execute(f'INSERT INTO sales_{_sid} VALUES (?,?,?,?,?)', row)
    return _sid


def _sales(name, qty=60, months=(6, 7, 8), supplier="", lt=""):
    return [(f"2026-{m:02}-15", name, str(qty), supplier, str(lt)) for m in months]


def _run(stock, sales, cols=None, groups=None):
    sid = _seed(stock, sales, cols)
    _rec_prompts.clear()
    _inv_prompts.clear()
    result = inventory.run_inventory_agent(sid, "test", groups or [], {})
    _check(f"inventory completes for synthetic case {sid}", "error" not in result, str(result))
    # Sizing cases deliberately enter the recommendation stage, independent
    # of health status which correctly uses physical stock rather than position.
    report = [dict(row, status="LOW") for row in result.get("report", [])]
    recs = recommendation.run_recommendation_agent(sid, "test", report, {},
              row_numbers=result.get("row_numbers"), confirmed_groups=groups or [])
    _check(f"recommendation completes for synthetic case {sid}", bool(recs) and "error" not in recs[0], str(recs))
    return result, recs


name = "KESSINGTON PASTA FUSILLI 500G"
stock = {"description": name, "qty_on_hand": "100", "qty_on_order": "50",
         "ar_inv_back_order": "30", "free_balance": "120", "uom": "CTN"}

# Invalid free stock must use the known position, never extract embedded digits.
for value in ("N/A", "", "abc123"):
    result, recs = _run([{**stock, "free_balance": value}], _sales(name))
    stamp = result["row_numbers"].get(shared.normalise_match_key(name), {})
    _check(f"free stock {value!r} falls back to 100 + 50 - 30", stamp.get("position") == 120, str(stamp))
    _check(f"invalid free stock {value!r} orders 90", parse_quantity(recs[0].get("suggested_quantity")) == 90)
for value, position, qty in (("(50)", -50, 260), ("1,200", 1200, None)):
    result, recs = _run([{**stock, "free_balance": value}], _sales(name))
    stamp = result["row_numbers"].get(shared.normalise_match_key(name), {})
    _check(f"free stock {value!r} reads {position}", stamp.get("position") == position)
    _check(f"free stock {value!r} gives correct order state", parse_quantity(recs[0].get("suggested_quantity")) == qty)

for value in ("0", "-5", "abc", "9999", ""):
    result, recs = _run([{**stock, "qty_on_hand": "0", "free_balance": "0"}],
                         _sales(name, lt=value))
    stamp = result["row_numbers"].get(shared.normalise_match_key(name), {})
    _check(f"invalid sheet lead time {value!r} is ignored", stamp.get("lead_time_days") is None)
    _check(f"invalid sheet lead time {value!r} keeps unknown-supplier default", recs[0].get("lead_time_days") is None and parse_quantity(recs[0].get("suggested_quantity")) == 210)

variant_a, variant_b = "PADIMAS FUSILLI 500G", "BROOKVALE FUSILLI 500G"
groups = [{"canonical": name, "variants": [variant_a, variant_b]}]
result, recs = _run([{**stock, "free_balance": "0"}],
                    _sales(variant_a, qty=30, lt=70) + _sales(variant_b, qty=30, lt=70), groups=groups)
_check("two 70-day lines stay 70, never sum to 140",
       result["row_numbers"][shared.normalise_match_key(name)]["lead_time_days"] == 70 and recs[0].get("lead_time_days") == 70)

_reply_mutator = lambda rows: [dict(r, suggested_quantity="Covered by 12,000 on order") for r in rows]
_, recs = _run([{**stock, "free_balance": "12000"}], _sales(name))
covered = recs[0]
_check("digit-bearing covered prose cannot become an order", covered.get("suggested_quantity") == "" and covered.get("order_calc", {}).get("state") == "covered")
_reply_mutator = lambda rows: [dict(r, item="kessington pasta-fusilli 500g") for r in rows]
_, recs = _run([{**stock, "free_balance": "12000"}], _sales(name))
_check("covered case and punctuation drift retain covered state", recs[0].get("order_calc", {}).get("state") == "covered" and recs[0].get("suggested_quantity") == "")

# The model cannot add arithmetic metadata, whether its name matches or not.
for forged_name in (name, "NORDVIK INVENTED BEANS 1KG"):
    _reply_mutator = lambda rows, echoed=forged_name: [dict(rows[0], item=echoed,
        order_calc={"state": "covered", "need": 1, "position": 900000, "spare": 900000})]
    _, recs = _run([stock], _sales(name))
    if forged_name == name:
        _check("matched model cannot replace Python order calculation",
               recs[0].get("order_calc", {}).get("state") == "order" and
               recs[0].get("order_calc", {}).get("need") == 210 and
               parse_quantity(recs[0].get("suggested_quantity")) == 90)
    else:
        _check("unsent item quantity becomes Verify with team", recs[0].get("suggested_quantity") == "Verify with team")
        _check("unsent item cannot retain forged order calculation", "order_calc" not in recs[0])
_reply_mutator = None

_, recs = _run([{**stock, "qty_on_hand": "N/A"},
               {"description": "NORDVIK CONTROL BEANS 1KG", "qty_on_hand": "10", "uom": "CTN"}], _sales(name),
               cols=["description", "qty_on_hand", "uom"])
_check("unreadable physical stock cannot save the gross order", recs[0].get("suggested_quantity") == "Verify with team" and recs[0].get("order_calc", {}).get("state") == "no_position")
_check("unreadable stock flag still states need", any("need about 210 CTN" in f for f in recs[0].get("flags", [])))
_, recs = _run([{**stock, "qty_on_hand": "0", "ar_inv_back_order": "1e6"}], _sales(name),
               cols=["description", "qty_on_hand", "ar_inv_back_order", "uom"])
_check("million-unit back order saves plain integer quantity", recs[0].get("suggested_quantity") == "1000210 CTN", str(recs[0]))

# Review 27 Sep 2026: "1e999" parses as infinity (inf - inf is NaN), and round()
# on it in the order step crashed the whole run. Such a position is unknown.
for label, cells in (("free stock 1e999", {"free_balance": "1e999"}),
                     ("on order and back order 1e999", {"free_balance": "", "qty_on_order": "1e999",
                                                        "ar_inv_back_order": "1e999"})):
    try:
        _, recs = _run([{**stock, **cells}], _sales(name))
        _check(f"{label} leaves the order unsized instead of crashing the run",
               recs[0].get("suggested_quantity") == "Verify with team"
               and recs[0].get("order_calc", {}).get("state") == "no_position", str(recs[0]))
    except Exception as exc:
        _check(f"{label} leaves the order unsized instead of crashing the run", False, repr(exc))

# Security review 27 Sep 2026: a stated Avg/Month cell of "1e999" reads as
# infinity too; that demand is unknown, not a reason to crash the run.
sid = _seed([stock], _sales(name))
db.execute(f"ALTER TABLE sales_{sid} ADD COLUMN avg_qty_month TEXT")
db.execute(f"UPDATE sales_{sid} SET avg_qty_month = '1e999'")
try:
    inf_result = inventory.run_inventory_agent(sid, "test", [], {})
    inf_recs = recommendation.run_recommendation_agent(
        sid, "test", [dict(r, status="LOW") for r in inf_result.get("report", [])], {},
        row_numbers=inf_result.get("row_numbers"))
    _check("infinite stated demand leaves the order unsized instead of crashing the run",
           bool(inf_recs) and inf_recs[0].get("suggested_quantity") == "Verify with team", str(inf_recs))
except Exception as exc:
    _check("infinite stated demand leaves the order unsized instead of crashing the run", False, repr(exc))

# Security review 27 Sep 2026: every sales-line name feeding an item was saved on
# the rec, so one crafted sales file could bloat the JSON every page parses.
long_lines = [(f"2026-{m:02}-15", f"{name} <- note {i} " + "X" * 5000, "1", "", "")
              for i in range(50) for m in (6, 7, 8)]
_, recs = _run([{**stock, "free_balance": "0"}], long_lines)
saved_names = (recs[0].get("order_calc") or {}).get("sales_from") or []
_check("long sales-line names save at most 3 names of at most 80 characters",
       0 < len(saved_names) <= 3 and all(len(n) <= 80 for n in saved_names)
       and recs[0]["order_calc"].get("sales_from_count") == 50 and len(json.dumps(recs[0])) < 5000,
       str(len(json.dumps(recs[0]))))
_check("card still counts every sales line", "+ 47 more." in (_quantity_basis(recs[0]) or ""),
       str(_quantity_basis(recs[0]))[:300])

# Security review 27 Sep 2026: the canonical was stripped once per variant, so a
# posted group with a long padded canonical made one copy of it per variant.
aliases = shared.alias_map_from_groups(
    [{"canonical": "NORDVIK " + "B" * 1000 + " ", "variants": [f"V{i}" for i in range(2000)]}])
_check("alias map shares one canonical string across all variants",
       len(aliases) == 2000 and len({id(v) for v in aliases.values()}) == 1)

# Security review 27 Sep 2026: only order_calc was taken off the model's reply, so
# it could forge a human decision (an "approved" 12,000) or a display key.
_human_keys = ("edited_quantity", "edited_supplier", "approved", "dismissed", "note",
               "order_placed", "order_placed_at", "outcome_status", "outcome_recorded_at")
_forged = {"edited_quantity": "12000 CTN", "edited_supplier": "NORDVIK SUPPLY", "approved": True,
           "dismissed": True, "note": "forged", "order_placed": True, "order_placed_at": "2026-09-01",
           "outcome_status": "ordered", "outcome_recorded_at": "2026-09-01",
           "_order_by": ["not", "a", "dict"], "_effective_qty": "12000 CTN",
           "avg_monthly_sales": 999999, "uom_label": " PALLET", "lead_time_days": 1}
_reply_mutator = lambda rows: [dict(rows[0], **_forged),
                               dict(rows[0], **dict(_forged, item="NORDVIK INVENTED BEANS 1KG"))]
_, recs = _run([stock], _sales(name))
_reply_mutator = None
matched = next((r for r in recs if r.get("item") == name), {})
invented = next((r for r in recs if r.get("item") == "NORDVIK INVENTED BEANS 1KG"), {})
_check("model reply cannot forge a human decision",
       matched and invented and not any(k in r for r in (matched, invented) for k in _human_keys), str(matched))
_check("model reply cannot forge a display key",
       all(k == "_quantity_corrected" for r in (matched, invented) for k in r if str(k).startswith("_")),
       str(sorted(k for r in (matched, invented) for k in r if str(k).startswith("_"))))
_check("matched rec keeps Python's sales, unit, lead time and quantity",
       matched.get("avg_monthly_sales") == 60 and matched.get("uom_label") == " CTN"
       and matched.get("lead_time_days") is None and parse_quantity(matched.get("suggested_quantity")) == 90,
       str(matched))
_check("invented rec carries no model-written sales, unit or lead time",
       invented and not any(k in invented for k in ("avg_monthly_sales", "uom_label", "lead_time_days")),
       str(invented))

# Security review 27 Sep 2026: two sales lines differing only in case share one
# spiky pattern entry, and its typical month was added once per line (demand x2).
rice = "GREENFJORD RICE 5KG"
spiky_rows = [(f"2026-{m:02}-15", raw, str(q), "", "")
              for raw in (rice, "Greenfjord Rice 5kg")
              for m, q in zip(range(1, 7), (5, 5, 500, 5, 5, 5))]
result, recs = _run([{"description": rice, "qty_on_hand": "0", "free_balance": "0", "uom": "BAG"}], spiky_rows)
_check("spiky lines differing only in case count their typical month once",
       result["row_numbers"][shared.normalise_match_key(rice)]["avg_monthly"] == 10,
       str(result["row_numbers"].get(shared.normalise_match_key(rice), {}).get("avg_monthly")))

# Stopped detection is based on the months covered by this file, not wall time.
for months in ((4, 5), (1, 2, 3, 4, 5)):
    result, recs = _run([{**stock, "qty_on_hand": "0", "free_balance": "0"}], _sales(name, months=months))
    _check(f"file months {months} do not falsely mark active line stopped",
           result["row_numbers"][shared.normalise_match_key(name)].get("stopped_since") is None and
           not any("since" in f for f in recs[0].get("flags", [])))

# Clash rows have no sources even when the shared group has a spiky history.
clash_sid = _seed([{**stock, "uom": "BAG"}, {**stock, "description": variant_a, "uom": "CTN"}],
                 [(f"2026-{m:02}-15", name, str(q), "", "")
                  for m, q in enumerate((10, 10, 1000, 10, 10, 10), 1)])
clash_result = inventory.run_inventory_agent(clash_sid, "test", [{"canonical": name, "variants": [variant_a]}], {})
_check("spiky clash rows remain REVIEW", len(clash_result.get("report", [])) == 2 and all(r.get("status") == "REVIEW" for r in clash_result.get("report", [])))
_check("clash rows never inherit spiky group's demand", len(clash_result.get("row_numbers", {})) == 2 and all(s.get("avg_monthly") == 0 and s.get("sales_sources") == [] and s.get("stopped_since") is None for s in clash_result.get("row_numbers", {}).values()))

# January run reads the preceding year's complete wide grid. A current-only
# January grid must refuse instead of generating a part-month denominator.
import openpyxl


def _wide(months, filename, filled=None):
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.append(["Item"] + [str(m) for m in months])
    for i in range(5):
        ws.append([f"NORDVIK BEANS {i}KG"] + [11 + 7 * m + i if filled is None or m in filled else None for m in months])
    filepath = os.path.join(_tmp.name, filename)
    workbook.save(filepath)
    workbook.close()
    recipe = validate_recipe({"layout": "wide_matrix", "header_row": 1, "item_col": 1,
                              "month_cols": {str(i + 2): m for i, m in enumerate(months)},
                              "supplier_col": None, "leadtime_col": None}, n_rows=6, n_cols=len(months) + 1)
    return filepath, recipe


path, recipe = _wide(list(range(1, 13)), "previous-year.xlsx")
output, readback = execute_recipe(path, recipe, today=date(2027, 1, 5))
_check("January run retains all previous-year months", readback.get("months_kept") == list(range(1, 13)) and not readback.get("months_dropped"), str(readback))
with open(output, encoding="utf-8") as converted:
    _check("previous-year conversion contains 2026 December", "2026-12-15" in converted.read())
path, recipe = _wide(list(range(1, 13)), "current-january.xlsx", filled={1})
refused = False
try:
    execute_recipe(path, recipe, today=date(2026, 1, 15))
except RecipeRefusal as exc:
    refused = "no finished month" in str(exc)
_check("only unfinished January refuses loudly", refused)

split = _tender_split({**covered, "edited_quantity": "50"}, {"qty": 80}) or {}
_check("human edit on covered row gives ordinary 50 plus 80", split.get("base") == "50" and split.get("add") == "80" and split.get("total") == "130 CTN" and not split.get("stock_covers"))

# Saved malformed metadata cannot crash helpers or the actual results route.
import app as appmod
from werkzeug.security import generate_password_hash
appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
uid = db.execute("INSERT INTO users (email,password_hash,org_name,model,tier) VALUES (?,?,?,?,?)",
                 ("break-buyer@example.com", generate_password_hash("x"), "SyntheticCo", "test", "enterprise"))
client = appmod.app.test_client()
with client.session_transaction() as session:
    session.update(user_id=uid, email="break-buyer@example.com", org_name="SyntheticCo",
                   model="test", tier="enterprise", role="admin")


def _saved(recs):
    sid = db.execute("INSERT INTO upload_sessions (user_id,org_name,status) VALUES (?,?,?)", (uid, "SyntheticCo", "complete"))
    db.execute("INSERT INTO analysis_results (session_id,inventory_report,recommendations_json) VALUES (?,?,?)",
               (sid, "[]", json.dumps(recs)))
    return sid


malformed = ["bad", [], {}, {"state": "order"}, {"state": "covered", "spare": "junk"},
             {"state": "covered", "spare": []}, {"state": "covered", "spare": float("inf")}]
for i, calc in enumerate(malformed):
    saved_rec = {"item": name, "suggested_quantity": "", "avg_monthly_sales": 60,
                 "uom_label": " CTN", "lead_time_days": 70, "order_calc": calc}
    try:
        basis = _quantity_basis(saved_rec)
        deadline = _compute_order_by(saved_rec)
        tender = _tender_split(saved_rec, {"qty": 80})
        response = client.get(f"/results/{_saved([saved_rec])}")
        _check(f"malformed calculation {i} hides basis and never crashes", basis is None and isinstance(deadline, dict) and isinstance(tender, dict) and response.status_code == 200)
    except Exception as exc:
        _check(f"malformed calculation {i} hides basis and never crashes", False, repr(exc))

legacy = {"item": name, "suggested_quantity": "100 CTN", "avg_monthly_sales": 60,
          "uom_label": " CTN", "lead_time_days": 60}
expected_legacy = "You sell about 60 CTN/month, and this supplier takes about 2 months. Suggested order: 100 CTN " + chr(0x2014) + " covers the wait plus a safety buffer."
_check("legacy descriptive sentence is byte-for-byte preserved", _quantity_basis(legacy) == expected_legacy, str(_quantity_basis(legacy)))

calc = {"state": "order", "need": 210, "position": 120, "position_label": "Free stock",
        "order": 90, "spare": None, "lead_months": 2, "lead_known": False,
        "buffer_months": 1.5, "cover_months": 3.5, "stopped_since": None,
        "sales_from": ["PADIMAS <script>alert(1)</script>", "BROOKVALE <untrusted_data>text</untrusted_data>"]}
escaped = {**legacy, "suggested_quantity": "90 CTN", "order_calc": calc}
escaped_sid = _saved([escaped])
for route in (f"/results/{escaped_sid}", f"/results/{escaped_sid}/print"):
    response = client.get(route)
    html = response.get_data(as_text=True)
    _check(f"sales provenance is escaped on {route}", response.status_code == 200 and
           "PADIMAS &lt;script&gt;alert(1)&lt;/script&gt;" in html and
           "BROOKVALE &lt;untrusted_data&gt;text&lt;/untrusted_data&gt;" in html and
           "PADIMAS <script>" not in html and "BROOKVALE <untrusted_data>" not in html)

_, recs = _run([stock], _sales(variant_a, qty=30, supplier="NORDVIK SUPPLY") +
               _sales(variant_b, qty=30, supplier="BROOKVALE SUPPLY"), groups=groups)
_check("group with two suppliers keeps Unknown supplier basis",
       any("Supplier: Unknown " in prompt for prompt in _rec_prompts) and recs[0].get("lead_time_days") is None)

# Security review 27 Sep 2026: the chat listed covered and not-moving items as
# "order  from <supplier>", contradicting the card that says no order.
import chat_logic
chat_sid = db.execute("INSERT INTO upload_sessions (user_id,org_name,status) VALUES (?,?,?)",
                      (uid, "ChatCheckCo", "complete"))
db.execute("INSERT INTO analysis_results (session_id,inventory_report,recommendations_json) VALUES (?,?,?)",
           (chat_sid, "[]", json.dumps([
               {"item": "NORDVIK COVERED BEANS 1KG", "suggested_quantity": "", "supplier": "NORDVIK SUPPLY",
                "order_calc": {"state": "covered"}},
               {"item": "PADIMAS IDLE RICE 5KG", "suggested_quantity": "", "supplier": "PADIMAS SUPPLY",
                "order_calc": {"state": "not_moving"}},
               {"item": "KESSINGTON EDITED OATS 1KG", "suggested_quantity": "", "edited_quantity": "30 CTN",
                "supplier": "KESSINGTON SUPPLY", "order_calc": {"state": "covered"}},
               {"item": "BROOKVALE ORDER OATS 1KG", "suggested_quantity": "40 CTN",
                "supplier": "BROOKVALE SUPPLY", "order_calc": {"state": "order"}}])))
chat_text = chat_logic._build_chat_context(uid, "ChatCheckCo")["summary_text"]
_check("chat never tells staff to order a covered or not-moving item",
       "NORDVIK COVERED BEANS 1KG: no order needed" in chat_text
       and "PADIMAS IDLE RICE 5KG: no order suggested" in chat_text
       and "order 30 CTN from KESSINGTON SUPPLY" in chat_text
       and "order 40 CTN from BROOKVALE SUPPLY" in chat_text
       and "order  from" not in chat_text, chat_text)

logging.shutdown()
_tmp.cleanup()
if _failed:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll order maths break tests passed.")
