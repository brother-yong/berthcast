"""Happy path for tender-aware order quantities (plan 013).

Proves the feature does what it says once a human has confirmed a pairing:

  1. the fuzzy matcher survives a different word order
  2. per_month and period_total both reduce to the same monthly figure
  3. the upload pairs tender rows without a separate screen
  4. a correction stores the CANONICAL item spelling, not what was typed
  5. the results page renders base + tender, in the gold class
  6. the printed sheet carries the total and a "+ N tender" sub-line
  7. the CSV exports the total, add-on, and raw tender source
  8. a confirmation survives deleting and re-uploading the sheet
  9. a contracted item with no recommendation is listed by NAME, no quantity

Throwaway temp DB, stubbed anthropic client, no API calls. CSRF is disabled for
the test client only. Run: python tests/test_tender_match.py
"""
import csv
import io
import os
import sys
import json
import tempfile
import types
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_tender_match.db")
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
from agents.shared import normalise_match_key           # noqa: E402
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


# ── Fixtures. Invented brands only: the repo is public ───────────────────────

# The org's own item names, as they come back from a completed run.
SAUCE = "PADIMAS OYSTER SAUCE 500ML"
MILK  = "BROOKVALE UHT MILK 1L"
COD   = "NORDVIK COD FILLET 1KG"

# The customer's spelling of the same two products: same words, different
# order and punctuation, which is what defeats a plain normalised key.
T_SAUCE = "SAUCE OYSTER_500GRM/BTL."
T_COD   = "FILLET COD NORDVIK_1KG"

K_SAUCE = normalise_match_key(T_SAUCE)
K_COD   = normalise_match_key(T_COD)

TODAY = date.today()
PERIOD_START = (TODAY - timedelta(days=30)).isoformat()
PERIOD_END   = (TODAY + timedelta(days=180)).isoformat()

TENDER_CSV = (
    "Item Description,Tender Qty\n"
    f"{T_SAUCE},100\n"
    f"{T_COD},77\n"
)

INVENTORY = [
    {"item": SAUCE, "status": "LOW", "spoilage_risk": "NONE", "days_of_supply": 11,
     "category": "DRY", "stock": "24 CTN", "observation": "low"},
    {"item": MILK, "status": "CRITICAL", "spoilage_risk": "NONE", "days_of_supply": 5,
     "category": "CHILLED", "stock": "8 CTN", "observation": "low"},
    # Healthy, so the pipeline writes no recommendation for it -- and it is the
    # item the "contracted, but not on this order list" block exists for.
    {"item": COD, "status": "HEALTHY", "spoilage_risk": "NONE", "days_of_supply": 90,
     "category": "FROZEN", "stock": "300 CTN", "observation": "ok"},
]

RECS = [
    {"item": SAUCE, "supplier": "KESTREL TRADING", "supplier_type": "local",
     "lead_time_days": 21, "days_of_supply": 11, "recommended_action": "REORDER",
     "suggested_quantity": "200 CTN", "uom_label": " CTN", "confidence": "HIGH",
     "supplier_risk": "None", "flags": [], "reason": "Stock low against steady sales.",
     "avg_monthly_sales": 60, "approved": True},
    {"item": MILK, "supplier": "VANMARK DAIRY", "supplier_type": "local",
     "lead_time_days": 14, "days_of_supply": 5, "recommended_action": "REORDER",
     "suggested_quantity": "60 CTN", "uom_label": " CTN", "confidence": "HIGH",
     "supplier_risk": "None", "flags": [], "reason": "Runs out inside the lead time.",
     "avg_monthly_sales": 40, "approved": True},
]


def _make_user(email, org):
    db.execute("INSERT INTO users (email, password_hash, org_name, model, tier, "
               "email_verified, role) VALUES (?,?,?,?,?,?,?)",
               (email, generate_password_hash("x"), org, "claude-sonnet-5",
                "enterprise", 1, "admin"))
    return db.query("SELECT id FROM users WHERE email=?", (email,))[0]["id"]


def _client(user_id, email, org, role="admin"):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["user_id"]  = user_id
        s["email"]    = email
        s["org_name"] = org
        s["model"]    = "claude-sonnet-5"
        s["is_admin"] = False
        s["tier"]     = "enterprise"
        s["role"]     = role
        s["sv"]       = 0
    return c


def _upload_tenders(client, csv_text, filename="tenders.csv"):
    data = {"customer": "NORDVIK CATERING", "period_start": PERIOD_START,
            "period_end": PERIOD_END, "qty_basis": "per_month",
            "file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    return client.post("/tenders/upload", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


ALPHA_ID = _make_user("alpha@example.com", "OrgAlpha")
alpha    = _client(ALPHA_ID, "alpha@example.com", "OrgAlpha")

SID = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
                 (ALPHA_ID, "OrgAlpha", "complete"))
db.execute("INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) "
           "VALUES (?,?,?)",
           (SID, json.dumps(INVENTORY), json.dumps(RECS)))


# ── 1. Word order does not defeat the matcher ────────────────────────────────

index = tenders.build_match_index([SAUCE, MILK, COD])
proposed = tenders.propose_matches(T_SAUCE, index)
_check("the reordered tender name finds the right item",
       bool(proposed) and proposed[0]["name"] == SAUCE,
       detail=str(proposed))


# ── 2. Both quantity bases reduce to the same monthly figure ─────────────────

def _matched_row(qty, basis, start=PERIOD_START, end=PERIOD_END):
    return {"id": 1, "customer": "NORDVIK CATERING", "item_name": T_SAUCE,
            "match_key": K_SAUCE, "quantity": qty, "qty_basis": basis,
            "period_start": start, "period_end": end,
            "inventory_item": SAUCE, "inventory_key": normalise_match_key(SAUCE)}


_per_month = tenders.tender_addons([_matched_row(100, "per_month")], TODAY.isoformat())
_check("a live per_month row contributes its own number",
       _per_month.get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(_per_month))

_six_months = tenders.tender_addons(
    [_matched_row(600, "period_total", "2026-07-01", "2026-12-31")], "2026-08-01")
_check("a period_total of 600 over six months is 100 a month",
       _six_months.get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(_six_months))


# ── 3. Uploading pairs the rows automatically, with no step for the user ─────

r = _upload_tenders(alpha, TENDER_CSV, "alpha_tenders.csv")
_check("the tender sheet imported", b"Imported 2 tender rows" in r.data)
_check("the upload reports the pairings stored", b"stored pairing" in r.data)
UPLOAD_ID = db.get_tender_uploads("OrgAlpha")[0]["id"]

auto = {m["tender_key"]: m for m in db.get_tender_matches("OrgAlpha")}
_check("the sauce line was paired without anyone being asked",
       auto.get(K_SAUCE, {}).get("inventory_item") == SAUCE,
       detail=str(auto.get(K_SAUCE)))
_check("the pairing is marked as a guess, not a human decision",
       auto.get(K_SAUCE, {}).get("confirmed_by") == appmod.TENDER_MATCH_AUTO,
       detail=str(auto.get(K_SAUCE, {}).get("confirmed_by")))
_check("the guess already feeds a monthly figure, no confirmation needed",
       appmod._tender_addon_map("OrgAlpha").get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(appmod._tender_addon_map("OrgAlpha")))

# Word order is what defeats a plain key, so this is the real test of the
# automatic pairing: "FILLET COD NORDVIK_1KG" against "NORDVIK COD FILLET 1KG".
_check("a reordered name is paired automatically too",
       auto.get(K_COD, {}).get("inventory_item") == COD,
       detail=str(auto.get(K_COD)))

# The old separate screen is gone. The client asked for the tender to reach the
# order sheet without anyone pairing items up first.
_check("there is no separate match screen any more",
       alpha.get("/tenders/match").status_code in (404, 405),
       detail=str(alpha.get("/tenders/match").status_code))


# ── 4. A correction sticks, and outlives a re-upload ─────────────────────────

# Stand in for a wrong automatic guess, then correct it to the intended item.
db.save_tender_match("OrgAlpha", K_SAUCE, T_SAUCE, MILK,
                     normalise_match_key(MILK), appmod.TENDER_MATCH_AUTO)
r = alpha.post("/tenders/match", data={
    "upload_id": str(UPLOAD_ID),
    # What the page showed when it rendered. The route writes only when this
    # still matches what is stored, so a pairing that changed after the page
    # was built cannot be clobbered by a stale tab.
    "orig__" + K_SAUCE: MILK,
    # Typed by hand, wrong case and a doubled space: the stored name must still
    # be the org's own spelling.
    "item__" + K_SAUCE: "padimas oyster sauce  500ml",
}, follow_redirects=True)
_check("saving a correction redirects back to the tenders page",
       r.status_code == 200, detail=str(r.status_code))

saved = {m["tender_key"]: m for m in db.get_tender_matches("OrgAlpha")}
_check("the typed name is stored in the canonical spelling",
       saved.get(K_SAUCE, {}).get("inventory_item") == SAUCE,
       detail=str(saved.get(K_SAUCE)))
_check("the stored key is the canonical item's key",
       saved.get(K_SAUCE, {}).get("inventory_key") == normalise_match_key(SAUCE),
       detail=str(saved.get(K_SAUCE)))
_check("a corrected pairing is no longer marked as a guess",
       saved.get(K_SAUCE, {}).get("confirmed_by") != appmod.TENDER_MATCH_AUTO,
       detail=str(saved.get(K_SAUCE, {}).get("confirmed_by")))

# Clearing the box stops that line contributing anything, and that has to be
# recorded as a decision rather than deleted: a deleted row looks exactly like
# one nobody has seen, so the next upload would re-guess it and silently put
# back the pairing the user just took out.
alpha.post("/tenders/match", data={
    "upload_id": str(UPLOAD_ID), "orig__" + K_COD: COD, "item__" + K_COD: ""
}, follow_redirects=True)
_check("clearing a pairing stops it contributing",
       normalise_match_key(COD) not in appmod._tender_addon_map("OrgAlpha"),
       detail=str(appmod._tender_addon_map("OrgAlpha")))
_cleared = {m["tender_key"]: m for m in db.get_tender_matches("OrgAlpha")}
_check("clearing is recorded as a decision, not a deletion",
       K_COD in _cleared and _cleared[K_COD]["inventory_key"] == "",
       detail=str(_cleared.get(K_COD)))
_check("and that decision is not marked as a guess",
       _cleared.get(K_COD, {}).get("confirmed_by") != appmod.TENDER_MATCH_AUTO,
       detail=str(_cleared.get(K_COD, {}).get("confirmed_by")))


# ── 5. The results page renders the split ────────────────────────────────────

html = alpha.get(f"/results/{SID}").get_data(as_text=True)
_check("the results page renders the gold add-on class", 'class="qty-tender"' in html)
_check("the add-on shows as + 100", "+ 100" in html)
_check("the quantity input still holds the base, not the total",
       'value="200 CTN"' in html and 'value="300 CTN"' not in html)
_check("the card states the total to order", "Order 300 CTN in total." in html)
_check("the customer behind the add-on is named", "NORDVIK CATERING" in html)
_check("the double-count disclosure is shown",
       "do not order it twice" in html)
_check("an item with no tender keeps its plain quantity",
       "60 CTN" in html)


# ── 6. The printed sheet carries the total and the sub-line ──────────────────

html = alpha.get(f"/results/{SID}/print").get_data(as_text=True)
_check("the print sheet shows the total", "300 CTN" in html)
_check("the print sheet spells out the split", "200 + 100 tender" in html)
_check("the print sheet names the raw tender line", f"from: {T_SAUCE}" in html)


# ── 7. The CSV gains its own add-on column ───────────────────────────────────

r = alpha.get(f"/results/{SID}/export.csv")
_check("the CSV downloads", r.status_code == 200, detail=str(r.status_code))
rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
header = rows[0]
by_item = {row[0]: dict(zip(header, row)) for row in rows[1:]}
_check("the header carries the add-on column", "Tender Add-On" in header, detail=str(header))
_check("the header carries the tender source column", "Tender Source" in header,
       detail=str(header))
_check("Qty To Order carries the total",
       by_item.get(SAUCE, {}).get("Qty To Order") == "300 CTN",
       detail=str(by_item.get(SAUCE)))
_check("the add-on column carries the addend only",
       by_item.get(SAUCE, {}).get("Tender Add-On") == "100",
       detail=str(by_item.get(SAUCE)))
_check("the source column carries the raw tender line",
       T_SAUCE in by_item.get(SAUCE, {}).get("Tender Source", ""),
       detail=str(by_item.get(SAUCE)))
_check("an item with no tender has an empty add-on cell",
       by_item.get(MILK, {}).get("Tender Add-On") == "",
       detail=str(by_item.get(MILK)))


# ── 8. A confirmation survives a delete and re-upload ────────────────────────
# Re-uploading a corrected sheet is the documented fix for a bad row, and it
# recreates every commitment row. The decision is keyed on the tender TEXT, so
# it has to still apply afterwards.

upload_id = db.get_tender_uploads("OrgAlpha")[0]["id"]
db.delete_tender_upload("OrgAlpha", upload_id)
_check("deleting the sheet removes its commitments",
       appmod._tender_addon_map("OrgAlpha") == {})

_upload_tenders(alpha, TENDER_CSV, "alpha_tenders.csv")
REUPLOAD_ID = db.get_tender_uploads("OrgAlpha")[0]["id"]
_check("a corrected pairing still applies after a re-upload",
       appmod._tender_addon_map("OrgAlpha").get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(appmod._tender_addon_map("OrgAlpha")))
_check("a CLEARED pairing is not silently re-guessed by the re-upload",
       normalise_match_key(COD) not in appmod._tender_addon_map("OrgAlpha"),
       detail=str(appmod._tender_addon_map("OrgAlpha")))
_check("the decisions themselves were never touched",
       len(db.get_tender_matches("OrgAlpha")) == 2)


# ── 9. Contracted, but not recommended this run ──────────────────────────────
# NAMES only. Attaching a quantity would mean inventing an order size the
# pipeline never computed.

# COD was cleared above to prove a cleared decision sticks, so pair it back:
# this block only lists items that HAVE a live pairing but no recommendation.
alpha.post("/tenders/match", data={
    # It was cleared above, so the box renders empty.
    "upload_id": str(REUPLOAD_ID), "orig__" + K_COD: "", "item__" + K_COD: COD
}, follow_redirects=True)
_check("the cod line is paired again for this check",
       normalise_match_key(COD) in appmod._tender_addon_map("OrgAlpha"),
       detail=str(appmod._tender_addon_map("OrgAlpha")))

html = alpha.get(f"/results/{SID}").get_data(as_text=True)
_check("the uncovered block renders",
       "Contracted, but not on this order list" in html)
block = html.split("Contracted, but not on this order list", 1)[1].split("</ul>", 1)[0]
_check("the contracted item with no recommendation is named", COD in block)
_check("the recommended item is not repeated in the block", SAUCE not in block)
_check("no tender quantity is printed in the block", "77" not in block,
       detail=block.strip()[:200])
_check("the recommended item still shows its split instead",
       'class="qty-tender"' in html and "+ 100" in html)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll tender match tests passed.")
