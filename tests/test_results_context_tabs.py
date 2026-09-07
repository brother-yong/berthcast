"""The expiry and tender tabs on /results (Sep 2026).

Two org-level panels were added beside the run's own output, because staff
were reading the recommendations and asking "where is my expiry date and my
tender quantity". They are shown BESIDE the recommendations and never joined
to them: the tender sheets' item names do not reliably match the inventory
upload, and a wrong join moves stock already promised to a customer.

What this guards:
  1. no tender sheet, or none currently running -> no tab at all, never an
     empty one that reads as "you have no tenders"
  2. only LIVE commitments show, soonest to finish first
  3. one org's commitments never reach another org's results page
  4. the display cap holds, and the page says it is capped
  5. no expiry snapshot -> no expiry tab, and the page still renders
  6. a block that raises degrades to "no tab", never to a 500

Throwaway temp DB, stubbed anthropic client, no API calls.
Run: python tests/test_results_context_tabs.py
"""
import json
import os
import sys
import tempfile
import types
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_results_context_tabs.db")
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

import database as db                                   # noqa: E402
import tenders                                          # noqa: E402
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


TODAY = date.today()


def _iso(days):
    return (TODAY + timedelta(days=days)).isoformat()


def _make_org(email, org):
    db.execute(
        "INSERT INTO users (email, password_hash, org_name, model, tier, "
        "email_verified, role) VALUES (?,?,?,?,?,?,?)",
        (email, generate_password_hash("x"), org, "claude-sonnet-5",
         "enterprise", 1, "admin"),
    )
    uid = db.query("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    sid = db.execute(
        "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
        (uid, org, "complete"),
    )
    inv = [{"item": "BROOKVALE UHT MILK 1L", "status": "LOW", "spoilage_risk": "NONE",
            "days_of_supply": 9, "category": "DRY", "stock": "6 CTN",
            "observation": "low"}]
    recs = [{"item": "BROOKVALE UHT MILK 1L", "supplier": "Nordvik Foods",
             "supplier_type": "local", "lead_time_days": 14, "days_of_supply": 9,
             "recommended_action": "REORDER", "suggested_quantity": "60 CTN",
             "confidence": "HIGH", "supplier_risk": "None", "flags": [],
             "reason": "Stock low against steady sales."}]
    db.execute(
        "INSERT INTO analysis_results (session_id, inventory_report, "
        "recommendations_json) VALUES (?,?,?)",
        (sid, json.dumps(inv), json.dumps(recs)),
    )
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s["user_id"]  = uid
        s["email"]    = email
        s["org_name"] = org
        s["model"]    = "claude-sonnet-5"
        s["is_admin"] = False
        s["tier"]     = "enterprise"
        s["role"]     = "admin"
        s["sv"]       = 0
    return uid, sid, client


def _add_tenders(org, rows, customer="NORDVIK CATERING"):
    """rows = [(item, qty, start_offset_days, end_offset_days)]"""
    upload_id = db.create_tender_upload(org, "sheet.csv", "x@example.com",
                                        customer=customer)
    db.save_tender_rows(org, upload_id, [
        {"customer": customer, "item_name": item,
         "match_key": item.lower().replace(" ", ""),
         "quantity": qty, "qty_basis": tenders.BASIS_PERIOD_TOTAL,
         "period_start": _iso(start), "period_end": _iso(end)}
        for item, qty, start, end in rows
    ])
    return upload_id


ALPHA_UID, ALPHA_SID, alpha = _make_org("alpha@example.com", "TabsAlpha")
BRAVO_UID, BRAVO_SID, bravo = _make_org("bravo@example.com", "TabsBravo")


# ── 1. Nothing uploaded: no tabs, page still renders ─────────────────────────

r = alpha.get(f"/results/{ALPHA_SID}")
html = r.get_data(as_text=True)
_check("results page renders with no expiry and no tenders",
       r.status_code == 200, detail=str(r.status_code))
_check("no tender tab when the org has no sheet", "tab-tender" not in html)
_check("no expiry tab when the org has no snapshot", "tab-expiry" not in html)
_check("the recommendations still render", "BROOKVALE UHT MILK 1L" in html)

_check("tender block is None with no sheet",
       appmod._tender_results_block("TabsAlpha") is None)


# ── 2. An expired-only sheet still produces no tab ───────────────────────────
# An empty tab reads as "you have no tenders", which is a different and wrong
# statement from "none are running right now".

_add_tenders("TabsAlpha", [("PADIMAS JASMINE RICE 5KG", 900, -120, -30)])
_check("tender block is None when every commitment has ended",
       appmod._tender_results_block("TabsAlpha") is None)

html = alpha.get(f"/results/{ALPHA_SID}").get_data(as_text=True)
_check("no tender tab when every commitment has ended", "tab-tender" not in html)


# ── 3. Live commitments show, soonest to finish first ────────────────────────

_add_tenders("TabsAlpha", [
    ("VANMARK CANNED TUNA 150G", 480, -10, 90),    # live, ends later
    ("BROOKVALE UHT MILK 1L",    600, -10, 20),    # live, ends soonest
    ("KESTREL FROZEN CHICKEN 1KG", 240, 30, 200),  # not started yet
])

block = appmod._tender_results_block("TabsAlpha")
_check("tender block appears once something is running", block is not None)
_check("only live commitments counted", block["total"] == 2, detail=str(block["total"]))
_check("a future-dated commitment is not live",
       all("CHICKEN" not in row["item_name"] for row in block["rows"]))
_check("an ended commitment is not live",
       all("RICE" not in row["item_name"] for row in block["rows"]))
_check("soonest to finish is listed first",
       block["rows"][0]["item_name"] == "BROOKVALE UHT MILK 1L",
       detail=block["rows"][0]["item_name"])
_check("per-month figure is derived, not stored",
       block["rows"][0]["monthly_display"] != "")

html = alpha.get(f"/results/{ALPHA_SID}").get_data(as_text=True)
_check("tender tab renders", 'id="tab-tender"' in html)
_check("tender tab button renders", "Tender commitments" in html)
_check("live commitment shows on the page", "VANMARK CANNED TUNA 150G" in html)
_check("ended commitment stays off the page", "PADIMAS JASMINE RICE 5KG" not in html)
_check("the page says an undecided row adds nothing",
       "Rows you have not" in html and "decided on yet add nothing." in html)


# ── 3b. The expiry tab renders once a snapshot exists ────────────────────────
# The half she actually asked about. A lot received close to its expiry is
# short-life (flagged at 28 days); one received far ahead of it is long-life
# (flagged at 210), so the receipt dates below decide which rows appear.

def _add_lots(org, lots):
    """lots = [(item, lot_no, expiry_offset, received_offset, qty)]"""
    upload_id = db.create_expiry_upload(org, "lots.csv", "x@example.com")
    db.save_expiry_lots(org, upload_id, [
        {"item_code": "X", "item_name": item,
         "match_key": item.lower().replace(" ", ""), "lot_no": lot,
         "uom": "CTN", "expiry_date": _iso(exp), "qty_on_hand": qty,
         "qty_available": qty, "received_date": _iso(recv), "category": None}
        for item, lot, exp, recv, qty in lots
    ])
    db.finalise_expiry_upload(org, upload_id, len(lots), 0, 0, "[]")
    return upload_id


_add_lots("TabsAlpha", [
    ("BROOKVALE CHILLED BUTTER 250G", "LOT-A", -6, -20, 25),   # already expired
    ("BROOKVALE FRESH YOGHURT 500G",  "LOT-B",  8, -20, 15),   # short life, due
    ("PADIMAS BASMATI RICE 5KG",      "LOT-C", 180, -400, 200),  # long life, due
    ("KESTREL FROZEN CHICKEN 1KG",    "LOT-D", 900, -400, 70),   # long life, far off
])

exp_block = appmod._expiry_report_block("TabsAlpha", "https://example.test")
_check("expiry block appears once a snapshot exists", exp_block is not None)
_check("an expired lot is counted as expired",
       exp_block["expired"] == 1, detail=str(exp_block["expired"]))
_check("a far-off long-life lot is not flagged",
       all("CHICKEN" not in row["item"] for row in exp_block["rows"]))
_check("the snapshot is dated so nobody reads it as today's shelf",
       bool(exp_block["snapshot_date"]))

html = alpha.get(f"/results/{ALPHA_SID}").get_data(as_text=True)
_check("expiry tab renders", 'id="tab-expiry"' in html)
_check("expiry tab button renders", "Expiring soon" in html)
# The page links relatively. An absolute link here would be built from the
# client-controlled Host header for no gain, since the page already has an origin.
_check("the expiry link is relative, not built from the Host header",
       'href="/expiry"' in html and "http://localhost/expiry" not in html)
_check("an expiring lot shows on the page", "BROOKVALE FRESH YOGHURT 500G" in html)
_check("an expired lot is called out as expired", "expired" in html.lower())
_check("the far-off lot stays off the page",
       "KESTREL FROZEN CHICKEN 1KG" not in html)


# ── 4. Org isolation ─────────────────────────────────────────────────────────
# org_name is the entire boundary here. These panels read org-level data on a
# page addressed by session id, so a leak would cross tenants silently.

_add_tenders("TabsBravo", [("NORDVIK FROZEN SALMON 1KG", 240, -5, 60)],
             customer="BRAVO ONLY CUSTOMER")

html = alpha.get(f"/results/{ALPHA_SID}").get_data(as_text=True)
_check("another org's commitment never reaches this page",
       "NORDVIK FROZEN SALMON 1KG" not in html)
_check("another org's customer name never reaches this page",
       "BRAVO ONLY CUSTOMER" not in html)

bravo_block = appmod._tender_results_block("TabsBravo")
_check("the other org sees only its own row",
       bravo_block["total"] == 1 and
       bravo_block["rows"][0]["item_name"] == "NORDVIK FROZEN SALMON 1KG")

# A blank tenant key must be refused BEFORE it reaches the database, not merely
# come back empty because the column happens to be NOT NULL. Proven by making
# the DAL explode: if the guard is removed, this raises instead of returning.
_reached = {"db": False}
_orig_dal = db.get_tender_commitments


def _dal_tripwire(org_name, *a, **k):
    if not org_name:
        _reached["db"] = True
        raise AssertionError("blank org_name reached the database")
    return _orig_dal(org_name, *a, **k)


db.get_tender_commitments = _dal_tripwire
try:
    _check("a blank org name returns nothing rather than filtering on ''",
           appmod._tender_results_block("") is None)
    _check("a blank org name never reaches the database at all",
           _reached["db"] is False)
    _check("a None org name is refused the same way",
           appmod._tender_results_block(None) is None and _reached["db"] is False)
finally:
    db.get_tender_commitments = _orig_dal


# ── 5. The display cap holds and is declared ─────────────────────────────────

_add_tenders("TabsBravo", [
    (f"PADIMAS ITEM {i:03d}", 100 + i, -5, 60 + i)
    for i in range(appmod.TENDER_RESULTS_ROWS + 8)
])
capped = appmod._tender_results_block("TabsBravo")
_check("total counts every live row, not just the shown ones",
       capped["total"] == appmod.TENDER_RESULTS_ROWS + 9,
       detail=str(capped["total"]))
_check("rows are capped for display",
       len(capped["rows"]) == appmod.TENDER_RESULTS_ROWS,
       detail=str(len(capped["rows"])))

html = bravo.get(f"/results/{BRAVO_SID}").get_data(as_text=True)
_check("the page admits it is showing a subset", "See them all" in html)


# ── 6. A failing block degrades to no tab, never to a broken page ────────────
# Both panels are extras. Losing one must not cost the operator the run they
# actually came to read.

_orig = appmod._tender_results_block
appmod._tender_results_block = lambda org: (_ for _ in ()).throw(RuntimeError("db wedged"))
try:
    r = alpha.get(f"/results/{ALPHA_SID}")
    html = r.get_data(as_text=True)
    _check("results page survives a failing tender block",
           r.status_code == 200, detail=str(r.status_code))
    _check("the failing block renders no tab", "tab-tender" not in html)
    _check("the recommendations still render", "BROOKVALE UHT MILK 1L" in html)
finally:
    appmod._tender_results_block = _orig

_orig_exp = appmod._expiry_report_block
appmod._expiry_report_block = lambda org, base: (_ for _ in ()).throw(RuntimeError("db wedged"))
try:
    r = alpha.get(f"/results/{ALPHA_SID}")
    _check("results page survives a failing expiry block",
           r.status_code == 200, detail=str(r.status_code))
    _check("the failing expiry block renders no tab",
           "tab-expiry" not in r.get_data(as_text=True))
finally:
    appmod._expiry_report_block = _orig_exp


# ── 7. /tenders still renders after the row-shaping extraction ───────────────
# _shape_tender_row was pulled out of tenders_page so both pages share one copy
# of the derived-field maths. That page must not have regressed.

r = alpha.get("/tenders")
_check("tenders page still renders", r.status_code == 200, detail=str(r.status_code))
_check("tenders page still shows its rows",
       "VANMARK CANNED TUNA 150G" in r.get_data(as_text=True))


print()
if _FAILED:
    print("FAILED")
    sys.exit(1)
print("all results-context-tab checks passed")
