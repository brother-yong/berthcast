"""Adversarial tests for tender-aware order quantities (plan 013).

The happy path lives in tests/test_tender_match.py. This file only tries to
break the thing, because every failure here is a wrong number on a purchase
order somebody actually places.

The three that cost real money if they regress:

  case 12  approving twice must not grow the quantity. If the rendered input
           ever carries the TOTAL, approve writes it into edited_quantity and
           the add-on stacks on the next load: 300, 400, 500, plausible at
           every step and silent.
  case 14  uploading the same sheet twice must not double the add-on. Nothing
           in the schema stops a second full set of commitment rows landing.
  case 15  an item a human deliberately zeroed must not come back as a real
           order through the tender add-on.

Throwaway temp DB, stubbed anthropic client, no API calls. CSRF is disabled for
the test client only. Run: python tests/test_tender_match_break.py
"""
import csv
import io
import os
import re
import sys
import json
import tempfile
import types
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_tender_match_break.db")
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
import rec_logic                                        # noqa: E402
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

SAUCE = "PADIMAS OYSTER SAUCE 500ML"
MILK  = "BROOKVALE UHT MILK 1L"
COD   = "NORDVIK COD FILLET 1KG"
OATS  = "KESTREL ROLLED OATS 750G"

# The customer spells the same product with the words the other way round,
# which is exactly what a plain normalised key cannot follow.
T_SAUCE = "SAUCE OYSTER_500GRM/BTL."
T_MILK  = "MILK UHT_1L/PKT_BROOKVALE"
T_OATS  = "OATS ROLLED_750GRM/PKT"

K_SAUCE = normalise_match_key(T_SAUCE)
K_MILK  = normalise_match_key(T_MILK)
K_OATS  = normalise_match_key(T_OATS)

TODAY        = date.today()
TODAY_ISO    = TODAY.isoformat()
PERIOD_START = (TODAY - timedelta(days=30)).isoformat()
PERIOD_END   = (TODAY + timedelta(days=180)).isoformat()

INVENTORY = [
    {"item": SAUCE, "status": "LOW", "spoilage_risk": "NONE", "days_of_supply": 11,
     "category": "DRY", "stock": "24 CTN", "observation": "low"},
    {"item": MILK, "status": "CRITICAL", "spoilage_risk": "NONE", "days_of_supply": 5,
     "category": "CHILLED", "stock": "8 CTN", "observation": "low"},
    {"item": OATS, "status": "LOW", "spoilage_risk": "NONE", "days_of_supply": 12,
     "category": "DRY", "stock": "30 CTN", "observation": "low"},
    {"item": COD, "status": "HEALTHY", "spoilage_risk": "NONE", "days_of_supply": 90,
     "category": "FROZEN", "stock": "300 CTN", "observation": "ok"},
]


def _rec(item, qty, **extra):
    r = {"item": item, "supplier": "KESTREL TRADING", "supplier_type": "local",
         "lead_time_days": 21, "days_of_supply": 11, "recommended_action": "REORDER",
         "suggested_quantity": qty, "uom_label": " CTN", "confidence": "HIGH",
         "supplier_risk": "None", "flags": [], "reason": "Stock low against sales.",
         "avg_monthly_sales": 60, "approved": True}
    r.update(extra)
    return r


RECS = [
    _rec(SAUCE, "200 CTN"),
    # No parseable quantity: the sanitiser refused to state one, so a tender
    # add-on must never be used to invent a total.
    _rec(MILK, "Verify with team"),
    # A human typed 0 meaning "do not order this".
    _rec(OATS, "40 CTN", edited_quantity="0"),
]


def _make_user(email, org, role="admin"):
    db.execute("INSERT INTO users (email, password_hash, org_name, model, tier, "
               "email_verified, role) VALUES (?,?,?,?,?,?,?)",
               (email, generate_password_hash("x"), org, "claude-sonnet-5",
                "enterprise", 1, role))
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


def _seed_session(user_id, org, recs=RECS, inventory=INVENTORY):
    sid = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) "
                     "VALUES (?,?,?)", (user_id, org, "complete"))
    db.execute("INSERT INTO analysis_results (session_id, inventory_report, "
               "recommendations_json) VALUES (?,?,?)",
               (sid, json.dumps(inventory), json.dumps(recs)))
    return sid


def _upload(client, csv_text, customer="NORDVIK CATERING", filename="tenders.csv",
            start=PERIOD_START, end=PERIOD_END, basis="per_month"):
    data = {"customer": customer, "period_start": start, "period_end": end,
            "qty_basis": basis,
            "file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    return client.post("/tenders/upload", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


def _confirm(client, key, value):
    """Post one decision through the real route, the way the screen would."""
    return client.post("/tenders/match", data={"match__" + key: value},
                       follow_redirects=True)


def _qty_input(html, item):
    """The rendered quantity input for one item, as (value, data_original).

    Scraped rather than asserted against a literal on purpose: if a future
    change puts the TOTAL in this box, case 12 posts that total back and the
    compounding starts.

    Scoped by data-field="quantity" AND this item's data-item. Matching on
    data-item alone is not enough: the note input and the supplier input carry
    it too, and the note input sits earlier in the card and renders an empty
    value, so a looser pattern silently scrapes the wrong box and every
    assertion below becomes meaningless.
    """
    m = re.search(r'<input[^>]*data-field="quantity"[^>]*data-item="'
                  + re.escape(item) + r'"[^>]*>', html)
    if not m:
        return None, None
    tag = m.group(0)
    val = re.search(r'\svalue="([^"]*)"', tag)
    org = re.search(r'\sdata-original="([^"]*)"', tag)
    return (val.group(1) if val else None), (org.group(1) if org else None)


def _approve(client, sid, item, qty):
    return client.post("/recommend/action", json={
        "session_id": sid, "item": item, "action": "approve",
        "edited_quantity": qty})


def _stored_rec(sid, item):
    row = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?",
                   (sid,))
    for r in json.loads(row[0]["recommendations_json"]):
        if r.get("item") == item:
            return r
    return {}


TENDER_CSV = f"Item Description,Tender Qty\n{T_SAUCE},100\n"

ALPHA_ID = _make_user("alpha@example.com", "OrgAlpha")
BRAVO_ID = _make_user("bravo@example.com", "OrgBravo")
VIEW_ID  = _make_user("viewer@example.com", "OrgAlpha", role="viewer")

alpha  = _client(ALPHA_ID, "alpha@example.com", "OrgAlpha")
bravo  = _client(BRAVO_ID, "bravo@example.com", "OrgBravo")
viewer = _client(VIEW_ID, "viewer@example.com", "OrgAlpha", role="viewer")

SID_A = _seed_session(ALPHA_ID, "OrgAlpha")
SID_B = _seed_session(BRAVO_ID, "OrgBravo")


# ── 1. Unconfirmed adds zero ─────────────────────────────────────────────────
# The sheet is uploaded and the text fuzzy-matches an item, but nobody has
# confirmed anything. Nothing may reach the order quantity.

_upload(alpha, TENDER_CSV)
_check("an unconfirmed sheet contributes no add-on",
       appmod._tender_addon_map("OrgAlpha") == {},
       detail=str(appmod._tender_addon_map("OrgAlpha")))

r = alpha.get(f"/results/{SID_A}")
_html = r.data.decode("utf-8", "replace")
_check("an unconfirmed sheet renders no gold split",
       'class="qty-tender"' not in _html)
_val, _orig = _qty_input(_html, SAUCE)
_check("the unconfirmed page still shows the plain base quantity",
       _val == "200 CTN", detail=str(_val))

# The matcher can see it, which is what makes the silence above meaningful.
_idx = tenders.build_match_index([SAUCE, MILK, COD, OATS])
_check("the matcher WOULD have proposed it, so the zero is the guard not luck",
       bool(tenders.propose_matches(T_SAUCE, _idx)))


# ── 2. Org isolation on every surface ────────────────────────────────────────

_upload(bravo, TENDER_CSV)
_confirm(bravo, K_SAUCE, SAUCE)
_check("org B's own confirmation lands",
       appmod._tender_addon_map("OrgBravo").get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(appmod._tender_addon_map("OrgBravo")))
_check("org B's confirmed tender never reaches org A's add-on map",
       appmod._tender_addon_map("OrgAlpha") == {},
       detail=str(appmod._tender_addon_map("OrgAlpha")))

r = alpha.get(f"/results/{SID_A}")
_check("org B's tender never renders on org A's results page",
       'class="qty-tender"' not in r.data.decode("utf-8", "replace"))
r = alpha.get(f"/results/{SID_A}/print")
_check("org B's tender never renders on org A's printed sheet",
       b"tender" not in r.data.lower())
r = alpha.get(f"/results/{SID_A}/export.csv")
_csv_a = r.data.decode("utf-8", "replace")
_check("org A's CSV carries an empty Tender Add-On column",
       "Tender Add-On" in _csv_a and ",100," not in _csv_a,
       detail=_csv_a[:200])

# A cross-org POST naming org A's tender key must write nothing for org A.
bravo.post("/tenders/match", data={"match__" + K_SAUCE: SAUCE,
                                   "org_name": "OrgAlpha"}, follow_redirects=True)
_check("a POST from org B writes no match for org A",
       db.get_tender_matches("OrgAlpha") == [],
       detail=str(db.get_tender_matches("OrgAlpha")))


# ── 3. A blank org key is refused before any query runs ──────────────────────
# org_name is the entire tenancy boundary on this path, so a blank key must be
# refused explicitly, not left to a NOT NULL column to reject by accident.

_reached = {"db": False}
_orig_matched = db.get_matched_tender_commitments


def _dal_tripwire(org_name, *a, **k):
    if not org_name:
        _reached["db"] = True
        raise AssertionError("blank org_name reached the database")
    return _orig_matched(org_name, *a, **k)


db.get_matched_tender_commitments = _dal_tripwire
try:
    _check("a blank org produces no add-ons", appmod._tender_addon_map("") == {})
    _check("a blank org never reaches the database", not _reached["db"])
finally:
    db.get_matched_tender_commitments = _orig_matched

_check("a blank org has no item universe", appmod._org_item_names("") == [])

# The helpers guard it, but a future caller can forget. The DAL refuses it too,
# at the layer nothing can bypass. NOT NULL accepts "", so without these a
# blank key would be a real filter value rather than a rejection.
_check("the DAL refuses a blank org on reads",
       db.get_tender_matches("") == []
       and db.get_matched_tender_commitments("") == []
       and db.count_unmatched_tender_commitments("", TODAY_ISO) == 0)
db.save_tender_match("", "somekey", "item", "inv", "invkey")
_check("the DAL refuses a blank org on write",
       db.query("SELECT COUNT(*) AS n FROM tender_item_matches "
                "WHERE org_name=''")[0]["n"] == 0)


# ── 4. A viewer cannot confirm anything ──────────────────────────────────────

_confirm(viewer, K_SAUCE, SAUCE)
_check("a viewer POST stores no match",
       db.get_tender_matches("OrgAlpha") == [],
       detail=str(db.get_tender_matches("OrgAlpha")))


# ── 5. A typed name that does not resolve stores nothing ─────────────────────
# Free text from a browser must never land in the database as an item name.

alpha.post("/tenders/match", data={"match__" + K_SAUCE: "__other__",
                                   "other__" + K_SAUCE: "TOTALLY MADE UP ITEM"},
           follow_redirects=True)
_check("an unresolvable typed item stores nothing",
       db.get_tender_matches("OrgAlpha") == [],
       detail=str(db.get_tender_matches("OrgAlpha")))

alpha.post("/tenders/match", data={"match__" + K_SAUCE: "__other__",
                                   "other__" + K_SAUCE: "x" * 400},
           follow_redirects=True)
_check("an over-long typed item stores nothing",
       db.get_tender_matches("OrgAlpha") == [])

# A typed name that DOES resolve is stored in the canonical spelling, so the
# rejection above is the guard rather than the route simply never saving.
alpha.post("/tenders/match", data={"match__" + K_SAUCE: "__other__",
                                   "other__" + K_SAUCE: "padimas oyster  sauce 500ml"},
           follow_redirects=True)
_m = db.get_tender_matches("OrgAlpha")
_check("a resolvable typed item is stored in the canonical spelling",
       len(_m) == 1 and _m[0]["inventory_item"] == SAUCE,
       detail=str(_m))


# ── 6. Dates: only a live contract contributes ───────────────────────────────

def _row(qty, basis="per_month", start=PERIOD_START, end=PERIOD_END, item=T_SAUCE,
         customer="NORDVIK CATERING"):
    return {"id": 1, "customer": customer, "item_name": item,
            "match_key": normalise_match_key(item), "quantity": qty,
            "qty_basis": basis, "period_start": start, "period_end": end,
            "inventory_item": SAUCE, "inventory_key": normalise_match_key(SAUCE)}


_expired = tenders.tender_addons(
    [_row(100, start=(TODAY - timedelta(days=400)).isoformat(),
          end=(TODAY - timedelta(days=40)).isoformat())], TODAY_ISO)
_check("an expired contract contributes zero", _expired == {}, detail=str(_expired))

_future = tenders.tender_addons(
    [_row(100, start=(TODAY + timedelta(days=40)).isoformat(),
          end=(TODAY + timedelta(days=400)).isoformat())], TODAY_ISO)
_check("a contract that has not started contributes zero", _future == {},
       detail=str(_future))


# ── 7. An unstated basis is never guessed ────────────────────────────────────

_null_basis = tenders.tender_addons([_row(100, basis=None)], TODAY_ISO)
_check("a NULL quantity basis contributes zero, never a guess",
       _null_basis == {}, detail=str(_null_basis))
_junk_basis = tenders.tender_addons([_row(100, basis="per_fortnight")], TODAY_ISO)
_check("an unrecognised basis contributes zero", _junk_basis == {},
       detail=str(_junk_basis))


# ── 8. A rate under half a unit produces no split ────────────────────────────
# Rounding 0.4 up to 1 would be inventing stock.

_tiny = rec_logic._tender_split(_rec(SAUCE, "200 CTN"),
                                {"qty": 0.4, "sources": [], "count": 1})
_check("a monthly rate of 0.4 produces no split at all", _tiny is None,
       detail=str(_tiny))
_one = rec_logic._tender_split(_rec(SAUCE, "200 CTN"),
                               {"qty": 0.6, "sources": [], "count": 1})
_check("a monthly rate of 0.6 does produce a split, so the floor is the guard",
       _one is not None and _one["add"] == "1", detail=str(_one))


# ── 9. No numeric base means no invented total ───────────────────────────────

_vague = rec_logic._tender_split(_rec(MILK, "Verify with team"),
                                 {"qty": 100.0, "sources": [], "count": 1})
_check("an unstatable base yields no total",
       _vague is not None and _vague["total"] == "" and _vague["base"] == "",
       detail=str(_vague))
_check("the add-on is still shown when the base cannot be stated",
       _vague is not None and _vague["add"] == "100", detail=str(_vague))


# ── 10. A zeroed item stays zeroed ───────────────────────────────────────────
# The two save routes accept "0" on purpose: it is a human saying "do not order
# this". A contract must not quietly turn it back into a real order.

_zeroed = rec_logic._tender_split(_rec(OATS, "40 CTN", edited_quantity="0"),
                                  {"qty": 100.0, "sources": [], "count": 1})
_check("an item a human zeroed produces no split", _zeroed is None,
       detail=str(_zeroed))


# ── 11. A tender line that normalises to nothing is refused at the door ──────
# A name like "***" or a "-----" spacer row has no letters or digits, so its
# match key is empty. Stored, it could never be matched and never even be
# decided (the confirm screen skips empty keys), yet it would still count
# toward the unmatched banner. A warning that cannot reach zero stops being
# read, which is the same failure the not-stocked sentinel exists to prevent.

_unmatched_pre = db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO)
r = _upload(alpha, "Item Description,Tender Qty\n***,55\n", filename="junk.csv")
_junk_keys = [x["match_key"] for x in db.get_tender_commitments("OrgAlpha")]
_check("11a a junk item name is never stored under an empty key",
       "" not in _junk_keys, detail=str(_junk_keys))
_check("11b the junk row is reported back to the user, not silently dropped",
       b"no letters or numbers" in r.data, detail="reject reason not shown")
_check("11c the junk row never inflates the unmatched count",
       db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO) == _unmatched_pre,
       detail=str(db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO)))
alpha.post("/tenders/match", data={"match__": SAUCE}, follow_redirects=True)
_check("11d an empty-key decision is never saved",
       all(m["tender_key"] for m in db.get_tender_matches("OrgAlpha")),
       detail=str(db.get_tender_matches("OrgAlpha")))

# Rows written BEFORE the ingest guard existed are still in the live database
# and cannot be re-parsed. The confirm screen skips an empty key, so counting
# one would nag forever about a row nobody can decide. Written straight to the
# table on purpose: it is the only way to reproduce a legacy row.
_legacy_upload = db.create_tender_upload("OrgAlpha", "legacy.csv", "NORDVIK CATERING")
db.save_tender_rows("OrgAlpha", _legacy_upload, [{
    "customer": "NORDVIK CATERING", "item_name": "-----", "match_key": "",
    "quantity": 12.0, "period_start": PERIOD_START, "period_end": PERIOD_END,
    "qty_basis": "per_month"}])
_check("11i the legacy empty-key row really is stored, so this case is live",
       any(x["match_key"] == "" for x in db.get_tender_commitments("OrgAlpha")))
_check("11j a legacy empty-key row never nags in the unmatched count",
       db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO) == _unmatched_pre,
       detail=str(db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO)))
_check("11k and it can never contribute a quantity either",
       "" not in appmod._tender_addon_map("OrgAlpha"))


# ── 11e. An oversized item name is refused, not stored ───────────────────────
# The ingest layer allows a 100,000 character cell. propose_matches does work
# that grows with the token count of the name it is matching, and the confirm
# screen runs it for every row on the page, so one pasted blob in an item
# column would hold the single production worker for minutes and take every
# other tenant down with it. Refused at the door, with a reason.

_long = "PADIMAS " * 4000                      # about 32,000 characters
r = _upload(alpha, f"Item Description,Tender Qty\n{_long},90\n", filename="long.csv")
_check("11e an oversized item name is never stored",
       all(len(x["item_name"]) <= tenders.MAX_TENDER_ITEM_CHARS
           for x in db.get_tender_commitments("OrgAlpha")),
       detail=str(max((len(x["item_name"])
                       for x in db.get_tender_commitments("OrgAlpha")), default=0)))
_check("11f the oversized row is reported back to the user",
       b"too long" in r.data, detail="reject reason not shown")

# A name right on the limit still imports, so the cap is a boundary and not a
# blanket refusal.
_ok_name = "PADIMAS " * 24 + "OATS"            # under the cap
_upload(alpha, f"Item Description,Tender Qty\n{_ok_name},7\n", filename="okname.csv")
_check("11g a long but legal item name still imports",
       any(x["item_name"] == _ok_name for x in db.get_tender_commitments("OrgAlpha")),
       detail=str(len(_ok_name)))

# And the matcher itself stays quick on a big universe, which is the thing the
# cap protects. Budget is deliberately loose: it is a runaway detector, not a
# benchmark, so it will not flap on a slow machine.
import time                                                        # noqa: E402
_big_index = tenders.build_match_index(
    [f"PADIMAS ITEM {n} 500ML CARTON" for n in range(5000)])
_t0 = time.time()
for _ in range(25):
    tenders.propose_matches("ITEM 4231 PADIMAS_500ML/CTN", _big_index)
_elapsed = time.time() - _t0
_check("11h 25 lookups against a 5,000 item universe stay well under a second",
       _elapsed < 5.0, detail=f"{_elapsed:.2f}s")


# ── 12. THE BIG ONE: approving twice must not grow the quantity ──────────────
# If the rendered input ever carries the TOTAL, approve posts it back,
# app.py saves it as edited_quantity, and the next load adds the tender again.
# Every value posted below is SCRAPED from the page, never a literal, so this
# test fails the moment the input starts showing the total.

r = alpha.get(f"/results/{SID_A}")
_html = r.data.decode("utf-8", "replace")
_val, _orig = _qty_input(_html, SAUCE)
_check("12a the input renders the BASE, not the total",
       _val == "200 CTN", detail=str(_val))
_check("12b data-original is the base too",
       _orig == "200 CTN", detail=str(_orig))
_check("12c the input does not contain the total",
       _val is not None and "300" not in _val, detail=str(_val))
_check("12d the page does render the split, so the check above means something",
       'class="qty-tender"' in _html)

_approve(alpha, SID_A, SAUCE, _val)
_check("12e posting the scraped base back stores no edited quantity",
       "edited_quantity" not in _stored_rec(SID_A, SAUCE),
       detail=str(_stored_rec(SID_A, SAUCE).get("edited_quantity")))

r = alpha.get(f"/results/{SID_A}")
_html2 = r.data.decode("utf-8", "replace")
_val2, _ = _qty_input(_html2, SAUCE)
_check("12f after one approve the input is unchanged",
       _val2 == "200 CTN", detail=str(_val2))

_approve(alpha, SID_A, SAUCE, _val2)
r = alpha.get(f"/results/{SID_A}")
_html3 = r.data.decode("utf-8", "replace")
_val3, _ = _qty_input(_html3, SAUCE)
_check("12g after a SECOND approve the input is STILL the base",
       _val3 == "200 CTN", detail=str(_val3))
_check("12h the quantity never compounded to 400",
       "400" not in _val3 if _val3 else False, detail=str(_val3))

# The blur path has identical compare-and-save logic, so it is the same trap
# through a different door.
alpha.post("/recommend/edit", json={"session_id": SID_A, "item": SAUCE,
                                    "quantity": _val3})
r = alpha.get(f"/results/{SID_A}")
_val4, _ = _qty_input(r.data.decode("utf-8", "replace"), SAUCE)
_check("12i the blur save path does not compound it either",
       _val4 == "200 CTN", detail=str(_val4))


# ── 13. The sentinel: "we do not stock this" versus "not decided yet" ────────
# Both add zero, but only one of them is a decision. If they were the same
# thing, the unmatched banner could never reach zero and would become wallpaper.

_upload(alpha, f"Item Description,Tender Qty\n{T_OATS},80\n", filename="oats.csv")
_unmatched_before = db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO)
_check("an undecided row counts as unmatched", _unmatched_before >= 1,
       detail=str(_unmatched_before))

_confirm(alpha, K_OATS, "__none__")
_check("13a a not-stocked decision adds zero",
       normalise_match_key(OATS) not in appmod._tender_addon_map("OrgAlpha"),
       detail=str(appmod._tender_addon_map("OrgAlpha")))
_check("13b a not-stocked decision IS stored",
       any(m["tender_key"] == K_OATS and m["inventory_key"] == ""
           for m in db.get_tender_matches("OrgAlpha")),
       detail=str(db.get_tender_matches("OrgAlpha")))
_check("13c a not-stocked decision clears the unmatched count for that row",
       db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO) < _unmatched_before,
       detail=str(db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO)))

# Clearing it again must put the row back into the unmatched count, or the
# banner would be a one-way latch.
_confirm(alpha, K_OATS, "")
_check("13d clearing a decision returns the row to the unmatched count",
       db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO) >= _unmatched_before,
       detail=str(db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO)))


# ── 14. Uploading the same sheet twice must not double the add-on ────────────
# save_tender_rows is a plain INSERT and tender_commitments has no UNIQUE, so
# a second upload appends a full second set of rows. Both join the one mapping
# row. Without the de-dupe a 100/month contract prints as "200 + 200".

_upload(alpha, TENDER_CSV, filename="again.csv")
_dupe_rows = [r for r in db.get_tender_commitments("OrgAlpha")
              if r["match_key"] == K_SAUCE]
_check("14a the duplicate really is in the database, so the test is live",
       len(_dupe_rows) >= 2, detail=str(len(_dupe_rows)))
_check("14b the same sheet uploaded twice contributes 100, not 200",
       appmod._tender_addon_map("OrgAlpha").get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(appmod._tender_addon_map("OrgAlpha")))

r = alpha.get(f"/results/{SID_A}")
_html = r.data.decode("utf-8", "replace")
_check("14c the results page renders + 100, never + 200",
       "+ 200" not in _html and "+ 100" in _html)

# A retyped customer name must not let the duplicate through.
_retyped = tenders.tender_addons(
    [_row(100, customer="NORDVIK CATERING"),
     _row(100, customer="NORDVIK CATERING PTE LTD")], TODAY_ISO)
_check("14d a retyped customer name does not evade the de-dupe",
       _retyped.get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(_retyped))

# Two genuinely different quantities are NOT duplicates and must still sum,
# or the de-dupe would be quietly swallowing real contracts.
_different = tenders.tender_addons([_row(100), _row(60)], TODAY_ISO)
_check("14e two different quantities still sum, the de-dupe is not too greedy",
       _different.get(normalise_match_key(SAUCE), {}).get("qty") == 160.0,
       detail=str(_different))
_check("14f the source count reflects surviving rows, not duplicate uploads",
       _retyped.get(normalise_match_key(SAUCE), {}).get("count") == 1,
       detail=str(_retyped))


# ── 15. A zeroed item never becomes a real order in the export ──────────────

r = alpha.get(f"/results/{SID_A}/export.csv")
_rows = list(csv.DictReader(io.StringIO(r.data.decode("utf-8", "replace"))))
_oats_row = next((x for x in _rows if x.get("Item") == OATS), None)
if _oats_row is None:
    _check("15a the zeroed item is present in the CSV", False, detail="row missing")
else:
    _check("15a the zeroed item exports its own 0, not a tender total",
           "100" not in (_oats_row.get("Qty To Order") or ""),
           detail=str(_oats_row.get("Qty To Order")))
    _check("15b the zeroed item carries no tender add-on",
           not (_oats_row.get("Tender Add-On") or "").strip(),
           detail=str(_oats_row.get("Tender Add-On")))

_check("15c no cell in the export renders the string None",
       all("None" not in (v or "") for x in _rows for v in x.values()),
       detail=str(_rows[:1]))


# ── 16. Formula injection survives the new column ────────────────────────────
# A matched item name still leaves through csv_safe_cell: the name came out of
# a file the client uploaded, so it is untrusted no matter who confirmed it.

EVIL = "=cmd()|'/c calc'!A1 PADIMAS"
SID_E = _seed_session(ALPHA_ID, "OrgAlpha",
                      recs=[_rec(EVIL, "200 CTN")],
                      inventory=[{"item": EVIL, "status": "LOW", "spoilage_risk": "NONE",
                                  "days_of_supply": 11, "category": "DRY",
                                  "stock": "24 CTN", "observation": "low"}])
r = alpha.get(f"/results/{SID_E}/export.csv")
_evil_csv = r.data.decode("utf-8", "replace")
_check("16 a formula-shaped item name is neutralised in the CSV",
       "\n=cmd" not in _evil_csv and ",=cmd" not in _evil_csv,
       detail=_evil_csv[:160])


# ── 17. The uncovered block names items, and never invents a quantity ───────
# A healthy item with a live contract gets no recommendation, so it would
# otherwise be silently missing from the page entirely.

_upload(alpha, "Item Description,Tender Qty\nFILLET COD NORDVIK_1KG,4321\n",
        filename="cod.csv")
_confirm(alpha, normalise_match_key("FILLET COD NORDVIK_1KG"), COD)
r = alpha.get(f"/results/{SID_A}")
_html = r.data.decode("utf-8", "replace")
_check("17a a contracted item with no recommendation is named on the page",
       COD in _html)
_check("17b no quantity is invented for it",
       "4321" not in _html, detail="a tender quantity leaked into the block")
_check("17c an item that DOES have a recommendation stays in the split",
       'class="qty-tender"' in _html)


# ── 18. A degenerate INVENTORY name cannot collide with the sentinel ─────────
# Runs last on purpose: it seeds a newer completed session, and _org_item_names
# reads the most recent one, so anything after this would see this item list.
#
# An inventory item whose name has no letters or digits normalises to "", which
# is the not-stocked sentinel. If such a name stayed in the resolver's lookup,
# a typed value that also normalises to "" would resolve to it and then be
# stored AS the sentinel: the user picks a real item and the screen reads back
# "we do not stock this", with the add-on silently zero.

SID_DEGEN = _seed_session(
    ALPHA_ID, "OrgAlpha",
    recs=[_rec(SAUCE, "200 CTN")],
    inventory=[{"item": "-----", "status": "LOW", "spoilage_risk": "NONE",
                "days_of_supply": 11, "category": "DRY", "stock": "1 CTN",
                "observation": "spacer row that survived the pipeline"},
               {"item": SAUCE, "status": "LOW", "spoilage_risk": "NONE",
                "days_of_supply": 11, "category": "DRY", "stock": "24 CTN",
                "observation": "low"}])
_check("18a the degenerate name really is in the item universe, so this is live",
       "-----" in appmod._org_item_names("OrgAlpha"),
       detail=str(appmod._org_item_names("OrgAlpha")))

db.delete_tender_match("OrgAlpha", K_SAUCE)
alpha.post("/tenders/match", data={"match__" + K_SAUCE: "__other__",
                                   "other__" + K_SAUCE: "***"},
           follow_redirects=True)
_saved = [m for m in db.get_tender_matches("OrgAlpha") if m["tender_key"] == K_SAUCE]
_check("18b a value normalising to nothing never resolves to the spacer item",
       _saved == [], detail=str(_saved))
_check("18c and it is certainly never stored as the not-stocked sentinel",
       not any(m["inventory_key"] == "" and m["tender_key"] == K_SAUCE
               for m in db.get_tender_matches("OrgAlpha")),
       detail=str(db.get_tender_matches("OrgAlpha")))

# Build the lookup the way the route would WITHOUT its empty-key filter, and
# show the collision is real. Without this, 18b and 18c would pass just as
# happily against a route that had no filter at all, and the guard could be
# removed with the suite staying green.
_unfiltered = {normalise_match_key(n): n for n in appmod._org_item_names("OrgAlpha")}
_check("18d unfiltered, a value normalising to nothing DOES resolve to the "
       "spacer, so the filter is the guard and not a coincidence",
       _unfiltered.get(normalise_match_key("***")) == "-----",
       detail=str(_unfiltered.get(normalise_match_key("***"))))


# ── 19. The below-threshold fallback path stays cheap too ────────────────────
# Case 11h times the path where candidates clear MATCH_MIN_SCORE. The confirm
# screen also calls propose_matches a SECOND time with min_score=0.0 whenever
# the first call finds nothing, which is the pathological row shape: broad but
# weak token overlap. Time that path, not just the easy one.

_weak_index = tenders.build_match_index(
    [f"PADIMAS CARTON CASE PACK VARIANT {n} ASSORTED RETAIL" for n in range(5000)])
_t0 = time.time()
for _ in range(25):
    _none = tenders.propose_matches("CARTON PACK", _weak_index)
    if not _none:
        tenders.propose_matches("CARTON PACK", _weak_index, min_score=0.0)
_elapsed = time.time() - _t0
_check("19 the below-threshold fallback path stays bounded",
       _elapsed < 10.0, detail=f"{_elapsed:.2f}s")


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll tender match break tests passed.")
