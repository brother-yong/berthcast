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
}
_FIELD_ORDER = ("expiry", "qty_available", "qty_on_hand", "lot_no",
                "item_code", "item_name", "uom")

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
    """
    normed = [(h, _norm_header(h)) for h in headers]
    mapping, claimed = {}, set()
    for field in _FIELD_ORDER:
        pool = ([(h, n) for h, n in normed if "date" not in n]
                if field in _QTY_FIELDS else normed)
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
      stats    {"read", "summary": n, "no_expiry": n}

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
        })
    return lots, rejects, stats


def days_remaining(expiry_iso: str, today: datetime.date) -> int:
    """Whole days until this lot expires. Negative when it already has.

    Never stored. It changes every night, and a stored copy is wrong by
    morning.
    """
    return (datetime.date.fromisoformat(expiry_iso) - today).days
