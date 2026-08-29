"""The three tender sheet shapes the pilot client actually sends.

Yesterday's version demanded five columns (customer, item, quantity, start,
end). Not one real sheet has them: they carry an item and a number, and the
customer and the period arrive in the filename or a chat message. This file is
the acceptance test for reading those sheets, so the checks are written as the
shapes themselves rather than as tidy invented headers.

Two things are easy to get silently wrong and both are pinned here:
  1. a sheet with BOTH a code column and a name column must store the NAME,
     because match_key is how a later version joins these rows to inventory
  2. "SIX MONTH VOLUME" is a total and "Est. Monthly Consumption" is a rate,
     from the same client at the same time -- so the basis is asked on the
     form, never guessed from the header, and the per-month figure is derived
     on read rather than stored

Sheet fixtures are anonymised: invented brands, and <COMPANY> stays a
placeholder. Run: python tests/test_tender_sheets.py
"""
import datetime
import io
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_tender_sheets.db")
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


# ── Column detection: the three real sheet shapes ────────────────────────────
# Asserted twice each. The ingest layer sanitises every header before the app
# sees it ("Est. Monthly Consumption" arrives as est_monthly_consumption), and
# _norm_header collapses both forms to the same string -- so the raw form
# documents the client's sheet and the sanitised form is what production runs.

# Sheet A: item code AND product name in the same sheet.
SHEET_A_RAW = ["Item number", "Grouping", "Product name", "Est Qty", "Unit"]
SHEET_A_SAN = ["item_number", "grouping", "product_name", "est_qty", "unit"]

_map, _miss, _conf = tenders.detect_columns(SHEET_A_RAW)
_check("sheet A: the readable product name wins, not the item code",
       _map.get("item") == "Product name", detail=str(_map))
_check("sheet A: quantity claims 'Est Qty'",
       _map.get("quantity") == "Est Qty", detail=str(_map))
_check("sheet A: nothing missing, nothing refused",
       _miss == [] and _conf == [], detail=str((_miss, _conf)))

_map, _miss, _conf = tenders.detect_columns(SHEET_A_SAN)
_check("sheet A sanitised: still the name column, not item_number",
       _map.get("item") == "product_name", detail=str(_map))
_check("sheet A sanitised: quantity claims est_qty",
       _map.get("quantity") == "est_qty", detail=str(_map))
_check("sheet A sanitised: nothing missing, nothing refused",
       _miss == [] and _conf == [], detail=str((_miss, _conf)))

# Sheet B: the trap. "<COMPANY> UPDATE" CONTAINS the word "company", so a
# substring net on customer keywords would refuse this client's real file on
# every single upload. Conflicts are matched EXACTLY for that reason.
SHEET_B_RAW = ["* Stk ID", "Stk Name", "REMARK", "SIX MONTH VOLUME", "UOM",
               "<COMPANY> UPDATE", "Column1", "Column2"]
SHEET_B_SAN = ["_stk_id", "stk_name", "remark", "six_month_volume", "uom",
               "company_update", "column1", "column2"]

_map, _miss, _conf = tenders.detect_columns(SHEET_B_RAW)
_check("sheet B: item claims 'Stk Name', not '* Stk ID'",
       _map.get("item") == "Stk Name", detail=str(_map))
_check("sheet B: quantity claims 'SIX MONTH VOLUME'",
       _map.get("quantity") == "SIX MONTH VOLUME", detail=str(_map))
_check("sheet B: '<COMPANY> UPDATE' does NOT count as a customer column",
       _conf == [], detail=str(_conf))
_check("sheet B: nothing missing", _miss == [], detail=str(_miss))

_map, _miss, _conf = tenders.detect_columns(SHEET_B_SAN)
_check("sheet B sanitised: item claims stk_name",
       _map.get("item") == "stk_name", detail=str(_map))
_check("sheet B sanitised: quantity claims six_month_volume",
       _map.get("quantity") == "six_month_volume", detail=str(_map))
_check("sheet B sanitised: company_update does NOT refuse the sheet",
       _conf == [] and _miss == [], detail=str((_miss, _conf)))

# Sheet C: "No." must not be read as an item, and UOM appears twice.
SHEET_C_RAW = ["No.", "Description", "Est. Monthly Consumption", "UOM",
               "Business Entity", "Brand", "Packing", "UOM", "Country"]
SHEET_C_SAN = ["no", "description", "est_monthly_consumption", "uom",
               "business_entity", "brand", "packing", "uom_1", "country"]

_map, _miss, _conf = tenders.detect_columns(SHEET_C_RAW)
_check("sheet C: item claims 'Description', not 'No.'",
       _map.get("item") == "Description", detail=str(_map))
_check("sheet C: quantity claims 'Est. Monthly Consumption'",
       _map.get("quantity") == "Est. Monthly Consumption", detail=str(_map))
_check("sheet C: 'Business Entity' does NOT count as a customer column",
       _conf == [] and _miss == [], detail=str((_miss, _conf)))
_check("sheet C: neither UOM column is claimed by any field",
       "UOM" not in _map.values(), detail=str(_map))

_map, _miss, _conf = tenders.detect_columns(SHEET_C_SAN)
_check("sheet C sanitised: item claims description",
       _map.get("item") == "description", detail=str(_map))
_check("sheet C sanitised: quantity claims est_monthly_consumption",
       _map.get("quantity") == "est_monthly_consumption", detail=str(_map))
_check("sheet C sanitised: the duplicate uom_1 is not claimed either",
       "uom" not in _map.values() and "uom_1" not in _map.values(), detail=str(_map))
_check("sheet C sanitised: nothing missing, nothing refused",
       _miss == [] and _conf == [], detail=str((_miss, _conf)))

# Demoting the code column is a preference, not a ban.
_map, _miss, _conf = tenders.detect_columns(["SKU", "Qty"])
_check("a code-only sheet still imports, item -> SKU",
       _map == {"item": "SKU", "quantity": "Qty"} and _miss == [],
       detail=str((_map, _miss)))


# ── Conflict detection ───────────────────────────────────────────────────────

_, _, _conf = tenders.detect_columns(
    ["Customer", "Item Description", "Tender Qty", "Start Date", "End Date"])
_check("the old five-column sheet is refused, in field order",
       _conf == [("customer", "Customer"), ("start", "Start Date"),
                 ("end", "End Date")], detail=str(_conf))

_, _, _conf = tenders.detect_columns(["Item", "Qty", "Start Date"])
_check("one date column on its own is refused, and only it",
       _conf == [("start", "Start Date")], detail=str(_conf))


# ── Period arithmetic ────────────────────────────────────────────────────────
# (year, month) difference, plus one when the end day reaches the start day.

_D = datetime.date
_check("a one-day period is one month, never zero",
       tenders.months_in_period(_D(2026, 7, 1), _D(2026, 7, 1)) == 1,
       detail=str(tenders.months_in_period(_D(2026, 7, 1), _D(2026, 7, 1))))
_check("1 Jul to 31 Dec is six months (the six-month sheet)",
       tenders.months_in_period(_D(2026, 7, 1), _D(2026, 12, 31)) == 6,
       detail=str(tenders.months_in_period(_D(2026, 7, 1), _D(2026, 12, 31))))
_check("a five-year contract is 60 months",
       tenders.months_in_period(_D(2026, 1, 1), _D(2030, 12, 31)) == 60,
       detail=str(tenders.months_in_period(_D(2026, 1, 1), _D(2030, 12, 31))))
_check("1 Jul to 30 Jun is twelve months",
       tenders.months_in_period(_D(2026, 7, 1), _D(2027, 6, 30)) == 12,
       detail=str(tenders.months_in_period(_D(2026, 7, 1), _D(2027, 6, 30))))
_check("15 Jul to 14 Aug is one month, not two",
       tenders.months_in_period(_D(2026, 7, 15), _D(2026, 8, 14)) == 1,
       detail=str(tenders.months_in_period(_D(2026, 7, 15), _D(2026, 8, 14))))
_check("31 Jan to 28 Feb is one month",
       tenders.months_in_period(_D(2026, 1, 31), _D(2026, 2, 28)) == 1,
       detail=str(tenders.months_in_period(_D(2026, 1, 31), _D(2026, 2, 28))))


# ── Basis conversion ─────────────────────────────────────────────────────────

_check("a six-month total of 1800 is 300 a month",
       tenders.monthly_rate(1800, "period_total", _D(2026, 7, 1), _D(2026, 12, 31)) == 300.0,
       detail=str(tenders.monthly_rate(1800, "period_total", _D(2026, 7, 1), _D(2026, 12, 31))))
_check("a per-month 1800 stays 1800",
       tenders.monthly_rate(1800, "per_month", _D(2026, 7, 1), _D(2026, 12, 31)) == 1800.0)
_check("a same-day period divides by one, not by zero",
       tenders.monthly_rate(1800, "period_total", _D(2026, 7, 1), _D(2026, 7, 1)) == 1800.0)
_check("ISO date strings work as well as date objects (rows come back as text)",
       tenders.monthly_rate(1800, "period_total", "2026-07-01", "2026-12-31") == 300.0,
       detail=str(tenders.monthly_rate(1800, "period_total", "2026-07-01", "2026-12-31")))
_check("an unstated basis gives no monthly figure, never a guess",
       tenders.monthly_rate(1800, None, "2026-07-01", "2026-12-31") is None)
_check("an unknown basis gives no monthly figure either",
       tenders.monthly_rate(1800, "weekly", "2026-07-01", "2026-12-31") is None)

# The template hardcodes these two strings as <option value>s.
_check("BASIS_PER_MONTH is the string the form posts",
       tenders.BASIS_PER_MONTH == "per_month", detail=tenders.BASIS_PER_MONTH)
_check("BASIS_PERIOD_TOTAL is the string the form posts",
       tenders.BASIS_PERIOD_TOTAL == "period_total", detail=tenders.BASIS_PERIOD_TOTAL)

_check("1800 displays as 1,800 and not 1,800.00",
       tenders.format_qty(1800.0) == "1,800", detail=tenders.format_qty(1800.0))
_check("a fractional quantity keeps its decimal",
       tenders.format_qty(12.5) == "12.5", detail=tenders.format_qty(12.5))
_check("a small rate does not round away to nothing",
       tenders.format_qty(0.08) == "0.08", detail=tenders.format_qty(0.08))
_check("no monthly figure renders as an empty cell",
       tenders.format_qty(None) == "", detail=repr(tenders.format_qty(None)))


# ── Row building: the form's answers are stamped on every row ────────────────

_rows, _rejects, _map = tenders.build_rows(
    [{"Stk Name": "BROOKVALE UHT MILK 1L", "SIX MONTH VOLUME": "1,800"},
     {"Stk Name": "KESTREL ORANGE JUICE 1L", "SIX MONTH VOLUME": "600"},
     {"Stk Name": "", "SIX MONTH VOLUME": ""},        # blank spacer
     {"Stk Name": "", "SIX MONTH VOLUME": "50"},      # no item name
     {"Stk Name": "ALDERMOOR RICE 5KG", "SIX MONTH VOLUME": "as agreed"}],
    "NORDVIK CATERING", _D(2026, 7, 1), _D(2026, 12, 31),
    tenders.BASIS_PERIOD_TOTAL)

_check("two good rows built", len(_rows) == 2, detail=str(len(_rows)))
_check("the form customer is on every row",
       all(r["customer"] == "NORDVIK CATERING" for r in _rows))
_check("both form dates are stamped ISO on every row",
       all(r["period_start"] == "2026-07-01" and r["period_end"] == "2026-12-31"
           for r in _rows), detail=str([(r["period_start"], r["period_end"]) for r in _rows]))
_check("the form basis is on every row",
       all(r["qty_basis"] == "period_total" for r in _rows),
       detail=str([r["qty_basis"] for r in _rows]))
_check("match_key comes from normalise_match_key",
       _rows[0]["match_key"] == "brookvaleuhtmilk1l", detail=_rows[0]["match_key"])
_check("a blank spacer row is skipped, not rejected",
       len(_rejects) == 2, detail=str(_rejects))
_reasons = [r["reason"] for r in _rejects]
_check("a row with no item name is rejected", "no item name" in _reasons, detail=str(_reasons))
_check("a row with a non-numeric quantity is rejected",
       any("quantity" in r for r in _reasons), detail=str(_reasons))


# ── Storage round trip ───────────────────────────────────────────────────────

_uid = db.create_tender_upload("OrgSheets", "sheet_b.csv", "s@example.com",
                               "NORDVIK CATERING")
db.save_tender_rows("OrgSheets", _uid, _rows)
_stored = db.get_tender_commitments("OrgSheets", _uid)
_check("the customer is stored on the upload row too",
       db.get_tender_uploads("OrgSheets")[0]["customer"] == "NORDVIK CATERING",
       detail=str(db.get_tender_uploads("OrgSheets")[0]["customer"]))
_check("qty_basis persists on the commitment rows",
       [r["qty_basis"] for r in _stored] == ["period_total", "period_total"],
       detail=str([r["qty_basis"] for r in _stored]))

# A row dict built before the basis existed must write NULL, not raise: those
# rows show "Not stated" and no derived monthly figure.
db.save_tender_rows("OrgSheets", _uid, [
    {"customer": "NORDVIK CATERING", "item_name": "PADIMAS BEEF 2KG",
     "match_key": "padimasbeef2kg", "quantity": 100.0,
     "period_start": "2026-07-01", "period_end": "2026-12-31"}])
_legacy = [r for r in db.get_tender_commitments("OrgSheets", _uid)
           if r["item_name"] == "PADIMAS BEEF 2KG"]
_check("a row without a basis stores NULL and reads back None",
       len(_legacy) == 1 and _legacy[0]["qty_basis"] is None, detail=str(_legacy))
_check("a NULL basis is labelled, not guessed",
       tenders.BASIS_LABELS.get(None, "Not stated") == "Not stated")
db.delete_tender_upload("OrgSheets", _uid)


# ── End to end: the three shapes through the real route ──────────────────────

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


def _upload(client, csv_text, filename, customer, basis,
            start="2026-07-01", end="2026-12-31"):
    return client.post(
        "/tenders/upload",
        data={"customer": customer, "period_start": start, "period_end": end,
              "qty_basis": basis,
              "file": (io.BytesIO(csv_text.encode("utf-8")), filename)},
        content_type="multipart/form-data", follow_redirects=True)


USER_ID = _make_user("sheets@example.com", "OrgSheets")
client  = _client(USER_ID, "sheets@example.com", "OrgSheets")

r = client.get("/tenders")
_check("the form offers both bases, spelled as the constants",
       b'value="per_month"' in r.data and b'value="period_total"' in r.data)

# Sheet A as the client sends it: two blank rows above the header and an empty
# column A. Both are already handled by the ingest layer; this proves it.
SHEET_A_CSV = (
    ",,,,,\n"
    ",,,,,\n"
    ",Item number,Grouping,Product name,Est Qty,Unit\n"
    ",IT-4471,Chilled,BROOKVALE UHT MILK 1L,1200,CTN\n"
    ",IT-4472,Chilled,KESTREL ORANGE JUICE 1L,800,CTN\n"
    ",IT-4473,Dry,ALDERMOOR RICE 5KG,240,BAG\n"
)
r = _upload(client, SHEET_A_CSV, "sheet_a.csv", "NORDVIK CATERING", "per_month")
_check("sheet A imports all three rows", b"Imported 3 tender rows" in r.data)
_a_rows = db.get_tender_commitments("OrgSheets")
_check("sheet A stores the product NAME, not the item code",
       sorted(x["item_name"] for x in _a_rows) ==
       ["ALDERMOOR RICE 5KG", "BROOKVALE UHT MILK 1L", "KESTREL ORANGE JUICE 1L"],
       detail=str([x["item_name"] for x in _a_rows]))
_check("sheet A: the customer from the form names the sheet",
       b"NORDVIK CATERING" in r.data)

# Sheet B: six-month total, trailing blank row, and the <COMPANY> UPDATE trap.
SHEET_B_CSV = (
    "* Stk ID,Stk Name,REMARK,SIX MONTH VOLUME,UOM,<COMPANY> UPDATE,Column1,Column2\n"
    "SK-100,PADIMAS BEEF 2KG,Start from 01/07/26,1800,CTN,confirmed,,\n"
    "SK-101,VANMARK PRAWN 1KG,ETA on 04/08,600,KG,pending,,\n"
    ",,,,,,,\n"
)
r = _upload(client, SHEET_B_CSV, "sheet_b.csv", "PADIMAS HOTELS", "period_total")
_check("sheet B imports both rows and refuses nothing",
       b"Imported 2 tender rows" in r.data)
_check("sheet B: the trailing blank row is not reported as skipped",
       b"skipped" not in r.data, detail="blank spacer counted as a reject")
_check("sheet B: a six-month total of 1800 shows 300 a month",
       b"<td>300</td>" in r.data, detail="per-month cell missing")
_check("sheet B: the quantity itself still shows in full",
       b"<td>1,800</td>" in r.data)
_check("sheet B: the basis is named on the row",
       b"Total for the period" in r.data)

# Sheet C: duplicate UOM columns, and "No." must not become the item.
SHEET_C_CSV = (
    "No.,Description,Est. Monthly Consumption,UOM,Business Entity,Brand,Packing,UOM,Country\n"
    "1,BROOKVALE CHEESE 500G,150,CTN,Central kitchen,BROOKVALE,12x500g,KG,SG\n"
    "2,NORDVIK SALMON 1KG,90,CTN,Central kitchen,NORDVIK,10x1kg,KG,NO\n"
)
r = _upload(client, SHEET_C_CSV, "sheet_c.csv", "ALDERMOOR GROUP", "per_month")
_check("sheet C imports both rows despite the duplicate UOM column",
       b"Imported 2 tender rows" in r.data)
_c_upload = db.get_tender_uploads("OrgSheets")[0]
_c_rows = db.get_tender_commitments("OrgSheets", _c_upload["id"])
_check("sheet C stores the description, not the row number",
       sorted(x["item_name"] for x in _c_rows) ==
       ["BROOKVALE CHEESE 500G", "NORDVIK SALMON 1KG"],
       detail=str([x["item_name"] for x in _c_rows]))
_check("sheet C: a per-month sheet shows its own number as the monthly rate",
       b"<td>150</td>" in r.data and b"Per month" in r.data)
_check("all three sheets are listed under their own customer",
       b"ALDERMOOR GROUP" in r.data and b"PADIMAS HOTELS" in r.data
       and b"NORDVIK CATERING" in r.data)


# -- Per-org row ceiling -----------------------------------------------------
# One sheet was already capped; an org's ACCUMULATED rows were not, and the
# page copies the whole set three times before rendering it. On the single
# 512 MB production worker that is how the wrong ERP export, uploaded twice,
# takes every tenant's site down. Temporarily lower the ceiling rather than
# uploading 20,000 rows in a test.
_real_cap = appmod.MAX_TENDER_ROWS_PER_ORG
appmod.MAX_TENDER_ROWS_PER_ORG = db.count_tender_commitments("OrgSheets") + 2
try:
    OVER_CSV = "\n".join(["Item,Qty", "VANMARK PRAWN 1KG,10",
                          "KESTREL JUICE 1L,20", "BROOKVALE 1L,30", ""])
    _before = db.count_tender_commitments("OrgSheets")
    _before_uploads = len(db.get_tender_uploads("OrgSheets"))
    r = _upload(client, OVER_CSV, "over_cap.csv", "VANMARK FOODS", "per_month")
    _check("a sheet that would breach the org ceiling is refused",
           b"stored tender rows" in r.data, detail=str(r.status_code))
    _check("the refused sheet stored no rows",
           db.count_tender_commitments("OrgSheets") == _before,
           detail=str((db.count_tender_commitments("OrgSheets"), _before)))
    _check("the refused sheet left no upload record",
           len(db.get_tender_uploads("OrgSheets")) == _before_uploads,
           detail=str(len(db.get_tender_uploads("OrgSheets"))))
    _check("the refused sheet left no scratch table",
           not any(x["name"].startswith("tender_import_") for x in db.query(
               "SELECT name FROM sqlite_master WHERE type='table'")),
           detail="scratch table survived")
finally:
    appmod.MAX_TENDER_ROWS_PER_ORG = _real_cap

_check("the page read is bounded by an explicit LIMIT",
       len(db.get_tender_commitments("OrgSheets", limit=1)) == 1,
       detail=str(len(db.get_tender_commitments("OrgSheets", limit=1))))
_check("the limit does not leak past the org filter",
       db.get_tender_commitments("OrgNoSuchOrg", limit=100) == [])
_check("counting rows is org-scoped",
       db.count_tender_commitments("OrgNoSuchOrg") == 0)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll tender sheet-shape tests passed.")
