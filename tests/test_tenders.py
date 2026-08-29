"""Tender sheet parsing, storage and org isolation.

A tender row is a promise the client has already made to a customer, so the
failure that matters is a SILENT one: a date read wrong, a quantity read wrong,
or one org seeing another's contracts. Every check here is aimed at that.

The Excel-serial case has its own check because the ingest layer reads raw cell
values and applies no number formats: a real date cell in an .xlsx arrives as
"46082". The period now comes off the upload form rather than the sheet, but
the form's ISO dates go through that same parser, so it stays covered.

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
# The customer and the period now come off the upload form, so a sheet that
# carries its own is REFUSED rather than overwritten: stamping one customer
# over a sheet naming several would mislabel a contract with no warning.

_mapping, _missing, _conflicts = tenders.detect_columns(
    ["Customer", "Item Description", "Tender Qty", "Start Date", "End Date"])
_check("item header claimed correctly",
       _mapping.get("item") == "Item Description", detail=str(_mapping))
_check("quantity header claimed correctly",
       _mapping.get("quantity") == "Tender Qty", detail=str(_mapping))
_check("the old five-column sheet is refused, naming all three columns",
       _conflicts == [("customer", "Customer"), ("start", "Start Date"),
                      ("end", "End Date")], detail=str(_conflicts))

_m2, _miss2, _conf2 = tenders.detect_columns(
    ["Buyer", "SKU", "Volume", "Date From", "Date To"])
_check("alternative wording for customer and dates is refused too",
       _conf2 == [("customer", "Buyer"), ("start", "Date From"),
                  ("end", "Date To")], detail=str(_conf2))

_m3, _miss3, _conf3 = tenders.detect_columns(["Customer", "Item", "Qty"])
_check("a customer column alone is enough to refuse the sheet",
       _conf3 == [("customer", "Customer")], detail=str(_conf3))

_m4, _miss4, _conf4 = tenders.detect_columns(["Item", "Qty"])
_check("the two columns a tender sheet must supply are enough",
       _m4 == {"item": "Item", "quantity": "Qty"} and not _miss4 and not _conf4,
       detail=str((_m4, _miss4, _conf4)))


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
# The sheet supplies the item and the quantity; the customer, the period and
# the basis are the form's answers, applied to every row.

_FORM_START = datetime.date(2026, 1, 1)
_FORM_END   = datetime.date(2026, 12, 31)

_records = [
    {"Item": "BROOKVALE UHT MILK 1L", "Qty": "1,200"},
    {"Item": "KESTREL ORANGE JUICE 1L", "Qty": "800"},
    {"Item": "", "Qty": "50"},                        # no item name
    {"Item": "VANMARK BEEF 2KG", "Qty": "nil"},       # unreadable quantity
    {"Item": "", "Qty": ""},   # blank spacer, silently skipped
]
_rows, _rejects, _map = tenders.build_rows(
    _records, "NORDVIK CATERING", _FORM_START, _FORM_END, tenders.BASIS_PER_MONTH)

_check("good rows imported, bad rows held back", len(_rows) == 2, detail=str(len(_rows)))
_check("two bad rows rejected (blank spacer not counted)",
       len(_rejects) == 2, detail=str(len(_rejects)))
_reasons = {r["reason"] for r in _rejects}
_check("missing item name reported", "no item name" in _reasons, detail=str(_reasons))
_check("unreadable quantity reported",
       any("quantity" in r for r in _reasons), detail=str(_reasons))
_check("reject carries its source row number",
       all(isinstance(r["row"], int) for r in _rejects))
_check("the form's customer is stamped on every row",
       all(r["customer"] == "NORDVIK CATERING" for r in _rows),
       detail=str([r["customer"] for r in _rows]))
_check("the form's period is stamped on every row, ISO",
       all(r["period_start"] == "2026-01-01" and r["period_end"] == "2026-12-31"
           for r in _rows),
       detail=str([(r["period_start"], r["period_end"]) for r in _rows]))
_check("the form's basis is stamped on every row",
       all(r["qty_basis"] == "per_month" for r in _rows),
       detail=str([r.get("qty_basis") for r in _rows]))
_check("match_key normalises the item name",
       _rows[0]["match_key"] == "brookvaleuhtmilk1l", detail=_rows[0]["match_key"])

_missing_map = tenders.build_rows(
    [{"Item": "Y", "Notes": "as agreed"}], "NORDVIK CATERING",
    _FORM_START, _FORM_END, tenders.BASIS_PER_MONTH)[2]
_check("a sheet with no quantity column imports nothing and says which is missing",
       _missing_map.get("__missing__") == ["quantity"], detail=str(_missing_map))


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
