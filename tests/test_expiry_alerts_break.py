"""Hostile input against the expiry push alerts (plan 012).

The coder's file (test_expiry_alerts.py) proves the report section and the
weekly digest work. This one tries to make them lie, leak, crash, or mark stock
announced that nobody was ever shown.

What is pinned here that nothing else covers:
  1. the receipt-date and category fences, RE-PINNED from scratch. Plan 011's
     break test forbade any field claiming "Original Receipt Date"; plan 012's
     Step 0 had the CODER narrow that fence to fit its own new field. An
     executor editing a break test to fit its own code is exactly the failure
     the fence existed to catch, so this file re-attacks both new fields
     against the raw 22-header list and imports NOTHING from the file that was
     edited
  2. the ledger records the lots the EMAIL LISTED and nothing wider -- checked
     by pulling the item names back out of the sent body and comparing them to
     the ledger keys, not by trusting a count
  3. the dedup is the weekly email's alone: the /expiry ROUTE and the report
     section still show a lot after the digest has announced it
  4. one org's lots, ledger and snapshot are unreachable from another org even
     when both hold the same item names and the same lot numbers
  5. the boundaries: 28 and 210 days exactly, a 180-day receipt gap exactly, a
     receipt date AFTER the expiry, a lot expiring today, the 14-day staleness
     edge and the 7-day cadence edge

Sections 17 and 18 found real defects, which were then fixed in emails.py and
app.py. Those checks are now regression pins on the fixes: they were written
red, went green when the code was corrected, and go red again if either
regresses. Do not loosen them.

emails._deliver is monkeypatched, so nothing here opens a socket. Throwaway
temp DB, stubbed anthropic client, no API calls. Invented brands only, the repo
is public. Run: python tests/test_expiry_alerts_break.py
"""
import datetime
import os
import sys
import tempfile
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_expiry_alerts_break.db")
for _ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + _ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
os.environ.setdefault("SECRET_KEY", "test-only-secret")
# Both senders bail out before building a body without these, so they are set
# BEFORE the import. _deliver is replaced below, so nothing is ever sent.
os.environ["MAIL_SENDER"] = "alerts@example.com"
os.environ["MAIL_APP_PASSWORD"] = "not-a-real-password"

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                                   # noqa: E402
import emails                                           # noqa: E402
import expiry                                           # noqa: E402
import app as appmod                                    # noqa: E402
from agents.shared import normalise_match_key           # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["TESTING"] = True
appmod.app.config["WTF_CSRF_ENABLED"] = False

TODAY = datetime.date.today()
TS_FMT = "%Y-%m-%d %H:%M:%S"

_FAILED = []


def _check(name, cond, detail=""):
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


# ── No sockets. Every send is captured, both bodies kept ─────────────────────
SENT = []
RAW_MSGS = []


def _capture(msg, sender, password, recipient):
    parts = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8")
             for p in msg.get_payload()}
    RAW_MSGS.append(msg)
    SENT.append({"to": recipient, "subject": msg["Subject"],
                 "text": parts.get("text/plain", ""),
                 "html": parts.get("text/html", "")})
    return True


emails._deliver = _capture


def _iso(offset):
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


def _utc(offset_days=0, offset_seconds=0):
    return (datetime.datetime.utcnow()
            - datetime.timedelta(days=offset_days, seconds=offset_seconds)
            ).strftime(TS_FMT)


def _user(email, org, role="admin"):
    db.execute("INSERT INTO users (email, password_hash, org_name, model, role, "
               "email_verified) VALUES (?,?,?,?,?,1)",
               (email, generate_password_hash("pw12345678"), org,
                "claude-sonnet-5", role))
    return db.query("SELECT * FROM users WHERE email=?", (email,))[0]


def _lot(name, days_out, qty=100.0, lot_no=None, received=None, category=None):
    """One stored lot row, shaped exactly as build_lots emits it."""
    return {"item_code": None, "item_name": name,
            "match_key": normalise_match_key(name), "lot_no": lot_no,
            "uom": "CTN", "expiry_date": _iso(days_out),
            "qty_on_hand": qty, "qty_available": None,
            "received_date": received, "category": category}


def _snapshot(org, lots, uploaded_by=""):
    upload_id = db.create_expiry_upload(org, "lots.csv", uploaded_by)
    db.save_expiry_lots(org, upload_id, lots)
    db.finalise_expiry_upload(org, upload_id, len(lots), 0, 0, "[]")
    return upload_id


def _ledger(org):
    return sorted(r["lot_key"] for r in db.get_sent_expiry_alerts(org, 10_000))


def _age_upload(org, upload_id, days=0, seconds=0):
    db.execute("UPDATE expiry_uploads SET uploaded_at=? WHERE id=? AND org_name=?",
               (_utc(days, seconds), upload_id, org))


def _age_ledger(org, days=0, seconds=0):
    db.execute("UPDATE expiry_alerts_sent SET sent_at=? WHERE org_name=?",
               (_utc(days, seconds), org))


# ═════════════════════════════════════════════════════════════════════════════
# 1. RE-PIN: the receipt-date fence
#
# Plan 011's break test listed "Original Receipt Date" in FORBIDDEN under the
# rule "no field may claim this". Plan 012 Step 0 had the CODER narrow that to
# "only `received` may claim this", because the life-class inference needs the
# receipt-to-expiry gap. The narrowing may be right, but it was made by the
# executor whose code it unblocks, so it is re-attacked here from the raw
# headers. Nothing is imported from tests/test_expiry_break.py on purpose: a
# re-pin that reuses the moved goalposts proves nothing.
#
# The original hazard is unchanged and is what these checks are really about:
# on the real file the receipt date runs ~477 days earlier than the expiry, so
# if `expiry` ever claims that column every lot renders as nearly expired and
# staff dump good stock.
# ═════════════════════════════════════════════════════════════════════════════
HEADERS_22 = [
    "Location Code", "Location Name", "Inventory Code", "Inventory Description",
    "UOM", "Lot No.", "Lot Reference No.", "Original Receipt Date",
    "Expiry Date", "Delivery Remarks", "Special Remarks", "Damage Remarks",
    "Condition Remarks", "Country of Origin", "Supplier Name",
    "Source Voucher No.", "Pack Size", "Qty On Hand", "Qty Selected",
    "Qty Allocated", "Qty Allocated Selected", "Qty Available",
]

MAP22, MISS22 = expiry.detect_columns(HEADERS_22)

_claimers = sorted(f for f, h in MAP22.items() if h == "Original Receipt Date")
_check("re-pin: exactly one field claims the receipt-date column, and it is `received`",
       _claimers == ["received"], detail=str(_claimers))
_check("re-pin: `expiry` claims the expiry column, never the receipt date",
       MAP22.get("expiry") == "Expiry Date", detail=str(MAP22.get("expiry")))
_check("re-pin: the 22 real headers still import with nothing missing",
       MISS22 == [], detail=str(MISS22))
_check("re-pin: no header on the real export is claimed by two fields",
       len(set(MAP22.values())) == len(MAP22), detail=str(MAP22))

# The guard that plan 011 built and plan 012 promised to preserve: with no
# expiry column the sheet is REFUSED, not quietly filled from the receipt date.
_no_exp, _no_exp_missing = expiry.detect_columns(
    [h for h in HEADERS_22 if h != "Expiry Date"])
_check("re-pin: dropping the expiry column REFUSES the sheet",
       "expiry date" in _no_exp_missing, detail=str(_no_exp_missing))
_check("re-pin: and `expiry` does not fall back to the receipt column",
       _no_exp.get("expiry") is None, detail=str(_no_exp.get("expiry")))
_check("re-pin: the receipt column is still only claimed by `received`",
       sorted(f for f, h in _no_exp.items() if h == "Original Receipt Date")
       == ["received"], detail=str(_no_exp))

# `received` is a DATE field. A header that is not a date must never satisfy
# it: a sheet with "Qty Received" would otherwise hand it a number, the parse
# would fail, and every lot would fall to the long-life default -- invisible,
# because it looks exactly like a sheet with no receipt column at all.
for _hdr in ("Qty Received", "Received By", "Receipt No.", "Received",
             "Receipt", "Goods Receipt", "Receiving Bay", "Qty Receipt"):
    _m, _ = expiry.detect_columns(["Item", "Expiry Date", "Qty On Hand", _hdr])
    _check(f"re-pin: `received` is NOT satisfied by {_hdr!r}",
           "received" not in _m, detail=str(_m))
    _check(f"re-pin: {_hdr!r} does not disturb the quantity mapping",
           _m.get("qty_on_hand") == "Qty On Hand", detail=str(_m))

# And a real receipt-date header still maps, or the feature is dead on arrival.
for _hdr in ("Receipt Date", "Received Date", "Date Received", "GRN Date",
             "Goods Receipt Date"):
    _m, _ = expiry.detect_columns(["Item", "Expiry Date", "Qty On Hand", _hdr])
    _check(f"re-pin: `received` DOES map {_hdr!r}",
           _m.get("received") == _hdr, detail=str(_m))


# ═════════════════════════════════════════════════════════════════════════════
# 2. RE-PIN: the category fence
#
# A false hit here is worse than a miss. An unrecognised category value means
# long-life, so a wrongly-claimed column silently marks every chilled lot as
# 210-day stock and switches off the 28-day rule -- the one thing the client
# actually asked for.
# ═════════════════════════════════════════════════════════════════════════════
_check("re-pin: `category` claims none of the 22 real headers",
       "category" not in MAP22, detail=str(MAP22.get("category")))

for _hdr in HEADERS_22 + ["Document Type", "Supplier Group", "Lot Type",
                          "Classification", "Country of Origin", "Doc Type",
                          "Customer Group", "Class"]:
    _m, _ = expiry.detect_columns(["Item", "Expiry Date", "Qty", _hdr])
    _check(f"re-pin: `category` does NOT claim {_hdr!r}",
           _m.get("category") is None, detail=str(_m))

# It must still catch a real one, or step 1 of the fallback is unreachable.
for _hdr in ("Item Category", "Product Group", "Storage Condition",
             "Temperature Zone", "Storage Type"):
    _m, _ = expiry.detect_columns(["Item", "Expiry Date", "Qty", _hdr])
    _check(f"re-pin: `category` DOES map {_hdr!r}",
           _m.get("category") == _hdr, detail=str(_m))

# Adding a category column to the real export must not move anything else.
_with_cat, _ = expiry.detect_columns(HEADERS_22 + ["Item Category"])
_check("re-pin: adding a category column displaces no existing mapping",
       {k: v for k, v in _with_cat.items() if k != "category"} == MAP22,
       detail=str(_with_cat))
_check("re-pin: and the category column itself is the one that gets claimed",
       _with_cat.get("category") == "Item Category", detail=str(_with_cat))


# ═════════════════════════════════════════════════════════════════════════════
# 3. Classification boundaries. Off by one here is thrown-away stock.
# ═════════════════════════════════════════════════════════════════════════════
def _gap_recv(gap_days, expiry_offset):
    """A receipt date exactly `gap_days` before an expiry `expiry_offset` out."""
    return (TODAY + datetime.timedelta(days=expiry_offset - gap_days)).isoformat()


for _gap, _want in ((179, "short"), (180, "short"), (181, "long")):
    _got = expiry.life_class(None, _gap_recv(_gap, 30), _iso(30))
    _check(f"a {_gap}-day receipt-to-expiry gap is {_want}-life", _got == _want,
           detail=_got)

_check("a receipt date AFTER the expiry (negative gap) is short-life, the safe direction",
       expiry.life_class(None, _iso(60), _iso(10)) == "short",
       detail=expiry.life_class(None, _iso(60), _iso(10)))
_check("a NULL receipt date falls to the long-life default",
       expiry.life_class(None, None, _iso(10)) == "long")
for _junk in ("0000-00-00", "", "31/02/2026", "not-a-date", 0, -1, [], {}):
    try:
        _got = expiry.life_class(None, _junk, _iso(10))
        _check(f"life_class survives a received_date of {_junk!r} -> long",
               _got == "long", detail=str(_got))
    except Exception as exc:
        _check(f"life_class survives a received_date of {_junk!r}", False,
               detail=repr(exc))
for _junk in (None, "", "not-a-date", 12345):
    try:
        expiry.life_class(None, _iso(-10), _junk)
        _check(f"life_class survives an expiry_iso of {_junk!r}", True)
    except Exception as exc:
        _check(f"life_class survives an expiry_iso of {_junk!r}", False,
               detail=repr(exc))


def _row(days_out, received=None, category=None, name="KESTREL JUICE 1L", qty=10.0):
    return {"item_name": name, "match_key": normalise_match_key(name),
            "lot_no": "L1", "expiry_date": _iso(days_out), "qty_on_hand": qty,
            "qty_available": None, "received_date": received, "category": category}


_f, _ = expiry.flag_lots([_row(27, category="CHILLED"), _row(28, category="CHILLED"),
                          _row(29, category="CHILLED")], TODAY)
_check("short-life flags at 27 and 28 days and stops at 29",
       [r["days_left"] for r in _f] == [27, 28], detail=str([r["days_left"] for r in _f]))

_f, _ = expiry.flag_lots([_row(209), _row(210), _row(211)], TODAY)
_check("long-life flags at 209 and 210 days and stops at 211",
       [r["days_left"] for r in _f] == [209, 210], detail=str([r["days_left"] for r in _f]))

_f, _ = expiry.flag_lots([_row(0)], TODAY)
_check("a lot expiring TODAY flags, with days_left 0 and not a negative",
       len(_f) == 1 and _f[0]["days_left"] == 0, detail=str([r["days_left"] for r in _f]))

# The stated failure mode: a lot whose stored expiry_date will not parse must be
# SKIPPED and counted, never crash the background email thread.
_junk_rows = [_row(5), dict(_row(5), expiry_date="not-a-date"),
              dict(_row(5), expiry_date=None), dict(_row(5), expiry_date=""),
              dict(_row(5), expiry_date=20260901)]
try:
    _f, _skipped = expiry.flag_lots(_junk_rows, TODAY)
    _check("flag_lots skips and counts unparseable expiry dates instead of raising",
           len(_f) == 1 and _skipped == 4, detail=str((len(_f), _skipped)))
except Exception as exc:
    _check("flag_lots skips and counts unparseable expiry dates instead of raising",
           False, detail=repr(exc))

_check("flag_lots on zero rows returns empty, not an error",
       expiry.flag_lots([], TODAY) == ([], 0))


# ═════════════════════════════════════════════════════════════════════════════
# 4. Degenerate files into build_lots, and the category sanity gate
# ═════════════════════════════════════════════════════════════════════════════
CAT_MAP = {"expiry": "Expiry Date", "item_name": "Item", "qty_on_hand": "Qty",
           "category": "Cat", "received": "Recv"}
GAP90 = (TODAY - datetime.timedelta(days=60)).isoformat()   # 90-day gap at +30


def _cat_records(values):
    return [{"Item": f"BROOKVALE ITEM {i:02d}", "Expiry Date": _iso(30),
             "Qty": "10", "Cat": v, "Recv": GAP90}
            for i, v in enumerate(values, start=1)]


# A detected column that recognises NOTHING is a mis-detected column, not a
# file full of unknown categories. Left alone it marks every lot long-life and
# switches the 28-day rule off silently.
for _label, _values in (("SO/PO order types", ["SO", "PO", "SO"]),
                        ("an entirely blank column", ["", "", ""]),
                        ("values nothing recognises", ["AMBIENT", "AMBIENT", "N/A"])):
    _l, _r, _s = expiry.build_lots(_cat_records(_values), CAT_MAP)
    _check(f"a category column holding {_label} is dropped on every row",
           all(x["category"] is None for x in _l) and _s.get("category_ignored") is True,
           detail=str([x["category"] for x in _l]))
    _f, _ = expiry.flag_lots([dict(x) for x in _l], TODAY)
    _check(f"...and a 90-day receipt gap then still gets the 28-day rule ({_label})",
           _f == [], detail=str([(x["days_left"], x["life"]) for x in _f]))

_l, _r, _s = expiry.build_lots(_cat_records(["SO", "CHILLED", "PO"]), CAT_MAP)
_check("a HALF-recognised category column is kept, not thrown away",
       [x["category"] for x in _l] == ["SO", "CHILLED", "PO"]
       and "category_ignored" not in _s, detail=str((_s, [x["category"] for x in _l])))

_l, _, _ = expiry.build_lots(_cat_records(["chilled", "Frozen", "dRy"]), CAT_MAP)
_f, _ = expiry.flag_lots([dict(x) for x in _l], TODAY)
_check("recognised tokens in odd casing still classify (lower/mixed)",
       sorted(x["life"] for x in _f) == ["long", "long"],
       detail=str([(x["item_name"], x["life"]) for x in _f]))

# Empty and degenerate shapes: the row-accounting invariant has to survive all
# of them, or the page silently loses a row.
for _label, _recs in (
        ("zero rows", []),
        ("one row", _cat_records(["CHILLED"])),
        ("duplicate item names", [{"Item": "PADIMAS RICE 5KG", "Expiry Date": _iso(9),
                                   "Qty": "5", "Cat": "DRY", "Recv": GAP90}] * 3),
        ("a blank quantity column", [{"Item": "NORDVIK SALMON 2KG",
                                      "Expiry Date": _iso(9), "Qty": "",
                                      "Cat": "", "Recv": ""}]),
        ("every cell blank", [{"Item": "", "Expiry Date": "", "Qty": "",
                               "Cat": "", "Recv": ""}])):
    try:
        _l, _r, _s = expiry.build_lots(_recs, CAT_MAP)
        _check(f"row accounting holds on {_label}",
               _s["read"] == _s["summary"] + len(_l) + len(_r),
               detail=str((_s, len(_l), len(_r))))
    except Exception as exc:
        _check(f"row accounting holds on {_label}", False, detail=repr(exc))

_l, _, _ = expiry.build_lots(
    [{"Item": "VANMARK PRAWN 1KG", "Expiry Date": _iso(9), "Qty": "5",
      "Recv": "1e400", "Cat": "CHILLED"}], CAT_MAP)
_check("a receipt cell of '1e400' stores None instead of overflowing",
       _l and _l[0]["received_date"] is None, detail=str(_l))


# ═════════════════════════════════════════════════════════════════════════════
# 5. Schema: idempotent, and old rows are NULL in both new columns
# ═════════════════════════════════════════════════════════════════════════════
OLD_ORG = "OrgLegacyRows"
_user("legacy@example.com", OLD_ORG)
_legacy_id = db.create_expiry_upload(OLD_ORG, "legacy.csv", "legacy@example.com")
# Written the way the code BEFORE this migration wrote it: the two new columns
# are not named at all, so they land NULL exactly like every pre-existing row.
db.execute("INSERT INTO expiry_lots (org_name, upload_id, item_code, item_name, "
           "match_key, lot_no, uom, expiry_date, qty_on_hand, qty_available) "
           "VALUES (?,?,?,?,?,?,?,?,?,?)",
           (OLD_ORG, _legacy_id, None, "PADIMAS RICE 5KG", "padimasrice5kg",
            "L-9", "BAG", _iso(100), 5.0, None))
db.finalise_expiry_upload(OLD_ORG, _legacy_id, 1, 0, 0, "[]")

_legacy_rows = db.get_expiry_lots(OLD_ORG, _legacy_id, _iso(365), 100)
_check("a pre-migration row reads back with NULL in both new columns",
       _legacy_rows[0]["received_date"] is None and _legacy_rows[0]["category"] is None,
       detail=str((_legacy_rows[0]["received_date"], _legacy_rows[0]["category"])))
_f, _ = expiry.flag_lots(_legacy_rows, TODAY)
_check("a pre-migration row classifies long-life without raising",
       len(_f) == 1 and _f[0]["life"] == "long", detail=str(_f))

try:
    db.init_db()
    db.init_db()
    _still = db.get_expiry_lots(OLD_ORG, _legacy_id, _iso(365), 100)
    _check("init_db() re-run twice ON A POPULATED DB is idempotent and loses nothing",
           len(_still) == 1, detail=str(len(_still)))
except Exception as exc:
    _check("init_db() re-run twice ON A POPULATED DB is idempotent and loses nothing",
           False, detail=repr(exc))

SENT.clear()
appmod._send_expiry_digest(OLD_ORG)
_check("a snapshot of only pre-migration rows still produces a digest",
       len(SENT) == 1 and len(_ledger(OLD_ORG)) == 1, detail=str((len(SENT), _ledger(OLD_ORG))))


# ═════════════════════════════════════════════════════════════════════════════
# 6. Staleness. Stock moves; a three-week-old picture must not be emailed.
#
# Both sides of the edge are set 30 seconds off it rather than exactly on it:
# uploaded_at and the cutoff are second-resolution UTC strings computed a
# moment apart, so an "exactly 14 days" fixture would coin-flip on the second
# boundary and flake in CI.
# ═════════════════════════════════════════════════════════════════════════════
for _days, _secs, _want_email, _label in (
        (13, 0, 1, "13 days old"),
        (14, -30, 1, "30 seconds inside the 14-day edge"),
        (14, 30, 0, "30 seconds past the 14-day edge"),
        (15, 0, 0, "15 days old")):
    _org = f"OrgStale{_days}{'m' if _secs < 0 else 'p'}{abs(_secs)}"
    _user(f"stale{_days}{_secs}@example.com", _org)
    _uid = _snapshot(_org, [_lot("BROOKVALE UHT MILK 1L", 10, lot_no="S-1")],
                     uploaded_by=f"stale{_days}{_secs}@example.com")
    _age_upload(_org, _uid, _days, _secs)
    SENT.clear()
    appmod._send_expiry_digest(_org)
    _check(f"staleness: {_label} sends {_want_email} email(s)",
           len(SENT) == _want_email, detail=str(len(SENT)))
    _check(f"staleness: {_label} leaves the ledger holding {_want_email} key(s)",
           len(_ledger(_org)) == _want_email, detail=str(_ledger(_org)))


# ═════════════════════════════════════════════════════════════════════════════
# 7. Cadence. Weekly means weekly, even when new lots keep arriving.
# ═════════════════════════════════════════════════════════════════════════════
for _days, _secs, _want, _label in (
        (2, 0, 0, "2 days after a send"),
        (7, -30, 0, "30 seconds inside the 7-day window"),
        (7, 30, 1, "30 seconds past the 7-day window")):
    _org = f"OrgCadence{_days}{'m' if _secs < 0 else 'p'}{abs(_secs)}"
    _user(f"cad{_days}{_secs}@example.com", _org)
    _snapshot(_org, [_lot(f"NORDVIK ITEM {i:02d}", i, lot_no=f"C-{i:02d}")
                     for i in range(1, 26)],
              uploaded_by=f"cad{_days}{_secs}@example.com")
    appmod._send_expiry_digest(_org)          # first send fills the ledger
    _before = _ledger(_org)
    _age_ledger(_org, _days, _secs)
    SENT.clear()
    appmod._send_expiry_digest(_org)
    _check(f"cadence: {_label} sends {_want} email(s)", len(SENT) == _want,
           detail=str(len(SENT)))
    _check(f"cadence: {_label} leaves the ledger {'grown' if _want else 'byte-identical'}",
           (_ledger(_org) != _before) if _want else (_ledger(_org) == _before),
           detail=str((len(_before), len(_ledger(_org)))))


# ═════════════════════════════════════════════════════════════════════════════
# 8. Volume, and no silent marking at scale.
#
# The strong form of the invariant: the ledger keys are compared against the
# item names actually present in the sent BODY, not against a count. A count
# can be right while the two lists are different.
# ═════════════════════════════════════════════════════════════════════════════
BIG_ORG = "OrgVolume"
_user("volume@example.com", BIG_ORG)
BIG_LOTS = [_lot(f"VANMARK ITEM {i:05d}", i % 200, lot_no=f"V-{i:05d}")
            for i in range(1, 5001)]
_snapshot(BIG_ORG, BIG_LOTS, uploaded_by="volume@example.com")

_snap, _flagged = appmod._expiry_flagged(BIG_ORG)
_check("5,000 flagged lots are capped at EXPIRY_ALERT_SCAN_LIMIT, not read whole",
       len(_flagged) == appmod.EXPIRY_ALERT_SCAN_LIMIT, detail=str(len(_flagged)))
_check("the cap keeps the EARLIEST-expiry end, which is the urgent one",
       _flagged[0]["days_left"] == 0
       and [r["days_left"] for r in _flagged] == sorted(r["days_left"] for r in _flagged),
       detail=str([r["days_left"] for r in _flagged[:3]]))

SENT.clear()
appmod._send_expiry_digest(BIG_ORG)
_check("5,000 flagged lots still produce exactly one email", len(SENT) == 1,
       detail=str(len(SENT)))
_body = SENT[0]["text"] if SENT else ""
_listed_names = [n for n in (f"VANMARK ITEM {i:05d}" for i in range(1, 5001))
                 if n in _body]
_check("the body lists at most EXPIRY_DIGEST_ROWS lots",
       len(_listed_names) <= appmod.EXPIRY_DIGEST_ROWS, detail=str(len(_listed_names)))
_check("the ledger gains at most that same number, never one per flagged lot",
       len(_ledger(BIG_ORG)) <= appmod.EXPIRY_DIGEST_ROWS,
       detail=str(len(_ledger(BIG_ORG))))

_by_name = {r["item_name"]: r for r in _flagged}
_expected_keys = sorted(expiry.lot_key(_by_name[n]) for n in _listed_names)
_check("EVERY ledger key belongs to a lot the email actually listed, and vice versa",
       _ledger(BIG_ORG) == _expected_keys,
       detail=str((_ledger(BIG_ORG)[:3], _expected_keys[:3])))

_age_ledger(BIG_ORG, 8)
SENT.clear()
appmod._send_expiry_digest(BIG_ORG)
_body2 = SENT[0]["text"] if SENT else ""
_listed2 = [n for n in (f"VANMARK ITEM {i:05d}" for i in range(1, 5001)) if n in _body2]
_check("the unlisted remainder is still returned as NEW on the next pass",
       len(_listed2) == appmod.EXPIRY_DIGEST_ROWS
       and not set(_listed2) & set(_listed_names),
       detail=str((len(_listed2), sorted(set(_listed2) & set(_listed_names))[:3])))
_check("two passes leave exactly 20 announced lots in the ledger",
       len(_ledger(BIG_ORG)) == 2 * appmod.EXPIRY_DIGEST_ROWS,
       detail=str(len(_ledger(BIG_ORG))))


# ═════════════════════════════════════════════════════════════════════════════
# 9. The dedup is the WEEKLY email's alone. Both the report section and the
#    /expiry page must still show a lot after the digest has named it.
#    A lot announced once and then hidden is the silent miss this whole feature
#    exists to prevent.
# ═════════════════════════════════════════════════════════════════════════════
SHOW_ORG = "OrgStillShown"
_show_user = _user("shown@example.com", SHOW_ORG)
_snapshot(SHOW_ORG, [_lot(f"KESTREL ITEM {i:02d}", i, lot_no=f"K-{i:02d}")
                     for i in range(1, 13)], uploaded_by="shown@example.com")
SENT.clear()
appmod._send_expiry_digest(SHOW_ORG)
_announced = len(_ledger(SHOW_ORG))
_block = appmod._expiry_report_block(SHOW_ORG, "https://x.test")
_check("the report section still counts all 12 after the digest announced 10",
       _announced == 10 and _block["total"] == 12,
       detail=str((_announced, _block and _block["total"])))
_check("the report section's own rows are not filtered by the ledger",
       len(_block["rows"]) == appmod.EXPIRY_REPORT_ROWS
       and "KESTREL ITEM 01" in [r["item"] for r in _block["rows"]],
       detail=str([r["item"] for r in _block["rows"]]))

_client = appmod.app.test_client()
with _client.session_transaction() as _s:
    _s["user_id"] = _show_user["id"]
    _s["email"] = _show_user["email"]
    _s["org_name"] = SHOW_ORG
    _s["role"] = _show_user["role"]
    _s["session_version"] = _show_user.get("session_version", 0)
_resp = _client.get("/expiry?days=30")
_page = _resp.get_data(as_text=True)
_check("/expiry renders after a digest run", _resp.status_code == 200,
       detail=str(_resp.status_code))
_check("/expiry still shows every lot the digest already announced",
       all(f"KESTREL ITEM {i:02d}" in _page for i in range(1, 13)),
       detail=str([i for i in range(1, 13) if f"KESTREL ITEM {i:02d}" not in _page]))


# ═════════════════════════════════════════════════════════════════════════════
# 10. Re-upload. expiry_lots.id and upload_id change every snapshot; the
#     physical lot does not. Driven through build_lots so the real match_key
#     is used, not a hand-written one.
# ═════════════════════════════════════════════════════════════════════════════
REUP_ORG = "OrgReupload"
_user("reup@example.com", REUP_ORG)
REUP_MAP = {"expiry": "Expiry Date", "item_name": "Item", "qty_on_hand": "Qty",
            "lot_no": "Lot No."}


def _upload_sheet(org, records):
    lots, _, _ = expiry.build_lots(records, REUP_MAP)
    return _snapshot(org, lots, uploaded_by="reup@example.com")


_week1 = [{"Item": "BROOKVALE UHT MILK 1L", "Expiry Date": _iso(5), "Qty": "10",
           "Lot No.": "B-2211"},
          {"Item": "NORDVIK SALMON 2KG", "Expiry Date": _iso(6), "Qty": "20",
           "Lot No.": "n-4417"}]
_id1 = _upload_sheet(REUP_ORG, _week1)
SENT.clear()
appmod._send_expiry_digest(REUP_ORG)
_check("re-upload: the first snapshot announces both lots",
       len(SENT) == 1 and len(_ledger(REUP_ORG)) == 2, detail=str(_ledger(REUP_ORG)))
_age_ledger(REUP_ORG, 8)

# Same physical lots, a fresh snapshot, and the text drift a re-export brings:
# different casing, extra spacing, punctuation in the item name.
_week2 = [{"Item": "brookvale  uht milk 1l", "Expiry Date": _iso(5), "Qty": "8",
           "Lot No.": " b-2211 "},
          {"Item": "Nordvik Salmon, 2kg", "Expiry Date": _iso(6), "Qty": "18",
           "Lot No.": "N-4417"}]
_id2 = _upload_sheet(REUP_ORG, _week2)
_check("re-upload: the snapshot id really did change", _id2 != _id1,
       detail=str((_id1, _id2)))
SENT.clear()
appmod._send_expiry_digest(REUP_ORG)
_check("re-upload: the SAME physical lots are not re-announced",
       len(SENT) == 0 and len(_ledger(REUP_ORG)) == 2,
       detail=str((len(SENT), _ledger(REUP_ORG))))
_age_ledger(REUP_ORG, 8)

# One genuine change in each half of the key: a new lot number, a new expiry.
_week3 = [{"Item": "BROOKVALE UHT MILK 1L", "Expiry Date": _iso(5), "Qty": "8",
           "Lot No.": "B-2212"},
          {"Item": "NORDVIK SALMON 2KG", "Expiry Date": _iso(7), "Qty": "18",
           "Lot No.": "N-4417"}]
_upload_sheet(REUP_ORG, _week3)
SENT.clear()
appmod._send_expiry_digest(REUP_ORG)
_body3 = SENT[0]["text"] if SENT else ""
_check("re-upload: a new lot number counts as a new lot",
       "B-2212" in _body3, detail=_body3[:300])
_check("re-upload: a changed expiry date counts as a new lot",
       _body3.count("NORDVIK SALMON 2KG") == 1 and _iso(7) in _body3,
       detail=_body3[:300])
_check("re-upload: and the ledger grew by exactly those two",
       len(_ledger(REUP_ORG)) == 4, detail=str(_ledger(REUP_ORG)))


# ═════════════════════════════════════════════════════════════════════════════
# 11. Org isolation, with the names and lot numbers deliberately overlapping so
#     the lot_key strings are IDENTICAL across the two orgs.
# ═════════════════════════════════════════════════════════════════════════════
_user("a@orgalpha.example.com", "OrgAlpha")
_user("b@orgbeta.example.com", "OrgBeta")
_shared = [_lot("BROOKVALE UHT MILK 1L", 3, lot_no="X-1"),
           _lot("NORDVIK SALMON 2KG", 4, lot_no="X-2"),
           _lot("PADIMAS RICE 5KG", 5, lot_no="X-3")]
_alpha_id = _snapshot("OrgAlpha", [dict(x) for x in _shared],
                      uploaded_by="a@orgalpha.example.com")
_beta_id = _snapshot("OrgBeta", [dict(x) for x in _shared[:1]],
                     uploaded_by="b@orgbeta.example.com")

SENT.clear()
appmod._send_expiry_digest("OrgAlpha")
_check("org A's digest goes to org A's own user only",
       [m["to"] for m in SENT] == ["a@orgalpha.example.com"],
       detail=str([m["to"] for m in SENT]))
_check("org A announcing a key does NOT suppress the identical key for org B",
       len(_ledger("OrgAlpha")) == 3 and _ledger("OrgBeta") == [],
       detail=str((_ledger("OrgAlpha"), _ledger("OrgBeta"))))

SENT.clear()
appmod._send_expiry_digest("OrgBeta")
_check("org B still gets its own lot even though org A already announced that key",
       len(SENT) == 1 and SENT[0]["to"] == "b@orgbeta.example.com"
       and "BROOKVALE UHT MILK 1L" in SENT[0]["text"],
       detail=str([m["to"] for m in SENT]))
_check("org B's digest contains none of org A's other lots",
       "PADIMAS RICE 5KG" not in SENT[0]["text"]
       and "NORDVIK SALMON 2KG" not in SENT[0]["text"], detail=SENT[0]["text"][:300])

_check("get_expiry_upload refuses another org's snapshot id",
       db.get_expiry_upload("OrgBeta", _alpha_id) is None
       and db.get_expiry_upload("OrgAlpha", _beta_id) is None,
       detail=str((db.get_expiry_upload("OrgBeta", _alpha_id),
                   db.get_expiry_upload("OrgAlpha", _beta_id))))
_check("get_sent_expiry_alerts never returns another org's rows",
       {r["lot_key"] for r in db.get_sent_expiry_alerts("OrgBeta", 100)}
       == set(_ledger("OrgBeta")) and len(_ledger("OrgBeta")) == 1,
       detail=str(_ledger("OrgBeta")))
_check("the report section for org B counts only org B's lots",
       (appmod._expiry_report_block("OrgBeta", "https://x.test") or {}).get("total") == 1,
       detail=str(appmod._expiry_report_block("OrgBeta", "https://x.test")))
_check("an org name that does not exist reads nothing rather than everything",
       appmod._expiry_flagged("OrgDoesNotExist") == (None, [])
       and appmod._expiry_flagged("") == (None, []),
       detail=str(appmod._expiry_flagged("OrgDoesNotExist")))


# ═════════════════════════════════════════════════════════════════════════════
# 12. Recipient safety. uploaded_by is a STORED string; it must never be
#     trusted straight into a To: header.
# ═════════════════════════════════════════════════════════════════════════════
_user("owner@orgrecip.example.com", "OrgRecip")
_user("stranger@orgother.example.com", "OrgOther")
_check("an uploaded_by belonging to ANOTHER org falls back to this org's admin",
       db.get_org_alert_recipient("OrgRecip", "stranger@orgother.example.com")
       == "owner@orgrecip.example.com",
       detail=str(db.get_org_alert_recipient("OrgRecip", "stranger@orgother.example.com")))
_check("an uploaded_by that is nobody's address falls back to this org's admin",
       db.get_org_alert_recipient("OrgRecip", "ghost@nowhere.example.com")
       == "owner@orgrecip.example.com")

_snapshot("OrgRecip", [_lot("KESTREL JUICE 1L", 6, lot_no="R-1")],
          uploaded_by="stranger@orgother.example.com")
SENT.clear()
appmod._send_expiry_digest("OrgRecip")
_check("the stranger is never mailed another org's stock",
       len(SENT) == 1 and SENT[0]["to"] == "owner@orgrecip.example.com",
       detail=str([m["to"] for m in SENT]))

_user("reviewer@orgnoadmin.example.com", "OrgNoAdmin", role="reviewer")
_snapshot("OrgNoAdmin", [_lot("VANMARK PRAWN 1KG", 6, lot_no="N-1")], uploaded_by="")
_check("an org with no admin resolves to nobody",
       db.get_org_alert_recipient("OrgNoAdmin", "") is None,
       detail=str(db.get_org_alert_recipient("OrgNoAdmin", "")))
SENT.clear()
appmod._send_expiry_digest("OrgNoAdmin")
_check("an org with no recipient sends nothing AND records nothing",
       len(SENT) == 0 and _ledger("OrgNoAdmin") == [],
       detail=str((len(SENT), _ledger("OrgNoAdmin"))))


# ═════════════════════════════════════════════════════════════════════════════
# 13. Injection. Item names and lot numbers are echoed from a client file into
#     two HTML email bodies, a Subject header, and a SQL ledger write.
# ═════════════════════════════════════════════════════════════════════════════
XSS_ITEM = '<img src=x onerror=alert(1)>'
XSS_LOT = '"><script>alert(2)</script>'
SQL_LOT = "'; DROP TABLE expiry_alerts_sent; --"
CRLF_ITEM = "BROOKVALE\r\nBcc: attacker@example.com"

INJ_ORG = "OrgInjection"
_inj_user = _user("inject@example.com", INJ_ORG)
_snapshot(INJ_ORG, [_lot(XSS_ITEM, 2, lot_no=XSS_LOT),
                    _lot(CRLF_ITEM, 3, lot_no=SQL_LOT),
                    _lot("PADIMAS RICE 5KG", 4, lot_no="OK-1")],
          uploaded_by="inject@example.com")

SENT.clear()
appmod._send_expiry_digest(INJ_ORG)
_dig = SENT[0] if SENT else {"text": "", "html": "", "subject": ""}
_check("digest HTML: the script-injecting item name is escaped",
       XSS_ITEM not in _dig["html"] and "&lt;img" in _dig["html"],
       detail=_dig["html"][:200])
_check("digest HTML: the attribute-breaking lot number is escaped",
       XSS_LOT not in _dig["html"] and "<script>" not in _dig["html"],
       detail=_dig["html"][:200])
_check("digest subject carries no CR or LF (no header injection)",
       "\r" not in (_dig["subject"] or "") and "\n" not in (_dig["subject"] or ""),
       detail=repr(_dig["subject"]))
_check("digest subject is counts only, never client text",
       "BROOKVALE" not in (_dig["subject"] or "")
       and "attacker@example.com" not in (_dig["subject"] or ""),
       detail=repr(_dig["subject"]))
_check("the whole message still serialises with hostile content in it",
       bool(RAW_MSGS) and len(RAW_MSGS[-1].as_bytes()) > 0)

_inj_block = appmod._expiry_report_block(INJ_ORG, "https://x.test")
SENT.clear()
emails._send_analysis_ready_email(
    _inj_user["id"], 1,
    {"total_items": 1, "critical": 0, "low": 0, "rec_count": 0, "flagged": 0},
    "https://x.test", _inj_block)
_rep = SENT[0]
_check("report-email HTML: the script-injecting item name is escaped",
       XSS_ITEM not in _rep["html"] and "&lt;img" in _rep["html"],
       detail=_rep["html"][-400:])
_check("report-email HTML: the attribute-breaking lot number is escaped",
       XSS_LOT not in _rep["html"] and "<script>" not in _rep["html"],
       detail=_rep["html"][-400:])
_check("report-email subject is untouched by the expiry section",
       _rep["subject"] == "Your berthcast analysis is ready", detail=str(_rep["subject"]))

_check("a lot number carrying SQL reaches the ledger as data, not as SQL",
       any(SQL_LOT.upper() in k for k in _ledger(INJ_ORG))
       and db.query("SELECT COUNT(*) AS n FROM expiry_alerts_sent")[0]["n"] > 0,
       detail=str(_ledger(INJ_ORG)))


# ═════════════════════════════════════════════════════════════════════════════
# 14. Failure paths must not poison state.
# ═════════════════════════════════════════════════════════════════════════════
FAIL_ORG = "OrgSmtpFails"
_user("smtp@example.com", FAIL_ORG)
_snapshot(FAIL_ORG, [_lot(f"KESTREL ITEM {i:02d}", i, lot_no=f"F-{i:02d}")
                     for i in range(1, 6)], uploaded_by="smtp@example.com")

emails._deliver = lambda *a, **k: False
try:
    appmod._send_expiry_digest(FAIL_ORG)
    _check("a bounced send (deliver -> False) returns normally", True)
except Exception as exc:
    _check("a bounced send (deliver -> False) returns normally", False, detail=repr(exc))
_check("a bounced send leaves the ledger EMPTY so next week retries",
       _ledger(FAIL_ORG) == [], detail=str(_ledger(FAIL_ORG)))


def _explode(*a, **k):
    raise RuntimeError("smtp connection reset")


emails._deliver = _explode
try:
    appmod._send_expiry_digest(FAIL_ORG)
    _check("a raising send is swallowed, not propagated to the scheduler thread", True)
except Exception as exc:
    _check("a raising send is swallowed, not propagated to the scheduler thread",
           False, detail=repr(exc))
_check("a raising send also leaves the ledger empty",
       _ledger(FAIL_ORG) == [], detail=str(_ledger(FAIL_ORG)))

emails._deliver = _capture
SENT.clear()
appmod._send_expiry_digest(FAIL_ORG)
_check("once mail works again the same lots are retried and announced",
       len(SENT) == 1 and len(_ledger(FAIL_ORG)) == 5,
       detail=str((len(SENT), _ledger(FAIL_ORG))))

# A pass over an org whose read blows up must not stop the other orgs.
#
# Bomb _expiry_snapshot, NOT _expiry_flagged. Since the gate reorder the
# snapshot read is the first thing _send_expiry_digest does, so it is reached
# whatever the org's cadence state; bombing the later scan let OrgAlpha return
# at the cadence gate before the bomb ever armed, so the check passed while
# testing nothing. _bombed proves it actually fired rather than assuming it.
_orig_snapshot = appmod._expiry_snapshot
_bombed = {"hit": False}


def _snapshot_bomb(org_name):
    if org_name == "OrgAlpha":
        _bombed["hit"] = True
        raise RuntimeError("db wedged")
    return _orig_snapshot(org_name)


appmod._expiry_snapshot = _snapshot_bomb
try:
    appmod._send_expiry_digest("OrgAlpha")
    _contained, _why = True, ""
except Exception as exc:
    _contained, _why = False, repr(exc)
appmod._expiry_snapshot = _orig_snapshot
_check("one org's read blowing up is contained inside _send_expiry_digest",
       _contained and _bombed["hit"],
       detail=str((_contained, _bombed["hit"], _why)))


# ═════════════════════════════════════════════════════════════════════════════
# 15. Scheduler guards. The loop is replaced with a recorder first, so no test
#     ever runs a real pass or a real 6-hour sleep.
# ═════════════════════════════════════════════════════════════════════════════
_orig_loop = appmod._expiry_alert_loop
_loop_runs = []


def _recording_loop():
    _loop_runs.append(threading.current_thread().name)


appmod._expiry_alert_loop = _recording_loop
appmod._expiry_sched_started = False

appmod._ensure_expiry_scheduler()
_check("under TESTING the scheduler starts nothing",
       appmod._expiry_sched_started is False and _loop_runs == [],
       detail=str((appmod._expiry_sched_started, _loop_runs)))

appmod.app.config["TESTING"] = False
_saved_pw = os.environ.pop("MAIL_APP_PASSWORD")
appmod._ensure_expiry_scheduler()
_check("with no mail credentials the scheduler starts nothing",
       appmod._expiry_sched_started is False and _loop_runs == [],
       detail=str((appmod._expiry_sched_started, _loop_runs)))
os.environ["MAIL_APP_PASSWORD"] = _saved_pw

appmod._ensure_expiry_scheduler()
appmod._ensure_expiry_scheduler()
appmod._ensure_expiry_scheduler()
for _ in range(50):
    if _loop_runs:
        break
    time.sleep(0.02)
time.sleep(0.2)
_check("three calls with mail configured start exactly ONE scheduler thread",
       len(_loop_runs) == 1 and _loop_runs[0] == "expiry-digest", detail=str(_loop_runs))
_check("no stray thread named expiry-digest is left running",
       [t for t in threading.enumerate() if t.name == "expiry-digest" and t.is_alive()] == [],
       detail=str([t.name for t in threading.enumerate()]))

appmod._expiry_alert_loop = _orig_loop
appmod.app.config["TESTING"] = True
appmod._expiry_sched_started = True   # keep the real loop from ever starting here


# ═════════════════════════════════════════════════════════════════════════════
# 16. Prune. Housekeeping by age, across every org, without eating recent rows.
# ═════════════════════════════════════════════════════════════════════════════
db.record_expiry_alerts("OrgPruneA", ["stale-a", "fresh-a"])
db.record_expiry_alerts("OrgPruneB", ["stale-b", "fresh-b"])
db.execute("UPDATE expiry_alerts_sent SET sent_at=? WHERE lot_key IN "
           "('stale-a','stale-b')", (_utc(500),))
_pruned = db.prune_expiry_alerts(_utc(appmod.EXPIRY_ALERT_KEEP_DAYS))
_check("prune removes only the rows past the keep window, in both orgs",
       _pruned == 2 and _ledger("OrgPruneA") == ["fresh-a"]
       and _ledger("OrgPruneB") == ["fresh-b"],
       detail=str((_pruned, _ledger("OrgPruneA"), _ledger("OrgPruneB"))))
_check("prune on an empty window removes nothing",
       db.prune_expiry_alerts(_utc(appmod.EXPIRY_ALERT_KEEP_DAYS)) == 0)
_check("record_expiry_alerts with an empty list is a no-op, not an error",
       db.record_expiry_alerts("OrgPruneA", []) is None
       and _ledger("OrgPruneA") == ["fresh-a"])


# ═════════════════════════════════════════════════════════════════════════════
# 17. REGRESSION PIN (defect found here, since FIXED) — HTML entities must not
#     leak into the PLAIN TEXT part of either email.
#
# As found, emails._expiry_section ran every client string through _esc for the
# text/plain body as well as the HTML one. A text/plain part is not HTML, so
# HTML entities in it are just wrong characters in the client's inbox: an item
# name of "BROOKVALE A&W SYRUP 1L" arrives as "BROOKVALE A&amp;W SYRUP 1L" and
# a lot number of "5'S PACK" arrives as "5&#x27;S PACK". Ampersands and
# apostrophes are ordinary in food product names, so this fires on real data,
# not on an attack.
#
# It also breaks the convention set two functions up in the SAME file:
# _send_critical_alert escapes only its HTML rows and leaves its text rows raw.
# The fix belongs in emails._expiry_section (escape for HTML only), NOT here,
# and the HTML half must stay escaped -- section 13 above pins that so nobody
# "fixes" this by deleting _esc everywhere.
# ═════════════════════════════════════════════════════════════════════════════
AMP_ITEM = "BROOKVALE A&W SYRUP 1L"
AMP_LOT = "5'S PACK"
AMP_ORG = "OrgAmpersand"
_amp_user = _user("amp@example.com", AMP_ORG)
_snapshot(AMP_ORG, [_lot(AMP_ITEM, 4, lot_no=AMP_LOT)], uploaded_by="amp@example.com")

SENT.clear()
appmod._send_expiry_digest(AMP_ORG)
_amp_text = SENT[0]["text"] if SENT else ""
_check("digest TEXT part renders an ampersand in an item name literally",
       AMP_ITEM in _amp_text, detail=repr(
           [l for l in _amp_text.splitlines() if "BROOKVALE" in l]))
_check("digest TEXT part renders an apostrophe in a lot number literally",
       AMP_LOT in _amp_text, detail=repr(
           [l for l in _amp_text.splitlines() if "PACK" in l]))

_amp_block = appmod._expiry_report_block(AMP_ORG, "https://x.test")
SENT.clear()
emails._send_analysis_ready_email(
    _amp_user["id"], 1,
    {"total_items": 1, "critical": 0, "low": 0, "rec_count": 0, "flagged": 0},
    "https://x.test", _amp_block)
_rep_text = SENT[0]["text"]
_check("report-email TEXT part renders an ampersand in an item name literally",
       AMP_ITEM in _rep_text, detail=repr(
           [l for l in _rep_text.splitlines() if "BROOKVALE" in l]))
# The existing sender in the same module is the reference for what "right"
# looks like, and it is checked here so the comparison is evidence, not opinion.
SENT.clear()
emails._send_critical_alert(
    _amp_user["id"], 1, [{"item": AMP_ITEM, "days_of_supply": 3, "observation": ""}],
    "https://x.test")
_check("reference: the existing critical alert keeps its TEXT part unescaped",
       AMP_ITEM in SENT[0]["text"], detail=repr(SENT[0]["text"][:200]))


# ═════════════════════════════════════════════════════════════════════════════
# 18. REGRESSION PIN (defect found here, since FIXED) — the expensive lot scan
#     must run AFTER the cheap cadence gate.
#
# As found, _send_expiry_digest called _expiry_flagged (a SELECT of up to
# EXPIRY_ALERT_SCAN_LIMIT lot rows, plus a Python pass over all of them) as its
# very first act, and only THEN read the ledger to discover that the weekly
# window is still closed and there is nothing to send. Six of every seven days
# that whole read was thrown away, once per org, every
# EXPIRY_DIGEST_CHECK_INTERVAL_S -- on one gunicorn worker with 512 MB, which
# is the constraint the plan's own STOP condition names.
#
# The fix is ordering, not logic: read the ledger and apply the cadence gate
# before touching expiry_lots. It belongs in app._send_expiry_digest, not here.
# ═════════════════════════════════════════════════════════════════════════════
COST_ORG = "OrgScanCost"
_user("cost@example.com", COST_ORG)
_snapshot(COST_ORG, [_lot(f"NORDVIK ITEM {i:04d}", i % 200, lot_no=f"S-{i:04d}")
                     for i in range(1, 1201)], uploaded_by="cost@example.com")
SENT.clear()
appmod._send_expiry_digest(COST_ORG)          # first pass: legitimately sends
_check("the cost fixture sent once, so the weekly window is now closed",
       len(SENT) == 1, detail=str(len(SENT)))

_reads = {"calls": 0, "rows": 0}
_orig_get_lots = db.get_expiry_lots


def _counting_get_lots(*a, **k):
    rows = _orig_get_lots(*a, **k)
    _reads["calls"] += 1
    _reads["rows"] += len(rows)
    return rows


db.get_expiry_lots = _counting_get_lots
SENT.clear()
appmod._send_expiry_digest(COST_ORG)          # cadence closed: sends nothing
db.get_expiry_lots = _orig_get_lots

_check("the closed-cadence pass correctly sends nothing", len(SENT) == 0,
       detail=str(len(SENT)))
_check("a pass that sends nothing reads NO lot rows (cadence gate before the scan)",
       _reads["rows"] == 0,
       detail=f"{_reads['calls']} get_expiry_lots call(s), {_reads['rows']} rows materialised")


if _FAILED:
    # Echoed to stderr as well, the same way tests/test_expiry_break.py does it:
    # run_tests.py prints only the last few lines of a failing script, and the
    # deliberate SMTP-explodes and db-wedged cases above each log a traceback to
    # stderr. Without this the runner's tail would be those tracebacks instead of
    # the list of what actually broke.
    for _stream in (sys.stdout, sys.stderr):
        print(f"\n{len(_FAILED)} CHECK(S) FAILED:", file=_stream)
        for _name in _FAILED:
            print("  - " + _name, file=_stream)
        print("SOME TESTS FAILED", file=_stream)
    sys.exit(1)
print("\nAll expiry alert break tests passed.")
