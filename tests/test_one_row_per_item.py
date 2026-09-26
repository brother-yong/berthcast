"""Regression: canonical rows must not split from aliases or share sales across units.

The real inventory and recommendation pipeline runs against a temporary DB.
Only Claude calls are canned; expected stock and sales totals are hand-calculated.
"""
import json
import logging
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_temp = tempfile.TemporaryDirectory(prefix="berthcast_one_row_")
os.environ["DB_PATH"] = os.path.join(_temp.name, "test.db")
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

_failed = False
_prompts = []
_progress = []


def _check(name, cond, detail=""):
    global _failed
    print(("ok: " if cond else "FAIL: ") + name + (f" [{detail}]" if not cond else ""))
    _failed |= not bool(cond)


def _fake_inventory(model, system, user, **kwargs):
    _prompts.append(user)
    rows = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            fields = dict(part.strip().split(": ", 1) for part in line.split(" | "))
            rows.append({"item": fields["Item"], "stock": float(fields["Stock"]),
                         "category": "GENERAL", "status": "CRITICAL",
                         "days_of_supply": 0, "spoilage_risk": "HIGH", "observation": "test"})
    return json.dumps(rows)


def _fake_recommendation(model, system, user, **kwargs):
    rows = []
    for block in user.split("---"):
        name = qty = None
        for line in block.splitlines():
            if line.startswith("Item: "):
                name = line[6:].strip()
            elif line.startswith("Pre-computed suggested order quantity: "):
                qty = line.split(": ", 1)[1].strip()
        if name:
            rows.append({"item": name, "suggested_quantity": qty,
                         "recommended_action": "REORDER", "supplier": "Unknown",
                         "supplier_risk": "NONE", "confidence": "HIGH", "flags": []})
    return json.dumps(rows)


shared._call_claude = lambda *a, **k: "{}"
inv._call_claude = _fake_inventory
rec._call_claude = _fake_recommendation
db.init_db()


def _run(sid, stock, sales, groups=None):
    db.execute("INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
               "VALUES (?,?,?,?,?,?)", (sid, 1, "SyntheticCo", "complete", "all", "{}"))
    db.execute(f'CREATE TABLE inventory_{sid} (description TEXT, qty_on_hand TEXT, uom TEXT)')
    db.execute(f'CREATE TABLE sales_{sid} (item_description TEXT, qty_sold TEXT, date TEXT)')
    for row in stock:
        db.execute(f"INSERT INTO inventory_{sid} VALUES (?,?,?)", row)
    for name, monthly in sales:
        for month in (6, 7, 8):
            db.execute(f"INSERT INTO sales_{sid} VALUES (?,?,?)", (name, str(monthly), f"2026-{month:02}-15"))
    _prompts.clear()
    _progress.clear()
    result = run_pipeline(sid, "test-model", groups or [], {}, emit=_progress.append)
    _check(f"session {sid} pipeline succeeds", "error" not in result, str(result))
    lines = [line for prompt in _prompts for line in prompt.splitlines() if line.startswith("Item: ")]
    return result, lines


canonical = "BROOKVALE OYSTER SAUCE 12X510G"
groups = [{"canonical": canonical,
           "variants": ["BROOKVALE OYSTR SCE 510G", "BRKVL OYSTER 510G"]}]
result, lines = _run(1701, [(canonical, "0", "BTL"), ("BROOKVALE OYSTR SCE 510G", "100", "BTL"),
                          ("NORDVIK SOY SAUCE 640ML", "50", "BTL")],
                     [(canonical, 100), ("BRKVL OYSTER 510G", 50)], groups)
oyster = [line for line in lines if line.startswith(f"Item: {canonical} |")]
_check("canonical and variant produce one prompt row", len(oyster) == 1, str(oyster))
_check("combined stock is 100", len(oyster) == 1 and "Stock: 100 |" in oyster[0], str(oyster))
_check("combined three-month sales are 450", len(oyster) == 1 and "Total sold (3mo): 450" in oyster[0])
report = [row for row in result.get("inventory_report", []) if row["item"] == canonical]
_check("canonical has one report row with stock 100", len(report) == 1 and report[0]["stock"] == 100, str(report))
_check("no zero-stock duplicate survives", not any(row["stock"] == 0 for row in report))
_check("pipeline creates exactly one oyster recommendation",
       sum(row.get("item") == canonical for row in result.get("recommendations", [])) == 1)

result, lines = _run(1702, [("PADIMAS RICE 5KG", "10", "BAG"), ("Padimas Rice-5kg", "15", "BAG")], [])
_check("case and punctuation duplicates merge without groups", len(lines) == 1 and "Stock: 25 |" in lines[0], str(lines))

rice = "PADIMAS JASMINE RICE 5KG"
carton = "PADIMAS JASMINE RICE 5KG X4"
sales_name = "PADIMAS JAS RICE 5KG"
result, lines = _run(1703, [(rice, "40", "BAG"), (carton, "10", "CTN"), ("PADIMAS JASMINE", "500", "BAG")],
                     [(sales_name, 100)], [{"canonical": rice, "variants": [carton, sales_name]}])
clash = [line for line in lines if line.startswith(f"Item: {rice}")]
_check("unit clash displays each own name with its unit",
       len(clash) == 2 and any(line.startswith(f"Item: {rice} (BAG) |") for line in clash)
       and any(line.startswith(f"Item: {carton} (CTN) |") for line in clash), str(clash))
_check("neither clashing unit gets group sales", len(clash) == 2 and all("no sales data in upload" in line for line in clash))
rows = [row for row in result.get("inventory_report", []) if row["item"].startswith(rice)]
_check("both clashing units forced to REVIEW", len(rows) == 2 and all(row["status"] == "REVIEW" for row in rows), str(rows))
_check("unit clash observation names the problem and units", len(rows) == 2 and all(
       "grouped items use different pack units" in row["observation"].lower() and "BAG vs CTN" in row["observation"] for row in rows))
_check("unit clash produces no recommendations", result.get("recommendations") == [], str(result.get("recommendations")))
_check("progress reports different pack units", any("different pack units" in line for line in _progress))
decoy = [line for line in lines if line.startswith("Item: PADIMAS JASMINE |")]
_check("prefix decoy cannot steal the parked group sales", len(decoy) == 1 and "no sales data in upload" in decoy[0], str(decoy))

result, lines = _run(1704, [("NORDVIK BUTTER 250G", "10", "KG"), ("NORDVIK BUTTER 250G", "5", "CTN")],
                     [("NORDVIK BUTTER 250G", 20)])
_check("same-name different units get separate display names", len(lines) == 2
       and any("NORDVIK BUTTER 250G (KG) |" in line for line in lines)
       and any("NORDVIK BUTTER 250G (CTN) |" in line for line in lines), str(lines))
_check("same-name unit clash has no sales on either row", len(lines) == 2 and all("no sales data in upload" in line for line in lines))
_check("same-name unit clash is REVIEW with no orders", len(result.get("inventory_report", [])) == 2
       and all(row["status"] == "REVIEW" for row in result["inventory_report"])
       and result.get("recommendations") == [])

logging.shutdown()
_temp.cleanup()
if _failed:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll one-row-per-item tests passed.")
