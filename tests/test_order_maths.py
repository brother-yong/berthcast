"""Orders use one verified demand figure and subtract stock before printing.

Only model calls are canned. Real inventory, recommendation, ingest and Flask
paths run with synthetic products and a temporary database.
"""
import csv
import inspect
import json
import logging
import os
import re
import sys
import tempfile
import types
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.TemporaryDirectory(prefix="berthcast_order_maths_", ignore_cleanup_errors=True)
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
from agents.orchestrator import run_pipeline
from quantity import parse_quantity
from rec_logic import _quantity_basis, _tender_split, _compute_order_by
from ingest_recipe import execute_recipe, validate_recipe

_failed = False
_inv_prompts, _rec_prompts = [], []
_reply_qty = None


def _check(name, cond, detail=""):
    global _failed
    print(("ok: " if cond else "FAIL: ") + name + (f" [{detail}]" if not cond else ""))
    _failed |= not bool(cond)


def _fake_inventory(model, system, user, **kwargs):
    _inv_prompts.append(user)
    rows = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            name = line.split(" | ")[0][6:]
            rows.append({"item": name, "stock": 0, "category": "GENERAL",
                         "status": "CRITICAL", "days_of_supply": 0,
                         "spoilage_risk": "NONE", "observation": "test"})
    return json.dumps(rows)


def _fake_recommendation(model, system, user, **kwargs):
    _rec_prompts.append(user)
    rows = []
    for block in user.split("---"):
        name = qty = None
        for line in block.splitlines():
            if line.startswith("Item: "):
                name = line[6:].strip()
            elif line.startswith("Pre-computed suggested order quantity: "):
                qty = line.split(": ", 1)[1]
        if name:
            value = _reply_qty(qty) if callable(_reply_qty) else (_reply_qty or qty)
            rows.append({"item": name, "suggested_quantity": value,
                         "recommended_action": "REORDER", "supplier": "Unknown",
                         "supplier_risk": "NONE", "confidence": "HIGH", "flags": []})
    return json.dumps(rows)


shared._call_claude = lambda *a, **k: "{}"
shared.sg_today = lambda: date(2026, 9, 26)
inv._call_claude = _fake_inventory
rec._call_claude = _fake_recommendation
db.init_db()
_sid = 17200


def _seed(stock, sales, cols=None):
    global _sid
    _sid += 1
    cols = cols or ["description", "qty_on_hand", "qty_on_order",
                    "qty_on_order_allocated", "ar_inv_back_order", "free_balance", "uom"]
    db.execute("INSERT INTO upload_sessions (id,user_id,org_name,status,scope,context_json) "
               "VALUES (?,?,?,?,?,?)", (_sid, 1, "SyntheticCo", "complete", "all", "{}"))
    db.execute(f'CREATE TABLE inventory_{_sid} (' + ",".join(f'"{c}" TEXT' for c in cols) + ')')
    db.execute(f'CREATE TABLE sales_{_sid} (date TEXT,item_description TEXT,qty_sold TEXT,supplier TEXT,lead_time_days TEXT)')
    for row in stock:
        db.execute(f'INSERT INTO inventory_{_sid} VALUES (' + ",".join("?" for _ in cols) + ')',
                   tuple(row.get(c, "") for c in cols))
    for row in sales:
        db.execute(f'INSERT INTO sales_{_sid} VALUES (?,?,?,?,?)', row)
    return _sid


def _sales(name, qty=60, supplier="", lt="", months=(6, 7, 8)):
    return [(f"2026-{m:02}-15", name, str(qty), supplier, str(lt)) for m in months]


def _run(stock, sales, cols=None, groups=None):
    sid = _seed(stock, sales, cols)
    _inv_prompts.clear()
    _rec_prompts.clear()
    result = inv.run_inventory_agent(sid, "test", groups or [], {})
    _check(f"inventory {sid} succeeds", "error" not in result, str(result))
    # Keep these tests about order sizing even when plenty of physical stock
    # would correctly make the inventory verifier label the item HEALTHY.
    report = [dict(r, status="LOW") for r in result.get("report", [])]
    kwargs = {}
    if "row_numbers" in inspect.signature(rec.run_recommendation_agent).parameters:
        kwargs = {"row_numbers": result.get("row_numbers"), "confirmed_groups": groups or []}
    recs = rec.run_recommendation_agent(sid, "test", report, {}, **kwargs)
    _check(f"recommendation {sid} succeeds", bool(recs) and "error" not in recs[0], str(recs))
    return result, recs[0] if recs else {}, sid


name = "KESSINGTON PASTA FUSILLI 500G"
row = {"description": name, "qty_on_hand": "100", "qty_on_order": "50",
       "qty_on_order_allocated": "999", "ar_inv_back_order": "30", "free_balance": "120", "uom": "CTN"}
result, first, sid = _run([row], _sales(name))
stamp = result.get("row_numbers", {}).get(shared.normalise_match_key(name), {})
_check("free balance takes precedence, including allocated decoy", stamp.get("position") == 120)
_check("free balance label", stamp.get("position_label") == "Free stock")
_check("order need 210 minus free stock 120 is 90", parse_quantity(first.get("suggested_quantity")) == 90, str(first))
_check("exact sales name has no provenance suffix", first.get("order_calc", {}).get("sales_from") is None)

cols = [c for c in row if c != "free_balance"]
result, fallback, _ = _run([row], _sales(name), cols)
stamp = result.get("row_numbers", {}).get(shared.normalise_match_key(name), {})
_check("fallback on hand plus on order less back orders", stamp.get("position") == 120)
_check("fallback quantity ignores allocated stock", parse_quantity(fallback.get("suggested_quantity")) == 90)
result, onhand, _ = _run([row], _sales(name), ["description", "qty_on_hand", "uom"])
stamp = result.get("row_numbers", {}).get(shared.normalise_match_key(name), {})
_check("on-hand-only position and label", stamp.get("position") == 100 and stamp.get("position_label") == "On hand")
_check("on-hand-only order is 110", parse_quantity(onhand.get("suggested_quantity")) == 110)

result, merged, _ = _run([
    {**row, "qty_on_hand": "40", "qty_on_order": "20", "ar_inv_back_order": "10", "free_balance": "50"},
    {**row, "description": "Kessington Pasta-Fusilli 500g", "qty_on_hand": "60",
     "qty_on_order": "30", "ar_inv_back_order": "20", "free_balance": "70"},
], _sales(name))
stamp = result.get("row_numbers", {}).get(shared.normalise_match_key(name), {})
_check("merged duplicate rows sum free balance", stamp.get("position") == 120 and parse_quantity(merged.get("suggested_quantity")) == 90)
result, merged_fallback, _ = _run([
    {**row, "qty_on_hand": "40", "qty_on_order": "20", "ar_inv_back_order": "10"},
    {**row, "description": "Kessington Pasta-Fusilli 500g", "qty_on_hand": "60",
     "qty_on_order": "30", "ar_inv_back_order": "20"},
], _sales(name), cols)
stamp = result.get("row_numbers", {}).get(shared.normalise_match_key(name), {})
_check("merged rows sum on-order and back-order columns", stamp.get("position") == 120 and parse_quantity(merged_fallback.get("suggested_quantity")) == 90)

result, negative, _ = _run([{**row, "qty_on_hand": "0", "free_balance": "-40"}], _sales(name))
stamp = result.get("row_numbers", {}).get(shared.normalise_match_key(name), {})
_check("negative free stock kept", stamp.get("position") == -40)
_check("negative position orders 250", parse_quantity(negative.get("suggested_quantity")) == 250)
_check("inventory health still sees genuine zero stock", any("Stock: 0 |" in p for p in _inv_prompts))

variant = "KESSINGTON/PADIMAS FUSILLI 500GM"
groups = [{"canonical": name, "variants": [variant, "PADIMAS FUSILLI 500G"]}]
result, lt_rec, _ = _run([{**row, "qty_on_hand": "0", "free_balance": "0"}],
                        _sales(variant, 30, "BROOKVALE SUPPLY", 70) +
                        _sales("PADIMAS FUSILLI 500G", 30, "BROOKVALE SUPPLY", 105), groups=groups)
stamp = result.get("row_numbers", {}).get(shared.normalise_match_key(name), {})
_check("sheet lead time is max 105, never sum 175", stamp.get("lead_time_days") == 105)
_check("saved lead time matches sizing", lt_rec.get("lead_time_days") == 105)
_check("prompt states same lead time", any("lead time: 105" in p for p in _rec_prompts))
_check("deadline uses saved lead time", _compute_order_by(dict(lt_rec, days_of_supply=110), as_of="2026-09-26").get("buffer_days") == 5)

no_lt_sid = _seed([{**row, "qty_on_hand": "0", "free_balance": "0"}], _sales(name, supplier="BROOKVALE SUPPLY"))
db.execute(f'ALTER TABLE sales_{no_lt_sid} DROP COLUMN lead_time_days')
no_lt_result = inv.run_inventory_agent(no_lt_sid, "test", [], {})
kwargs = {"row_numbers": no_lt_result.get("row_numbers")} if "row_numbers" in inspect.signature(rec.run_recommendation_agent).parameters else {}
no_lt_recs = rec.run_recommendation_agent(no_lt_sid, "test", no_lt_result.get("report", []), {}, **kwargs)
_check("missing lead column retains named-supplier fallback 56 days", bool(no_lt_recs) and no_lt_recs[0].get("lead_time_days") == 56)

sid = _seed([{**row, "qty_on_hand": "0", "free_balance": "0"}], _sales(variant))
pipeline = run_pipeline(sid, "test", [{"canonical": name, "variants": [variant]}], {})
linked = (pipeline.get("recommendations") or [{}])[0]
_check("pipeline passes stamp and yields numeric grouped order", parse_quantity(linked.get("suggested_quantity")) == 210, str(linked))
_check("grouped sales provenance retained", linked.get("order_calc", {}).get("sales_from") == [variant])
_check("card states grouped sales source", "Sales figure from: " + variant in (_quantity_basis(linked) or ""))

_reply_qty = "12,000 CTN"
result, covered, _ = _run([{**row, "free_balance": "500"}], _sales(name))
_check("covered stays listed but saves empty quantity", covered.get("suggested_quantity") == "" and parse_quantity(covered.get("suggested_quantity")) is None)
_check("covered state and Python flag", covered.get("order_calc", {}).get("state") == "covered" and (covered.get("flags") or [""])[0].startswith("Covered:"))
_check("covered has no ordering deadline", _compute_order_by(covered).get("status") == "unknown")
_reply_qty = None
covered_tender = {"suggested_quantity": "", "uom_label": " CTN", "order_calc": {"state": "covered", "spare": 50}}
split = _tender_split(covered_tender, {"qty": 80}) or {}
_check("covered tender consumes spare then orders 30", split.get("add") == "80" and split.get("stock_covers") == "50" and split.get("total") == "30 CTN")
split = _tender_split({**covered_tender, "order_calc": {"state": "covered", "spare": 200}}, {"qty": 80}) or {}
_check("spare covers entire tender", split.get("total") == "" and split.get("stock_covers") == "80")
_check("human zero still suppresses tender", _tender_split({**covered_tender, "edited_quantity": "0"}, {"qty": 80}) is None)

import openpyxl
for borderline in (True, False):
    workbook = openpyxl.Workbook()
    ws = workbook.active
    month_count = 12 if borderline else 9
    ws.append(["Item"] + [str(m) for m in range(1, month_count + 1)])
    # Most September values are copied forward, but one differs. This
    # triggers the existing borderline rescue before upload-month filtering.
    for i in range(5):
        vals = [10 + i + m for m in range(1, 9)]
        vals += [30 + i if (not borderline or i == 0) else 20 + i]
        if borderline:
            vals += [20 + i, 20 + i, 20 + i]
        ws.append([f"NORDVIK BEANS {i}KG"] + vals)
    path = os.path.join(_tmp.name, f"months-{borderline}.xlsx")
    workbook.save(path)
    recipe = validate_recipe({"layout": "wide_matrix", "header_row": 1, "item_col": 1,
                              "month_cols": {str(m + 1): m for m in range(1, month_count + 1)},
                              "supplier_col": None, "leadtime_col": None}, n_rows=6, n_cols=month_count + 1)
    output, readback = execute_recipe(path, recipe, today=date(2026, 9, 26))
    _check(f"upload month excluded ({borderline})", readback.get("months_kept") == list(range(1, 9)), str(readback))
    _check(f"unfinished month leaves no question ({borderline})", not readback.get("question") and readback.get("tier") == "confident")
    with open(output, encoding="utf-8") as handle:
        _check(f"no September rows ({borderline})", "2026-09-15" not in handle.read())
    if borderline:
        _check("all unfinished/future months recorded", readback.get("months_dropped") == [9, 10, 11, 12], str(readback))

stopped_a, stopped_b, active = "GREENFJORD CHEDDAR 1KG", "NORDVIK GHEE 1KG", "BROOKVALE BEANS 1KG"
sales = _sales(stopped_a, 60, months=range(1, 6)) + _sales(stopped_b, 60, months=range(1, 6)) + _sales(active, 60, months=range(1, 9))
stock = [{"description": stopped_a, "qty_on_hand": "0", "free_balance": "0", "uom": "CTN"},
         {"description": stopped_b, "qty_on_hand": "20", "free_balance": "20", "uom": "CTN"},
         {"description": active, "qty_on_hand": "0", "free_balance": "0", "uom": "CTN"}]
sid = _seed(stock, sales)
res = inv.run_inventory_agent(sid, "test", [], {})
kwargs = {"row_numbers": res.get("row_numbers")} if "row_numbers" in inspect.signature(rec.run_recommendation_agent).parameters else {}
recs = rec.run_recommendation_agent(sid, "test", [dict(r, status="LOW") for r in res.get("report", [])], {}, **kwargs)
by_name = {r.get("item"): r for r in recs}
_check("stopped empty-stock line orders with question", parse_quantity(by_name.get(stopped_a, {}).get("suggested_quantity")) is not None and any("No sales since May 2026" in f for f in by_name.get(stopped_a, {}).get("flags", [])))
_check("stopped held-stock line has no order", by_name.get(stopped_b, {}).get("order_calc", {}).get("state") == "not_moving" and by_name.get(stopped_b, {}).get("suggested_quantity") == "")
_check("active item has no stopped question", not any("since" in f for f in by_name.get(active, {}).get("flags", [])))

_reply_qty = lambda qty: str(int(parse_quantity(qty) or 0) * 2) + " CTN"
_, corrected, _ = _run([row], _sales(name))
_reply_qty = None
_check("model doubling ignored even within old allowed range", parse_quantity(corrected.get("suggested_quantity")) == 90 and corrected.get("_quantity_corrected") is True)
sentence = _quantity_basis(corrected) or ""
_check("printed arithmetic is exact", "Need about 210 CTN" in sentence and "Free stock 120 CTN" in sentence and "Order 90 CTN" in sentence, sentence)
_check("new basis has no em dash", "\u2014" not in sentence)

spiky, normal = "PADIMAS PASTA SPIKE 500G", "PADIMAS PASTA STEADY 500G"
sales = [(f"2026-{m:02}-15", spiky, str(q), "", "") for m, q in enumerate([10, 10, 1000, 10, 10, 10], 1)] + _sales(normal, 20, months=range(1, 7))
result, group_rec, _ = _run([{**row, "qty_on_hand": "30", "free_balance": "30"}], sales,
                          groups=[{"canonical": name, "variants": [spiky, normal]}])
stamp = result.get("row_numbers", {}).get(shared.normalise_match_key(name), {})
_check("per-line spiky median plus steady mean is 30", stamp.get("avg_monthly") == 30, str(stamp))
_check("inventory uses same monthly demand", any("Months of supply: 1.0" in p for p in _inv_prompts))

# Saved analyses exercise display glue without running a real model.
import app as appmod
from werkzeug.security import generate_password_hash
appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
uid = db.execute("INSERT INTO users (email,password_hash,org_name,model,tier) VALUES (?,?,?,?,?)",
                 ("math-buyer@example.com", generate_password_hash("x"), "SyntheticCo", "test", "enterprise"))
client = appmod.app.test_client()
with client.session_transaction() as session:
    session.update(user_id=uid, email="math-buyer@example.com", org_name="SyntheticCo", model="test", tier="enterprise", role="admin")
saved = db.execute("INSERT INTO upload_sessions (user_id,org_name,status) VALUES (?,?,?)", (uid, "SyntheticCo", "complete"))
db.execute("INSERT INTO analysis_results (session_id,inventory_report,recommendations_json) VALUES (?,?,?)",
           (saved, json.dumps([{"item": name, "stock": 100, "status": "LOW"}]), json.dumps([covered, linked])))
html = client.get(f"/results/{saved}/print").get_data(as_text=True)
_check("print clarifies stock subtraction", "stock already taken off" in html)
_check("print shows covered and source calculation", "No order (covered)" in html and "Need about" in html and "Sales figure from" in html)
page = client.get(f"/results/{saved}").get_data(as_text=True)
_check("summary excludes covered row", "<strong>1</strong> item to order." in page, "summary HTML")
legacy_sid = db.execute("INSERT INTO upload_sessions (user_id,org_name,status) VALUES (?,?,?)", (uid, "SyntheticCo", "complete"))
legacy = {"item": name, "suggested_quantity": "210 CTN", "avg_monthly_sales": 60, "uom_label": " CTN"}
db.execute("INSERT INTO analysis_results (session_id,inventory_report,recommendations_json) VALUES (?,?,?)", (legacy_sid, "[]", json.dumps([legacy])))
html = client.get(f"/results/{legacy_sid}/print").get_data(as_text=True)
_check("legacy print has no new calculation", "stock already taken off" not in html and "You sell about" not in html)

logging.shutdown()
_tmp.cleanup()
if _failed:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll order maths feature tests passed.")
