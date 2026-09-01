"""Parsing and validation for uploaded lot-tracking exports.

Pure functions only -- no SQL, no Flask. All database work lives in
database.py, per the repo convention (same split as tenders.py).

A lot is one physical batch of one item with its own expiry date. The page this
feeds answers two questions: what has already expired while stock is still
sitting on it, and what expires in the next N days. Everything here exists to
turn the client's own export into rows that can be ranked, and to REFUSE rows it
cannot read rather than guess -- a wrong expiry date either scares staff into
dumping good stock or hides stock that is already bad, and both cost real money.

The lot rows are read here and shown here. They do NOT feed the recommendation
maths: the pipeline merges lots into one row per item on purpose, which is right
for reorder quantities and wrong for "which lot goes off first".
"""
import datetime
import math
import re

from agents.shared import normalise_match_key
from tenders import parse_date   # same Excel-serial handling; one date parser, not two

# Ordered most specific first. A header satisfies a field if it CONTAINS one of
# these once punctuation and case are stripped. One header satisfies one field.
#
# The omissions are the design. There is deliberately NO bare "code" keyword
# ("Location Code" is column 0 and would steal the item code), NO bare "name"
# ("Location Name" and "Supplier Name" would steal the item name), NO bare
# "qty" ("Qty Selected" / "Qty Allocated" are not stock you can sell), and NO
# bare "date" ("Original Receipt Date" sits two columns before the expiry).
_FIELD_KEYWORDS = {
    "expiry":        ("expirydate", "expdate", "expiry", "expiration",
                      "bestbefore", "useby", "shelflifeend"),
    # OPTIONAL, and it sits immediately after "expiry" so the expiry column is
    # always claimed first and can never be stolen by this. Feeds the life-class
    # inference (a lot received three months before it expires is fresh stock,
    # one received two years before is not); nothing renders it.
    "received":      ("originalreceiptdate", "receiptdate", "receiveddate",
                      "datereceived", "goodsreceiptdate", "grndate"),
    "qty_available": ("qtyavailable", "availableqty", "quantityavailable",
                      "qtyavail", "available"),
    "qty_on_hand":   ("qtyonhand", "onhandqty", "quantityonhand", "onhand",
                      "stockonhand", "closingqty", "balanceqty"),
    "lot_no":        ("lotno", "lotnumber", "batchno", "batchnumber", "lot", "batch"),
    "item_code":     ("inventorycode", "itemcode", "stockcode", "productcode",
                      "materialcode", "stkid", "itemno", "sku"),
    "item_name":     ("inventorydescription", "itemdescription",
                      "productdescription", "itemname", "productname",
                      "stkname", "description", "desc"),
    "uom":           ("uom", "unitofmeasure", "unit"),
    # OPTIONAL. The client's ERP knows chilled/frozen/dry but their current
    # export does not carry it, so this usually misses and the receipt-gap
    # inference takes over. It exists for the day the column appears.
    #
    # The omissions are again the design. NO bare "type" ("Document Type",
    # "Lot Type"), NO bare "group" ("Supplier Group"), NO bare "class"
    # ("Classification"). A false hit here is worse than a miss: it would
    # silently classify every fresh lot as long-life on unrecognised values,
    # and chilled stock would then stop being flagged at 28 days -- the one
    # thing this feature was asked for.
    "category":      ("itemcategory", "productcategory", "stockcategory",
                      "inventorycategory", "itemgroup", "productgroup",
                      "stockgroup", "itemtype", "producttype", "storagetype",
                      "storagecondition", "temperaturezone", "tempzone",
                      "category"),
}
# "category" goes LAST so every other field claims its header first.
_FIELD_ORDER = ("expiry", "received", "qty_available", "qty_on_hand", "lot_no",
                "item_code", "item_name", "uom", "category")

# Last resort, and EXACT rather than substring -- that is the whole point. A
# simpler sheet whose columns are just "Item" and "Qty" has to import, but the
# substring nets that would catch those also catch "Qty Selected", "Qty
# Allocated" and "Item Code" on the real 22-column export, and none of those is
# the number or the label this page needs. An exact match cannot hit any of
# them. Only consulted for a field every keyword above missed.
_FIELD_EXACT = {
    "qty_on_hand": ("qty", "quantity"),
    "item_name":   ("item", "product"),
}

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")

# A quantity is never a date. "Available Date" contains "available", so without
# this the date column is taken as the available quantity -- and the page's
# COALESCE prefers qty_available, so an Excel date serial (46311) outranks the
# real figure (12) on the screen staff use to decide what to dump. Same lesson
# the tenders build learned: a substring net has to exclude what it must never
# catch.
_QTY_FIELDS = ("qty_available", "qty_on_hand")

# A date field is never a quantity, the same way a quantity is never a date.
# Without this, a sheet with "Qty Received" hands the receipt-date field a
# number, the parse fails, and every lot silently falls to the long-life
# default -- the failure is invisible because it looks exactly like a sheet
# with no receipt column at all.
_DATE_FIELDS = ("received",)

# The client's own numbers. They will not sell chilled stock with under 14 days
# left or dry/frozen with under 6 months, so the alert has to fire while there
# is still time to move it: roughly two weeks of buffer on short-life stock and
# a month on long-life. Dry and frozen carry the same rule, which is why this
# is a two-way split and not a three-way one.
SHORT_LIFE_FLAG_DAYS = 28
LONG_LIFE_FLAG_DAYS  = 210

# Nothing in the export says chilled or frozen, but the shelf life does: stock
# received 3 months before it expires is fresh, stock received 2 years before
# it expires is not. 180 days sits in the empty middle of the real file's
# distribution, so a wobble either way moves nothing.
SHORT_LIFE_GAP_DAYS = 180

# Checked SHORT first on purpose: a value that somehow contains both puts the
# lot in the class that flags earlier. An early alert is noise, a late one is
# thrown-away stock.
_SHORT_LIFE_TOKENS = ("CHILL", "FRESH")
_LONG_LIFE_TOKENS  = ("FROZEN", "DRY")

# Report noise, not products. See is_summary_value for why the code column and
# the name column are judged by different rules.
_TOTAL_EXACT = {"TOTAL", "TOTALS", "SUBTOTAL", "SUB-TOTAL", "SUB TOTAL", "GRAND TOTAL"}
_TOTAL_PREFIX = re.compile(r"^\s*(?:inventory|stock|item)?\s*(?:grand\s+|sub[\s-]*)?totals?\b",
                           re.IGNORECASE)
# What may follow a total word and still be report noise: "Total (CARTON)".
# Anything else after it is a product name -- "TOTAL PROTEIN MIX 5KG".
_TOTAL_PAREN = re.compile(r"^\s*\(.*\)\s*$")

# Bound what a single bad file can write into the rejects blob. Mirrors
# tenders.MAX_REJECTS_STORED; the stored list is capped, the COUNT is the true
# one, so imported + skipped + unreadable always accounts for every row read.
MAX_REJECTS_STORED = 50


def _norm_header(name) -> str:
    return _NOT_ALNUM.sub("", str(name).casefold())


def _first_keyword_hit(normed, claimed, keywords):
    """First unclaimed header containing the earliest keyword that hits."""
    for keyword in keywords:
        hit = next((h for h, n in normed
                    if h not in claimed and keyword in n), None)
        if hit is not None:
            return hit
    return None


def detect_columns(headers):
    """Map field -> header for a lot-tracking export.

    Returns (mapping, missing).
      mapping  any subset of _FIELD_ORDER that matched
      missing  which of the three REQUIRED things is absent, named in plain
               English for the flash message: "expiry date", "quantity",
               "item name or code"

    Required = an expiry date, at least one quantity column, and at least one
    item identifier. Everything else is optional and renders blank.

    `received` and `category` are optional too, and neither is ever added to
    `missing`: a sheet without them still imports, and its lots fall to the
    documented long-life default.
    """
    normed = [(h, _norm_header(h)) for h in headers]
    mapping, claimed = {}, set()
    for field in _FIELD_ORDER:
        if field in _QTY_FIELDS:
            pool = [(h, n) for h, n in normed if "date" not in n]
        elif field in _DATE_FIELDS:
            pool = [(h, n) for h, n in normed if "date" in n]
        else:
            pool = normed
        hit = _first_keyword_hit(pool, claimed, _FIELD_KEYWORDS[field])
        if hit is None and field in _FIELD_EXACT:
            # A header that IS the word, not one that merely contains it.
            hit = next((h for h, n in pool
                        if h not in claimed and n in _FIELD_EXACT[field]), None)
        if hit is not None:
            mapping[field] = hit
            claimed.add(hit)

    missing = []
    if "expiry" not in mapping:
        missing.append("expiry date")
    if "qty_available" not in mapping and "qty_on_hand" not in mapping:
        missing.append("quantity")
    if "item_name" not in mapping and "item_code" not in mapping:
        missing.append("item name or code")
    return mapping, missing


def is_summary_value(value) -> bool:
    """True when this identifier cell belongs to a report total, not a product.

    Judged by the SHAPE of the label, never by which column it arrived in. A
    report total is one of four things: absent, exactly a total word, a colon
    form ("Inventory Total : BRK-001"), or a total word followed by a
    parenthesised unit ("Total (CARTON)"). A product identifier is none of
    those even when it begins with the word -- TOTAL PROTEIN MIX 5KG is real
    stock, and so is a product coded TOTAL-500.

    Judging by the column instead is what made the same row import or vanish
    depending on whether its header said "code" or "name": a bare prefix match
    was safe enough for codes and swallowed products, while the strict rule was
    safe for names and let a "Total (CARTON)" line through as a lot carrying the
    sum of the real ones. Both directions are wrong on a page whose only job is
    to say what is expiring, so neither column gets its own rule.
    """
    text = str(value or "").strip()
    if not text:
        # A row with neither a code nor a name is not a lot anybody can act on.
        return True
    if text.upper() in _TOTAL_EXACT:
        return True
    # The label sits in front of the colon, so a product cannot be mistaken for
    # it however it is named.
    head, sep, _ = text.partition(":")
    if sep and _TOTAL_PREFIX.match(head):
        return True
    hit = _TOTAL_PREFIX.match(text)
    return bool(hit and _TOTAL_PAREN.match(text[hit.end():]))


def parse_qty(value):
    """Non-negative number, or None. Accepts '486.0', '1,200', ' 8031.0 '.

    Returns None for '', 'LOOSE', None, and for NaN/Infinity: float() accepts
    'nan', 'inf' and '1e400', neither survives a `< 0` test (NaN compares False
    against everything), and SQLite stores NaN as NULL -- which would abort the
    whole executemany and throw away every good row beside the bad one.
    Negative stock is always an error, never a number to display.

    Zero is kept, unlike tenders.parse_quantity: a lot fully allocated to
    somebody else still has an expiry date worth seeing.
    """
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    try:
        qty = float(text)
    except ValueError:
        return None
    if not math.isfinite(qty) or qty < 0:
        return None
    return qty


def build_lots(records, mapping, today=None):
    """Turn raw {header: value} dicts into validated lot rows.

    Returns (lots, rejects, stats).
      lots     dicts ready for db.save_expiry_lots
      rejects  [{"row", "reason", "item", "lot"}] -- same shape discipline as
               tenders: the user must be able to fix the sheet without guessing
               which cell offended
      stats    {"read", "summary": n, "no_expiry": n}, plus
               "category_ignored": True only when a detected category column
               turned out to recognise nothing

    INVARIANT, asserted in the tests: stats["read"] == stats["summary"]
    + len(lots) + len(rejects). Every data row is accounted for. A count that
    does not add up is worse than the file, in a product whose whole job is
    arithmetic.

    `today` is accepted and unused: nothing derived from the current date is
    stored, because days-remaining changes every night and a stored copy is
    wrong by morning.
    """
    code_col = mapping.get("item_code")
    name_col = mapping.get("item_name")
    lot_col  = mapping.get("lot_no")
    uom_col  = mapping.get("uom")
    avail_col   = mapping.get("qty_available")
    on_hand_col = mapping.get("qty_on_hand")
    recv_col = mapping.get("received")
    cat_col  = mapping.get("category")

    lots, rejects = [], []
    stats = {"read": 0, "summary": 0, "no_expiry": 0}
    for i, rec in enumerate(records, start=1):
        stats["read"] += 1
        code = str(rec.get(code_col) or "").strip() if code_col else ""
        name = str(rec.get(name_col) or "").strip() if name_col else ""
        lot  = str(rec.get(lot_col) or "").strip() if lot_col else ""
        uom  = str(rec.get(uom_col) or "").strip() if uom_col else ""

        if is_summary_value(code or name):
            stats["summary"] += 1
            continue

        label = (name or code)[:200]
        expiry = parse_date(rec.get(mapping["expiry"]))
        if expiry is None:
            stats["no_expiry"] += 1
            # Reject strings are echoed from a client file into HTML (Jinja
            # autoescapes them; never add |safe). Truncated so one 5,000-char
            # cell cannot bloat the stored blob or the page.
            rejects.append({"row": i, "reason": "no expiry date",
                            "item": label[:80], "lot": lot[:80]})
            continue

        # Same parse_date the expiry column uses -- one date parser, not two,
        # so an Excel serial in this column reads the same way it does there.
        received = parse_date(rec.get(recv_col)) if recv_col else None
        cat = str(rec.get(cat_col) or "").strip() if cat_col else ""

        available = parse_qty(rec.get(avail_col)) if avail_col else None
        on_hand   = parse_qty(rec.get(on_hand_col)) if on_hand_col else None
        if available is None and on_hand is None:
            rejects.append({"row": i, "reason": "quantity is missing or not a number",
                            "item": label[:80], "lot": lot[:80]})
            continue

        lots.append({
            "item_code":     code[:120] or None,
            "item_name":     label,
            "match_key":     normalise_match_key(label),
            "lot_no":        lot[:80] or None,
            "uom":           uom[:32] or None,
            "expiry_date":   expiry.isoformat(),
            "qty_on_hand":   on_hand,
            "qty_available": available,
            # Raw inputs, not the class derived from them: the inference rule
            # can then be corrected without asking the client to re-upload.
            "received_date": received.isoformat() if received else None,
            "category":      (cat[:60] or None),
        })

    # A category column that recognises nothing across a whole file is a
    # mis-detected column, not a file full of unknown categories. Left alone it
    # would silently mark every lot long-life and switch off the 28-day rule.
    # Drop it and let the receipt-gap inference do the work it would have done
    # anyway. This has to happen here because parse time is the only place the
    # whole file is visible; the per-row rule (unrecognised value -> long-life)
    # is untouched.
    if cat_col and not any(_category_class(r["category"]) for r in lots):
        for r in lots:
            r["category"] = None
        stats["category_ignored"] = True
    return lots, rejects, stats


def days_remaining(expiry_iso: str, today: datetime.date) -> int:
    """Whole days until this lot expires. Negative when it already has.

    Never stored. It changes every night, and a stored copy is wrong by
    morning.
    """
    return (datetime.date.fromisoformat(expiry_iso) - today).days


def _category_class(value):
    """Return "short" / "long", or None when the value says nothing recognisable.

    None is NOT "unknown means long-life" -- that decision belongs to
    life_class. Kept separate so build_lots can ask "did this column recognise
    anything at all" without inheriting the default.
    """
    text = str(value or "").upper()
    if not text:
        return None
    if any(token in text for token in _SHORT_LIFE_TOKENS):
        return "short"
    if any(token in text for token in _LONG_LIFE_TOKENS):
        return "long"
    return None


def life_class(category, received_iso, expiry_iso) -> str:
    """Return "short" or "long": category first, then the receipt-to-expiry gap,
    then long-life as the documented default.

    NEVER raises. A received_date of "0000-00-00", or any other junk a client
    file can carry, is treated as absent. This runs in a loop over uploaded
    data on a background email thread, and one bad cell must not kill a digest.

    A negative gap (receipt recorded after expiry, which is a data error) lands
    in short-life: the class that alerts earlier is the safe direction to be
    wrong in.
    """
    hit = _category_class(category)
    if hit:
        return hit
    try:
        gap = (datetime.date.fromisoformat(expiry_iso)
               - datetime.date.fromisoformat(received_iso)).days
    except (TypeError, ValueError):
        return "long"
    return "short" if gap <= SHORT_LIFE_GAP_DAYS else "long"


def flag_threshold(life) -> int:
    """How many days before expiry this life class starts being shouted about."""
    return SHORT_LIFE_FLAG_DAYS if life == "short" else LONG_LIFE_FLAG_DAYS


def flag_lots(rows, today):
    """Flagged lots from a snapshot, most urgent first, as (flagged, skipped).

    `rows` come straight from db.get_expiry_lots, so the sellable-stock filter
    and the ordering have already happened in SQL. This applies the per-lot
    threshold, which SQL cannot: it depends on the lot's own life class. Input
    order (expiry_date ASC, item_name ASC) is preserved -- it is already the
    order the email wants.

    Adds days_left, life and threshold to each row it keeps. Nothing derived is
    ever stored: days_left changes every night and a stored copy is wrong by
    morning, the same rule the /expiry page follows.

    A row whose expiry_date will not parse is SKIPPED and counted, never
    crashed on. Every row here was written by a parser that only stores ISO
    dates, so this cannot fire today; it exists because a background email
    thread must not die on one malformed cell if anything ever writes to the
    table by another path.
    """
    flagged, skipped = [], 0
    for row in rows:
        try:
            left = days_remaining(row.get("expiry_date"), today)
        except (TypeError, ValueError):
            skipped += 1
            continue
        life = life_class(row.get("category"), row.get("received_date"),
                          row.get("expiry_date"))
        limit = flag_threshold(life)
        if left <= limit:
            row["days_left"] = left
            row["life"] = life
            row["threshold"] = limit
            flagged.append(row)
    return flagged, skipped


def lot_key(row) -> str:
    """Stable fingerprint of a physical lot, for the weekly digest's ledger.

    Deliberately NOT the row id. A new snapshot writes entirely new rows, so
    expiry_lots.id and upload_id both change on every re-upload -- a ledger
    keyed on either would find nothing familiar and re-announce every flagged
    lot every single week, which is a catalogue, not an alert.

    What does NOT change when the client re-exports is the physical lot: the
    same product, in the same batch, going off on the same day. match_key is
    already stored for the item half (normalised, so casing and punctuation
    drift in the item name cannot fork the key). lot_no and expiry_date carry
    the batch half.

    A blank lot number collapses two otherwise-identical lots into one key.
    That is correct: without a lot number they are indistinguishable to anyone
    reading the email, so announcing them once is the honest count.
    """
    return "|".join((row.get("match_key") or "",
                     (row.get("lot_no") or "").strip().upper(),
                     row.get("expiry_date") or ""))
