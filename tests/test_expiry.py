"""The lot-tracking export, read and ranked earliest-expiry-first.

Proves the happy path of the /expiry page: the real export's 22 columns are
mapped to the right seven fields, its interleaved subtotal lines are skipped
and counted, every row read is accounted for, and the page ranks what is left
by expiry date.

Three things are easy to get silently wrong and all three are pinned here:
  1. the column names nearly collide -- "Location Code" would steal the item
     code, "Original Receipt Date" would steal the expiry, "Qty Selected"
     would steal the quantity. A wrong number here tells staff to throw away
     good stock.
  2. the export interleaves "Inventory Total : <code>" and "Total (CARTON)"
     rows through the data. They are report noise, but a real product can be
     called TOTAL PROTEIN MIX 5KG and must survive.
  3. the counters have to add up: rows read == summary lines skipped + lots
     stored + rows rejected. Silent loss is the one outcome this page cannot
     have.

Fixtures are invented brands only. Run: python tests/test_expiry.py
"""
import datetime
import io
import os
import sys
import tempfile
import types
import zipfile
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_expiry.db")
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
import expiry                                           # noqa: E402
import app as appmod                                    # noqa: E402
from agents.shared import normalise_match_key           # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
flask_app = appmod.app

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
TODAY = datetime.date.today()

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _col_letter(ci):
    """0 -> A, 25 -> Z, 26 -> AA. The real export is 22 columns wide, so this
    only has to survive past Z if someone widens the fixture."""
    letters = ""
    while True:
        letters = chr(ord("A") + ci % 26) + letters
        ci = ci // 26 - 1
        if ci < 0:
            return letters


def _sheet_xml(rows):
    cells_xml = []
    for ri, row in enumerate(rows, 1):
        cs = []
        for ci, val in enumerate(row):
            if val is None or str(val) == "":
                continue
            # The real report's title block contains "&", which breaks raw XML.
            cs.append(f'<c r="{_col_letter(ci)}{ri}" t="inlineStr">'
                      f'<is><t>{escape(str(val))}</t></is></c>')
        cells_xml.append(f'<row r="{ri}">' + "".join(cs) + "</row>")
    return (f'<worksheet xmlns="{NS}"><sheetData>'
            + "".join(cells_xml) + "</sheetData></worksheet>")


def make_xlsx(rows):
    path = tempfile.mktemp(suffix=".xlsx")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))
    return path


# ── Column detection against the real export's 22 headers ────────────────────
# Asserted twice. The ingest layer sanitises every header before the app sees
# it ("Qty On Hand" arrives as qty_on_hand), and _norm_header collapses both
# forms to the same string -- so the raw form documents the export and the
# sanitised form is what production actually runs on.

RAW = ["Location Code", "Location Name", "Inventory Code", "Inventory Description",
       "UOM", "Lot No.", "Lot Reference No.", "Original Receipt Date",
       "Expiry Date", "Delivery Remarks", "Special Remarks", "Damage Remarks",
       "Condition Remarks", "Country of Origin", "Supplier Name",
       "Source Voucher No.", "Pack Size", "Qty On Hand", "Qty Selected",
       "Qty Allocated", "Qty Allocated Selected", "Qty Available"]
SAN = ["location_code", "location_name", "inventory_code", "inventory_description",
       "uom", "lot_no", "lot_reference_no", "original_receipt_date",
       "expiry_date", "delivery_remarks", "special_remarks", "damage_remarks",
       "condition_remarks", "country_of_origin", "supplier_name",
       "source_voucher_no", "pack_size", "qty_on_hand", "qty_selected",
       "qty_allocated", "qty_allocated_selected", "qty_available"]

for _label, _headers, _want in (
        ("raw", RAW, {"expiry": "Expiry Date", "qty_available": "Qty Available",
                      "qty_on_hand": "Qty On Hand", "lot_no": "Lot No.",
                      "item_code": "Inventory Code",
                      "item_name": "Inventory Description", "uom": "UOM"}),
        ("sanitised", SAN, {"expiry": "expiry_date", "qty_available": "qty_available",
                            "qty_on_hand": "qty_on_hand", "lot_no": "lot_no",
                            "item_code": "inventory_code",
                            "item_name": "inventory_description", "uom": "uom"})):
    _map, _miss = expiry.detect_columns(_headers)
    _check(f"{_label}: expiry is the expiry date, NOT the receipt date",
           _map.get("expiry") == _want["expiry"], detail=str(_map.get("expiry")))
    _check(f"{_label}: item_code is the inventory code, NOT the location code",
           _map.get("item_code") == _want["item_code"], detail=str(_map.get("item_code")))
    _check(f"{_label}: item_name is the inventory description, NOT a location "
           f"or supplier name",
           _map.get("item_name") == _want["item_name"], detail=str(_map.get("item_name")))
    _check(f"{_label}: qty_available is Qty Available, NOT an allocated column",
           _map.get("qty_available") == _want["qty_available"],
           detail=str(_map.get("qty_available")))
    _check(f"{_label}: qty_on_hand is Qty On Hand, NOT Qty Selected",
           _map.get("qty_on_hand") == _want["qty_on_hand"],
           detail=str(_map.get("qty_on_hand")))
    _check(f"{_label}: lot_no is the lot number, NOT the lot reference",
           _map.get("lot_no") == _want["lot_no"], detail=str(_map.get("lot_no")))
    _check(f"{_label}: uom is claimed", _map.get("uom") == _want["uom"],
           detail=str(_map.get("uom")))
    _check(f"{_label}: nothing required is missing", _miss == [], detail=str(_miss))
    _unclaimed = [h for i, h in enumerate(_headers) if i in (9, 10, 11, 12, 13, 15, 16)]
    _check(f"{_label}: no field claims pack size, voucher no, country or remarks",
           not any(h in _map.values() for h in _unclaimed), detail=str(_map))

_map, _miss = expiry.detect_columns(["Item", "Expiry Date", "Qty"])
_check("a minimal sheet maps and reports nothing missing",
       _miss == [] and _map.get("expiry") == "Expiry Date"
       and _map.get("qty_on_hand") == "Qty" and _map.get("item_name") == "Item",
       detail=str((_map, _miss)))

_map, _miss = expiry.detect_columns(["Item Code", "Expiry", "Qty On Hand"])
_check("a code-only sheet maps too: the code becomes the label",
       _miss == [] and _map.get("item_code") == "Item Code"
       and "item_name" not in _map, detail=str((_map, _miss)))


# ── Summary-row skip ─────────────────────────────────────────────────────────

_check("the colon form is a total",
       expiry.is_summary_value("Inventory Total : BRK-001"))
_check("a row with no identifier at all is report noise",
       expiry.is_summary_value("") and expiry.is_summary_value(None))
_check("the carton total line is a total",
       expiry.is_summary_value("Total (CARTON)"))
_check("a bare GRAND TOTAL label is a total",
       expiry.is_summary_value("GRAND TOTAL"))
_check("a real product called TOTAL PROTEIN MIX 5KG survives",
       expiry.is_summary_value("TOTAL PROTEIN MIX 5KG") is False)
_check("a product coded TOTAL-500 survives",
       expiry.is_summary_value("TOTAL-500") is False)


# ── Quantity parsing ─────────────────────────────────────────────────────────

_check("'486.0' reads as 486", expiry.parse_qty("486.0") == 486.0)
_check("a thousands separator survives", expiry.parse_qty("8,031") == 8031.0)
_check("surrounding spaces survive", expiry.parse_qty(" 40.0 ") == 40.0)
_check("zero is kept -- a fully allocated lot still has an expiry date",
       expiry.parse_qty("0") == 0.0, detail=str(expiry.parse_qty("0")))
_check("'LOOSE' is not a quantity", expiry.parse_qty("LOOSE") is None)
_check("an empty cell is not a quantity", expiry.parse_qty("") is None)
_check("a missing cell is not a quantity", expiry.parse_qty(None) is None)
_check("negative stock is refused", expiry.parse_qty("-5") is None)
_check("NaN is refused before it can reach SQLite", expiry.parse_qty("nan") is None)
_check("an overflowing literal is refused", expiry.parse_qty("1e400") is None)


# ── Date parsing on this path ────────────────────────────────────────────────

_check("an Excel serial date parses to the right day",
       expiry.parse_date("46100") == datetime.date(2026, 3, 19),
       detail=str(expiry.parse_date("46100")))
_check("an ISO date parses", expiry.parse_date("2026-11-30") == datetime.date(2026, 11, 30))
_check("a blank expiry cell parses to nothing", expiry.parse_date("") is None)


# ── build_lots and the accounting invariant ──────────────────────────────────

MAPPING = {"expiry": "expiry_date", "qty_available": "qty_available",
           "qty_on_hand": "qty_on_hand", "lot_no": "lot_no",
           "item_code": "inventory_code", "item_name": "inventory_description",
           "uom": "uom"}

RECORDS = [
    {"inventory_code": "BRK-001", "inventory_description": "BROOKVALE UHT MILK 1L",
     "uom": "CTN", "lot_no": "LOT-A", "expiry_date": "2026-11-30",
     "qty_on_hand": "100.0", "qty_available": "40.0"},
    {"inventory_code": "BRK-001", "inventory_description": "BROOKVALE UHT MILK 1L",
     "uom": "CTN", "lot_no": "LOT-B", "expiry_date": "2027-01-15",
     "qty_on_hand": "60.0", "qty_available": "25.0"},
    {"inventory_code": "Inventory Total : BRK-001", "qty_selected": "8517.0"},
    {"pack_size": "Total (CARTON)", "qty_on_hand": "LOOSE"},
    {"inventory_code": "NRD-002", "inventory_description": "NORDVIK SALMON 1KG",
     "uom": "KG", "lot_no": "LOT-N", "expiry_date": "",
     "qty_on_hand": "12.0", "qty_available": "12.0"},
    {"inventory_code": "KES-003", "inventory_description": "KESTREL ORANGE JUICE 1L",
     "uom": "CTN", "lot_no": "LOT-K", "expiry_date": "2026-12-01",
     "qty_on_hand": "LOOSE", "qty_available": ""},
]

_lots, _rejects, _stats = expiry.build_lots(RECORDS, MAPPING)
_check("two readable lots are built", len(_lots) == 2, detail=str(len(_lots)))
_check("both summary lines are counted, not rejected",
       _stats["summary"] == 2, detail=str(_stats))
_check("the row with no expiry date is counted separately",
       _stats["no_expiry"] == 1, detail=str(_stats))
_check("two rows are rejected with reasons", len(_rejects) == 2, detail=str(_rejects))
_check("every row read is accounted for",
       _stats["read"] == _stats["summary"] + len(_lots) + len(_rejects),
       detail=str((_stats, len(_lots), len(_rejects))))
_reasons = [r["reason"] for r in _rejects]
_check("the reject reasons read in plain English",
       "no expiry date" in _reasons
       and "quantity is missing or not a number" in _reasons, detail=str(_reasons))
_check("match_key comes from normalise_match_key",
       _lots[0]["match_key"] == normalise_match_key("BROOKVALE UHT MILK 1L"),
       detail=_lots[0]["match_key"])
_check("expiry_date is stored ISO, so sorting it sorts chronologically",
       [x["expiry_date"] for x in _lots] == ["2026-11-30", "2027-01-15"],
       detail=str([x["expiry_date"] for x in _lots]))
_check("the two lots of one item stay two rows, never merged",
       [x["lot_no"] for x in _lots] == ["LOT-A", "LOT-B"],
       detail=str([x["lot_no"] for x in _lots]))


# ── days_remaining ───────────────────────────────────────────────────────────

_check("a past expiry is negative",
       expiry.days_remaining("2026-01-01", datetime.date(2026, 1, 11)) == -10,
       detail=str(expiry.days_remaining("2026-01-01", datetime.date(2026, 1, 11))))
_check("today is zero",
       expiry.days_remaining("2026-01-11", datetime.date(2026, 1, 11)) == 0)
_check("a future expiry is positive",
       expiry.days_remaining("2026-02-01", datetime.date(2026, 1, 11)) == 21,
       detail=str(expiry.days_remaining("2026-02-01", datetime.date(2026, 1, 11))))
_check("counting crosses a month boundary correctly",
       expiry.days_remaining("2026-03-01", datetime.date(2026, 1, 31)) == 29,
       detail=str(expiry.days_remaining("2026-03-01", datetime.date(2026, 1, 31))))


# ── Storage round trip ───────────────────────────────────────────────────────

STORE_ORG = "OrgExpiryStore"
_uid = db.create_expiry_upload(STORE_ORG, "lots.xlsx", "store@example.com")
db.save_expiry_lots(STORE_ORG, _uid, _lots + [
    # on-hand only: the page must still rank it, without inventing an
    # availability figure.
    {"item_code": "PDM-004", "item_name": "PADIMAS BEEF 2KG",
     "match_key": normalise_match_key("PADIMAS BEEF 2KG"), "lot_no": "LOT-P",
     "uom": "CTN", "expiry_date": "2026-10-01",
     "qty_on_hand": 9.0, "qty_available": None},
    # nothing left on the lot: stored, but never shown.
    {"item_code": "VNM-005", "item_name": "VANMARK PRAWN 1KG",
     "match_key": normalise_match_key("VANMARK PRAWN 1KG"), "lot_no": "LOT-V",
     "uom": "KG", "expiry_date": "2026-10-02",
     "qty_on_hand": 0.0, "qty_available": 0.0},
])
db.finalise_expiry_upload(STORE_ORG, _uid, len(_lots), len(_rejects),
                          _stats["summary"], "[]")

_stored = db.get_expiry_lots(STORE_ORG, _uid, "2099-01-01", 100)
_check("stored lots come back earliest-expiry first",
       [r["expiry_date"] for r in _stored] ==
       ["2026-10-01", "2026-11-30", "2027-01-15"],
       detail=str([r["expiry_date"] for r in _stored]))
_check("a lot with only an on-hand figure still ranks (the COALESCE)",
       any(r["lot_no"] == "LOT-P" for r in _stored),
       detail=str([r["lot_no"] for r in _stored]))
_check("a lot with nothing left on it is stored but not shown",
       db.count_expiry_lots(STORE_ORG) == 4
       and not any(r["lot_no"] == "LOT-V" for r in _stored),
       detail=str((db.count_expiry_lots(STORE_ORG), [r["lot_no"] for r in _stored])))
_upload_row = db.get_expiry_uploads(STORE_ORG, 10)[0]
_check("all three counters are stored on the upload row",
       (_upload_row["rows_imported"], _upload_row["rows_rejected"],
        _upload_row["rows_skipped"]) == (2, 2, 2), detail=str(dict(_upload_row)))
_check("the cutoff is respected",
       [r["lot_no"] for r in db.get_expiry_lots(STORE_ORG, _uid, "2026-11-30", 100)]
       == ["LOT-P", "LOT-A"],
       detail=str(db.get_expiry_lots(STORE_ORG, _uid, "2026-11-30", 100)))


# ── End to end, the real shape ───────────────────────────────────────────────

def _make_user(email, org):
    db.execute("INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
               (email, generate_password_hash("x"), org, "claude-sonnet-5"))
    return db.query("SELECT id FROM users WHERE email=?", (email,))[0]["id"]


def _client(user_id, email, org):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["user_id"]  = user_id
        s["email"]    = email
        s["org_name"] = org
        s["model"]    = "claude-sonnet-5"
        s["is_admin"] = False
        s["role"]     = "admin"
        s["sv"]       = 0
    return c


def _row(**cells):
    """One 22-wide export row, filled by sanitised field name."""
    return [cells.get(name) for name in SAN]


TITLE_ROWS = [
    ["Printed By:", "warehouse@example.test"],
    ["Printed Date:", TODAY.isoformat()],
    ["BROOKVALE DISTRIBUTION PTE LTD"],
    ["Lot Tracking Stock Status"],
    ["Report Parameter & Filter:"],          # the & that breaks raw XML
    ["Location: ALL / Expiry: ALL"],
]

EXPIRED_ON = (TODAY - datetime.timedelta(days=10)).isoformat()
SOON_ON    = (TODAY + datetime.timedelta(days=10)).isoformat()
LATER_ON   = (TODAY + datetime.timedelta(days=60)).isoformat()

SHEET = TITLE_ROWS + [RAW] + [
    _row(location_code="W1", location_name="Main Store", inventory_code="BRK-001",
         inventory_description="BROOKVALE UHT MILK 1L", uom="CTN", lot_no="LOT-A",
         lot_reference_no="REF-A", original_receipt_date="2026-01-05",
         expiry_date=EXPIRED_ON, supplier_name="BROOKVALE DAIRY",
         pack_size="12x1L", qty_on_hand="100.0", qty_selected="0.0",
         qty_allocated="60.0", qty_allocated_selected="0.0", qty_available="40.0"),
    _row(location_code="W1", location_name="Main Store", inventory_code="BRK-001",
         inventory_description="BROOKVALE UHT MILK 1L", uom="CTN", lot_no="LOT-B",
         lot_reference_no="REF-B", original_receipt_date="2026-02-05",
         expiry_date=LATER_ON, supplier_name="BROOKVALE DAIRY",
         pack_size="12x1L", qty_on_hand="60.0", qty_selected="0.0",
         qty_allocated="35.0", qty_allocated_selected="0.0", qty_available="25.0"),
    _row(inventory_code="Inventory Total : BRK-001", qty_selected="8517.0"),
    _row(pack_size="Total (CARTON)", qty_on_hand="LOOSE"),
    _row(location_code="W2", location_name="Cold Room", inventory_code="NRD-002",
         inventory_description="NORDVIK SALMON 1KG", uom="KG", lot_no="LOT-N",
         original_receipt_date="2026-03-01", expiry_date="",
         qty_on_hand="12.0", qty_available="12.0"),
    _row(location_code="W2", location_name="Cold Room", inventory_code="KES-003",
         inventory_description="KESTREL ORANGE JUICE 1L", uom="CTN", lot_no="LOT-K",
         original_receipt_date="2026-04-01", expiry_date=SOON_ON,
         qty_on_hand="30.0", qty_available="8.0"),
]

ORG = "OrgExpiry"
USER_ID = _make_user("expiry@example.com", ORG)
client  = _client(USER_ID, "expiry@example.com", ORG)


def _post(path, filename):
    with open(path, "rb") as fh:
        payload = fh.read()
    return client.post("/expiry/upload",
                       data={"file": (io.BytesIO(payload), filename)},
                       content_type="multipart/form-data", follow_redirects=True)


r = _post(make_xlsx(SHEET), "lot_tracking.xlsx")
_check("the upload is accepted", r.status_code == 200, detail=str(r.status_code))
_check("the counters reported back add up to the rows read",
       b"Read 6 rows: 3 lots stored, 2 summary lines skipped, 1 rows unreadable."
       in r.data, detail="flash text differs")
_check("the row with no expiry date is named in the report",
       b"1 of those had no expiry date." in r.data)

_rows = db.query("SELECT * FROM expiry_lots WHERE org_name=? ORDER BY expiry_date",
                 (ORG,))
_check("three lots are stored", len(_rows) == 3, detail=str(len(_rows)))
_check("the six title rows did not become the header",
       sorted(x["item_name"] for x in _rows) ==
       ["BROOKVALE UHT MILK 1L", "BROOKVALE UHT MILK 1L", "KESTREL ORANGE JUICE 1L"],
       detail=str([x["item_name"] for x in _rows]))
_check("neither the subtotal nor the carton-total row was stored as a lot",
       not any("Total" in (x["item_name"] or "") or "Total" in (x["item_code"] or "")
               for x in _rows), detail=str([x["item_code"] for x in _rows]))
_check("the two lots of one item code stay separate rows",
       sorted(x["lot_no"] for x in _rows) == ["LOT-A", "LOT-B", "LOT-K"],
       detail=str([x["lot_no"] for x in _rows]))
_check("the item code is the inventory code, not the location code",
       sorted({x["item_code"] for x in _rows}) == ["BRK-001", "KES-003"],
       detail=str([x["item_code"] for x in _rows]))
_check("the quantity stored is availability, not the allocated figure",
       sorted(x["qty_available"] for x in _rows) == [8.0, 25.0, 40.0],
       detail=str([x["qty_available"] for x in _rows]))

r = client.get("/expiry")
_check("the page renders", r.status_code == 200, detail=str(r.status_code))
body = r.data
_check("the expired lot is on the page", b"LOT-A" in body)
_check("the lot expiring inside the window is on the page", b"LOT-K" in body)
_check("the expired section is shown", b"Already expired" in body)
_check("the upcoming section names the window",
       b"Expiring in the next 120 days" in body)
_check("earliest expiry renders before later expiry",
       body.index(b"LOT-K") < body.index(b"LOT-B"),
       detail=str((body.index(b"LOT-K"), body.index(b"LOT-B"))))
_check("the lot with no expiry date is reported as unreadable, not stored",
       b"no expiry date" in body and b"LOT-N" in body)

r = client.get("/expiry?days=30")
_check("a narrower window drops the lot 60 days out",
       r.status_code == 200 and b"LOT-K" in r.data and b"LOT-B" not in r.data,
       detail=str(r.status_code))
r = client.get("/expiry?days=999")
_check("a window outside the whitelist falls back to 120 days",
       r.status_code == 200 and b"Expiring in the next 120 days" in r.data,
       detail=str(r.status_code))

_uid_1 = db.get_expiry_uploads(ORG, 10)[0]["id"]
_cutoff = (TODAY + datetime.timedelta(days=120)).isoformat()
_check("the page read is bounded by an explicit LIMIT",
       len(db.get_expiry_lots(ORG, _uid_1, _cutoff, 1)) == 1,
       detail=str(len(db.get_expiry_lots(ORG, _uid_1, _cutoff, 1))))
_check("the limit does not leak past the org filter",
       db.get_expiry_lots("OrgNoSuchOrg", _uid_1, _cutoff, 100) == [])
_check("counting lots is org-scoped", db.count_expiry_lots("OrgNoSuchOrg") == 0)


# ── A second snapshot supersedes the first ───────────────────────────────────

SHEET_2 = TITLE_ROWS + [RAW] + [
    _row(location_code="W1", location_name="Main Store", inventory_code="VNM-005",
         inventory_description="VANMARK PRAWN 1KG", uom="KG", lot_no="LOT-V",
         original_receipt_date="2026-05-01", expiry_date=SOON_ON,
         qty_on_hand="20.0", qty_available="20.0"),
]
r = _post(make_xlsx(SHEET_2), "lot_tracking_sept.xlsx")
_check("the second snapshot imports", b"1 lots stored" in r.data,
       detail="second upload flash differs")
body = r.data
_check("the newest snapshot's lots render", b"LOT-V" in body)
_check("the superseded snapshot's lots do not render",
       b"BROOKVALE UHT MILK 1L" not in body, detail="old snapshot still shown")
_check("the superseded snapshot is still listed, and tagged",
       b"lot_tracking.xlsx" in body and b"Superseded" in body)
_check("nothing was deleted: both snapshots' lots are still stored",
       db.count_expiry_lots(ORG) == 4, detail=str(db.count_expiry_lots(ORG)))


# ── Per-org lot ceiling ──────────────────────────────────────────────────────
# One file was already capped; an org's ACCUMULATED lots need their own ceiling
# or a dozen full exports fill the disk. Temporarily lower it rather than
# uploading 40,000 rows in a test.

_real_cap = appmod.MAX_EXPIRY_LOTS_PER_ORG
appmod.MAX_EXPIRY_LOTS_PER_ORG = db.count_expiry_lots(ORG) + 1
try:
    OVER_CSV = ("Item,Expiry Date,Qty\n"
                f"ALDERMOOR RICE 5KG,{SOON_ON},10\n"
                f"PADIMAS BEEF 2KG,{SOON_ON},20\n"
                f"VANMARK PRAWN 1KG,{SOON_ON},30\n")
    _before_lots    = db.count_expiry_lots(ORG)
    _before_uploads = len(db.get_expiry_uploads(ORG, 50))
    r = client.post("/expiry/upload",
                    data={"file": (io.BytesIO(OVER_CSV.encode("utf-8")), "over_cap.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
    _check("a file that would breach the org ceiling is refused",
           b"stored lots" in r.data, detail=str(r.status_code))
    _check("the refused file stored no lots",
           db.count_expiry_lots(ORG) == _before_lots,
           detail=str((db.count_expiry_lots(ORG), _before_lots)))
    _check("the refused file left no upload record",
           len(db.get_expiry_uploads(ORG, 50)) == _before_uploads,
           detail=str(len(db.get_expiry_uploads(ORG, 50))))
    _check("the refused file left no scratch table",
           not any(x["name"].startswith("expiry_import_") for x in db.query(
               "SELECT name FROM sqlite_master WHERE type='table'")),
           detail="scratch table survived")
finally:
    appmod.MAX_EXPIRY_LOTS_PER_ORG = _real_cap


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll expiry tests passed.")
