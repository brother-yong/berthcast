"""Tender sheet parsing, storage and org isolation.

A tender row is a promise the client has already made to a customer, so the
failure that matters is a SILENT one: a date read wrong, a quantity read wrong,
or one org seeing another's contracts. Every check here is aimed at that.

The Excel-serial case has its own check because the ingest layer reads raw cell
values and applies no number formats: a real date cell in an .xlsx arrives as
"46082", and without conversion every .xlsx upload would reject all its rows
while the same sheet saved as .csv imported cleanly.

Run: python tests/test_tenders.py
"""
import datetime
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_tenders.db")
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

import database as db      # noqa: E402
import tenders             # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── Column detection ─────────────────────────────────────────────────────────

_mapping, _missing = tenders.detect_columns(
    ["Customer Name", "Item Description", "Tender Qty", "Start Date", "End Date"])
_check("all five fields detected from ordinary headers", not _missing, detail=str(_missing))
_check("customer header claimed correctly",
       _mapping.get("customer") == "Customer Name", detail=str(_mapping))
_check("item header claimed correctly",
       _mapping.get("item") == "Item Description", detail=str(_mapping))
_check("quantity header claimed correctly",
       _mapping.get("quantity") == "Tender Qty", detail=str(_mapping))
_check("start and end are not the same header",
       _mapping.get("start") != _mapping.get("end"), detail=str(_mapping))

# "Date To" must be claimed by end, not swallowed by a looser net.
_m2, _miss2 = tenders.detect_columns(["Buyer", "SKU", "Volume", "Date From", "Date To"])
_check("alternative header wording still maps all five", not _miss2, detail=str(_miss2))
_check("'Date To' maps to end", _m2.get("end") == "Date To", detail=str(_m2))
_check("'Date From' maps to start", _m2.get("start") == "Date From", detail=str(_m2))

_m3, _miss3 = tenders.detect_columns(["Customer", "Item", "Qty"])
_check("missing date columns are reported, not guessed",
       set(_miss3) == {"start", "end"}, detail=str(_miss3))


# ── Date parsing ─────────────────────────────────────────────────────────────

_check("ISO date parses", tenders.parse_date("2026-04-03") == datetime.date(2026, 4, 3))
_check("day-first is the default (03/04/2026 is 3 April)",
       tenders.parse_date("03/04/2026") == datetime.date(2026, 4, 3),
       detail=str(tenders.parse_date("03/04/2026")))
_check("month-first available when asked",
       tenders.parse_date("03/04/2026", day_first=False) == datetime.date(2026, 3, 4))
_check("two-digit year expands to 2000s",
       tenders.parse_date("03/04/26") == datetime.date(2026, 4, 3))
_check("Excel serial converts to the right date",
       tenders.parse_date("46082") == datetime.date(2026, 3, 1),
       detail=str(tenders.parse_date("46082")))
_check("a quantity-sized bare number is NOT read as a date",
       tenders.parse_date("500") is None, detail=str(tenders.parse_date("500")))
_check("impossible date rejected", tenders.parse_date("31/02/2026") is None)
_check("free text rejected", tenders.parse_date("next quarter") is None)
_check("empty cell rejected", tenders.parse_date("") is None)


# ── Quantity parsing ─────────────────────────────────────────────────────────

_check("thousands separator accepted", tenders.parse_quantity("1,200") == 1200.0)
_check("zero rejected", tenders.parse_quantity("0") is None)
_check("negative rejected", tenders.parse_quantity("-5") is None)
_check("text rejected", tenders.parse_quantity("as agreed") is None)


# ── Row building ─────────────────────────────────────────────────────────────

_records = [
    {"Customer": "NORDVIK CATERING", "Item": "BROOKVALE UHT MILK 1L",
     "Qty": "1,200", "Start Date": "01/01/2026", "End Date": "31/12/2026"},
    # Excel-serial dates, the .xlsx case.
    {"Customer": "PADIMAS HOTELS", "Item": "KESTREL ORANGE JUICE 1L",
     "Qty": "800", "Start Date": "46023", "End Date": "46387"},
    {"Customer": "", "Item": "ALDERMOOR RICE 5KG",
     "Qty": "50", "Start Date": "01/01/2026", "End Date": "31/12/2026"},
    {"Customer": "VANMARK FOODS", "Item": "VANMARK CHICKEN 2KG",
     "Qty": "300", "Start Date": "01/06/2026", "End Date": "01/01/2026"},
    {"Customer": "VANMARK FOODS", "Item": "VANMARK BEEF 2KG",
     "Qty": "nil", "Start Date": "01/06/2026", "End Date": "31/12/2026"},
    {"Customer": "", "Item": "", "Qty": ""},   # blank spacer, silently skipped
]
_rows, _rejects, _map = tenders.build_rows(_records)

_check("good rows imported, bad rows held back", len(_rows) == 2, detail=str(len(_rows)))
_check("three bad rows rejected (blank spacer not counted)",
       len(_rejects) == 3, detail=str(len(_rejects)))
_reasons = {r["reason"] for r in _rejects}
_check("missing customer reported", "no customer name" in _reasons, detail=str(_reasons))
_check("end-before-start reported",
       "end date is before the start date" in _reasons, detail=str(_reasons))
_check("unreadable quantity reported",
       any("quantity" in r for r in _reasons), detail=str(_reasons))
_check("reject carries its source row number",
       all(isinstance(r["row"], int) for r in _rejects))
_check("Excel-serial row survived and dated correctly (46023 is 1 Jan 2026)",
       any(r["period_start"] == "2026-01-01" for r in _rows),
       detail=str([r["period_start"] for r in _rows]))
_check("match_key normalises the item name",
       _rows[0]["match_key"] == "brookvaleuhtmilk1l", detail=_rows[0]["match_key"])

_missing_map = tenders.build_rows([{"Customer": "X", "Item": "Y", "Qty": "1"}])[2]
_check("a sheet with no date columns imports nothing and says which are missing",
       set(_missing_map.get("__missing__", [])) == {"start", "end"},
       detail=str(_missing_map))


# ── Overlap detection ────────────────────────────────────────────────────────

_ov_rows = [
    {"customer": "NORDVIK", "item_name": "MILK 1L", "match_key": "milk1l",
     "quantity": 100, "period_start": "2026-01-01", "period_end": "2026-06-30"},
    {"customer": "nordvik", "item_name": "milk 1l", "match_key": "milk1l",
     "quantity": 200, "period_start": "2026-06-30", "period_end": "2026-12-31"},
    {"customer": "PADIMAS", "item_name": "MILK 1L", "match_key": "milk1l",
     "quantity": 300, "period_start": "2026-01-01", "period_end": "2026-06-30"},
]
_clashes = tenders.find_overlaps(_ov_rows)
_check("touching periods on one customer+item count as an overlap",
       len(_clashes) == 1, detail=str(_clashes))
_check("a different customer on the same item is NOT an overlap",
       all(c["customer"].lower() == "nordvik" for c in _clashes), detail=str(_clashes))

_no_clash = tenders.find_overlaps([
    {"customer": "NORDVIK", "item_name": "MILK 1L", "match_key": "milk1l",
     "quantity": 100, "period_start": "2026-01-01", "period_end": "2026-06-29"},
    {"customer": "NORDVIK", "item_name": "MILK 1L", "match_key": "milk1l",
     "quantity": 200, "period_start": "2026-06-30", "period_end": "2026-12-31"},
])
_check("back-to-back periods that do not share a day are clean",
       _no_clash == [], detail=str(_no_clash))


# ── Storage and org isolation ────────────────────────────────────────────────

db.init_db()

_uid_a = db.create_tender_upload("OrgAlpha", "alpha_tenders.xlsx", "a@example.com")
_uid_b = db.create_tender_upload("OrgBravo", "bravo_tenders.xlsx", "b@example.com")
db.save_tender_rows("OrgAlpha", _uid_a, _rows)
db.save_tender_rows("OrgBravo", _uid_b, _rows[:1])
db.finalise_tender_upload("OrgAlpha", _uid_a, len(_rows), len(_rejects), json.dumps(_rejects))

_alpha = db.get_tender_commitments("OrgAlpha")
_bravo = db.get_tender_commitments("OrgBravo")
_check("rows land under the uploading org", len(_alpha) == 2, detail=str(len(_alpha)))
_check("the other org sees only its own rows", len(_bravo) == 1, detail=str(len(_bravo)))
_check("org A cannot read org B's rows by guessing the upload id",
       db.get_tender_commitments("OrgAlpha", _uid_b) == [],
       detail=str(db.get_tender_commitments("OrgAlpha", _uid_b)))

# The scratch reader interpolates its column list into the SELECT, so the
# whitelist against the table's real headers is what keeps that safe.
db.execute('CREATE TABLE "tender_import_9001" ("customer" TEXT, "qty" TEXT, "_session_id" TEXT)')
db.execute('INSERT INTO "tender_import_9001" VALUES (?,?,?)', ("NORDVIK", "10", "9001"))
_heads = db.scratch_table_headers("tender_import_9001")
_check("scratch headers exclude the internal _session_id column",
       _heads == ["customer", "qty"], detail=str(_heads))
_hostile = db.read_scratch_table(
    "tender_import_9001", ["customer", 'x" ; DROP TABLE users;--'], 10)
_check("a hostile column name is dropped, not interpolated",
       _hostile == [{"customer": "NORDVIK"}], detail=str(_hostile))
_check("the hostile column name did not drop the users table",
       db.table_exists("users"))
_check("an all-unknown column list reads nothing",
       db.read_scratch_table("tender_import_9001", ["nope"], 10) == [])
_check("max_rows bounds the read",
       len(db.read_scratch_table("tender_import_9001", ["customer"], 0)) == 0)
db.drop_scratch_table("tender_import_9001")

_up_a = db.get_tender_uploads("OrgAlpha")
_check("upload record stores the reject count",
       _up_a and _up_a[0]["rows_rejected"] == len(_rejects),
       detail=str(_up_a[0]["rows_rejected"] if _up_a else None))
_check("upload listing is org-scoped",
       all(u["org_name"] == "OrgAlpha" for u in _up_a))

# Deleting must not reach across orgs, and must take the rows with it.
db.delete_tender_upload("OrgBravo", _uid_a)
_check("delete from the wrong org is a no-op",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))

db.delete_tender_upload("OrgAlpha", _uid_a)
_check("delete removes the sheet's rows",
       db.get_tender_commitments("OrgAlpha") == [])
_check("delete removes the sheet record",
       db.get_tender_uploads("OrgAlpha") == [])
_check("the other org is untouched by the delete",
       len(db.get_tender_commitments("OrgBravo")) == 1)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll tender tests passed.")
