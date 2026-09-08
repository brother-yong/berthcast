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
from html.parser import HTMLParser

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


class _TenderFormParser(HTMLParser):
    """Collect the real correction forms and every named input they render."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            action = attrs.get("action", "")
            self._current = {} if action.endswith("/tenders/match") else None
        elif tag == "input" and self._current is not None:
            name = attrs.get("name")
            if name:
                self._current[name] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}


class _RecommendationSourceParser(HTMLParser):
    """Read source text only inside one recommendation card's audit block."""

    def __init__(self, item):
        super().__init__()
        self.item = item
        self.card_depth = 0
        self.source_depth = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if not self.card_depth:
            if tag == "div" and "rec-card" in classes and attrs.get("data-item") == self.item:
                self.card_depth = 1
            return

        if tag not in _VOID_TAGS:
            self.card_depth += 1
        if tag == "div" and "rec-tender-line" in classes:
            self.source_depth = 1
        elif self.source_depth and tag not in _VOID_TAGS:
            self.source_depth += 1

    def handle_endtag(self, tag):
        if not self.card_depth:
            return
        if self.source_depth:
            self.source_depth -= 1
        self.card_depth -= 1

    def handle_data(self, data):
        if self.source_depth:
            self.text.append(data)


class _TableRowParser(HTMLParser):
    """Collect visible text per table row so print assertions stay row-scoped."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.current = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if not self.depth:
            if tag == "tr":
                self.depth = 1
                self.current = []
            return
        if tag not in _VOID_TAGS:
            self.depth += 1

    def handle_endtag(self, tag):
        if not self.depth:
            return
        self.depth -= 1
        if not self.depth:
            self.rows.append(" ".join(" ".join(self.current).split()))
            self.current = []

    def handle_data(self, data):
        if self.depth:
            self.current.append(data)


def _tender_forms(html):
    parser = _TenderFormParser()
    parser.feed(html)
    return parser.forms


def _recommendation_source_text(html, item):
    parser = _RecommendationSourceParser(item)
    parser.feed(html)
    return " ".join(" ".join(parser.text).split())


def _printed_row_text(html, item):
    parser = _TableRowParser()
    parser.feed(html)
    return next((row for row in parser.rows if item in row), "")


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


def _sheet(item, qty):
    """A one-row tender sheet. Built here so no case hand-writes CSV."""
    return f"Item Description,Tender Qty{chr(10)}{item},{qty}{chr(10)}"


def _upload_id_for_key(org, key):
    ids = [r["upload_id"] for r in db.get_tender_commitments(org)
           if r["match_key"] == key]
    return max(ids) if ids else 0


def _seed_tender_upload(org, item_rows, customer="NORDVIK CATERING",
                        filename="seeded.csv"):
    """Store a finished tender upload without exercising the upload matcher."""
    upload_id = db.create_tender_upload(org, filename, "tester@example.com", customer)
    rows = [{
        "customer": customer,
        "item_name": item,
        "match_key": normalise_match_key(item),
        "quantity": qty,
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "qty_basis": "per_month",
    } for item, qty in item_rows]
    db.save_tender_rows(org, upload_id, rows)
    db.finalise_tender_upload(org, upload_id, len(rows), 0, "[]")
    return upload_id


def _confirm(client, key, value, org="OrgAlpha"):
    """Correct one pairing through the real route, the way the page would.

    An empty value clears it, which the route records as a decision rather
    than a deletion so a later upload cannot silently re-guess it.
    """
    # Post the form the page actually rendered, with one field changed. Building
    # the payload by hand skipped the hidden orig__ fields, which meant these
    # tests exercised a request no browser can send.
    u_id = _upload_id_for_key(org, key)
    form = next((f for f in _tender_forms(
        client.get("/tenders").data.decode("utf-8", "replace"))
        if f.get("upload_id") == str(u_id)), None)
    data = dict(form) if form else {"upload_id": str(u_id)}
    if form is None:
        # No form rendered (a viewer, or corrections unavailable). Supply the
        # orig__ from stored state so the request still reaches the auth guard
        # instead of being killed early by the stale-form guard.
        current = next((m for m in db.get_tender_matches(org)
                        if m["tender_key"] == key), None)
        data["orig__" + key] = (current["inventory_item"] or "") if current else ""
    data["item__" + key] = value
    return client.post("/tenders/match", data=data, follow_redirects=True)


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


# ── 1. A guess is a guess, and it is always disclosed ────────────────────────
# The pairing is now made automatically at upload, so "adds nothing until
# confirmed" is no longer the contract. The contract is that whatever the guess
# produced is PRINTED beside the quantity, on both surfaces a person orders
# from. That source line is the only thing between a wrong guess and a wrong
# order, so it is what these tests defend.

_upload(alpha, TENDER_CSV)
_check("uploading pairs the row without anyone being asked",
       appmod._tender_addon_map("OrgAlpha").get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(appmod._tender_addon_map("OrgAlpha")))

r = alpha.get(f"/results/{SID_A}")
_html = r.data.decode("utf-8", "replace")
_source_block = _recommendation_source_text(_html, SAUCE)
_check("1a the results page names the tender line the quantity came from",
       T_SAUCE in _source_block,
       detail="source line missing from the recommendation card's audit block")
_check("1b the gold split renders", 'class="qty-tender"' in _html)

r = alpha.get(f"/results/{SID_A}/print")
_print = r.data.decode("utf-8", "replace")
_print_row = _printed_row_text(_print, SAUCE)
_check("1c the PRINTED sheet names the tender line too",
       "from: " + T_SAUCE in _print_row,
       detail="literal source line missing from the item's printed row")
_check("1d the printed sheet still spells out the split",
       "200 + 100 tender" in _print)
_check("1e the printed sheet does not rely on colour alone",
       "tender" in _print)

# A line nothing in the item list comes close to must pair with nothing and add
# nothing. Silence is the correct answer here: guessing anyway is what puts a
# quantity against the wrong product.
_upload(alpha, "Item Description,Tender Qty\nZZQQ UNMATCHABLE THING,44\n",
        filename="nomatch.csv")
_check("1f a line with no plausible item adds nothing",
       all(v["qty"] != 44.0 for v in appmod._tender_addon_map("OrgAlpha").values()),
       detail=str(appmod._tender_addon_map("OrgAlpha")))
_check("1g and it is not stored as a pairing at all",
       all(m["tender_key"] != normalise_match_key("ZZQQ UNMATCHABLE THING")
           for m in db.get_tender_matches("OrgAlpha")),
       detail=str(db.get_tender_matches("OrgAlpha")))


# ── 2. Org isolation on every surface ────────────────────────────────────────

_upload(bravo, TENDER_CSV)
_check("org B's own upload pairs for org B",
       appmod._tender_addon_map("OrgBravo").get(normalise_match_key(SAUCE), {}).get("qty") == 100.0,
       detail=str(appmod._tender_addon_map("OrgBravo")))

# Org A has its own identical sheet, so the isolation test cannot lean on
# "org A has nothing". Prove it by the SOURCE instead: org B's customer name
# must never appear on org A's pages.
_upload(bravo, f"Item Description,Tender Qty\n{T_MILK},70\n",
        customer="KESTREL BANQUET", filename="bravo_only.csv")
_check("org B's second sheet lands for org B",
       any(m["tender_key"] == K_MILK for m in db.get_tender_matches("OrgBravo")))
_check("org B's row never appears in org A's matches",
       all(m["tender_key"] != K_MILK for m in db.get_tender_matches("OrgAlpha")),
       detail=str(db.get_tender_matches("OrgAlpha")))

for _path, _label in ((f"/results/{SID_A}", "results page"),
                      (f"/results/{SID_A}/print", "printed sheet"),
                      (f"/results/{SID_A}/export.csv", "CSV")):
    _body = alpha.get(_path).data.decode("utf-8", "replace")
    _check(f"org B's customer never appears on org A's {_label}",
           "KESTREL BANQUET" not in _body)

# A cross-org POST naming org A's tender key must write nothing for org A.
_before = {m["tender_key"]: m["inventory_item"] for m in db.get_tender_matches("OrgAlpha")}
bravo.post("/tenders/match", data={
    "upload_id": str(_upload_id_for_key("OrgBravo", K_SAUCE)),
    "item__" + K_SAUCE: MILK, "org_name": "OrgAlpha"
}, follow_redirects=True)
_after = {m["tender_key"]: m["inventory_item"] for m in db.get_tender_matches("OrgAlpha")}
_check("a POST from org B changes nothing for org A", _before == _after,
       detail=f"{_before} -> {_after}")


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

def _pairs(org="OrgAlpha"):
    """Current pairings as {tender key: (item, who decided it)}."""
    return {m["tender_key"]: (m["inventory_item"], m["confirmed_by"])
            for m in db.get_tender_matches(org)}


_before = _pairs()
_confirm(viewer, K_SAUCE, MILK)
_check("a viewer POST changes no pairing", _pairs() == _before,
       detail=f"{_before} -> {_pairs()}")


# ── 5. A typed name that does not resolve stores nothing ─────────────────────
# Free text from a browser must never land in the database as an item name.

_before = _pairs()
_confirm(alpha, K_SAUCE, "TOTALLY MADE UP ITEM")
_check("an unresolvable typed item changes nothing", _pairs() == _before,
       detail=f"{_before} -> {_pairs()}")

_confirm(alpha, K_SAUCE, "x" * 400)
_check("an over-long typed item changes nothing", _pairs() == _before,
       detail=f"{_before} -> {_pairs()}")

# A typed name that DOES resolve is stored in the canonical spelling, so the
# rejections above are the guard rather than the route simply never saving.
db.save_tender_match("OrgAlpha", K_SAUCE, T_SAUCE, MILK,
                     normalise_match_key(MILK), appmod.TENDER_MATCH_AUTO)
_confirm(alpha, K_SAUCE, "padimas oyster  sauce 500ml")
_check("a resolvable typed item is stored in the canonical spelling",
       _pairs().get(K_SAUCE, ("", ""))[0] == SAUCE, detail=str(_pairs().get(K_SAUCE)))
_check("and a human correction is no longer marked as a guess",
       _pairs().get(K_SAUCE, ("", ""))[1] != appmod.TENDER_MATCH_AUTO,
       detail=str(_pairs().get(K_SAUCE)))


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
# decided (the correction form skips empty keys), yet it would still count
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
alpha.post("/tenders/match", data={
    "upload_id": str(_upload_id_for_key("OrgAlpha", K_SAUCE)), "item__": SAUCE
}, follow_redirects=True)
_check("11d an empty-key decision is never saved",
       all(m["tender_key"] for m in db.get_tender_matches("OrgAlpha")),
       detail=str(db.get_tender_matches("OrgAlpha")))

# Rows written BEFORE the ingest guard existed are still in the live database
# and cannot be re-parsed. The correction form skips an empty key, so counting
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
# Deliberately unlike any fixture item: this case is about the LENGTH cap, and
# a name the matcher can pair would add a phantom quantity to every add-on
# assertion below it.
_ok_name = "ZQXJV " * 24 + "WIDGET"            # under the cap
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


# ── 13. Clearing a pairing is a DECISION, not a deletion ─────────────────────
# This is the trap in an automatic pairing. If clearing simply deleted the row,
# the cleared state would be indistinguishable from one nobody has seen, and
# the next upload of the same sheet would re-guess it and silently put back the
# quantity the user just took out. That is a wrong number on a purchase order,
# arriving without anybody touching anything.

_upload(alpha, _sheet(T_OATS, 80), filename="oats.csv")
_check("13a uploading pairs the oats line on its own",
       appmod._tender_addon_map("OrgAlpha").get(normalise_match_key(OATS), {}).get("qty") == 80.0,
       detail=str(appmod._tender_addon_map("OrgAlpha")))

_confirm(alpha, K_OATS, "")
_check("13b clearing it stops it contributing",
       normalise_match_key(OATS) not in appmod._tender_addon_map("OrgAlpha"),
       detail=str(appmod._tender_addon_map("OrgAlpha")))
_check("13c the clearing is STORED as a decision, not deleted",
       any(m["tender_key"] == K_OATS and m["inventory_key"] == ""
           for m in db.get_tender_matches("OrgAlpha")),
       detail=str(db.get_tender_matches("OrgAlpha")))
_check("13d and it is attributed to the person, not to the guesser",
       all(m["confirmed_by"] != appmod.TENDER_MATCH_AUTO
           for m in db.get_tender_matches("OrgAlpha") if m["tender_key"] == K_OATS),
       detail=str(db.get_tender_matches("OrgAlpha")))

# The load-bearing one: re-upload the same sheet and the cleared line must
# stay cleared.
_upload(alpha, _sheet(T_OATS, 80), filename="oats2.csv")
_check("13e a re-upload does NOT silently re-guess a cleared line",
       normalise_match_key(OATS) not in appmod._tender_addon_map("OrgAlpha"),
       detail=str(appmod._tender_addon_map("OrgAlpha")))

# A decided row, either way, must not sit in the unmatched count nagging. The
# count is not zero here and should not be: earlier cases uploaded lines that
# genuinely pair with nothing, and those SHOULD be flagged. What matters is
# that a line somebody has ruled on is not among them, or the banner becomes a
# number that never goes down and stops being read.
_decided_keys = {m["tender_key"] for m in db.get_tender_matches("OrgAlpha")}
_live_keys = {r["match_key"] for r in db.get_tender_commitments("OrgAlpha")
              if r["period_end"] >= TODAY_ISO}
_check("13f the unmatched count is exactly the lines nobody has ruled on",
       db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO)
       == len([k for k in _live_keys if k and k not in _decided_keys]),
       detail=str(db.count_unmatched_tender_commitments("OrgAlpha", TODAY_ISO)))
_check("13g and the cleared line is not one of them", K_OATS in _decided_keys,
       detail=str(sorted(_decided_keys)))


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


# ── 20. Upload matching stops at 200 distinct tender keys ────────────────────
# The file and all 201 valid rows may be stored, but only the server-selected
# first 200 may reach fuzzy matching or acquire automatic mapping rows.

CAP_ORG = "OrgCap"
CAP_ITEM = "VANMARK WAREHOUSE ITEM 1KG"
CAP_ID = _make_user("cap@example.com", CAP_ORG)
cap = _client(CAP_ID, "cap@example.com", CAP_ORG)
_seed_session(CAP_ID, CAP_ORG, recs=[], inventory=[{
    "item": CAP_ITEM, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}])
_cap_names = [f"VANMARK CONTRACT LINE {n:03d} 1KG" for n in range(1, 202)]
_cap_csv = "Item Description,Tender Qty\n" + "".join(
    f"{name},{n}\n" for n, name in enumerate(_cap_names, 1))
_cap_seen = []
_orig_auto_match = tenders.auto_match


def _recording_auto_match(rows, index, *args, **kwargs):
    _cap_seen.extend(r["match_key"] for r in rows)
    return {r["match_key"]: CAP_ITEM for r in rows}


tenders.auto_match = _recording_auto_match
try:
    _upload(cap, _cap_csv, customer="VANMARK CATERING", filename="cap.csv")
finally:
    tenders.auto_match = _orig_auto_match

_cap_matches = {m["tender_key"] for m in db.get_tender_matches(CAP_ORG)}
_check("20a all 201 valid tender rows are stored",
       len(db.get_tender_commitments(CAP_ORG)) == 201,
       detail=str(len(db.get_tender_commitments(CAP_ORG))))
_check("20b auto_match receives exactly 200 distinct rows, not 201",
       len(_cap_seen) == 200 and len(set(_cap_seen)) == 200,
       detail=f"received {len(_cap_seen)} rows and {len(set(_cap_seen))} keys")
_check("20c the 201st key never gets an automatic mapping",
       normalise_match_key(_cap_names[200]) not in _cap_matches,
       detail=f"stored mappings: {len(_cap_matches)}")
_cap_upload = _upload_id_for_key(CAP_ORG, normalise_match_key(_cap_names[200]))
_cap_form = next(f for f in _tender_forms(
    cap.get("/tenders").data.decode("utf-8", "replace"))
    if f.get("upload_id") == str(_cap_upload))
_cap_fields = {k for k in _cap_form if k.startswith("item__")}
_check("20d the 201st key has no rendered correction input",
       len(_cap_fields) == 200
       and "item__" + normalise_match_key(_cap_names[200]) not in _cap_fields,
       detail=f"rendered {len(_cap_fields)} controls")
cap.post("/tenders/match", data={
    "upload_id": str(_cap_upload),
    # Carries the orig__ the page would have rendered for an unmapped key, so
    # the stale-form guard passes it through and the 200-row boundary is what
    # actually rejects it. Without this the request dies one branch earlier and
    # the test would stay green with the boundary removed.
    "orig__" + normalise_match_key(_cap_names[200]): "",
    "item__" + normalise_match_key(_cap_names[200]): CAP_ITEM,
}, follow_redirects=True)
_check("20e a forged 201st correction field is ignored",
       all(m["tender_key"] != normalise_match_key(_cap_names[200])
           for m in db.get_tender_matches(CAP_ORG)),
       detail="the unrendered boundary key acquired a mapping")


# ── 21. Posting one correction form unchanged is a complete no-op ───────────
# Parse the actual HTML form, including its hidden upload id and every named
# item input. This catches a browser echo being mistaken for a human decision.

NOOP_ORG = "OrgNoop"
NOOP_ID = _make_user("noop@example.com", NOOP_ORG)
noop = _client(NOOP_ID, "noop@example.com", NOOP_ORG)
_seed_session(NOOP_ID, NOOP_ORG, recs=[], inventory=[{
    "item": SAUCE, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}])
_noop_blank = "QXZV UNMATCHED CONTRACT LINE"
_noop_csv = ("Item Description,Tender Qty\n"
             f"{T_SAUCE},10\n{_noop_blank},20\n")
_orig_auto_match = tenders.auto_match


def _one_guess(rows, index, *args, **kwargs):
    return {K_SAUCE: SAUCE}


tenders.auto_match = _one_guess
try:
    _upload(noop, _noop_csv, customer="PADIMAS CATERING", filename="noop.csv")
finally:
    tenders.auto_match = _orig_auto_match

_noop_upload = _upload_id_for_key(NOOP_ORG, K_SAUCE)
_noop_page = noop.get("/tenders").data.decode("utf-8", "replace")
_noop_forms = _tender_forms(_noop_page)
_noop_form = next((f for f in _noop_forms
                   if f.get("upload_id") == str(_noop_upload)), None)
_blank_key = normalise_match_key(_noop_blank)
_check("21a the real correction form carries its upload id and both inputs",
       _noop_form is not None
       and _noop_form.get("item__" + K_SAUCE) == SAUCE
       and _noop_form.get("item__" + _blank_key) == "",
       detail=str(_noop_form))
if _noop_form is not None:
    noop.post("/tenders/match", data=dict(_noop_form), follow_redirects=True)
_noop_matches = {m["tender_key"]: m for m in db.get_tender_matches(NOOP_ORG)}
_check("21b posting the form exactly as rendered keeps the guess automatic",
       _noop_matches.get(K_SAUCE, {}).get("confirmed_by") == appmod.TENDER_MATCH_AUTO,
       detail=str(_noop_matches.get(K_SAUCE)))
_check("21c the untouched blank still has no mapping row",
       _blank_key not in _noop_matches, detail=str(_noop_matches.get(_blank_key)))


# ── 22. Upload forms cannot change each other's keys ─────────────────────────

CROSS_ORG = "OrgCrossUpload"
CROSS_ID = _make_user("cross@example.com", CROSS_ORG)
cross = _client(CROSS_ID, "cross@example.com", CROSS_ORG)
_seed_session(CROSS_ID, CROSS_ORG, recs=[], inventory=[{
    "item": SAUCE, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}, {
    "item": MILK, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}])
_cross_a = _seed_tender_upload(CROSS_ORG, [(T_SAUCE, 10)],
                                 filename="cross-a.csv")
_cross_b = _seed_tender_upload(CROSS_ORG, [(T_MILK, 20)],
                                 filename="cross-b.csv")
db.save_tender_match(CROSS_ORG, K_SAUCE, T_SAUCE, SAUCE,
                     normalise_match_key(SAUCE), appmod.TENDER_MATCH_AUTO)
db.save_tender_match(CROSS_ORG, K_MILK, T_MILK, MILK,
                     normalise_match_key(MILK), appmod.TENDER_MATCH_AUTO)
_cross_forms = _tender_forms(cross.get("/tenders").data.decode("utf-8", "replace"))
_cross_by_upload = {int(f["upload_id"]): f for f in _cross_forms
                    if f.get("upload_id", "").isdigit()}
_cross_a_fields = {k for k in _cross_by_upload.get(_cross_a, {})
                   if k.startswith("item__")}
_cross_b_fields = {k for k in _cross_by_upload.get(_cross_b, {})
                   if k.startswith("item__")}
_check("22a each upload renders its own bounded correction controls",
       _cross_a_fields == {"item__" + K_SAUCE}
       and _cross_b_fields == {"item__" + K_MILK},
       detail=f"A={_cross_a_fields}, B={_cross_b_fields}")
_before_b = next(m for m in db.get_tender_matches(CROSS_ORG)
                 if m["tender_key"] == K_MILK)
_forged = dict(_cross_by_upload[_cross_a])
# A forger inventing the item field invents the orig field too, and picks the
# value that gets furthest. The POST reads `saved` scoped to upload A, so K_MILK
# has no entry there and its `stored` is "". Supplying "" therefore clears the
# stale-form guard, and the per-upload row scoping is what actually rejects it.
# Supplying B's real rendered value instead would be stopped one branch earlier
# and this test would stay green with the scoping removed.
_forged["orig__" + K_MILK] = ""
_forged["item__" + K_MILK] = SAUCE
cross.post("/tenders/match", data=_forged, follow_redirects=True)
_after_b = next(m for m in db.get_tender_matches(CROSS_ORG)
                if m["tender_key"] == K_MILK)
_check("22b a forged upload B field posted through upload A is ignored",
       (_after_b["inventory_key"], _after_b["confirmed_by"])
       == (_before_b["inventory_key"], _before_b["confirmed_by"]),
       detail=f"{dict(_before_b)} -> {dict(_after_b)}")


# ── 23. A field after the rendered limit is ignored even when forged ────────

LIMIT_ORG = "OrgLimit"
LIMIT_ID = _make_user("limit@example.com", LIMIT_ORG)
limit_client = _client(LIMIT_ID, "limit@example.com", LIMIT_ORG)
_seed_session(LIMIT_ID, LIMIT_ORG, recs=[], inventory=[{
    "item": SAUCE, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}])
_limit_names = ["BROOKVALE ALPHA CONTRACT 1L",
                "BROOKVALE BRAVO CONTRACT 1L",
                "BROOKVALE CHARLIE CONTRACT 1L"]
_limit_upload = _seed_tender_upload(
    LIMIT_ORG, [(name, 10 + i) for i, name in enumerate(_limit_names)],
    filename="limit.csv")
_limit_extra_key = normalise_match_key(_limit_names[2])
_orig_match_rows = appmod.MAX_MATCH_ROWS
appmod.MAX_MATCH_ROWS = 2
try:
    _limit_forms = _tender_forms(
        limit_client.get("/tenders").data.decode("utf-8", "replace"))
    _limit_form = next(f for f in _limit_forms
                       if f.get("upload_id") == str(_limit_upload))
    _limit_fields = {k for k in _limit_form if k.startswith("item__")}
    limit_client.post("/tenders/match", data={
        "upload_id": str(_limit_upload),
        # Same reason as 20e: reach the row cap, not the stale-form guard.
        "orig__" + _limit_extra_key: "",
        "item__" + _limit_extra_key: SAUCE,
    }, follow_redirects=True)
finally:
    appmod.MAX_MATCH_ROWS = _orig_match_rows

_check("23a only the first two distinct keys render item inputs",
       len(_limit_fields) == 2 and "item__" + _limit_extra_key not in _limit_fields,
       detail=str(_limit_fields))
_check("23b a forged field for the unrendered third key is ignored",
       all(m["tender_key"] != _limit_extra_key
           for m in db.get_tender_matches(LIMIT_ORG)),
       detail=str(db.get_tender_matches(LIMIT_ORG)))


# ── 24. Orphan history cannot hide or overwrite a current human decision ────

PRESERVE_ORG = "OrgPreserve"
PRESERVE_ID = _make_user("preserve@example.com", PRESERVE_ORG)
preserve = _client(PRESERVE_ID, "preserve@example.com", PRESERVE_ORG)
_seed_session(PRESERVE_ID, PRESERVE_ORG, recs=[], inventory=[{
    "item": SAUCE, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}, {
    "item": MILK, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}])
_preserve_tender = "VANMARK LEGACY CONTRACT 500ML"
_preserve_key = normalise_match_key(_preserve_tender)
for _orphan_key in ("aaa orphan mapping", "aab orphan mapping"):
    db.save_tender_match(PRESERVE_ORG, _orphan_key, _orphan_key, SAUCE,
                         normalise_match_key(SAUCE), "preserve@example.com")
_seed_tender_upload(PRESERVE_ORG, [(_preserve_tender, 15)],
                    filename="preserve-old.csv")
db.save_tender_match(PRESERVE_ORG, _preserve_key, _preserve_tender, MILK,
                     normalise_match_key(MILK), "preserve@example.com")
_historical_first = db.get_tender_matches(PRESERVE_ORG, limit=2)
_current_tight = db.get_current_tender_matches(PRESERVE_ORG, limit=1)
_check("24a orphan mappings really sort ahead in the historical read",
       len(_historical_first) == 2
       and all(m["tender_key"].startswith("aa") for m in _historical_first),
       detail=str(_historical_first))
_check("24b a tight current-only read still returns the human mapping",
       len(_current_tight) == 1
       and _current_tight[0]["tender_key"] == _preserve_key
       and _current_tight[0]["confirmed_by"] == "preserve@example.com",
       detail=str(_current_tight))

_orig_auto_match = tenders.auto_match
_orig_save_match = db.save_tender_match
_orig_tender_limit = appmod.MAX_TENDER_ROWS_PER_ORG
_preserve_save_calls = []


def _wrong_reupload_guess(rows, index, *args, **kwargs):
    return {_preserve_key: SAUCE}


def _record_preserve_save(*args, **kwargs):
    _preserve_save_calls.append(args)
    return _orig_save_match(*args, **kwargs)


tenders.auto_match = _wrong_reupload_guess
db.save_tender_match = _record_preserve_save
# One existing row plus this re-upload fits exactly. A historical read capped
# at two would contain only the two orphans and miss the human mapping.
appmod.MAX_TENDER_ROWS_PER_ORG = 2
try:
    _preserve_response = _upload(
        preserve, _sheet(_preserve_tender, 15),
        customer="VANMARK CATERING", filename="preserve-new.csv")
finally:
    tenders.auto_match = _orig_auto_match
    db.save_tender_match = _orig_save_match
    appmod.MAX_TENDER_ROWS_PER_ORG = _orig_tender_limit

_preserved = next(m for m in db.get_tender_matches(PRESERVE_ORG)
                  if m["tender_key"] == _preserve_key)
_check("24c re-upload performs no write over a preserved human decision",
       _preserve_save_calls == [], detail=str(_preserve_save_calls))
_check("24d the human item and attribution survive the wrong fresh guess",
       _preserved["inventory_key"] == normalise_match_key(MILK)
       and _preserved["confirmed_by"] == "preserve@example.com",
       detail=str(dict(_preserved)))
_check("24e the flash counts that preserved human mapping as paired",
       b"1 of 1 distinct tender item name has a stored pairing" in _preserve_response.data,
       detail=_preserve_response.data.decode("utf-8", "replace")[:300])


# ── 25. Partial automatic saves are reported from persisted state ───────────

PARTIAL_ORG = "OrgPartial"
PARTIAL_ID = _make_user("partial@example.com", PARTIAL_ORG)
partial = _client(PARTIAL_ID, "partial@example.com", PARTIAL_ORG)
_seed_session(PARTIAL_ID, PARTIAL_ORG, recs=[], inventory=[{
    "item": SAUCE, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}, {
    "item": MILK, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}])
_partial_one = "BROOKVALE PARTIAL CONTRACT 1L"
_partial_two = "PADIMAS PARTIAL CONTRACT 500ML"
_partial_keys = {normalise_match_key(_partial_one), normalise_match_key(_partial_two)}
_orig_auto_match = tenders.auto_match
_orig_save_match = db.save_tender_match
_partial_calls = []


def _two_guesses(rows, index, *args, **kwargs):
    return {r["match_key"]: (MILK if "BROOKVALE" in r["item_name"] else SAUCE)
            for r in rows}


def _fail_second_save(*args, **kwargs):
    _partial_calls.append(args)
    if len(_partial_calls) == 2:
        raise RuntimeError("injected second-save failure")
    return _orig_save_match(*args, **kwargs)


tenders.auto_match = _two_guesses
db.save_tender_match = _fail_second_save
try:
    _partial_response = _upload(
        partial,
        ("Item Description,Tender Qty\n"
         f"{_partial_one},10\n{_partial_two},20\n"),
        customer="BROOKVALE CATERING", filename="partial.csv")
finally:
    tenders.auto_match = _orig_auto_match
    db.save_tender_match = _orig_save_match

_partial_current = db.get_current_tender_matches(
    PARTIAL_ORG, limit=appmod.MAX_TENDER_ROWS_PER_ORG)
_check("25a the injected failure occurs after exactly one committed save",
       len(_partial_calls) == 2 and len(_partial_current) == 1
       and _partial_current[0]["tender_key"] in _partial_keys,
       detail=f"calls={len(_partial_calls)}, current={_partial_current}")
_check("25b the flash reports one of two paired from persisted state",
       b"1 of 2 distinct tender item names has a stored pairing" in _partial_response.data
       and b"1 name is unpaired or cleared and adds nothing" in _partial_response.data,
       detail=_partial_response.data.decode("utf-8", "replace")[:400])


# ── 26. Duplicate commitment rows count one distinct paired key ─────────────

DUPCOUNT_ORG = "OrgDuplicateCount"
DUPCOUNT_ID = _make_user("dupcount@example.com", DUPCOUNT_ORG)
dupcount = _client(DUPCOUNT_ID, "dupcount@example.com", DUPCOUNT_ORG)
_seed_session(DUPCOUNT_ID, DUPCOUNT_ORG, recs=[], inventory=[{
    "item": SAUCE, "status": "LOW", "spoilage_risk": "NONE",
    "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
    "observation": "low",
}])
_orig_auto_match = tenders.auto_match
tenders.auto_match = _one_guess
try:
    _dupcount_response = _upload(
        dupcount,
        ("Item Description,Tender Qty\n"
         f"{T_SAUCE},10\n{T_SAUCE},20\n"),
        customer="PADIMAS CATERING", filename="duplicate-count.csv")
finally:
    tenders.auto_match = _orig_auto_match

_check("26a both duplicate commitment rows are stored, so the count test is live",
       len(db.get_tender_commitments(DUPCOUNT_ORG)) == 2,
       detail=str(len(db.get_tender_commitments(DUPCOUNT_ORG))))
_check("26b the flash reports one of one distinct tender item name paired",
       b"1 of 1 distinct tender item name has a stored pairing" in _dupcount_response.data,
       detail=_dupcount_response.data.decode("utf-8", "replace")[:300])
_check("26c the duplicate row is not falsely reported as unmatched",
       b"unpaired or cleared" not in _dupcount_response.data,
       detail=_dupcount_response.data.decode("utf-8", "replace")[:300])


# ── 27. Every contributing raw source survives all three order surfaces ──────

SOURCE_ORG = "OrgSources"
SOURCE_ID = _make_user("sources@example.com", SOURCE_ORG)
sources_client = _client(SOURCE_ID, "sources@example.com", SOURCE_ORG)
SOURCE_SID = _seed_session(SOURCE_ID, SOURCE_ORG,
                           recs=[_rec(SAUCE, "200 CTN")],
                           inventory=[INVENTORY[0]])
_source_one = "PADIMAS SAUCE CONTRACT 500ML"
_source_two = "VANMARK SAUCE RESERVE 500ML"
_seed_tender_upload(SOURCE_ORG, [(_source_one, 30), (_source_two, 70)],
                    customer="KESTREL CATERING", filename="sources.csv")
for _source in (_source_one, _source_two):
    db.save_tender_match(SOURCE_ORG, normalise_match_key(_source), _source,
                         SAUCE, normalise_match_key(SAUCE), appmod.TENDER_MATCH_AUTO)

_source_html = sources_client.get(
    f"/results/{SOURCE_SID}").data.decode("utf-8", "replace")
_source_card = _recommendation_source_text(_source_html, SAUCE)
_check("27a both raw sources occur inside the recommendation card audit block",
       all(source in _source_card for source in (_source_one, _source_two)),
       detail=_source_card)

_source_print = sources_client.get(
    f"/results/{SOURCE_SID}/print").data.decode("utf-8", "replace")
_source_print_row = _printed_row_text(_source_print, SAUCE)
_check("27b the printed item row retains a literal from line for every source",
       all("from: " + source in _source_print_row
           for source in (_source_one, _source_two))
       and _source_print_row.count("from:") == 2,
       detail=_source_print_row)

_source_csv = list(csv.DictReader(io.StringIO(sources_client.get(
    f"/results/{SOURCE_SID}/export.csv").data.decode("utf-8", "replace"))))
_source_csv_row = next((row for row in _source_csv if row.get("Item") == SAUCE), {})
_source_csv_cell = _source_csv_row.get("Tender Source", "")
_check("27c the CSV Tender Source cell contains every raw source",
       all(source in _source_csv_cell for source in (_source_one, _source_two)),
       detail=_source_csv_cell)


# ── 28. A formula-shaped tender source is neutralised in the CSV ─────────────

FORMULA_ORG = "OrgFormulaSource"
FORMULA_ID = _make_user("formula-source@example.com", FORMULA_ORG)
formula_client = _client(FORMULA_ID, "formula-source@example.com", FORMULA_ORG)
FORMULA_SID = _seed_session(FORMULA_ID, FORMULA_ORG,
                            recs=[_rec(SAUCE, "200 CTN")],
                            inventory=[INVENTORY[0]])
_formula_source = "=2+3 KESTREL SAUCE CONTRACT"
_seed_tender_upload(FORMULA_ORG, [(_formula_source, 25)],
                    customer="KESTREL CATERING", filename="formula-source.csv")
db.save_tender_match(FORMULA_ORG, normalise_match_key(_formula_source),
                     _formula_source, SAUCE, normalise_match_key(SAUCE),
                     appmod.TENDER_MATCH_AUTO)
_formula_rows = list(csv.DictReader(io.StringIO(formula_client.get(
    f"/results/{FORMULA_SID}/export.csv").data.decode("utf-8", "replace"))))
_formula_cell = next(row for row in _formula_rows
                     if row.get("Item") == SAUCE).get("Tender Source", "")
_check("28 the CSV source cell neutralises a formula-shaped tender line",
       _formula_cell.startswith("'=") and not _formula_cell.startswith("="),
       detail=_formula_cell)


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
_check("18a the degenerate name is dropped from the item universe at the source",
       "-----" not in appmod._org_item_names("OrgAlpha"),
       detail=str(appmod._org_item_names("OrgAlpha")))
_check("18a2 the real item beside it survives, so the filter is not too greedy",
       SAUCE in appmod._org_item_names("OrgAlpha"),
       detail=str(appmod._org_item_names("OrgAlpha")))
# The automatic pairing reads the same list, so it cannot pick the spacer and
# write the "adds nothing" sentinel while the page reports a match.
_check("18a3 the pairing index cannot offer the spacer either",
       all(c["name"] != "-----" for c in tenders.propose_matches(
           "-----", tenders.build_match_index(appmod._org_item_names("OrgAlpha")),
           min_score=0.0)))

db.delete_tender_match("OrgAlpha", K_SAUCE)
alpha.post("/tenders/match", data={
    "upload_id": str(_upload_id_for_key("OrgAlpha", K_SAUCE)),
    "item__" + K_SAUCE: "***"},
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
_raw = [str(e.get("item") or "").strip()
        for e in json.loads(db.query(
            "SELECT inventory_report FROM analysis_results WHERE session_id=?",
            (SID_DEGEN,))[0]["inventory_report"])]
_check("18d the spacer IS in the raw report, so the filter is the guard and "
       "not a coincidence", "-----" in _raw, detail=str(_raw))


# ── 29. A mapping that appears between render and submit is not clobbered ────
# The no-op check compares what was typed against the value stored NOW. If a
# pairing lands after the page was built (a re-upload, or the org's first run
# completing so auto_match can finally fire), an untouched blank box is a
# different value from the fresh mapping. Without the rendered-value guard the
# route reads that as a deliberate clear and writes the permanent "we do not
# stock this" sentinel under the name of someone who never touched the row.

RACE_ORG = "OrgRace"
RACE_ID = _make_user("race@example.com", RACE_ORG)
race = _client(RACE_ID, "race@example.com", RACE_ORG)
_seed_session(RACE_ID, RACE_ORG, recs=[], inventory=[
    {"item": MILK, "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
     "observation": "low"},
    {"item": SAUCE, "status": "LOW", "spoilage_risk": "NONE",
     "days_of_supply": 10, "category": "DRY", "stock": "2 CTN",
     "observation": "low"},
])


def _no_guess(rows, index, *args, **kwargs):
    return {}


tenders.auto_match = _no_guess
try:
    _upload(race, "Item Description,Tender Qty\n" f"{T_MILK},40\n",
            customer="NORDVIK CATERING", filename="race.csv")
finally:
    tenders.auto_match = _orig_auto_match

_race_upload = _upload_id_for_key(RACE_ORG, K_MILK)
_race_form = next((f for f in _tender_forms(race.get("/tenders").data.decode(
    "utf-8", "replace")) if f.get("upload_id") == str(_race_upload)), None)
_check("29a the unmatched row renders blank and carries what it rendered",
       _race_form is not None
       and _race_form.get("item__" + K_MILK) == ""
       and _race_form.get("orig__" + K_MILK) == "",
       detail=str(_race_form))

# The race. A pairing lands after that page was built and before it is posted.
_MILK_KEY = normalise_match_key(MILK)
db.save_tender_match(RACE_ORG, K_MILK, T_MILK, MILK, _MILK_KEY,
                     appmod.TENDER_MATCH_AUTO)
_race_resp = None
if _race_form is not None:
    _race_resp = race.post("/tenders/match", data=dict(_race_form),
                           follow_redirects=True)
_race_saved = {m["tender_key"]: m for m in db.get_tender_matches(RACE_ORG)}
_check("29b a stale blank does not clear a mapping that appeared after render",
       _race_saved.get(K_MILK, {}).get("inventory_key") == _MILK_KEY,
       detail=str(_race_saved.get(K_MILK)))
_check("29c and nobody is credited with a decision they never made",
       (_race_saved.get(K_MILK, {}).get("confirmed_by") or "")
       == appmod.TENDER_MATCH_AUTO,
       detail=str(_race_saved.get(K_MILK)))
# Skipping is the safe outcome, but a SILENT skip is its own bug: the user
# clicks Save, the page reloads, and nothing tells them the edit was dropped.
# Without this check the flash could be deleted and the suite would stay green.
_check("29f the user is told the page was out of date, not left guessing",
       _race_resp is not None
       and "out of date" in _race_resp.data.decode("utf-8", "replace"),
       detail="no stale-form flash rendered on the reloaded page")

# Mirror case: the lost update. A colleague corrects the row, then the stale tab
# posts a value of its own. It must not win, and it must not take the credit.
db.save_tender_match(RACE_ORG, K_MILK, T_MILK, MILK, _MILK_KEY,
                     "colleague@example.com")
_stale = dict(_race_form or {})
_stale["item__" + K_MILK] = SAUCE
race.post("/tenders/match", data=_stale, follow_redirects=True)
_race_after = {m["tender_key"]: m for m in db.get_tender_matches(RACE_ORG)}
_check("29d a stale tab cannot overwrite a colleague's newer correction",
       _race_after.get(K_MILK, {}).get("inventory_key") == _MILK_KEY
       and (_race_after.get(K_MILK, {}).get("confirmed_by") or "")
       == "colleague@example.com",
       detail=str(_race_after.get(K_MILK)))

# The guard must not seize up the normal path: posting against what the page
# actually showed still works. Without this, deleting the whole correction
# feature would pass 29a to 29d.
_fresh_form = next((f for f in _tender_forms(race.get("/tenders").data.decode(
    "utf-8", "replace")) if f.get("upload_id") == str(_race_upload)), None)
if _fresh_form is not None:
    _fresh_form["item__" + K_MILK] = SAUCE
    race.post("/tenders/match", data=dict(_fresh_form), follow_redirects=True)
_race_final = {m["tender_key"]: m for m in db.get_tender_matches(RACE_ORG)}
_check("29e a correction posted against the current render still saves",
       _race_final.get(K_MILK, {}).get("inventory_key")
       == normalise_match_key(SAUCE)
       and (_race_final.get(K_MILK, {}).get("confirmed_by") or "")
       == "race@example.com",
       detail=str(_race_final.get(K_MILK)))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll tender match break tests passed.")
