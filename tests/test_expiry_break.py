"""Hostile and degenerate input against the /expiry lot-tracking page.

The companion file (test_expiry.py) proves the real export imports and ranks.
This one tries to make the same code lie, crash, leak, or lose a row.

What is pinned here that nothing else covers:
  1. one org can never read, render, count or delete another org's lots, and a
     guessed / oversized / non-numeric upload_id is ignored rather than fatal
  2. a refused upload leaves NO lot rows, NO upload row, NO expiry_import_*
     scratch table and NO tmp_expiry* file on disk
  3. the accounting invariant survives junk: rows read == summary skipped +
     lots stored + rows rejected, on every shape of bad sheet
  4. the column fence holds -- no header the plan forbids can claim a field,
     including through the exact-match last resort the executor added
  5. quantities and dates the client's own export can plausibly contain
     ("LOOSE", "nan", "1e400", "-5", "31/02/2026", an Excel serial) are
     refused with a reason instead of being stored or crashing the worker

Some checks in this file FAIL on purpose: they describe behaviour that is
wrong today. Do not loosen them to make the suite green -- fix the code, or
argue the check is wrong. Every failure is listed again at the end of the run
so the runner's short tail shows what broke.

Throwaway temp DB, stubbed anthropic client, no API calls. Invented brands
only, the repo is public. Run: python tests/test_expiry_break.py
"""
import datetime
import io
import json
import os
import sys
import tempfile
import time
import types
import zipfile
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_expiry_break.db")
for _ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + _ext)
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
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
flask_app = appmod.app

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
TODAY = datetime.date.today()
XL_EPOCH = datetime.date(1899, 12, 30)

_FAILED = []


def _check(name, cond, detail=""):
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


def _serial(d):
    """The Excel serial an .xlsx date cell actually arrives as."""
    return str((d - XL_EPOCH).days)


# ── .xlsx fixture builder ────────────────────────────────────────────────────
# Every cell value is XML-escaped: the real report's title block contains "&",
# which breaks raw f-string XML. The column helper runs past Z because the real
# export is 22 columns wide.

def _col_letter(ci):
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


# ── Users, clients, state helpers ────────────────────────────────────────────

def _make_user(email, org):
    db.execute("INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
               (email, generate_password_hash("x"), org, "claude-sonnet-5"))
    return db.query("SELECT id FROM users WHERE email=?", (email,))[0]["id"]


def _client(user_id, email, org, role="admin"):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["user_id"]  = user_id
        s["email"]    = email
        s["org_name"] = org
        s["model"]    = "claude-sonnet-5"
        s["is_admin"] = False
        s["role"]     = role
        s["sv"]       = 0
    return c


def _scratch_tables():
    return [r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'expiry_import_%'")]


def _upload_files():
    d = appmod.UPLOAD_FOLDER
    return sorted(f for f in os.listdir(d) if "expiry" in f) if os.path.isdir(d) else []


def _wipe(org):
    for u in db.get_expiry_uploads(org, 500):
        db.delete_expiry_upload(org, u["id"])


def _state(org):
    """Everything a refusal must leave untouched, in one tuple."""
    return (db.count_expiry_lots(org),
            len(db.get_expiry_uploads(org, 500)),
            _scratch_tables(),
            [f for f in _upload_files() if f not in _BASELINE_FILES])


def _post(client, payload, filename):
    return client.post("/expiry/upload",
                       data={"file": (io.BytesIO(payload), filename)},
                       content_type="multipart/form-data", follow_redirects=True)


def _post_csv(client, text, filename="lots.csv"):
    return _post(client, text.encode("utf-8"), filename)


def _post_xlsx(client, rows, filename="lots.xlsx"):
    with open(make_xlsx(rows), "rb") as fh:
        return _post(client, fh.read(), filename)


_BASELINE_FILES = _upload_files()

ALPHA_ID = _make_user("alpha@example.com", "OrgAlpha")
BRAVO_ID = _make_user("bravo@example.com", "OrgBravo")
VIEW_ID  = _make_user("viewer@example.com", "OrgAlpha")

alpha  = _client(ALPHA_ID, "alpha@example.com", "OrgAlpha")
bravo  = _client(BRAVO_ID, "bravo@example.com", "OrgBravo")
viewer = _client(VIEW_ID, "viewer@example.com", "OrgAlpha", role="viewer")
anon   = flask_app.test_client()

EXPIRED_ON = (TODAY - datetime.timedelta(days=10)).isoformat()
SOON_ON    = (TODAY + datetime.timedelta(days=10)).isoformat()
LATER_ON   = (TODAY + datetime.timedelta(days=300)).isoformat()

GOOD_CSV = ("Item,Expiry Date,Qty On Hand\n"
            f"BROOKVALE UHT MILK 1L,{EXPIRED_ON},100\n"
            f"KESTREL ORANGE JUICE 1L,{SOON_ON},80\n")

# The real export's 22 headers, in file order. Used as the fence to test
# against: nothing added to this sheet may displace one of the seven picks.
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
EXPECTED = {"expiry": "Expiry Date", "received": "Original Receipt Date",
            "qty_available": "Qty Available",
            "qty_on_hand": "Qty On Hand", "lot_no": "Lot No.",
            "item_code": "Inventory Code", "item_name": "Inventory Description",
            "uom": "UOM"}

# The plan's section 12 tripwire list, verbatim, MINUS "Original Receipt
# Date". A field claiming any of these puts the wrong number in front of staff
# deciding what to throw away.
#
# The receipt date moved out of this list, and only out of this list: it is
# RECLASSIFIED, not unguarded. Its status went from "no field may claim it" to
# "only `received` may claim it", because the life-class inference needs the
# receipt-to-expiry gap and without it every lot falls to the long-life default
# and the 28-day chilled rule never fires. The danger that put it here in the
# first place is unchanged and is pinned by RECEIPT_FENCE below: if `expiry`
# ever claims this column, every lot renders a date typically 477 days too
# early and staff dump good stock. Do not fold it back into FORBIDDEN, and do
# not delete the replacement check as redundant.
FORBIDDEN = ["Location Code", "Location Name", "Supplier Name",
             "Qty Selected", "Qty Allocated",
             "Qty Allocated Selected", "Lot Reference No.", "Source Voucher No.",
             "Pack Size"]


# ── 1. The column fence, including the exact-match last resort ───────────────
# The executor added a tier the plan did not specify: after every keyword
# misses, a header whose WHOLE normalised name is "qty"/"quantity" (on-hand) or
# "item"/"product" (name) is taken. The claim is that no forbidden header can
# reach it. Attacked three ways: the fence on the full sheet, the fence with
# each required column removed (which is what actually lets the last resort
# run), and header text that collapses onto the exact words once punctuation
# and case are stripped.

_map, _miss = expiry.detect_columns(RAW)
_check("the 22 real headers map to the seven intended columns",
       _map == EXPECTED and _miss == [], detail=str((_map, _miss)))

for _f in FORBIDDEN:
    _check(f"no field claims the forbidden header {_f!r}",
           _f not in _map.values(), detail=str(_map))
    _check(f"{_f!r} cannot reach the exact-match tier",
           expiry._norm_header(_f) not in ("qty", "quantity", "item", "product"),
           detail=expiry._norm_header(_f))

# RECEIPT_FENCE. Replaces the plain FORBIDDEN entry for "Original Receipt
# Date": the column may now be claimed, but by exactly one field. `expiry`
# claiming it is the original hazard -- on the real file the receipt date runs
# typically 477 days earlier than the expiry, so every lot would render as
# nearly expired and staff would dump good stock.
_check("only the `received` field may claim the receipt-date column",
       [f for f, h in _map.items() if h == "Original Receipt Date"] == ["received"],
       detail=str(_map))
_check("the expiry field still claims the expiry column, not the receipt date",
       _map.get("expiry") == "Expiry Date", detail=str(_map.get("expiry")))

# Drop each required column in turn. This is the only way the exact tier ever
# runs on this sheet, so it is where a wrong steal would actually happen.
_map2, _miss2 = expiry.detect_columns([h for h in RAW if h != "Expiry Date"])
_check("with no expiry column the sheet is refused, not filled from the receipt date",
       _miss2 == ["expiry date"] and "expiry" not in _map2, detail=str((_map2, _miss2)))

_map3, _miss3 = expiry.detect_columns(
    [h for h in RAW if h not in ("Qty Available", "Qty On Hand")])
_check("with no on-hand or available column the sheet is refused, and neither "
       "Qty Selected nor Qty Allocated is taken as the quantity",
       _miss3 == ["quantity"] and "qty_on_hand" not in _map3
       and "qty_available" not in _map3, detail=str((_map3, _miss3)))

_map4, _miss4 = expiry.detect_columns(
    [h for h in RAW if h not in ("Inventory Code", "Inventory Description")])
_check("with no item column the sheet is refused, and neither Location Name nor "
       "Supplier Name is taken as the item",
       _miss4 == ["item name or code"] and "item_name" not in _map4
       and "item_code" not in _map4, detail=str((_map4, _miss4)))

# A stray bare column beside the real ones must not displace them.
for _extra in ("Qty", "Quantity", "Item", "Product", "QTY.", "Item #", "(Qty)"):
    _m, _ = expiry.detect_columns(RAW + [_extra])
    _check(f"a stray {_extra!r} column beside the real 22 changes nothing",
           _m == EXPECTED, detail=str(_m))
    _m, _ = expiry.detect_columns([_extra] + RAW)
    _check(f"a stray {_extra!r} column FIRST in the sheet changes nothing",
           _m == EXPECTED, detail=str(_m))

# Same fence on the sanitised names production actually sees.
_msan, _missan = expiry.detect_columns(SAN)
_check("the sanitised header forms map the same way",
       _missan == [] and _msan.get("expiry") == "expiry_date"
       and _msan.get("qty_on_hand") == "qty_on_hand"
       and _msan.get("qty_available") == "qty_available"
       and _msan.get("item_code") == "inventory_code"
       and _msan.get("item_name") == "inventory_description",
       detail=str(_msan))

# Punctuation and case collapse onto the exact words. Harmless on their own
# (they ARE the quantity / the item), but they prove the tier is reachable by
# anything that normalises to those four strings.
for _hdr, _field in (("QTY.", "qty_on_hand"), ("Qty:", "qty_on_hand"),
                     ("(Qty)", "qty_on_hand"), ("Quantity!", "qty_on_hand"),
                     ("ITEM", "item_name"), ("Item #", "item_name"),
                     ("Product.", "item_name")):
    _m, _ms = expiry.detect_columns(["Expiry Date", _hdr]
                                    + (["Item"] if _field != "item_name" else ["Qty On Hand"]))
    _check(f"a punctuated {_hdr!r} still reads as {_field}",
           _m.get(_field) == _hdr, detail=str(_m))

# Degenerate and hostile header lists: never a crash, never a wrong claim.
_HEADER_CASES = [
    ("an empty header list", [], {}, ["expiry date", "quantity", "item name or code"]),
    ("headers that say nothing", ["Colour", "Remarks", "Notes"], {},
     ["expiry date", "quantity", "item name or code"]),
    ("a None header is ignored", [None, "Item", "Expiry Date", "Qty"],
     {"expiry": "Expiry Date", "qty_on_hand": "Qty", "item_name": "Item"}, []),
    ("a numeric header is ignored", [1, 2.5, "Item", "Expiry Date", "Qty"],
     {"expiry": "Expiry Date", "qty_on_hand": "Qty", "item_name": "Item"}, []),
    ("an empty-string header is ignored", ["", "Item", "Expiry Date", "Qty"],
     {"expiry": "Expiry Date", "qty_on_hand": "Qty", "item_name": "Item"}, []),
    ("a punctuation-only header is ignored", ["***", "Item", "Expiry Date", "Qty"],
     {"expiry": "Expiry Date", "qty_on_hand": "Qty", "item_name": "Item"}, []),
    ("a header carrying SQL is ignored",
     ['"; DROP TABLE users;--', "Item", "Expiry Date", "Qty"],
     {"expiry": "Expiry Date", "qty_on_hand": "Qty", "item_name": "Item"}, []),
    ("a 300-character header is ignored",
     ["I" * 300, "Item", "Expiry Date", "Qty"],
     {"expiry": "Expiry Date", "qty_on_hand": "Qty", "item_name": "Item"}, []),
    ("a script-tag header is ignored",
     ["<script>alert(1)</script>", "Item", "Expiry Date", "Qty"],
     {"expiry": "Expiry Date", "qty_on_hand": "Qty", "item_name": "Item"}, []),
    ("duplicate item columns claim one header each, never the same one twice",
     ["Item", "Item", "Expiry Date", "Qty"],
     {"expiry": "Expiry Date", "qty_on_hand": "Qty", "item_name": "Item"}, []),
]
for _name, _hdrs, _want_map, _want_miss in _HEADER_CASES:
    try:
        _got = expiry.detect_columns(_hdrs)
    except Exception as ex:                      # noqa: BLE001 - the point is it must not
        _got = f"RAISED {type(ex).__name__}: {ex}"
    _check("detect_columns: " + _name, _got == (_want_map, _want_miss), detail=str(_got))

_m, _ms = expiry.detect_columns(["Item", "Expiry Date", "Qty On Hand"]
                                + ["UOM"] + [f"uom_{i}" for i in range(1, 25)])
_check("25 duplicate UOM columns claim exactly one field",
       _m.get("uom") == "UOM" and _ms == [], detail=str(_m))

# One field, one header: nothing may be claimed twice.
for _hdrs in (RAW, SAN, ["Item", "Expiry Date", "Qty"],
              ["Item Code", "Item Description", "Batch No", "Expiry Date",
               "Qty On Hand", "Qty Available", "UOM"]):
    _m, _ = expiry.detect_columns(_hdrs)
    _check(f"no header is claimed by two fields ({len(_hdrs)} headers)",
           len(set(_m.values())) == len(_m), detail=str(_m))


# ── 2. parse_qty: what a client's own export can plausibly contain ───────────

_QTY = [
    ("486.0", 486.0), ("8,031", 8031.0), (" 40.0 ", 40.0), ("0", 0.0),
    ("0.0", 0.0), ("LOOSE", None), ("", None), ("   ", None), (None, None),
    ("-5", None), ("-0.5", None), ("nan", None), ("NaN", None), ("inf", None),
    ("-inf", None), ("Infinity", None), ("1e400", None), ("-1e400", None),
    ("N/A", None), ("-", None), ("TBC", None), ("about 40", None),
    ("(500)", None), ("5 CTN", None), ("1.2.3", None), ("0x10", None),
    ("True", None), (True, None), ("1,200.50", 1200.5), ("1 200", 1200.0),
    ("  8,031.00  ", 8031.0), ("1e3", 1000.0),
]
for _v, _want in _QTY:
    try:
        _got = expiry.parse_qty(_v)
    except Exception as ex:                      # noqa: BLE001
        _got = f"RAISED {type(ex).__name__}: {ex}"
    _check(f"parse_qty({_v!r}) is {_want!r}", _got == _want, detail=repr(_got))

# Documented, not endorsed: float() accepts PEP 515 underscores, so a cell of
# "1_0" reads as ten. No spreadsheet writes that, but if this ever changes it
# changes a stored quantity.
_check("parse_qty('1_0') reads as 10.0 (documented Python float behaviour)",
       expiry.parse_qty("1_0") == 10.0, detail=repr(expiry.parse_qty("1_0")))


# ── 3. parse_date and days_remaining on this path ────────────────────────────

_DATES = [
    ("46100", datetime.date(2026, 3, 19)), ("46100.0", datetime.date(2026, 3, 19)),
    ("2026-11-30", datetime.date(2026, 11, 30)),
    ("2026-11-30 00:00:00", datetime.date(2026, 11, 30)),
    ("2026-11-30T10:00:00+08:00", datetime.date(2026, 11, 30)),
    ("31/02/2026", None), ("2026-02-30", None), ("not-a-date", None),
    ("0", None), ("99999", None), ("-46100", None), ("20261130", None),
    ("", None), ("   ", None), (None, None), ("1e5", None),
    ("30-Nov-2026", None), ("D" * 300, None), ("9" * 400, None),
    ("=1+1", None), ("'; DROP TABLE users;--", None),
]
for _v, _want in _DATES:
    try:
        _got = expiry.parse_date(_v)
    except Exception as ex:                      # noqa: BLE001
        _got = f"RAISED {type(ex).__name__}: {ex}"
    _check(f"parse_date({_v!r:.40}) is {_want!r}", _got == _want, detail=repr(_got))

# Documented, not endorsed: an .xlsx date cell carrying a TIME arrives as a
# fractional serial, and a fractional serial is refused outright. A sheet whose
# expiry cells are datetimes rather than dates rejects every row with
# "no expiry date" -- counted and reported, never silently stored, but the
# whole file reads as unreadable.
_check("a fractional Excel serial (a datetime cell) is refused, not rounded",
       expiry.parse_date("46100.5") is None, detail=repr(expiry.parse_date("46100.5")))

# Documented: day-first is assumed. A month-first export silently reads
# 12/01/2026 as 12 January, not 1 December -- a wrong date, never a reject.
_check("12/01/2026 is read day-first (documented Singapore convention)",
       expiry.parse_date("12/01/2026") == datetime.date(2026, 1, 12),
       detail=str(expiry.parse_date("12/01/2026")))

for _iso, _want in (("1900-01-01", (datetime.date(1900, 1, 1) - TODAY).days),
                    ("2099-12-31", (datetime.date(2099, 12, 31) - TODAY).days),
                    ("0001-01-01", (datetime.date(1, 1, 1) - TODAY).days)):
    try:
        _got = expiry.days_remaining(_iso, TODAY)
        _ok = isinstance(_got, int) and _got == _want
    except Exception as ex:                      # noqa: BLE001
        _got, _ok = f"RAISED {type(ex).__name__}", False
    _check(f"days_remaining is a finite int for {_iso}", _ok, detail=repr(_got))


# ── 4. build_lots: the accounting invariant under junk ───────────────────────
# rows read == summary skipped + lots stored + rows rejected. A count that does
# not add up is worse than the file, in a product whose whole job is arithmetic.

MAPPING = {"expiry": "expiry_date", "qty_available": "qty_available",
           "qty_on_hand": "qty_on_hand", "lot_no": "lot_no",
           "item_code": "inventory_code", "item_name": "inventory_description",
           "uom": "uom"}

_JUNK_VALUES = ["", None, "0", "-1", "nan", "1e400", "LOOSE", "1,200", "TOTAL",
                "Total (CARTON)", "Inventory Total : BRK-001", "2026-11-30",
                "46100", "31/02/2026", "junk", "   ", "TOTAL PROTEIN MIX 5KG",
                "<script>alert(1)</script>", "x" * 5000]

# Deterministic sweep: every junk value in every mapped column, one at a time,
# on top of a good row. 7 columns x N values, no randomness, so a failure is
# reproducible from its name alone.
_GOOD = {"inventory_code": "BRK-001", "inventory_description": "BROOKVALE UHT MILK 1L",
         "uom": "CTN", "lot_no": "LOT-A", "expiry_date": "2026-11-30",
         "qty_on_hand": "100.0", "qty_available": "40.0"}
_broke = []
for _col in MAPPING.values():
    for _v in _JUNK_VALUES:
        _rec = dict(_GOOD)
        _rec[_col] = _v
        try:
            _lots, _rej, _st = expiry.build_lots([_rec], MAPPING)
        except Exception as ex:                  # noqa: BLE001
            _broke.append((_col, _v, f"RAISED {type(ex).__name__}: {ex}"))
            continue
        if _st["read"] != _st["summary"] + len(_lots) + len(_rej):
            _broke.append((_col, _v, (_st, len(_lots), len(_rej))))
_check("every single-cell corruption is still accounted for "
       f"({len(MAPPING) * len(_JUNK_VALUES)} combinations)",
       _broke == [], detail=str(_broke[:3]))

# Whole rows of junk, and the degenerate mappings a sparse sheet produces.
for _label, _mapping in (("full mapping", MAPPING),
                         ("no code column", {k: v for k, v in MAPPING.items()
                                             if k != "item_code"}),
                         ("no name column", {k: v for k, v in MAPPING.items()
                                             if k != "item_name"}),
                         ("no available column", {k: v for k, v in MAPPING.items()
                                                  if k != "qty_available"}),
                         ("no on-hand column", {k: v for k, v in MAPPING.items()
                                                if k != "qty_on_hand"}),
                         ("no lot or uom column", {k: v for k, v in MAPPING.items()
                                                   if k not in ("lot_no", "uom")})):
    _recs = [{c: _JUNK_VALUES[(i + j) % len(_JUNK_VALUES)]
              for j, c in enumerate(MAPPING.values())}
             for i in range(len(_JUNK_VALUES))]
    try:
        _lots, _rej, _st = expiry.build_lots(_recs, _mapping)
        _ok = _st["read"] == _st["summary"] + len(_lots) + len(_rej) == len(_recs) \
            and _st["read"] == len(_recs)
        _detail = str((_st, len(_lots), len(_rej)))
    except Exception as ex:                      # noqa: BLE001
        _ok, _detail = False, f"RAISED {type(ex).__name__}: {ex}"
    _check(f"a sheet of pure junk is fully accounted for: {_label}", _ok, detail=_detail)

_check("an empty record list is not an error",
       expiry.build_lots([], MAPPING) == ([], [], {"read": 0, "summary": 0, "no_expiry": 0}),
       detail=str(expiry.build_lots([], MAPPING)))

_lots, _rej, _st = expiry.build_lots([dict(_GOOD, inventory_description="N" * 5000,
                                           inventory_code="C" * 5000,
                                           lot_no="L" * 5000, uom="U" * 5000)], MAPPING)
_check("a 5,000-character name is truncated before storage, not stored whole",
       len(_lots) == 1 and len(_lots[0]["item_name"]) == 200
       and len(_lots[0]["item_code"]) == 120 and len(_lots[0]["lot_no"]) == 80
       and len(_lots[0]["uom"]) == 32,
       detail=str({k: len(str(v)) for k, v in _lots[0].items()}))

_lots, _rej, _st = expiry.build_lots(
    [dict(_GOOD, expiry_date="", inventory_description="N" * 5000, lot_no="L" * 5000)],
    MAPPING)
_check("a reject echoed back into HTML is truncated to 80 characters",
       len(_rej) == 1 and len(_rej[0]["item"]) == 80 and len(_rej[0]["lot"]) == 80,
       detail=str({k: len(str(v)) for k, v in _rej[0].items()}))

# Both quantity columns unreadable is a reject, never a stored NULL-quantity lot.
_lots, _rej, _st = expiry.build_lots(
    [dict(_GOOD, qty_on_hand="LOOSE", qty_available="N/A")], MAPPING)
_check("a lot with no readable quantity at all is rejected, not stored",
       _lots == [] and len(_rej) == 1
       and _rej[0]["reason"] == "quantity is missing or not a number",
       detail=str((_lots, _rej)))

# A negative quantity in one column must not be laundered into the other.
_lots, _rej, _st = expiry.build_lots(
    [dict(_GOOD, qty_available="-5", qty_on_hand="12")], MAPPING)
_check("a negative available figure is dropped, and on-hand is not overwritten by it",
       len(_lots) == 1 and _lots[0]["qty_available"] is None
       and _lots[0]["qty_on_hand"] == 12.0, detail=str(_lots))

_lots, _rej, _st = expiry.build_lots(
    [dict(_GOOD, qty_available="1e400", qty_on_hand="nan")], MAPPING)
_check("NaN and Infinity never reach the insert",
       _lots == [] and len(_rej) == 1, detail=str((_lots, _rej)))


# ── 5. The summary-row skip, judged per row ──────────────────────────────────
# WAS a per-column rule (a loose prefix match on codes, a strict one on names),
# which erred in opposite directions: the loose side swallowed real products, the
# strict side let a "Total (CARTON)" line through as a lot carrying the sum of the
# real ones. Same data, different header word, different answer. Found by the
# tester, fixed by judging the label's SHAPE and ignoring which column it came
# from. These checks now pass and exist to keep both directions shut.

_check("the colon form is a total whichever column it came from",
       expiry.is_summary_value("Inventory Total : BRK-001"))
_check("a blank identifier is report noise",
       expiry.is_summary_value("") and expiry.is_summary_value(None))
_check("a real product whose name merely starts with the letters of total survives",
       expiry.is_summary_value("TOTALE PASTA 500G") is False
       and expiry.is_summary_value("Totally Fresh Juice 1L") is False,
       detail="a brand was mistaken for a report total")

# FAILS TODAY. A subtotal line whose label lands in the DESCRIPTION column with
# the group's expiry date and summed quantity is stored as a lot: the code cell
# is blank, so the row is judged by its name under the strict rule, and
# "Total (CARTON)" is not one of the exact labels. The plan's rule -- identifier
# = the code cell when a code COLUMN exists -- skips it, because a blank code is
# report noise. What lands on the page is a made-up lot holding the sum of the
# real ones.
_SUBTOTAL_ROWS = [
    dict(_GOOD, lot_no="LOT-A"),
    {"inventory_code": "", "inventory_description": "Total (CARTON)",
     "uom": "CTN", "lot_no": "", "expiry_date": "2026-11-30",
     "qty_on_hand": "8517.0", "qty_available": "8517.0"},
]
_lots, _rej, _st = expiry.build_lots(_SUBTOTAL_ROWS, MAPPING)
_check("a subtotal line with a date and a summed quantity is not stored as a lot",
       len(_lots) == 1 and _st["summary"] == 1,
       detail=f"stored {[x['item_name'] for x in _lots]}, stats {_st}")

# FAILS TODAY. The mirror image. On a sheet whose only identifier column is a
# code column -- the shape detect_columns explicitly supports, where the code
# becomes the label -- the loose prefix rule is applied to whatever that column
# holds. A real product loses its row because of its brand, counted as a
# "summary line skipped" and never reported as an error.
_CODE_ONLY = {"expiry": "expiry_date", "qty_on_hand": "qty_on_hand",
              "item_code": "item_code"}
_NAME_ONLY = {"expiry": "expiry_date", "qty_on_hand": "qty_on_hand",
              "item_name": "item_code"}
_PRODUCT_ROW = [{"item_code": "TOTAL PROTEIN MIX 5KG", "expiry_date": "2026-11-30",
                 "qty_on_hand": "40"}]
_lots_c, _, _st_c = expiry.build_lots(_PRODUCT_ROW, _CODE_ONLY)
_lots_n, _, _st_n = expiry.build_lots(_PRODUCT_ROW, _NAME_ONLY)
_check("a real product survives whether its column is headed as a code or a name",
       len(_lots_c) == len(_lots_n) == 1,
       detail=f"code-headed kept {len(_lots_c)}, name-headed kept {len(_lots_n)}, "
              f"code-headed stats {_st_c}")

_lots_b, _, _st_b = expiry.build_lots(
    [{"inventory_code": "TOTAL-500", "inventory_description": "BROOKVALE UHT MILK 1L",
      "uom": "CTN", "lot_no": "LOT-A", "expiry_date": "2026-11-30",
      "qty_on_hand": "40", "qty_available": "40"}], MAPPING)
_check("a lot is not thrown away because its item CODE starts with the word total",
       len(_lots_b) == 1, detail=f"stats {_st_b}")


# ── 6. Refusals leave nothing behind ─────────────────────────────────────────
# No lot rows, no orphan upload row, no expiry_import_* scratch table, no
# tmp_expiry* file. create_expiry_upload runs before the sheet is understood,
# so every early return has to clean up after itself.

_BAD_FILES = [
    ("a wrong extension (.pdf)",      b"%PDF-1.4 not really", "lots.pdf"),
    ("an executable (.exe)",          b"MZ\x90\x00", "lots.exe"),
    ("a double extension (.xlsx.exe)", b"MZ\x90\x00", "lots.xlsx.exe"),
    ("a zero-byte csv",               b"", "empty.csv"),
    ("a zero-byte xlsx",              b"", "empty.xlsx"),
    ("html served as csv",            b"<html><body>Not a sheet</body></html>", "page.csv"),
    ("a .xlsx that is not a zip",     b"just some text", "broken.xlsx"),
    ("random bytes as csv",           bytes(range(256)), "bytes.csv"),
]
for _name, _payload, _fn in _BAD_FILES:
    _before = _state("OrgAlpha")
    r = _post(alpha, _payload, _fn)
    _after = _state("OrgAlpha")
    _check(f"refused and nothing left behind: {_name}",
           r.status_code == 200 and _after == _before == (0, 0, [], []),
           detail=str((r.status_code, _before, _after)))
    _wipe("OrgAlpha")

r = alpha.post("/expiry/upload", data={}, content_type="multipart/form-data",
               follow_redirects=True)
_check("a POST with no file at all is refused cleanly",
       r.status_code == 200 and _state("OrgAlpha") == (0, 0, [], []),
       detail=str(_state("OrgAlpha")))

r = _post(alpha, b"", "")
_check("a file field with an empty filename is refused cleanly",
       r.status_code == 200 and _state("OrgAlpha") == (0, 0, [], []),
       detail=str(_state("OrgAlpha")))

_MISSING_COLUMN_SHEETS = [
    ("no expiry column", "Item,Qty On Hand\nBROOKVALE UHT MILK 1L,10\n", b"expiry date"),
    ("no quantity column",
     f"Item,Expiry Date\nBROOKVALE UHT MILK 1L,{SOON_ON}\n", b"quantity"),
    ("no item column",
     f"Expiry Date,Qty On Hand\n{SOON_ON},10\n", b"item name or code"),
    ("nothing usable at all", "Colour,Remarks\nred,none\n", b"expiry date"),
]
for _name, _csv, _needle in _MISSING_COLUMN_SHEETS:
    r = _post_csv(alpha, _csv, "missing.csv")
    _check(f"a sheet with {_name} is refused, names the field, and stores nothing",
           _needle in r.data and _state("OrgAlpha") == (0, 0, [], []),
           detail=str((_needle in r.data, _state("OrgAlpha"))))
    _wipe("OrgAlpha")

# A headers-only sheet and a sheet of nothing but report noise: no crash, and
# the counts the user is shown must add up.
r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n", "headers_only.csv")
_check("a headers-only sheet imports nothing without crashing",
       r.status_code == 200 and db.count_expiry_lots("OrgAlpha") == 0
       and _scratch_tables() == [],
       detail=str((r.status_code, db.count_expiry_lots("OrgAlpha"))))
_wipe("OrgAlpha")

r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n"
                     "Inventory Total : BRK-001,,8517\n"
                     "GRAND TOTAL,,9000\n"
                     "Sub Total:,,500\n", "totals_only.csv")
_check("a sheet of nothing but totals reports them as skipped, not as an error page",
       r.status_code == 200 and b"3 summary lines skipped" in r.data
       and db.count_expiry_lots("OrgAlpha") == 0,
       detail=str((r.status_code, db.count_expiry_lots("OrgAlpha"))))
_wipe("OrgAlpha")

r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n"
                     f"BROOKVALE UHT MILK 1L,{SOON_ON},LOOSE\n"
                     f"KESTREL ORANGE JUICE 1L,{SOON_ON},N/A\n"
                     f"PADIMAS BEEF 2KG,{SOON_ON},-\n", "all_text_qty.csv")
_check("a sheet whose quantities are all text stores nothing and stays a 200",
       r.status_code == 200 and db.count_expiry_lots("OrgAlpha") == 0
       and b"3 rows unreadable" in r.data,
       detail=str((r.status_code, db.count_expiry_lots("OrgAlpha"))))
_check("the unreadable rows are shown with a reason the user can act on",
       b"quantity is missing or not a number" in alpha.get("/expiry").data)
_wipe("OrgAlpha")

r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n"
                     "BROOKVALE UHT MILK 1L,31/02/2026,10\n"
                     "KESTREL ORANGE JUICE 1L,not-a-date,10\n"
                     "PADIMAS BEEF 2KG,0,10\n"
                     "VANMARK PRAWN 1KG,99999,10\n"
                     f"ALDERMOOR RICE 5KG,{'D' * 300},10\n", "junk_dates.csv")
_check("junk expiry dates are rejects with a reason, never stored, never a 500",
       r.status_code == 200 and db.count_expiry_lots("OrgAlpha") == 0
       and b"5 rows unreadable" in r.data and b"5 of those had no expiry date" in r.data,
       detail=str((r.status_code, db.count_expiry_lots("OrgAlpha"))))
_wipe("OrgAlpha")

r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n"
                     "BROOKVALE UHT MILK 1L,1900-01-01,10\n"
                     "KESTREL ORANGE JUICE 1L,2099-12-31,10\n", "extreme_years.csv")
_page = alpha.get("/expiry?days=365")
_check("a 1900-dated and a 2099-dated lot both store and render without a crash",
       r.status_code == 200 and db.count_expiry_lots("OrgAlpha") == 2
       and _page.status_code == 200 and b"1900-01-01" in _page.data,
       detail=str((r.status_code, db.count_expiry_lots("OrgAlpha"), _page.status_code)))
_wipe("OrgAlpha")

# Negative and overflowing quantities.
r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand,Qty Available\n"
                     f"BROOKVALE UHT MILK 1L,{SOON_ON},-5,-5\n"
                     f"KESTREL ORANGE JUICE 1L,{SOON_ON},1e400,1e400\n"
                     f"PADIMAS BEEF 2KG,{SOON_ON},nan,nan\n", "bad_numbers.csv")
_check("negative, infinite and NaN quantities are refused, not stored",
       db.count_expiry_lots("OrgAlpha") == 0 and b"3 rows unreadable" in r.data,
       detail=str(db.count_expiry_lots("OrgAlpha")))
_check("'inf' and 'nan' never reach a rendered cell",
       b">inf<" not in r.data and b">nan<" not in r.data)
_wipe("OrgAlpha")

# One bad cell must not take the good rows beside it down (SQLite stores NaN as
# NULL, and a single aborted executemany would discard the whole sheet).
r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n"
                     f"BROOKVALE UHT MILK 1L,{SOON_ON},100\n"
                     f"KESTREL ORANGE JUICE 1L,{SOON_ON},nan\n"
                     f"PADIMAS BEEF 2KG,{SOON_ON},600\n", "one_nan.csv")
_check("one NaN cell does not throw away the two good rows beside it",
       db.count_expiry_lots("OrgAlpha") == 2 and b"Something went wrong" not in r.data,
       detail=str(db.count_expiry_lots("OrgAlpha")))
_wipe("OrgAlpha")

# The stored rejects blob is capped at 50. The COUNT must not be: capping the
# count is how a sheet with 63 bad rows tells the user "3 imported, 50 skipped"
# and ten rows vanish with no record. That exact bug shipped once on tenders.
r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n"
                     + "".join(f"ALDERMOOR ITEM {i},{SOON_ON},TBC\n" for i in range(60))
                     + "".join(f"NORDVIK GOOD {i},{SOON_ON},100\n" for i in range(3)),
              "many_rejects.csv")
_u = db.get_expiry_uploads("OrgAlpha", 10)[0]
_check("the unreadable count shown is the real one, not the capped stored list",
       _u["rows_rejected"] == 60, detail=f"reported {_u['rows_rejected']}, actually 60")
_check("stored plus skipped plus unreadable accounts for every row read",
       _u["rows_imported"] + _u["rows_rejected"] + _u["rows_skipped"] == 63,
       detail=f"{_u['rows_imported']} + {_u['rows_rejected']} + {_u['rows_skipped']}")
_check("the stored reject blob is capped and the page says so",
       len(json.loads(_u["rejects_json"])) == expiry.MAX_REJECTS_STORED
       and b"Showing the first 50 of 60" in alpha.get("/expiry").data,
       detail=str(len(json.loads(_u["rejects_json"]))))
_wipe("OrgAlpha")

# Degenerate but legal sheets: one row, and a mapped column blank in every row.
r = _post_csv(alpha, f"Item,Expiry Date,Qty On Hand\nBROOKVALE UHT MILK 1L,{SOON_ON},1\n",
              "one_row.csv")
_check("a one-row sheet imports one lot and renders",
       db.count_expiry_lots("OrgAlpha") == 1 and b"Read 1 rows" in r.data
       and alpha.get("/expiry").status_code == 200,
       detail=str(db.count_expiry_lots("OrgAlpha")))
_wipe("OrgAlpha")

r = _post_csv(alpha, "Item,Lot No,UOM,Expiry Date,Qty On Hand,Qty Available\n"
                     f"BROOKVALE UHT MILK 1L,,,{SOON_ON},12,\n"
                     f"KESTREL ORANGE JUICE 1L,,,{SOON_ON},8,\n", "blank_columns.csv")
_stored = db.query("SELECT * FROM expiry_lots WHERE org_name=?", ("OrgAlpha",))
_page = alpha.get("/expiry")
_check("an available column that is blank in every row falls back to on-hand",
       len(_stored) == 2 and all(x["qty_available"] is None for x in _stored)
       and all(x["qty_on_hand"] is not None for x in _stored)
       and _page.status_code == 200 and b"LOT-" not in _page.data,
       detail=str([(x["qty_on_hand"], x["qty_available"], x["lot_no"]) for x in _stored]))
_wipe("OrgAlpha")


# ── 7. Hostile headers and cells through the whole route ─────────────────────

_HOSTILE_SHEETS = [
    ("a SQL payload in a header",
     'Item"; DROP TABLE users;--,Expiry Date,Qty On Hand\n'
     f"BROOKVALE UHT MILK 1L,{SOON_ON},10\n"),
    ("a script tag as a header",
     "<script>alert(1)</script>,Item,Expiry Date,Qty On Hand\n"
     f"x,BROOKVALE UHT MILK 1L,{SOON_ON},10\n"),
    ("a 300-character header",
     ("I" * 300) + ",Item,Expiry Date,Qty On Hand\n"
     f"x,BROOKVALE UHT MILK 1L,{SOON_ON},10\n"),
    ("a punctuation-only header",
     "***,Item,Expiry Date,Qty On Hand\n"
     f"x,BROOKVALE UHT MILK 1L,{SOON_ON},10\n"),
    ("25 duplicate UOM columns",
     ",".join(["Item", "Expiry Date", "Qty On Hand"] + ["UOM"] * 25) + "\n"
     + ",".join(["BROOKVALE UHT MILK 1L", SOON_ON, "10"] + ["CTN"] * 25) + "\n"),
    ("a header called _session_id",
     "_session_id,Item,Expiry Date,Qty On Hand\n"
     f"1,BROOKVALE UHT MILK 1L,{SOON_ON},10\n"),
]
for _name, _csv in _HOSTILE_SHEETS:
    r = _post_csv(alpha, _csv, "hostile.csv")
    _check(f"a hostile sheet survives ingest without damage: {_name}",
           r.status_code == 200 and db.table_exists("users")
           and b"<script>alert(1)</script>" not in r.data,
           detail=str(r.status_code))
    _wipe("OrgAlpha")

_HOSTILE_CELLS = [
    ("a script tag as the item name", "<script>alert(1)</script>"),
    ("a SQL payload as the item name", "BROOKVALE'); DROP TABLE users;--"),
    ("a quoted name with a semicolon", 'BROOKVALE "1L"; DELETE FROM users'),
    ("a 5,000-character name", "N" * 5000),
    ("a name that is only punctuation", "!!!***"),
]
for _name, _cell in _HOSTILE_CELLS:
    r = _post_csv(alpha, "Item,Lot No,Expiry Date,Qty On Hand\n"
                         f'"{_cell}","LOT-A\'; DROP TABLE users;--",{SOON_ON},10\n',
                  "hostile_cells.csv")
    _page = alpha.get("/expiry")
    _stored = db.query("SELECT * FROM expiry_lots WHERE org_name=?", ("OrgAlpha",))
    _check(f"a hostile cell is stored as text and rendered escaped: {_name}",
           (r.status_code == 200 and db.table_exists("users")
            and _page.status_code == 200
            and b"<script>alert(1)</script>" not in _page.data
            and all(len(x["item_name"]) <= 200 for x in _stored)),
           detail=str((r.status_code, _page.status_code, len(_stored))))
    _wipe("OrgAlpha")

# A fully non-ASCII stem: secure_filename() strips it to nothing, so the route
# has to carry the validated extension itself or a .csv lands on disk
# extensionless and gets handed to the .xlsx parser.
#
# The "empty stem" and "whitespace-only stem" cases WERE broken, here and in the
# already-live tenders_upload, because the two halves disagreed: _allowed() read
# the extension with rsplit(".", 1) and said "csv", while os.path.splitext() said
# a name that is nothing BUT an extension ("..csv", ".csv") has none. The file
# landed on disk with no suffix, went to the .xlsx parser, and a valid CSV was
# refused with "Could not read that file". Nothing leaked; the user was just told
# their good file was broken. Found by the tester, fixed with auth_utils'
# _upload_ext, which splits the name the same way _allowed validated it. Both
# routes use it now. These checks pass and exist to keep the two halves agreeing.
# Labels stay ASCII: the runner captures stdout through a cp1252 console, and a
# non-ASCII check NAME would kill the run before it could report anything.
for _label, _fn in (("a fully non-ASCII stem", "訂單.csv"),
                    ("a whitespace-only stem", "  .csv"),
                    ("an empty stem", "..csv"),
                    ("a 200-character stem", "a" * 200 + ".csv"),
                    ("an upper-case extension", "lots.CSV"),
                    ("a double extension ending .csv", "lots.xlsx.csv")):
    _wipe("OrgAlpha")
    r = _post_csv(alpha, GOOD_CSV, _fn)
    _check(f"an awkward filename still imports and leaves no file behind: {_label}",
           r.status_code == 200 and db.count_expiry_lots("OrgAlpha") == 2
           and [f for f in _upload_files() if f not in _BASELINE_FILES] == [],
           detail=str((r.status_code, db.count_expiry_lots("OrgAlpha"),
                       _upload_files())))
_wipe("OrgAlpha")

# The filename is client text too, and it is echoed into the snapshot list.
r = _post_csv(alpha, GOOD_CSV, "<script>alert(1)</script>.csv")
_page = alpha.get("/expiry")
_check("a hostile filename renders escaped",
       _page.status_code == 200 and b"<script>alert(1)</script>" not in _page.data,
       detail=str(_page.status_code))
_wipe("OrgAlpha")


# ── 8. A quantity column that is not a quantity ──────────────────────────────
# FAILS TODAY. "available" is a bare keyword, so any column whose name contains
# it is taken as the figure that RANKS the lot -- and COALESCE prefers it over
# the real on-hand column. An .xlsx date cell arrives as an Excel serial, so a
# lot-release date column called "Available Date" is stored, and displayed, as
# 46,550 cartons of stock against a lot that holds 12.

_AVAIL_SHEET = [["Item", "Batch No", "Expiry Date", "On Hand", "Available Date"],
                ["BROOKVALE UHT MILK 1L", "LOT-A", _serial(TODAY + datetime.timedelta(days=10)),
                 "12", _serial(TODAY + datetime.timedelta(days=45))]]
_m, _ms = expiry.detect_columns(["Item", "Batch No", "Expiry Date", "On Hand",
                                 "Available Date"])
_check("a date column called 'Available Date' is not taken as the available quantity",
       _m.get("qty_available") != "Available Date",
       detail=str(_m))

r = _post_xlsx(alpha, _AVAIL_SHEET, "available_date.xlsx")
_stored = db.query("SELECT * FROM expiry_lots WHERE org_name=?", ("OrgAlpha",))
_serial_value = float(_serial(TODAY + datetime.timedelta(days=45)))
_check("a release date is never stored as the quantity available on a lot",
       not any(x["qty_available"] == _serial_value for x in _stored),
       detail=str([(x["item_name"], x["qty_on_hand"], x["qty_available"]) for x in _stored]))
_page = alpha.get("/expiry")
_check("a date is never rendered as a stock figure on the expiry page",
       f"{_serial_value:,.0f}".encode() not in _page.data,
       detail=f"page shows {_serial_value:,.0f} as available stock")
_wipe("OrgAlpha")


# ── 9. A snapshot that imported nothing still supersedes the good one ────────
# FAILS TODAY. Only the newest snapshot is read, and an upload that stored zero
# lots is still the newest snapshot. Upload the wrong export by accident and the
# page's top line goes from "1 expired" to "0 expired" with the previous
# snapshot's expired stock still in the table, unread. The error flash is shown
# once, on the redirect; the next visit is a silent all-clear. This page exists
# to stop stock going bad unnoticed, so a false zero is its worst output.

_wipe("OrgAlpha")
_post_csv(alpha, GOOD_CSV, "good_snapshot.csv")
_before = alpha.get("/expiry").data
_check("the good snapshot reports its expired lot",
       b"<span>1 expired</span>" in _before, detail="setup failed")

_post_csv(alpha, "Item,Expiry Date,Qty On Hand\n", "wrong_export.csv")
_after = alpha.get("/expiry").data
_check("an upload that stored no lots does not blank the expired count while the "
       "org still holds expired stock",
       not (b"<span>0 expired</span>" in _after and db.count_expiry_lots("OrgAlpha") > 0),
       detail=f"page says 0 expired, {db.count_expiry_lots('OrgAlpha')} lots still stored")
_wipe("OrgAlpha")


# ── 10. The days window: a query string is attacker input ────────────────────

_DAYS_ABUSE = ["abc", "", "   ", "-1", "0", "1", "29", "31", "121", "364", "366",
               "999999999999999999999999", "-999999999999999999999999",
               "120;DROP TABLE users", "120 OR 1=1", "<script>alert(1)</script>",
               "1e5", "12.0", "0x78", "null", "None", "[120]", "120%00",
               # URL-encoded Arabic-Indic "30": int() accepts non-ASCII digits,
               # so the whitelist is what has to catch it. Kept encoded so the
               # check name stays printable on a Windows console.
               "%D9%A3%D9%A0", "+120", " 120 ", "1_2_0"]
_post_csv(alpha, GOOD_CSV, "window.csv")
for _d in _DAYS_ABUSE:
    try:
        rr = alpha.get(f"/expiry?days={_d}")
        _ok = (rr.status_code == 200
               and b"<script>alert(1)</script>" not in rr.data
               and db.table_exists("users"))
        _detail = str(rr.status_code)
    except Exception as ex:                      # noqa: BLE001
        _ok, _detail = False, f"RAISED {type(ex).__name__}: {ex}"
    _check(f"a hostile days window is ignored, not fatal: {_d!r}", _ok, detail=_detail)

rr = alpha.get("/expiry?days=abc")
_check("an unparseable window falls back to the 120-day default",
       b"Expiring in the next 120 days" in rr.data, detail="fallback wording differs")
rr = alpha.get("/expiry?days=30&days=365")
_check("a repeated days parameter takes one value from the whitelist",
       rr.status_code == 200 and (b"next 30 days" in rr.data or b"next 365 days" in rr.data),
       detail=str(rr.status_code))
rr = alpha.get("/expiry?days[]=120")
_check("an array-style days parameter is ignored",
       rr.status_code == 200 and b"Expiring in the next 120 days" in rr.data,
       detail=str(rr.status_code))
_wipe("OrgAlpha")


# ── 11. upload_id abuse on the delete route ──────────────────────────────────

_post_csv(alpha, GOOD_CSV, "keep_me.csv")
_KEEP = db.get_expiry_uploads("OrgAlpha", 10)[0]["id"]
for _bad in ["", "   ", "abc", "-1", "0", "1.5", "1 OR 1=1", "1; DROP TABLE users;--",
             "9" * 19, str(2 ** 64), str(-2 ** 64), "null", "[1]", "0x1"]:
    try:
        rr = alpha.post("/expiry/delete", data={"upload_id": _bad}, follow_redirects=True)
        _ok = rr.status_code == 200
        _detail = str(rr.status_code)
    except Exception as ex:                      # noqa: BLE001
        _ok, _detail = False, f"RAISED {type(ex).__name__}: {ex}"
    _check(f"a junk upload_id is ignored, not fatal: {_bad[:24]!r}",
           _ok and db.count_expiry_lots("OrgAlpha") == 2 and db.table_exists("users"),
           detail=_detail)

rr = alpha.post("/expiry/delete", data={}, follow_redirects=True)
_check("a delete POST with no upload_id at all is ignored",
       rr.status_code == 200 and db.count_expiry_lots("OrgAlpha") == 2,
       detail=str(rr.status_code))

rr = alpha.post("/expiry/delete", data={"upload_id": str(_KEEP)}, follow_redirects=True)
_check("the owner's own delete removes the lots and the upload row",
       db.count_expiry_lots("OrgAlpha") == 0 and db.get_expiry_uploads("OrgAlpha", 10) == [],
       detail=str((db.count_expiry_lots("OrgAlpha"),
                   len(db.get_expiry_uploads("OrgAlpha", 10)))))


# ── 12. Org isolation. The one that ends the business ────────────────────────

_wipe("OrgAlpha")
_wipe("OrgBravo")
_post_csv(alpha, "Item,Lot No,Expiry Date,Qty On Hand\n"
                 f"ALDERMOOR SECRET RICE 5KG,LOT-SECRET,{SOON_ON},10\n",
          "alpha_private_lots.csv")
_post_csv(bravo, "Item,Lot No,Expiry Date,Qty On Hand\n"
                 f"VANMARK PRAWN 1KG,LOT-BRAVO,{SOON_ON},20\n",
          "bravo_lots.csv")
_A_ID = db.get_expiry_uploads("OrgAlpha", 10)[0]["id"]
_B_ID = db.get_expiry_uploads("OrgBravo", 10)[0]["id"]
_CUTOFF = (TODAY + datetime.timedelta(days=365)).isoformat()

_bpage = bravo.get("/expiry")
_check("org B's page never contains org A's item name",
       b"ALDERMOOR SECRET RICE 5KG" not in _bpage.data)
_check("org B's page never contains org A's lot number",
       b"LOT-SECRET" not in _bpage.data)
_check("org B's page never contains org A's filename",
       b"alpha_private_lots.csv" not in _bpage.data)
_check("org B's own data does render on org B's page",
       b"VANMARK PRAWN 1KG" in _bpage.data and b"bravo_lots.csv" in _bpage.data)

_check("an upload id alone does not read across orgs",
       db.get_expiry_lots("OrgBravo", _A_ID, _CUTOFF, 100) == []
       and db.get_expiry_lots("OrgAlpha", _B_ID, _CUTOFF, 100) == [],
       detail=str((db.get_expiry_lots("OrgBravo", _A_ID, _CUTOFF, 100),
                   db.get_expiry_lots("OrgAlpha", _B_ID, _CUTOFF, 100))))
_check("counting lots never counts another org's rows",
       db.count_expiry_lots("OrgBravo") == 1 and db.count_expiry_lots("OrgNoSuchOrg") == 0,
       detail=str(db.count_expiry_lots("OrgBravo")))
_check("listing snapshots never lists another org's",
       [u["filename"] for u in db.get_expiry_uploads("OrgBravo", 50)] == ["bravo_lots.csv"],
       detail=str([u["filename"] for u in db.get_expiry_uploads("OrgBravo", 50)]))

bravo.post("/expiry/delete", data={"upload_id": str(_A_ID)}, follow_redirects=True)
_check("org B cannot delete org A's snapshot by id",
       db.count_expiry_lots("OrgAlpha") == 1
       and len(db.get_expiry_uploads("OrgAlpha", 10)) == 1,
       detail=str((db.count_expiry_lots("OrgAlpha"),
                   len(db.get_expiry_uploads("OrgAlpha", 10)))))

db.delete_expiry_upload("OrgBravo", _A_ID)
_check("delete_expiry_upload is org-filtered at the database layer too",
       db.count_expiry_lots("OrgAlpha") == 1, detail=str(db.count_expiry_lots("OrgAlpha")))

db.finalise_expiry_upload("OrgBravo", _A_ID, 999, 999, 999, '[{"row":1}]')
_arow = db.get_expiry_uploads("OrgAlpha", 10)[0]
_check("org B cannot overwrite the counters on org A's snapshot",
       (_arow["rows_imported"], _arow["rows_rejected"]) == (1, 0),
       detail=str(dict(_arow)))

# Guessing ids around a real one must never surface another org's rows. Org B
# owns an id in this range, so the test is on the CONTENT: nothing org A owns
# may come back, whichever id is guessed.
_leaked = [r["item_name"] for d in range(-3, 4)
           for r in db.get_expiry_lots("OrgBravo", _A_ID + d, _CUTOFF, 100)
           if r["item_name"] != "VANMARK PRAWN 1KG"]
_check("guessing ids around a real one never surfaces another org's lots",
       _leaked == [], detail=str(_leaked))

# A third org that has never uploaded sees an empty page, not somebody else's.
_CID = _make_user("carol@example.com", "OrgCarol")
carol = _client(_CID, "carol@example.com", "OrgCarol")
_cpage = carol.get("/expiry")
_check("an org with no snapshot sees the empty state, not another org's lots",
       _cpage.status_code == 200 and b"No lot list uploaded yet" in _cpage.data
       and b"ALDERMOOR SECRET RICE 5KG" not in _cpage.data
       and b"VANMARK PRAWN 1KG" not in _cpage.data, detail=str(_cpage.status_code))


# ── 13. Roles and the logged-out client ──────────────────────────────────────

_before = db.count_expiry_lots("OrgAlpha")
r = _post_csv(viewer, GOOD_CSV, "viewer_upload.csv")
_check("a viewer cannot upload",
       db.count_expiry_lots("OrgAlpha") == _before
       and len(db.get_expiry_uploads("OrgAlpha", 10)) == 1,
       detail=str((db.count_expiry_lots("OrgAlpha"), _before)))
_check("a viewer's refused upload leaves no scratch table and no file",
       _scratch_tables() == []
       and [f for f in _upload_files() if f not in _BASELINE_FILES] == [],
       detail=str((_scratch_tables(), _upload_files())))

viewer.post("/expiry/delete", data={"upload_id": str(_A_ID)}, follow_redirects=True)
_check("a viewer cannot delete",
       db.count_expiry_lots("OrgAlpha") == _before,
       detail=str(db.count_expiry_lots("OrgAlpha")))
_vpage = viewer.get("/expiry")
_check("a viewer can read the page but is not offered the remove button",
       _vpage.status_code == 200 and b"Remove this snapshot" not in _vpage.data,
       detail=str(_vpage.status_code))

for _path, _method in (("/expiry", "get"), ("/expiry/upload", "post"),
                       ("/expiry/delete", "post")):
    rr = getattr(anon, _method)(_path, follow_redirects=False)
    _check(f"a logged-out client is redirected from {_path}",
           rr.status_code in (301, 302, 308), detail=str(rr.status_code))
rr = anon.get("/expiry", follow_redirects=True)
_check("a logged-out client sees no lot data",
       b"ALDERMOOR SECRET RICE 5KG" not in rr.data and b"VANMARK PRAWN 1KG" not in rr.data)


# ── 14. The per-org ceiling under repetition ─────────────────────────────────
# Lowered rather than uploading 40,000 rows. Two consecutive uploads must not
# walk past the cap, and the refused one must leave nothing behind.

_wipe("OrgAlpha")
_real_cap = appmod.MAX_EXPIRY_LOTS_PER_ORG
appmod.MAX_EXPIRY_LOTS_PER_ORG = 4
try:
    _THREE = ("Item,Expiry Date,Qty On Hand\n"
              f"ALDERMOOR RICE 5KG,{SOON_ON},10\n"
              f"PADIMAS BEEF 2KG,{SOON_ON},20\n"
              f"VANMARK PRAWN 1KG,{SOON_ON},30\n")
    _counts = []
    for _i in range(4):
        _post_csv(alpha, _THREE, f"repeat_{_i}.csv")
        _counts.append(db.count_expiry_lots("OrgAlpha"))
    _check("repeated uploads cannot walk past the per-org ceiling",
           max(_counts) <= appmod.MAX_EXPIRY_LOTS_PER_ORG, detail=str(_counts))
    _check("a refused upload leaves no partial rows, no upload row, no scratch table",
           db.count_expiry_lots("OrgAlpha") == 3
           and len(db.get_expiry_uploads("OrgAlpha", 50)) == 1
           and _scratch_tables() == []
           and [f for f in _upload_files() if f not in _BASELINE_FILES] == [],
           detail=str(_state("OrgAlpha")))
    _check("the refusal names the fix rather than failing silently",
           b"stored lots" in _post_csv(alpha, _THREE, "over.csv").data,
           detail="no cap message")
finally:
    appmod.MAX_EXPIRY_LOTS_PER_ORG = _real_cap
_wipe("OrgAlpha")


# ── 15. Boundaries: the window edge, the row cap, zero quantity ─────────────
# Off-by-one here is invisible: a lot that expires on the last day of the
# window either shows or it does not, and nobody notices the one that did not.

_wipe("OrgAlpha")
_EDGES = {
    "expired yesterday":        TODAY - datetime.timedelta(days=1),
    "expires today":            TODAY,
    "expires tomorrow":         TODAY + datetime.timedelta(days=1),
    "expires on the last day of the window":  TODAY + datetime.timedelta(days=120),
    "expires one day past the window":        TODAY + datetime.timedelta(days=121),
}
_post_csv(alpha, "Item,Lot No,Expiry Date,Qty On Hand\n"
                 + "".join(f"BROOKVALE {k.upper()},LOT-{i},{v.isoformat()},10\n"
                           for i, (k, v) in enumerate(_EDGES.items())),
          "edges.csv")
_edge_page = alpha.get("/expiry").data
_body_split = _edge_page.split(b"Expiring in the next 120 days")
_expired_half, _soon_half = _body_split[0], _body_split[-1]
_check("a lot that expired yesterday is in the expired table",
       b"LOT-0" in _expired_half, detail="missing from the expired section")
_check("a lot expiring TODAY is not filed as already expired",
       b"LOT-1" in _soon_half and b"LOT-1" not in _expired_half,
       detail="today's lot was reported as already expired")
_check("a lot expiring exactly on the last day of the window is shown",
       b"LOT-3" in _soon_half, detail="the window edge is off by one")
_check("a lot expiring one day past the window is not shown",
       b"LOT-4" not in _edge_page, detail="the window let in a lot beyond it")
_check("the expired count matches the one lot that is actually expired",
       b"<span>1 expired</span>" in _edge_page, detail="hero count disagrees")
_wipe("OrgAlpha")

# The row cap, lowered rather than uploading 20,000 rows. Exactly at the cap
# must import; one past it must be refused with nothing left behind.
_real_rows = appmod.MAX_EXPIRY_ROWS
appmod.MAX_EXPIRY_ROWS = 3
try:
    _rows3 = "".join(f"NORDVIK ITEM {i},{SOON_ON},{i + 1}\n" for i in range(3))
    r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n" + _rows3, "exactly_cap.csv")
    _check("a file with exactly the maximum number of rows imports all of them",
           db.count_expiry_lots("OrgAlpha") == 3 and b"Read 3 rows" in r.data,
           detail=str(db.count_expiry_lots("OrgAlpha")))
    _wipe("OrgAlpha")
    _rows4 = "".join(f"NORDVIK ITEM {i},{SOON_ON},{i + 1}\n" for i in range(4))
    r = _post_csv(alpha, "Item,Expiry Date,Qty On Hand\n" + _rows4, "over_cap.csv")
    _check("a file one row past the cap is refused and leaves nothing behind",
           db.count_expiry_lots("OrgAlpha") == 0 and _state("OrgAlpha") == (0, 0, [], []),
           detail=str(_state("OrgAlpha")))
finally:
    appmod.MAX_EXPIRY_ROWS = _real_rows
_wipe("OrgAlpha")

# Zero available with stock still on hand. Applied decision (plan section 11.5):
# stored, filtered off the page by the "> 0" clause. Pinned so the day it
# changes it is a deliberate change: a lot that is fully allocated and expiring
# tomorrow is invisible here, and 500 cartons still have to go somewhere.
_post_csv(alpha, "Item,Lot No,Expiry Date,Qty On Hand,Qty Available\n"
                 f"BROOKVALE ALLOCATED 1L,LOT-Z,{SOON_ON},500,0\n"
                 f"KESTREL TRACE 1L,LOT-T,{SOON_ON},500,0.0001\n", "zero_avail.csv")
_zpage = alpha.get("/expiry").data
_check("a fully allocated lot is stored", db.count_expiry_lots("OrgAlpha") == 2,
       detail=str(db.count_expiry_lots("OrgAlpha")))
_check("zero available hides the lot even with 500 on hand (documented decision)",
       b"LOT-Z" not in _zpage, detail="behaviour changed; check plan section 11.5")
_check("a trace quantity above zero is still shown", b"LOT-T" in _zpage)
_wipe("OrgAlpha")


# ── 16. Bounded reads ────────────────────────────────────────────────────────

_post_csv(alpha, "Item,Expiry Date,Qty On Hand\n"
                 + "".join(f"BROOKVALE ITEM {i},{SOON_ON},{i + 1}\n" for i in range(200)),
          "two_hundred.csv")
_UID = db.get_expiry_uploads("OrgAlpha", 10)[0]["id"]
_check("200 lots import", db.count_expiry_lots("OrgAlpha") == 200,
       detail=str(db.count_expiry_lots("OrgAlpha")))
_check("the page read honours an explicit LIMIT",
       len(db.get_expiry_lots("OrgAlpha", _UID, _CUTOFF, 1)) == 1
       and len(db.get_expiry_lots("OrgAlpha", _UID, _CUTOFF, 10)) == 10,
       detail=str(len(db.get_expiry_lots("OrgAlpha", _UID, _CUTOFF, 10))))
_check("a zero limit reads nothing",
       db.get_expiry_lots("OrgAlpha", _UID, _CUTOFF, 0) == [])
# Documented trap, not a live bug: SQLite treats LIMIT -1 as no limit at all.
# Every caller today passes a constant, but this read has no floor of its own,
# so the day someone passes a user-supplied limit the bound disappears.
_check("a negative limit disables the LIMIT entirely (documented trap)",
       len(db.get_expiry_lots("OrgAlpha", _UID, _CUTOFF, -1)) == 200,
       detail=str(len(db.get_expiry_lots("OrgAlpha", _UID, _CUTOFF, -1))))

# Truncation must keep the URGENT end of the list and must say it truncated.
_real_shown = appmod.MAX_EXPIRY_LOTS_SHOWN
appmod.MAX_EXPIRY_LOTS_SHOWN = 2
try:
    _tpage = alpha.get("/expiry").data
    _check("a truncated page says so rather than quietly showing a subset",
           b"Showing the first 2 lots" in _tpage, detail="no truncation notice")
    _check("truncation keeps the earliest expiries, not an arbitrary two",
           _tpage.count(b"<tr>") <= 4, detail=str(_tpage.count(b"<tr>")))
finally:
    appmod.MAX_EXPIRY_LOTS_SHOWN = _real_shown

# 200 lots that all share one expiry date: no pairing, no quadratic anything.
_t0 = time.time()
_page200 = alpha.get("/expiry")
_dt200 = time.time() - _t0
_check("200 same-date lots render in bounded time",
       _page200.status_code == 200 and _dt200 < 5.0, detail=f"{_dt200:.2f}s")
_wipe("OrgAlpha")

_post_csv(alpha, "Item,Expiry Date,Qty On Hand\n"
                 + "".join(f"BROOKVALE ITEM {i},{SOON_ON},{i + 1}\n" for i in range(100)),
          "one_hundred.csv")
_page100 = alpha.get("/expiry")
_check("doubling the lots does not more than double the rendered page",
       len(_page200.data) <= 2.5 * len(_page100.data),
       detail=f"{len(_page100.data):,} bytes at 100, {len(_page200.data):,} at 200")
_wipe("OrgAlpha")

# Duplicate item names in one snapshot stay one row in, one row out.
_post_csv(alpha, "Item,Lot No,Expiry Date,Qty On Hand\n"
                 + "".join(f"BROOKVALE UHT MILK 1L,LOT-{i},{SOON_ON},10\n"
                           for i in range(50)), "dupes.csv")
_check("50 lots of one item stay 50 rows, never merged or paired",
       db.count_expiry_lots("OrgAlpha") == 50,
       detail=str(db.count_expiry_lots("OrgAlpha")))
_wipe("OrgAlpha")


# ── 17. Schema: additive, idempotent, and NULL-tolerant on every read path ───

db.init_db()
db.init_db()
_lot_cols = [c["name"] for c in db.query("PRAGMA table_info(expiry_lots)")]
_up_cols  = [c["name"] for c in db.query("PRAGMA table_info(expiry_uploads)")]
_check("init_db is idempotent for the two new tables",
       len(_lot_cols) == len(set(_lot_cols)) and len(_up_cols) == len(set(_up_cols))
       and "expiry_date" in _lot_cols and "rejects_json" in _up_cols,
       detail=str((_lot_cols, _up_cols)))
_check("running init_db twice does not disturb the existing tables",
       db.table_exists("users") and db.table_exists("tender_commitments"))

# A row written before any later column existed: everything optional is NULL.
_NULL_ORG = "OrgNulls"
_nuid = db.create_expiry_upload(_NULL_ORG, "legacy.xlsx", "")
db.execute("INSERT INTO expiry_lots (org_name, upload_id, item_code, item_name, "
           "match_key, lot_no, uom, expiry_date, qty_on_hand, qty_available) "
           "VALUES (?,?,?,?,?,?,?,?,?,?)",
           (_NULL_ORG, _nuid, None, "NORDVIK SALMON 1KG", "nordviksalmon1kg",
            None, None, SOON_ON, 5.0, None))
db.execute("INSERT INTO expiry_lots (org_name, upload_id, item_code, item_name, "
           "match_key, lot_no, uom, expiry_date, qty_on_hand, qty_available) "
           "VALUES (?,?,?,?,?,?,?,?,?,?)",
           (_NULL_ORG, _nuid, None, "PADIMAS BEEF 2KG", "padimasbeef2kg",
            None, None, SOON_ON, None, None))
_nid = _make_user("nulls@example.com", _NULL_ORG)
nulls = _client(_nid, "nulls@example.com", _NULL_ORG)
_npage = nulls.get("/expiry")
_check("a lot with NULL code, lot, uom and available still renders",
       _npage.status_code == 200 and b"NORDVIK SALMON 1KG" in _npage.data,
       detail=str(_npage.status_code))
_check("a lot with both quantities NULL is never shown as stock",
       b"PADIMAS BEEF 2KG" not in _npage.data, detail="a NULL-quantity lot was ranked")
_check("an upload row with a NULL rejects_json renders",
       b"legacy.xlsx" in _npage.data)

# A rejects_json blob that is not a list of dicts must not take the page down.
# The route guards json.loads against a PARSE error but not against a valid
# JSON value of the wrong SHAPE, and the template then calls length on it.
# Written straight to the table here: nothing on the upload path can store a
# non-list today, so this is the defence-in-depth half, not a live exploit.
for _blob in ("not json at all", "null", '"a string"', "123", "true",
              '[{"row": 1}]', '{"row": 1}', "[]"):
    db.execute("UPDATE expiry_uploads SET rejects_json=? WHERE id=? AND org_name=?",
               (_blob, _nuid, _NULL_ORG))
    try:
        _code = nulls.get("/expiry").status_code
    except Exception as ex:                      # noqa: BLE001 - a 500 must not end the run
        _code = f"RAISED {type(ex).__name__}: {ex}"
    _check(f"a malformed rejects_json blob does not break the page: {_blob[:20]!r}",
           _code == 200, detail=str(_code))
_wipe(_NULL_ORG)


# ── 18. The read_only dimension trap, as a regression ────────────────────────
# openpyxl in read_only mode reports this workbook's dimensions as 1x1, so a
# reader that trusts the declared dimension imports zero rows. Zero lots from
# this fixture is that bug's signature.

TITLE_ROWS = [
    ["Printed By:", "warehouse@example.test"],
    ["Printed Date:", TODAY.isoformat()],
    ["BROOKVALE DISTRIBUTION PTE LTD"],
    ["Lot Tracking Stock Status"],
    ["Report Parameter & Filter:"],          # the & that breaks raw XML
    ["Location: ALL / Expiry: ALL"],
]


def _row22(**cells):
    return [cells.get(name) for name in SAN]


_REAL_SHAPE = TITLE_ROWS + [RAW] + [
    _row22(location_code="W1", location_name="Main Store", inventory_code="BRK-001",
           inventory_description="BROOKVALE UHT MILK 1L", uom="CTN", lot_no="LOT-A",
           lot_reference_no="REF-A", original_receipt_date=_serial(TODAY),
           expiry_date=_serial(TODAY - datetime.timedelta(days=5)),
           supplier_name="BROOKVALE DAIRY", pack_size="12x1L",
           qty_on_hand="100.0", qty_selected="0.0", qty_allocated="60.0",
           qty_allocated_selected="0.0", qty_available="40.0"),
    _row22(inventory_code="Inventory Total : BRK-001", qty_selected="8517.0"),
    _row22(pack_size="Total (CARTON)", qty_on_hand="LOOSE"),
]
_wipe("OrgAlpha")
r = _post_xlsx(alpha, _REAL_SHAPE, "lot_tracking_22col.xlsx")
_check("the six-title-row, 22-column .xlsx imports a non-zero number of lots",
       db.count_expiry_lots("OrgAlpha") > 0,
       detail=f"{db.count_expiry_lots('OrgAlpha')} lots — the signature of a "
              f"reader trusting the declared sheet dimensions")
_check("the title rows never become the item names",
       not any("Printed" in (x["item_name"] or "")
               for x in db.query("SELECT * FROM expiry_lots WHERE org_name=?", ("OrgAlpha",))),
       detail="a report title was stored as a product")
_check("the counters on the real shape add up",
       b"Read 3 rows: 1 lots stored, 2 summary lines skipped, 0 rows unreadable."
       in r.data, detail="flash text differs")
_wipe("OrgAlpha")

with open(os.path.join(ROOT, "expiry.py"), "r", encoding="utf-8") as _fh:
    _src = _fh.read()
_check("expiry.py does not import openpyxl", "openpyxl" not in _src)
_check("expiry.py builds no SQL of its own",
       not any(k in _src.upper() for k in ("SELECT ", "INSERT ", "DELETE FROM", "UPDATE ")),
       detail="SQL leaked into the pure-function module")


# ── 19. Nothing survives the run ─────────────────────────────────────────────

_check("no expiry scratch table survives the whole run", _scratch_tables() == [],
       detail=str(_scratch_tables()))
_check("no tmp_expiry file survives the whole run",
       [f for f in _upload_files() if f not in _BASELINE_FILES] == [],
       detail=str(_upload_files()))


if _FAILED:
    # Echoed to stderr as well: run_tests.py shows only the last few lines of a
    # failing script, and a deliberate junk-file case logs a traceback to
    # stderr. Without this the tail would be that traceback instead of the list
    # of what actually broke.
    for _stream in (sys.stdout, sys.stderr):
        print(f"\n{len(_FAILED)} CHECK(S) FAILED:", file=_stream)
        for _n in _FAILED:
            print("  - " + _n, file=_stream)
        print("SOME TESTS FAILED", file=_stream)
    sys.exit(1)
print("\nAll expiry break tests passed.")
