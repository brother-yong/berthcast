"""Expiry push alerts: the report-email section and the weekly newly-flagged digest.

Proves the happy path of plan 012. The lot export already imports and ranks
(test_expiry.py); this file proves the two things built on top of it:

  C  a section appended to the analysis-ready email after every run, showing the
     FULL current flagged list (capped for display) and never a deduped one
  B  a weekly standalone email listing only what is newly flagged since the last
     send, with the ledger recording exactly the rows the email listed

Four things are easy to get silently wrong and all four are pinned here:
  1. the two new optional columns must not steal a header from the seven the
     page already maps -- `received` may claim the receipt date and nothing
     else may, and `expiry` must still claim the expiry column
  2. the life class decides the threshold (28 days chilled, 210 dry/frozen), so
     the category rule, the receipt-gap inference and the long-life default all
     have to land where the client's own numbers say
  3. the dedup key is the physical lot (match_key | lot_no | expiry_date), never
     a row id -- an id-keyed ledger looks perfect on one upload and re-announces
     every lot every week in production
  4. the ledger records the rows the email LISTED and nothing wider. Recording
     the whole newly-flagged set while showing ten of it marks lots announced
     that nobody ever saw, and they never surface in a weekly email again

emails._deliver is stubbed, so nothing here opens a socket. Throwaway temp DB,
stubbed anthropic client, no API calls. Invented brands only, the repo is
public. Run: python tests/test_expiry_alerts.py
"""
import datetime
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_expiry_alerts.db")
for _ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + _ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
# Both senders bail out early without these, so they are set BEFORE the import.
# _deliver is stubbed below, so nothing is ever actually sent.
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
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["TESTING"] = True

TODAY = datetime.date.today()
XL_EPOCH = datetime.date(1899, 12, 30)

_FAILED = []


def _check(name, cond, detail=""):
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


# ── No sockets: every send is captured instead ───────────────────────────────
# app.py imported _send_expiry_digest_email by name, but that function still
# calls emails._deliver by module global, so patching here catches both senders.
SENT = []


def _capture(msg, sender, password, recipient):
    parts = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8")
             for p in msg.get_payload()}
    SENT.append({"to": recipient, "subject": msg["Subject"],
                 "text": parts.get("text/plain", ""),
                 "html": parts.get("text/html", "")})
    return True


emails._deliver = _capture


def _serial(d):
    """The Excel serial an .xlsx date cell actually arrives as."""
    return str((d - XL_EPOCH).days)


def _iso(offset):
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


def _make_user(email, org, role="admin"):
    db.execute("INSERT INTO users (email, password_hash, org_name, model, role) "
               "VALUES (?,?,?,?,?)",
               (email, generate_password_hash("x"), org, "claude-sonnet-5", role))
    return db.query("SELECT id FROM users WHERE email=?", (email,))[0]["id"]


def _lot(name, days_out, qty=100.0, lot_no=None, received=None, category=None):
    """One stored lot row, as build_lots would emit it."""
    return {"item_code": None, "item_name": name,
            "match_key": name.lower().replace(" ", ""),
            "lot_no": lot_no, "uom": "CTN",
            "expiry_date": _iso(days_out),
            "qty_on_hand": qty, "qty_available": None,
            "received_date": received, "category": category}


def _snapshot(org, lots, uploaded_by=""):
    """Store one snapshot for an org and return its upload id."""
    upload_id = db.create_expiry_upload(org, "lots.csv", uploaded_by)
    db.save_expiry_lots(org, upload_id, lots)
    db.finalise_expiry_upload(org, upload_id, len(lots), 0, 0, "[]")
    return upload_id


def _age_ledger(org, days):
    """Push an org's ledger rows back in time, so the next pass is out of the
    weekly window. sent_at is UTC text in SQLite's CURRENT_TIMESTAMP format."""
    when = (datetime.datetime.utcnow() - datetime.timedelta(days=days)
            ).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE expiry_alerts_sent SET sent_at=? WHERE org_name=?",
               (when, org))


def _ledger(org):
    return [r["lot_key"] for r in db.get_sent_expiry_alerts(org, 10_000)]


# ── 1. The two new optional columns, on the real export's 22 headers ─────────
# The fence: `received` is allowed to claim the receipt date and nothing else
# may, `expiry` still claims the expiry column, and `category` correctly stays
# unmapped because today's export does not carry one.

RAW = ["Location Code", "Location Name", "Inventory Code", "Inventory Description",
       "UOM", "Lot No.", "Lot Reference No.", "Original Receipt Date",
       "Expiry Date", "Delivery Remarks", "Special Remarks", "Damage Remarks",
       "Condition Remarks", "Country of Origin", "Supplier Name",
       "Source Voucher No.", "Pack Size", "Qty On Hand", "Qty Selected",
       "Qty Allocated", "Qty Allocated Selected", "Qty Available"]

_map, _miss = expiry.detect_columns(RAW)
_check("received claims the receipt-date column on the real export",
       _map.get("received") == "Original Receipt Date", detail=str(_map))
_check("expiry still claims the expiry column, not the receipt date",
       _map.get("expiry") == "Expiry Date" and _miss == [],
       detail=str((_map.get("expiry"), _miss)))
_check("category stays unmapped: today's export does not carry one",
       "category" not in _map, detail=str(_map.get("category")))
_check("no header is claimed twice",
       len(set(_map.values())) == len(_map.values()), detail=str(_map))

# ── 2. A date field is never satisfied by a quantity column ─────────────────
_m, _ = expiry.detect_columns(["Item", "Expiry Date", "Qty Received", "Qty On Hand"])
_check("Qty Received does not satisfy the receipt-date field",
       "received" not in _m, detail=str(_m))
_check("the same sheet still maps its real quantity",
       _m.get("qty_on_hand") == "Qty On Hand", detail=str(_m))

# ── 3. The category keyword list catches a category and nothing near it ─────
_m, _ = expiry.detect_columns(["Item", "Expiry Date", "Qty", "Item Category"])
_check("category maps on a header named Item Category",
       _m.get("category") == "Item Category", detail=str(_m))
for _hdr in ("Document Type", "Supplier Group", "Lot Type", "Classification"):
    _m, _ = expiry.detect_columns(["Item", "Expiry Date", "Qty", _hdr])
    _check(f"category does NOT claim {_hdr!r}",
           "category" not in _m, detail=str(_m))

# ── 4. build_lots stores the receipt date, from either cell shape ────────────
MAPPING = {"expiry": "Expiry Date", "received": "Original Receipt Date",
           "item_name": "Item", "qty_on_hand": "Qty On Hand"}
RECEIVED_ON = TODAY - datetime.timedelta(days=90)
RECORDS = [
    # An .xlsx date cell arrives as an Excel serial, a .csv as text.
    {"Item": "BROOKVALE UHT MILK 1L", "Expiry Date": _iso(30),
     "Original Receipt Date": _serial(RECEIVED_ON), "Qty On Hand": "100"},
    {"Item": "NORDVIK SALMON 2KG", "Expiry Date": _iso(60),
     "Original Receipt Date": RECEIVED_ON.isoformat(), "Qty On Hand": "40"},
    {"Item": "Total (CARTON)", "Expiry Date": "", "Qty On Hand": "140"},
    {"Item": "PADIMAS RICE 5KG", "Expiry Date": "", "Qty On Hand": "12"},
]
_lots, _rejects, _stats = expiry.build_lots(RECORDS, MAPPING)
_check("an Excel serial receipt cell is stored as an ISO date",
       _lots[0]["received_date"] == RECEIVED_ON.isoformat(),
       detail=str(_lots[0]["received_date"]))
_check("a text receipt cell is stored as the same ISO date",
       _lots[1]["received_date"] == RECEIVED_ON.isoformat(),
       detail=str(_lots[1]["received_date"]))
_check("row accounting still adds up with the new columns",
       _stats["read"] == _stats["summary"] + len(_lots) + len(_rejects),
       detail=str((_stats, len(_lots), len(_rejects))))

_lots2, _, _ = expiry.build_lots(
    [{"Item": "VANMARK PRAWN 1KG", "Expiry Date": _iso(20), "Qty On Hand": "5"}],
    {"expiry": "Expiry Date", "item_name": "Item", "qty_on_hand": "Qty On Hand"})
_check("a sheet with no receipt column stores None, and still imports",
       _lots2[0]["received_date"] is None and _lots2[0]["category"] is None,
       detail=str(_lots2[0]))

# ── 5. The life class: category, then the receipt gap, then the default ─────
for _value, _want in (("CHILL", "short"), ("FRESH PRODUCE", "short"),
                      ("FROZEN", "long"), ("DRY GOODS", "long"), ("XYZ", "long")):
    _check(f"category {_value!r} classifies as {_want}-life",
           expiry.life_class(_value, None, _iso(30)) == _want,
           detail=expiry.life_class(_value, None, _iso(30)))

for _gap, _want in ((90, "short"), (200, "long"), (180, "short")):
    _recv = (TODAY - datetime.timedelta(days=_gap - 30)).isoformat()
    _check(f"no category, a {_gap}-day receipt-to-expiry gap is {_want}-life",
           expiry.life_class(None, _recv, _iso(30)) == _want,
           detail=expiry.life_class(None, _recv, _iso(30)))

_check("no category and no receipt date falls to the long-life default",
       expiry.life_class(None, None, _iso(30)) == "long")
_check("the thresholds are the client's own numbers",
       expiry.flag_threshold("short") == 28 and expiry.flag_threshold("long") == 210,
       detail=str((expiry.flag_threshold("short"), expiry.flag_threshold("long"))))

# ── 6. flag_lots applies the per-lot threshold, most urgent first ────────────
SHORT_RECV = (TODAY - datetime.timedelta(days=60)).isoformat()   # 90-day gap at +30
FIXED = TODAY


def _row(days_out, received=None, category=None, name="KESTREL JUICE 1L"):
    return {"item_name": name, "match_key": name.lower(), "lot_no": "L1",
            "expiry_date": _iso(days_out), "qty_on_hand": 10.0,
            "qty_available": None, "received_date": received, "category": category}


_flagged, _skipped = expiry.flag_lots(
    [_row(28, category="CHILLED"), _row(29, category="CHILLED")], FIXED)
_check("a short-life lot flags at 28 days and not at 29",
       len(_flagged) == 1 and _flagged[0]["days_left"] == 28 and _skipped == 0,
       detail=str([(r["days_left"], r["life"]) for r in _flagged]))

_flagged, _ = expiry.flag_lots([_row(210), _row(211)], FIXED)
_check("a long-life lot flags at 210 days and not at 211",
       len(_flagged) == 1 and _flagged[0]["days_left"] == 210,
       detail=str([r["days_left"] for r in _flagged]))

_flagged, _ = expiry.flag_lots([_row(-5), _row(3), _row(100)], FIXED)
_check("an already-expired lot with stock flags, and the order stays earliest-first",
       [r["days_left"] for r in _flagged] == [-5, 3, 100],
       detail=str([r["days_left"] for r in _flagged]))
_check("flag_lots records the threshold it applied",
       _flagged[0]["threshold"] == expiry.LONG_LIFE_FLAG_DAYS,
       detail=str(_flagged[0]["threshold"]))

# ── 7. The dedup key is the physical lot, never a row id ─────────────────────
_a = {"id": 1, "upload_id": 7, "match_key": "brookvaleuhtmilk1l",
      "lot_no": "b-2211", "expiry_date": "2026-11-04"}
_b = {"id": 999, "upload_id": 41, "match_key": "brookvaleuhtmilk1l",
      "lot_no": " B-2211 ", "expiry_date": "2026-11-04"}
_check("the same physical lot keys the same across re-uploads",
       expiry.lot_key(_a) == expiry.lot_key(_b), detail=expiry.lot_key(_b))
_c = dict(_a, expiry_date="2026-11-05")
_check("a different expiry date is a different lot",
       expiry.lot_key(_a) != expiry.lot_key(_c), detail=expiry.lot_key(_c))
_d = {"match_key": "nordviksalmon2kg", "lot_no": None, "expiry_date": "2026-11-04"}
_check("a blank lot number still produces a usable key",
       expiry.lot_key(_d) == "nordviksalmon2kg||2026-11-04", detail=expiry.lot_key(_d))

# ── 8. The migration: both new columns round-trip, and it is idempotent ──────
try:
    db.init_db()
    db.init_db()
    _check("init_db() runs twice without raising (the ALTERs are idempotent)", True)
except Exception as exc:
    _check("init_db() runs twice without raising (the ALTERs are idempotent)",
           False, detail=repr(exc))

RT_ORG = "OrgRoundTrip"
_rt_id = _snapshot(RT_ORG, [_lot("BROOKVALE UHT MILK 1L", 30, lot_no="B-1",
                                 received=_iso(-120), category="CHILLED")])
_rt = db.get_expiry_lots(RT_ORG, _rt_id, _iso(365), 100)
_check("received_date and category round-trip through the database",
       _rt[0]["received_date"] == _iso(-120) and _rt[0]["category"] == "CHILLED",
       detail=str((_rt[0]["received_date"], _rt[0]["category"])))
_check("get_expiry_upload returns the org's own snapshot",
       (db.get_expiry_upload(RT_ORG, _rt_id) or {}).get("id") == _rt_id,
       detail=str(db.get_expiry_upload(RT_ORG, _rt_id)))

# ── 9. The ledger: idempotent writes, and one org's keys stay its own ────────
db.record_expiry_alerts("OrgLedgerA", ["k1", "k2"])
db.record_expiry_alerts("OrgLedgerA", ["k1", "k2"])
db.record_expiry_alerts("OrgLedgerB", ["k9"])
_check("recording the same key twice writes one row",
       sorted(_ledger("OrgLedgerA")) == ["k1", "k2"], detail=str(_ledger("OrgLedgerA")))
_check("org B's ledger never shows org A's keys",
       _ledger("OrgLedgerB") == ["k9"], detail=str(_ledger("OrgLedgerB")))

# ── 10. Section C: the FULL flagged list, capped for display only ────────────
REPORT_ORG = "OrgReport"
_make_user("report@example.com", REPORT_ORG)
# 3 already expired + 11 expiring inside the long-life window = 14 flagged.
_snapshot(REPORT_ORG,
          [_lot(f"BROOKVALE ITEM {i:02d}", d, lot_no=f"R-{i:02d}")
           for i, d in enumerate([-30, -10, -1] + list(range(5, 60, 5)), start=1)])
BLOCK = appmod._expiry_report_block(REPORT_ORG, "https://x.test")
_check("the report block counts the FULL flagged list, not the rows it shows",
       BLOCK["total"] == 14 and len(BLOCK["rows"]) == appmod.EXPIRY_REPORT_ROWS,
       detail=str((BLOCK["total"], len(BLOCK["rows"]))))
_check("the report block counts the already-expired lots separately",
       BLOCK["expired"] == 3, detail=str(BLOCK["expired"]))
# Compared against the stored row, not a recomputed clock: uploaded_at is UTC
# and TODAY is local, so recomputing it here would flake once a day.
_report_upload = db.get_expiry_upload(REPORT_ORG, db.latest_expiry_upload_id(REPORT_ORG))
_check("the report block carries the snapshot date and the /expiry link",
       BLOCK["url"] == "https://x.test/expiry"
       and BLOCK["snapshot_date"] == str(_report_upload["uploaded_at"])[:10],
       detail=str((BLOCK["url"], BLOCK["snapshot_date"])))
_check("the report block's rows are earliest-expiry first",
       [r["days"] for r in BLOCK["rows"]] == sorted(r["days"] for r in BLOCK["rows"]),
       detail=str([r["days"] for r in BLOCK["rows"]]))

# ── 11. Section C renders nothing rather than an empty box ───────────────────
_check("no snapshot at all: the section is None",
       appmod._expiry_report_block("OrgNothingHere", "https://x.test") is None)

EMPTY_ORG = "OrgEmptySnapshot"
db.finalise_expiry_upload(EMPTY_ORG, db.create_expiry_upload(EMPTY_ORG, "empty.csv"),
                          0, 3, 0, "[]")
_check("a snapshot that imported no lots: the section is None",
       appmod._expiry_report_block(EMPTY_ORG, "https://x.test") is None)

FAR_ORG = "OrgNothingFlagged"
_snapshot(FAR_ORG, [_lot("NORDVIK SALMON 2KG", 400, lot_no="F-1")])
_check("lots exist but nothing is inside a threshold: the section is None",
       appmod._expiry_report_block(FAR_ORG, "https://x.test") is None)

# ── 12. The analysis-ready email: silent without a block, a section with one ─
SUMMARY = {"total_items": 12, "critical": 2, "low": 3, "rec_count": 4, "flagged": 1}
REPORT_USER = db.query("SELECT id FROM users WHERE email=?",
                       ("report@example.com",))[0]["id"]

SENT.clear()
emails._send_analysis_ready_email(REPORT_USER, 1, SUMMARY, "https://x.test")
_plain = SENT[-1]
_check("with no expiry block the email carries none of the section's marker text",
       "Stock to push" not in _plain["text"] and "Stock to push" not in _plain["html"]
       and "/expiry" not in _plain["text"] and "/expiry" not in _plain["html"],
       detail=_plain["text"][-120:])

SENT.clear()
emails._send_analysis_ready_email(REPORT_USER, 1, SUMMARY, "https://x.test", BLOCK)
_withblock = SENT[-1]
_check("with a block the TEXT part carries the count and the /expiry link",
       "Stock to push" in _withblock["text"] and "14 lots" in _withblock["text"]
       and "https://x.test/expiry" in _withblock["text"],
       detail=_withblock["text"][-400:])
_check("with a block the HTML part carries the count and the /expiry link",
       "Stock to push" in _withblock["html"] and "14 lots" in _withblock["html"]
       and "https://x.test/expiry" in _withblock["html"],
       detail=_withblock["html"][-400:])
_check("with a block the email names the already-expired lots",
       "3 already expired" in _withblock["text"]
       and "3 already expired" in _withblock["html"],
       detail=_withblock["text"][-400:])

# ── 13. The weekly digest, first pass: an empty ledger reads as never sent ───
WEEK_ORG = "OrgWeekly"
_make_user("weekly@example.com", WEEK_ORG)
_snapshot(WEEK_ORG, [_lot(f"KESTREL ITEM {i:02d}", i * 3, lot_no=f"W-{i:02d}")
                     for i in range(1, 13)], uploaded_by="weekly@example.com")

SENT.clear()
appmod._send_expiry_digest(WEEK_ORG)
_check("the first pass on an empty ledger sends exactly one email",
       len(SENT) == 1, detail=str(len(SENT)))
_first = SENT[0] if SENT else {"text": "", "html": "", "subject": "", "to": ""}
_check("the first digest goes to the person who uploaded the snapshot",
       _first["to"] == "weekly@example.com", detail=str(_first["to"]))
_check("the first digest lists the EXPIRY_DIGEST_ROWS most urgent lots",
       all(f"KESTREL ITEM {i:02d}" in _first["text"] for i in range(1, 11))
       and "KESTREL ITEM 11" not in _first["text"],
       detail=_first["text"][:400])
_check("the first digest's subject counts every newly flagged lot",
       "12 lots to push this week" in (_first["subject"] or ""),
       detail=str(_first["subject"]))
_check("the first pass records exactly the lots it listed",
       len(_ledger(WEEK_ORG)) == appmod.EXPIRY_DIGEST_ROWS,
       detail=str(len(_ledger(WEEK_ORG))))
_check("the ledger holds lot fingerprints, not row ids",
       all(k.count("|") == 2 for k in _ledger(WEEK_ORG)),
       detail=str(_ledger(WEEK_ORG)[:2]))

# ── 14. The drain: nothing is ever marked announced that was not in an email ─
DRAIN_ORG = "OrgDrain"
_make_user("drain@example.com", DRAIN_ORG)
_snapshot(DRAIN_ORG, [_lot(f"PADIMAS ITEM {i:02d}", i, lot_no=f"D-{i:02d}")
                      for i in range(1, 26)], uploaded_by="drain@example.com")

SENT.clear()
appmod._send_expiry_digest(DRAIN_ORG)
_check("25 newly flagged lots produce one email listing 10",
       len(SENT) == 1 and all(f"PADIMAS ITEM {i:02d}" in SENT[0]["text"]
                              for i in range(1, 11)),
       detail=str(len(SENT)))
_check("the body states the true new-count, not the number of rows shown",
       "25 lots" in SENT[0]["text"], detail=SENT[0]["text"][:300])
_check("the ledger gains exactly 10 rows, never one per flagged lot",
       len(_ledger(DRAIN_ORG)) == 10, detail=str(len(_ledger(DRAIN_ORG))))

_age_ledger(DRAIN_ORG, 8)
SENT.clear()
appmod._send_expiry_digest(DRAIN_ORG)
_check("the following week the unlisted 15 are still new, and the next 10 are listed",
       len(SENT) == 1 and "15 lots" in SENT[0]["text"]
       and all(f"PADIMAS ITEM {i:02d}" in SENT[0]["text"] for i in range(11, 21))
       and "PADIMAS ITEM 21" not in SENT[0]["text"],
       detail=SENT[0]["text"][:400] if SENT else "no email")
_check("after two passes the ledger holds 20 rows, one per lot actually emailed",
       len(_ledger(DRAIN_ORG)) == 20, detail=str(len(_ledger(DRAIN_ORG))))

# ── 15. A newly arrived urgent lot jumps the queue with no extra logic ───────
URGENT_ORG = "OrgUrgent"
_make_user("urgent@example.com", URGENT_ORG)
_urgent_upload = _snapshot(
    URGENT_ORG, [_lot(f"VANMARK ITEM {i:02d}", i + 5, lot_no=f"U-{i:02d}")
                 for i in range(1, 26)], uploaded_by="urgent@example.com")
appmod._send_expiry_digest(URGENT_ORG)
_age_ledger(URGENT_ORG, 8)

# Same snapshot, one more lot, already expired: it sorts ahead of the tail.
db.save_expiry_lots(URGENT_ORG, _urgent_upload,
                    [_lot("NORDVIK URGENT LOT 1KG", -2, lot_no="U-99")])
SENT.clear()
appmod._send_expiry_digest(URGENT_ORG)
_body = SENT[0]["text"] if SENT else ""
_check("the newly arrived urgent lot is the first row listed",
       _body.index("NORDVIK URGENT LOT 1KG") < _body.index("VANMARK ITEM 16"),
       detail=_body[:400])
_check("the new-count counts every newly flagged lot, not just the 10 listed",
       "16 lots" in _body, detail=_body[:300])
_check("only the 10 listed lots were added to the ledger",
       len(_ledger(URGENT_ORG)) == 20, detail=str(len(_ledger(URGENT_ORG))))

# ── 16. Who the weekly digest is addressed to ───────────────────────────────
RECIP_ORG = "OrgRecipient"
_make_user("owner@example.com", RECIP_ORG)
_make_user("picker@example.com", RECIP_ORG, role="reviewer")
_check("uploaded_by is used when it is a real user of that org",
       db.get_org_alert_recipient(RECIP_ORG, "picker@example.com") == "picker@example.com",
       detail=str(db.get_org_alert_recipient(RECIP_ORG, "picker@example.com")))
_check("a blank uploaded_by falls back to the org's admin",
       db.get_org_alert_recipient(RECIP_ORG, "") == "owner@example.com",
       detail=str(db.get_org_alert_recipient(RECIP_ORG, "")))

_snapshot(RECIP_ORG, [_lot("BROOKVALE UHT MILK 1L", 10, lot_no="P-1")],
          uploaded_by="picker@example.com")
SENT.clear()
appmod._send_expiry_digest(RECIP_ORG)
_check("the digest is actually addressed to the resolved recipient",
       len(SENT) == 1 and SENT[0]["to"] == "picker@example.com",
       detail=str([m["to"] for m in SENT]))

# ── The dedup is the weekly email's alone ───────────────────────────────────
# The lots the digest just announced must still be on /expiry and in section C.
_check("section C still shows every flagged lot after the digest announced them",
       (appmod._expiry_report_block(WEEK_ORG, "https://x.test") or {}).get("total") == 12,
       detail=str(appmod._expiry_report_block(WEEK_ORG, "https://x.test")))
_check("orgs_with_expiry_lots finds the orgs the weekly loop has to scan",
       {WEEK_ORG, DRAIN_ORG, URGENT_ORG} <= set(db.orgs_with_expiry_lots(200)),
       detail=str(db.orgs_with_expiry_lots(200)))


if _FAILED:
    print("\nSOME TESTS FAILED")
    for _name in _FAILED:
        print("  FAILED: " + _name)
    sys.exit(1)
print("\nAll expiry alert tests passed.")
