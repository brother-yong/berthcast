"""Hostile input against the tender upload form and the sheets it accepts.

The companion file (test_tender_sheets.py) proves the three real sheet shapes
import. This one tries to make the same code lie, crash, leak or lose data.

Four things are pinned here that nothing else covers:
  1. a rejected form leaves NO upload row, NO scratch table and NO file on disk
  2. the period arithmetic never divides by zero and never guesses a basis
  3. a quantity cell the client's own sheet can plausibly contain ("nan",
     "inf") must not be storable and must not take the whole sheet down
  4. one org cannot read, render or delete another org's tender data, and a
     guessed or oversized upload_id is ignored rather than fatal

Some checks in this file FAIL on purpose: they describe behaviour that is
wrong today. Do not loosen them to make the suite green -- fix the code or
argue the check is wrong. Every failure is listed again at the end of the run
so the runner's 12-line tail shows what broke.

Throwaway temp DB, stubbed anthropic client, no API calls. Invented brands
only, the repo is public. Run: python tests/test_tender_sheets_break.py
"""
import datetime
import io
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_tender_sheets_break.db")
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

_FAILED = []
_D = datetime.date


def _check(name, cond, detail=""):
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


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


# Sane defaults for every field, so each case overrides exactly one thing and
# a failure names the field that caused it.
_FORM = {"customer": "NORDVIK CATERING", "period_start": "2026-07-01",
         "period_end": "2026-12-31", "qty_basis": "per_month"}


def _upload(client, csv_text, filename="tenders.csv", **form):
    data = dict(_FORM)
    data.update(form)
    data["file"] = (io.BytesIO(csv_text.encode("utf-8")), filename)
    return client.post("/tenders/upload", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


def _scratch_tables():
    return [r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tender_import_%'")]


def _upload_files():
    d = appmod.UPLOAD_FOLDER
    return sorted(f for f in os.listdir(d) if "tender" in f) if os.path.isdir(d) else []


def _wipe(org):
    for u in db.get_tender_uploads(org):
        db.delete_tender_upload(org, u["id"])


_BASELINE_FILES = _upload_files()

ALPHA_ID = _make_user("alpha@example.com", "OrgAlpha")
BRAVO_ID = _make_user("bravo@example.com", "OrgBravo")
VIEW_ID  = _make_user("viewer@example.com", "OrgAlpha")

alpha  = _client(ALPHA_ID, "alpha@example.com", "OrgAlpha")
bravo  = _client(BRAVO_ID, "bravo@example.com", "OrgBravo")
viewer = _client(VIEW_ID, "viewer@example.com", "OrgAlpha", role="viewer")

GOOD_CSV = ("Item,Qty\n"
            "BROOKVALE UHT MILK 1L,1200\n"
            "KESTREL ORANGE JUICE 1L,800\n")


# ── 1. Hostile form fields ───────────────────────────────────────────────────
# Everything on this form is attacker-controlled text posted to an
# authenticated route. A refusal has to be total: the plan's rule is no upload
# row, no scratch table and no file, because create_tender_upload used to run
# before anything was validated.

_BAD_FORMS = [
    ("customer is missing entirely",        {"customer": ""}),
    ("customer is whitespace only",         {"customer": "   \t \n "}),
    ("customer is 121 characters",          {"customer": "N" * 121}),
    ("customer is 5000 characters",         {"customer": "N" * 5000}),
    ("customer is a script payload over the cap",
                                            {"customer": "<script>alert(1)</script>" * 20}),
    ("period_start is missing",             {"period_start": ""}),
    ("period_end is missing",               {"period_end": ""}),
    ("period_start is not a date",          {"period_start": "not-a-date"}),
    ("period_start is 2026-13-45",          {"period_start": "2026-13-45"}),
    ("period_start is 31/02/2026",          {"period_start": "31/02/2026"}),
    ("period_end is a bare word",           {"period_end": "whenever"}),
    ("period_end is before period_start",   {"period_start": "2026-12-31",
                                             "period_end": "2026-07-01"}),
    ("qty_basis is missing",                {"qty_basis": ""}),
    ("qty_basis is 'weekly'",               {"qty_basis": "weekly"}),
    ("qty_basis is the wrong case",         {"qty_basis": "PER_MONTH"}),
    ("qty_basis has a trailing space",      {"qty_basis": "per_month "}),
    ("qty_basis has a leading space",       {"qty_basis": " per_month"}),
    ("qty_basis carries SQL",               {"qty_basis": "per_month; DROP TABLE users;--"}),
    ("qty_basis is a list of both values",  {"qty_basis": "per_month,period_total"}),
]

for _name, _override in _BAD_FORMS:
    r = _upload(alpha, GOOD_CSV, "rejected.csv", **_override)
    _state = (r.status_code,
              len(db.get_tender_commitments("OrgAlpha")),
              len(db.get_tender_uploads("OrgAlpha")),
              _scratch_tables(),
              [f for f in _upload_files() if f not in _BASELINE_FILES])
    _check(f"refused, and nothing left behind: {_name}",
           _state == (200, 0, 0, [], []), detail=str(_state))
    _wipe("OrgAlpha")

# Missing keys, not just empty ones: a hand-rolled POST need not send a field.
r = alpha.post("/tenders/upload",
               data={"file": (io.BytesIO(GOOD_CSV.encode()), "nofields.csv")},
               content_type="multipart/form-data", follow_redirects=True)
_check("a POST with no form fields at all is refused cleanly",
       (r.status_code, len(db.get_tender_uploads("OrgAlpha")), _scratch_tables())
       == (200, 0, []),
       detail=str((r.status_code, len(db.get_tender_uploads("OrgAlpha")), _scratch_tables())))

# Boundaries on the one length cap the route owns.
r = _upload(alpha, GOOD_CSV, "cap120.csv", customer="N" * 120)
_check("a customer of exactly 120 characters is accepted",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))
_check("a 120-character customer is stored whole, never truncated",
       db.get_tender_uploads("OrgAlpha")[0]["customer"] == "N" * 120)
_wipe("OrgAlpha")

r = _upload(alpha, GOOD_CSV, "pad120.csv", customer="   " + ("N" * 120) + "   ")
_check("padding is stripped before the length cap is applied",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))
_wipe("OrgAlpha")

# Hostile customer text is stored raw and escaped on the way out. It must never
# reach the page as live markup, and it must never reach SQL as SQL.
for _label, _cust in [("markup", "<script>alert(1)</script> CATERING"),
                      ("a SQL quote", "NORDVIK'); DROP TABLE users;--"),
                      ("a newline", "NORDVIK\nCATERING"),
                      ("a null byte", "NORDVIK\x00CATERING")]:
    _upload(alpha, GOOD_CSV, "cust.csv", customer=_cust)
    page = alpha.get("/tenders")
    _check(f"a customer containing {_label} renders escaped and harms nothing",
           page.status_code == 200
           and b"<script>alert(1)</script>" not in page.data
           and db.table_exists("users"),
           detail=str(page.status_code))
    _wipe("OrgAlpha")


# ── 2. Period arithmetic ─────────────────────────────────────────────────────
# Floored at 1 so a same-day period can never divide by zero. Every one of
# these is a number a client can produce with a real contract.

_MONTHS = [
    ("a same-day period is one month, never zero",   _D(2026, 7, 1),  _D(2026, 7, 1),  1),
    ("a same-day period on a leap day is one month", _D(2028, 2, 29), _D(2028, 2, 29), 1),
    ("a period spanning a leap day counts both months",
                                                     _D(2028, 1, 29), _D(2028, 2, 29), 2),
    ("end day one short of start day rounds down",   _D(2026, 7, 15), _D(2026, 8, 14), 1),
    ("end day exactly at start day rounds up",       _D(2026, 7, 15), _D(2026, 8, 15), 2),
    ("a five-year contract is 60 months",            _D(2026, 1, 1),  _D(2030, 12, 31), 60),
    ("a reversed period floors at one, it does not go negative",
                                                     _D(2026, 12, 31), _D(2026, 7, 1), 1),
    ("a century-long period does not overflow",      _D(2026, 1, 1),  _D(2126, 1, 1), 1201),
]
for _name, _s, _e, _want in _MONTHS:
    _got = tenders.months_in_period(_s, _e)
    _check(_name, _got == _want, detail=f"got {_got}, wanted {_want}")

# 31 Jan to 28 Feb and 29 Jan to 29 Feb are the same length of contract but the
# rule gives 1 and 2. Pinned, not endorsed: if this ever changes, it changes
# every derived monthly figure for a February period.
_check("Feb-end periods are leap-year sensitive (documented, pinned)",
       (tenders.months_in_period(_D(2027, 1, 29), _D(2027, 2, 28)),
        tenders.months_in_period(_D(2028, 1, 29), _D(2028, 2, 29))) == (1, 2),
       detail=str((tenders.months_in_period(_D(2027, 1, 29), _D(2027, 2, 28)),
                   tenders.months_in_period(_D(2028, 1, 29), _D(2028, 2, 29)))))

_DEGENERATE = [
    ("no quantity means no monthly figure",
     (None, "period_total", "2026-07-01", "2026-12-31"), None),
    ("a NULL basis is never guessed at",
     (100, None, "2026-07-01", "2026-12-31"), None),
    ("an unknown basis is never guessed at",
     (100, "weekly", "2026-07-01", "2026-12-31"), None),
    ("a wrong-case basis is not accepted as the real one",
     (100, "PER_MONTH", "2026-07-01", "2026-12-31"), None),
    ("a basis with whitespace is not accepted as the real one",
     (100, "per_month ", "2026-07-01", "2026-12-31"), None),
    ("an unreadable start date gives no figure",
     (100, "period_total", "garbage", "2026-12-31"), None),
    ("NULL dates give no figure",
     (100, "period_total", None, None), None),
    ("a non-numeric quantity gives no figure",
     (" ", "per_month", "2026-07-01", "2026-12-31"), None),
    ("a same-day period divides by one",
     (1800, "period_total", "2026-07-01", "2026-07-01"), 1800.0),
    ("a reversed period divides by one rather than raising",
     (100, "period_total", "2026-12-31", "2026-07-01"), 100.0),
    ("a six-month total converts to a monthly rate",
     (1800, "period_total", "2026-07-01", "2026-12-31"), 300.0),
]
for _name, _args, _want in _DEGENERATE:
    try:
        _got = tenders.monthly_rate(*_args)
    except Exception as ex:                      # noqa: BLE001 - the point is it must not
        _got = f"RAISED {type(ex).__name__}: {ex}"
    _check("monthly_rate: " + _name, _got == _want, detail=f"got {_got!r}, wanted {_want!r}")

_check("format_qty never raises on junk",
       (tenders.format_qty("abc"), tenders.format_qty(None), tenders.format_qty(0))
       == ("", "", "0"),
       detail=str((tenders.format_qty("abc"), tenders.format_qty(None), tenders.format_qty(0))))


# ── 3. parse_quantity: "nan" and "inf" are not quantities ────────────────────
# WAS a pre-existing hole, not a regression: `if qty <= 0` is False for NaN, and
# inf is greater than zero, so both walked straight through the guard. Found by
# the tester, fixed with math.isfinite; these checks now pass and exist to keep
# it that way. NaN was the dangerous one -- SQLite stores it as NULL against a
# NOT NULL column, so one junk cell aborted the insert of every good row too.

_check("parse_quantity rejects the text 'nan'",
       tenders.parse_quantity("nan") is None, detail=repr(tenders.parse_quantity("nan")))
_check("parse_quantity rejects the text 'inf'",
       tenders.parse_quantity("inf") is None, detail=repr(tenders.parse_quantity("inf")))
_check("parse_quantity rejects the text 'Infinity'",
       tenders.parse_quantity("Infinity") is None,
       detail=repr(tenders.parse_quantity("Infinity")))
_check("parse_quantity rejects a number that overflows to infinity",
       tenders.parse_quantity("1e400") is None, detail=repr(tenders.parse_quantity("1e400")))
_check("parse_quantity still rejects the ordinary bad values",
       [tenders.parse_quantity(v) for v in ("0", "-5", "", "  ", None, "about 40", "-inf")]
       == [None] * 7,
       detail=str([tenders.parse_quantity(v) for v in
                   ("0", "-5", "", "  ", None, "about 40", "-inf")]))
_check("parse_quantity still accepts the messy-but-real forms",
       [tenders.parse_quantity(v) for v in ("1,200", "1 200.5", " 40 ", 12)]
       == [1200.0, 1200.5, 40.0, 12.0],
       detail=str([tenders.parse_quantity(v) for v in ("1,200", "1 200.5", " 40 ", 12)]))

# End to end: SQLite stores NaN as NULL, the quantity column is NOT NULL, so a
# single "nan" cell raises inside save_tender_rows and the route's blanket
# except discards the WHOLE sheet with a message that names no cell.
r = _upload(alpha, "Item,Qty\n"
                   "BROOKVALE UHT MILK 1L,1200\n"
                   "KESTREL ORANGE JUICE 1L,nan\n"
                   "PADIMAS BEEF 2KG,600\n", "nan_cell.csv")
_check("one 'nan' cell does not throw away the two good rows beside it",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))
_check("one 'nan' cell is a skipped row, not a server error",
       b"Something went wrong" not in r.data,
       detail="route fell into its blanket exception handler")
_wipe("OrgAlpha")

r = _upload(alpha, "Item,Qty\nBROOKVALE UHT MILK 1L,inf\n", "inf_cell.csv")
_check("an 'inf' quantity is never stored as a commitment",
       len(db.get_tender_commitments("OrgAlpha")) == 0,
       detail=str([x["quantity"] for x in db.get_tender_commitments("OrgAlpha")]))
_check("'inf' never reaches a rendered cell",
       b">inf<" not in r.data, detail="page shows a commitment of infinity")
_wipe("OrgAlpha")


# ── 4. Column detection under hostile and degenerate headers ─────────────────

_HEADER_CASES = [
    ("two headers match the same field",  ["Qty", "Quantity", "Item"],
     {"quantity": "Quantity", "item": "Item"}, [], []),
    ("one header could satisfy both fields, so item is reported missing",
     ["Item Qty"], {"quantity": "Item Qty"}, ["item"], []),
    ("no usable columns at all",          ["Colour", "Remarks", "Notes"],
     {}, ["quantity", "item"], []),
    ("an empty header list",              [],
     {}, ["quantity", "item"], []),
    ("an empty-string header is ignored", ["", "Item", "Qty"],
     {"quantity": "Qty", "item": "Item"}, [], []),
    ("a punctuation-only header is ignored", ["***", "!!!", "Item", "Qty"],
     {"quantity": "Qty", "item": "Item"}, [], []),
    ("a None header does not crash",      [None, "Item", "Qty"],
     {"quantity": "Qty", "item": "Item"}, [], []),
    ("a numeric header does not crash",   [1, 2.5, "Item", "Qty"],
     {"quantity": "Qty", "item": "Item"}, [], []),
    ("a header carrying a SQL quote is ignored",
     ['"; DROP TABLE users;--', "Item", "Qty"],
     {"quantity": "Qty", "item": "Item"}, [], []),
    ("a 300-character header does not crash",
     ["I" * 300, "Item", "Qty"], {"quantity": "Qty", "item": "Item"}, [], []),
    ("a script-tag header is ignored",
     ["<script>alert(1)</script>", "Item", "Qty"],
     {"quantity": "Qty", "item": "Item"}, [], []),
    ("25 duplicate UOM columns claim nothing",
     ["Item", "Qty"] + ["UOM"] + [f"uom_{i}" for i in range(1, 25)],
     {"quantity": "Qty", "item": "Item"}, [], []),
    ("a bare 'To' column is refused as an end date",
     ["Item", "Qty", "To"], {"quantity": "Qty", "item": "Item"}, [], [("end", "To")]),
    ("a bare 'From' column is refused as a start date",
     ["Item", "Qty", "From"], {"quantity": "Qty", "item": "Item"}, [], [("start", "From")]),
    ("the old five-column sheet is refused, in field order",
     ["Customer", "Item Description", "Tender Qty", "Start Date", "End Date"],
     {"quantity": "Tender Qty", "item": "Item Description"}, [],
     [("customer", "Customer"), ("start", "Start Date"), ("end", "End Date")]),
    ("the same sheet after the ingest layer sanitises it is refused too",
     ["customer", "item_description", "tender_qty", "start_date", "end_date"],
     {"quantity": "tender_qty", "item": "item_description"}, [],
     [("customer", "customer"), ("start", "start_date"), ("end", "end_date")]),
]
for _name, _hdrs, _want_map, _want_miss, _want_conf in _HEADER_CASES:
    try:
        _got = tenders.detect_columns(_hdrs)
    except Exception as ex:                      # noqa: BLE001
        _got = f"RAISED {type(ex).__name__}: {ex}"
    _check("detect_columns: " + _name, _got == (_want_map, _want_miss, _want_conf),
           detail=str(_got))

# The plan's tripwire: a false hit on any of these refuses a real client file,
# which is worse than the feature not shipping. Each is checked on its own so a
# failure names the exact header that broke.
for _hdr in ["Grouping", "REMARK", "Column1", "Column2", "Business Entity", "Brand",
             "Packing", "Country", "No.", "<COMPANY> UPDATE", "Unit", "UOM",
             "* Stk ID", "Est Qty", "Product name", "Stk Name", "Description",
             "SIX MONTH VOLUME", "Est. Monthly Consumption", "company_update",
             "business_entity", "six_month_volume", "est_monthly_consumption"]:
    _, _, _conf = tenders.detect_columns(["Item", "Qty", _hdr])
    _check(f"a real-world header does not falsely refuse the sheet: {_hdr!r}",
           _conf == [], detail=str(_conf))

# Conflict sheets through the real route: refused, and nothing left behind.
_CONFLICT_SHEETS = [
    ("its own Customer column", "Customer,Item,Qty\nNORDVIK,BROOKVALE UHT MILK 1L,10\n"),
    ("its own date columns",
     "Item,Qty,Start Date,End Date\nBROOKVALE UHT MILK 1L,10,01/01/2026,31/12/2026\n"),
    ("the whole old five-column format",
     "Customer,Item Description,Tender Qty,Start Date,End Date\n"
     "NORDVIK CATERING,BROOKVALE UHT MILK 1L,1200,01/01/2026,31/12/2026\n"),
]
for _name, _csv in _CONFLICT_SHEETS:
    r = _upload(alpha, _csv, "conflict.csv")
    _state = (b"has its own customer or date column" in r.data,
              len(db.get_tender_commitments("OrgAlpha")),
              len(db.get_tender_uploads("OrgAlpha")),
              _scratch_tables(),
              [f for f in _upload_files() if f not in _BASELINE_FILES])
    _check(f"a sheet with {_name} is refused and leaves nothing behind",
           _state == (True, 0, 0, [], []), detail=str(_state))
    _wipe("OrgAlpha")

r = _upload(alpha, "Item,Notes\nBROOKVALE UHT MILK 1L,as agreed\n", "no_qty.csv")
_check("a sheet with no quantity column is refused and names the field",
       (b"quantity" in r.data
        and len(db.get_tender_uploads("OrgAlpha")) == 0
        and _scratch_tables() == []),
       detail=str((b"quantity" in r.data, len(db.get_tender_uploads("OrgAlpha")),
                   _scratch_tables())))
_wipe("OrgAlpha")

# Hostile headers all the way through ingest: the users table must survive and
# nothing may reach the page as live markup.
_HOSTILE_SHEETS = [
    ("a SQL payload inside the item header",
     'Item"; DROP TABLE users;--,Qty\nBROOKVALE UHT MILK 1L,10\n'),
    ("a script tag as a header",
     "<script>alert(1)</script>,Item,Qty\nx,BROOKVALE UHT MILK 1L,10\n"),
    ("a 300-character header",
     ("I" * 300) + ",Item,Qty\nx,BROOKVALE UHT MILK 1L,10\n"),
    ("a punctuation-only header",
     "***,Item,Qty\nx,BROOKVALE UHT MILK 1L,10\n"),
    ("25 duplicate UOM columns",
     ",".join(["Item", "Qty"] + ["UOM"] * 25) + "\n"
     + ",".join(["BROOKVALE UHT MILK 1L", "10"] + ["CTN"] * 25) + "\n"),
]
for _name, _csv in _HOSTILE_SHEETS:
    r = _upload(alpha, _csv, "hostile.csv")
    _check(f"hostile sheet survives ingest without damage: {_name}",
           (r.status_code == 200
            and db.table_exists("users")
            and b"<script>alert(1)</script>" not in r.data),
           detail=str(r.status_code))
    _wipe("OrgAlpha")

# A quote-heavy item VALUE is a parameterised bind, never SQL.
_upload(alpha, "Item,Qty\n\"BROOKVALE 1L\"\"); DROP TABLE users;--\",900\n", "quote_value.csv")
_check("a SQL payload in an item cell is stored as text, not executed",
       db.table_exists("users"), detail="users table gone")
_wipe("OrgAlpha")


# ── 5. Empty and degenerate sheets ───────────────────────────────────────────

r = _upload(alpha, "Item,Qty\n", "headers_only.csv")
_check("a headers-only sheet imports nothing and says so",
       (b"No usable rows" in r.data
        and len(db.get_tender_commitments("OrgAlpha")) == 0
        and _scratch_tables() == []),
       detail=str((b"No usable rows" in r.data,
                   len(db.get_tender_commitments("OrgAlpha")), _scratch_tables())))
_wipe("OrgAlpha")

r = _upload(alpha, "", "empty_file.csv")
_check("a zero-byte file is refused and leaves no upload row",
       (len(db.get_tender_uploads("OrgAlpha")) == 0 and _scratch_tables() == []),
       detail=str((len(db.get_tender_uploads("OrgAlpha")), _scratch_tables())))
_wipe("OrgAlpha")

r = _upload(alpha, "Item,Qty\nBROOKVALE UHT MILK 1L,1\n", "one_row.csv")
_check("a one-row sheet imports one row",
       b"Imported 1 tender row." in r.data, detail="singular wording or count wrong")
_wipe("OrgAlpha")

r = _upload(alpha, "Item,Qty,Notes\nBROOKVALE UHT MILK 1L,5,\nKESTREL JUICE 1L,6,\n",
            "blank_column.csv")
_check("a column that is blank in every row is ignored, not fatal",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))
_wipe("OrgAlpha")

r = _upload(alpha, "Item,Qty\nBROOKVALE UHT MILK 1L,\nKESTREL JUICE 1L,\n",
            "blank_qty_column.csv")
_check("a quantity column blank in every row imports nothing and renders",
       (len(db.get_tender_commitments("OrgAlpha")) == 0
        and alpha.get("/tenders").status_code == 200),
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))
_wipe("OrgAlpha")

# The skipped count the user is shown must be the real one. MAX_REJECTS_STORED
# caps the stored blob at 50, which is right, but the same capped list is what
# gets counted -- so imported + skipped stops adding up past 50 bad rows.
_many = ["Item,Qty"]
_many += [f"ALDERMOOR ITEM {i},TBC" for i in range(60)]
_many += [f"NORDVIK GOOD {i},100" for i in range(3)]
r = _upload(alpha, "\n".join(_many) + "\n", "many_rejects.csv")
_u = db.get_tender_uploads("OrgAlpha")[0]
_check("the skipped count shown to the user is the real number of skipped rows",
       _u["rows_rejected"] == 60, detail=f"reported {_u['rows_rejected']}, actually 60")
_check("imported plus skipped accounts for every data row in the sheet",
       _u["rows_imported"] + _u["rows_rejected"] == 63,
       detail=f"{_u['rows_imported']} + {_u['rows_rejected']}")
_wipe("OrgAlpha")

# Duplicate item names in one sheet. Every row now shares one customer AND one
# period, so every pair of duplicates is guaranteed to overlap: find_overlaps
# is O(n^2) with no cap and the whole list is rendered into the page. At the
# route's own MAX_TENDER_ROWS of 20,000 that is ~200 million dicts on a single
# 512 MB worker.
_sizes = {}
for _n in (100, 200):
    _wipe("OrgAlpha")
    r = _upload(alpha, "Item,Qty\n" + ("BROOKVALE UHT MILK 1L,10\n" * _n), f"dupes_{_n}.csv")
    _sizes[_n] = (len(r.data),
                  len(tenders.find_overlaps(
                      [dict(x) for x in db.get_tender_commitments("OrgAlpha")])))
_check("duplicate item names do not explode the overlap table",
       _sizes[200][1] <= 200,
       detail=f"200 duplicate rows produced {_sizes[200][1]} overlap rows")
_check("doubling the rows does not more than double the rendered page",
       _sizes[200][0] <= 2.5 * _sizes[100][0],
       detail=f"{_sizes[100][0]:,} bytes at 100 rows, {_sizes[200][0]:,} bytes at 200")
_wipe("OrgAlpha")


# ── 6. Schema additions: old rows are NULL in the new columns ────────────────

_legacy = db.create_tender_upload("OrgAlpha", "legacy_sheet.csv", "alpha@example.com", "")
db.save_tender_rows("OrgAlpha", _legacy, [
    {"customer": "PADIMAS HOTELS", "item_name": "VANMARK PRAWN 1KG",
     "match_key": "vanmarkprawn1kg", "quantity": 1200.0,
     "period_start": "2026-07-01", "period_end": "2026-12-31"}])
db.finalise_tender_upload("OrgAlpha", _legacy, 1, 0, "[]")
r = alpha.get("/tenders")
_check("a row with a NULL qty_basis renders as 'Not stated'",
       r.status_code == 200 and b"Not stated" in r.data, detail=str(r.status_code))
_check("a NULL qty_basis never invents a per-month number",
       b"<td>200</td>" not in r.data, detail="a monthly figure appeared from nowhere")
_check("an upload row with a NULL customer falls back to the rows",
       b"PADIMAS HOTELS" in r.data)

# A basis string that is not one of the two known literals, written straight to
# the table: the page must label it and derive nothing.
db.execute("UPDATE tender_commitments SET qty_basis=? WHERE org_name=? AND upload_id=?",
           ("weekly'; DROP TABLE users;--", "OrgAlpha", _legacy))
r = alpha.get("/tenders")
_check("an unknown basis already in the table is labelled, not trusted",
       r.status_code == 200 and b"Not stated" in r.data and db.table_exists("users"),
       detail=str(r.status_code))
_wipe("OrgAlpha")

# An upload that imported nothing AND predates the customer column: the
# fallback has no rows to read from.
_orphan = db.create_tender_upload("OrgAlpha", "nothing_imported.csv", "alpha@example.com", "")
db.finalise_tender_upload("OrgAlpha", _orphan, 0, 0, "[]")
r = alpha.get("/tenders")
_check("an upload row with no rows and no customer still renders",
       r.status_code == 200 and b"nothing_imported.csv" in r.data, detail=str(r.status_code))
_wipe("OrgAlpha")

db.init_db()
db.init_db()
_cols = [c["name"] for c in db.query("PRAGMA table_info(tender_commitments)")]
_ucols = [c["name"] for c in db.query("PRAGMA table_info(tender_uploads)")]
_check("init_db is still idempotent and adds each new column exactly once",
       _cols.count("qty_basis") == 1 and _ucols.count("customer") == 1,
       detail=str((_cols, _ucols)))
_check("no existing tender column was dropped or renamed by the migration",
       set(["id", "org_name", "upload_id", "customer", "item_name", "match_key",
            "quantity", "period_start", "period_end"]).issubset(set(_cols)),
       detail=str(_cols))


# ── 7. Org isolation, re-proved against the new columns ──────────────────────

_upload(alpha, GOOD_CSV, "alpha_secret_sheet.csv", customer="ALDERMOOR GROUP")
_upload(bravo, GOOD_CSV, "bravo_sheet.csv", customer="VANMARK FOODS")
_alpha_id = db.get_tender_uploads("OrgAlpha")[0]["id"]
_bravo_id = db.get_tender_uploads("OrgBravo")[0]["id"]

r = bravo.get("/tenders")
_check("org B's page never contains org A's customer",
       b"ALDERMOOR GROUP" not in r.data)
_check("org B's page never contains org A's filename",
       b"alpha_secret_sheet.csv" not in r.data)
_check("org B's own sheet does render on org B's page",
       b"VANMARK FOODS" in r.data and b"bravo_sheet.csv" in r.data)

_check("an upload id alone does not read across orgs",
       db.get_tender_commitments("OrgBravo", _alpha_id) == []
       and db.get_tender_commitments("OrgAlpha", _bravo_id) == [],
       detail=str((db.get_tender_commitments("OrgBravo", _alpha_id),
                   db.get_tender_commitments("OrgAlpha", _bravo_id))))

bravo.post("/tenders/delete", data={"upload_id": str(_alpha_id)}, follow_redirects=True)
_check("org B cannot delete org A's sheet by id",
       len(db.get_tender_commitments("OrgAlpha")) == 2
       and len(db.get_tender_uploads("OrgAlpha")) == 1,
       detail=str((len(db.get_tender_commitments("OrgAlpha")),
                   len(db.get_tender_uploads("OrgAlpha")))))

# Guessed ids, including ones that are not ids at all.
for _bad in ["abc", "", "-1", "0", "1.5", "1 OR 1=1", "  ", "1; DROP TABLE users;--"]:
    try:
        rr = bravo.post("/tenders/delete", data={"upload_id": _bad}, follow_redirects=True)
        _ok = rr.status_code == 200
    except Exception as ex:                      # noqa: BLE001
        _ok = f"RAISED {type(ex).__name__}"
    _check(f"a junk upload_id is ignored, not fatal: {_bad!r}",
           _ok is True and len(db.get_tender_commitments("OrgAlpha")) == 2,
           detail=str(_ok))

# int() happily builds a Python integer wider than SQLite's INTEGER, and the
# route only catches TypeError/ValueError.
try:
    rr = alpha.post("/tenders/delete", data={"upload_id": "9" * 19}, follow_redirects=True)
    _over = rr.status_code
except Exception as ex:                          # noqa: BLE001
    _over = f"RAISED {type(ex).__name__}: {ex}"
_check("an upload_id wider than SQLite's INTEGER is ignored, not a 500",
       _over == 200, detail=str(_over))

_wipe("OrgAlpha")
_wipe("OrgBravo")


# ── 8. Role enforcement on the new form ──────────────────────────────────────

_upload(alpha, GOOD_CSV, "owner_sheet.csv")
_owner_id = db.get_tender_uploads("OrgAlpha")[0]["id"]

r = _upload(viewer, GOOD_CSV, "viewer_sheet.csv")
_check("a viewer cannot upload through the new form",
       len(db.get_tender_uploads("OrgAlpha")) == 1,
       detail=str([u["filename"] for u in db.get_tender_uploads("OrgAlpha")]))
_check("a viewer's upload leaves no scratch table and no file",
       _scratch_tables() == []
       and [f for f in _upload_files() if f not in _BASELINE_FILES] == [],
       detail=str((_scratch_tables(), _upload_files())))

viewer.post("/tenders/delete", data={"upload_id": str(_owner_id)}, follow_redirects=True)
_check("a viewer cannot delete",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))
_check("a viewer is not offered the remove button",
       b"Remove this sheet" not in viewer.get("/tenders").data)
_wipe("OrgAlpha")


# ── 9. Nothing is left on disk or in scratch when the run ends ───────────────

_check("no tender scratch table survives the whole run", _scratch_tables() == [],
       detail=str(_scratch_tables()))
_check("no tender upload file survives the whole run",
       [f for f in _upload_files() if f not in _BASELINE_FILES] == [],
       detail=str(_upload_files()))


if _FAILED:
    # Echoed to stderr as well: run_tests.py prints stdout+stderr and shows only
    # the last 12 lines, and the deliberate "nan" case logs a traceback to
    # stderr. Without this the runner's tail would be that traceback instead of
    # the list of what actually broke.
    for _stream in (sys.stdout, sys.stderr):
        print(f"\n{len(_FAILED)} CHECK(S) FAILED:", file=_stream)
        for _n in _FAILED:
            print("  - " + _n, file=_stream)
        print("SOME TESTS FAILED", file=_stream)
    sys.exit(1)
print("\nAll tender break tests passed.")
