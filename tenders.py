"""Parsing and validation for uploaded tender commitment sheets.

Pure functions only -- no SQL, no Flask. All database work lives in
database.py, per the repo convention (same split as supplier_directory.py).

A tender is a contract the client has already won: an agreed quantity of an
agreed item, sold to one customer, over a fixed period. That stock is spoken
for before it lands, so it must not be offered to anyone else. Everything here
exists to turn the client's own spreadsheet into rows state can be computed
from, and to REFUSE rows it cannot read rather than guess -- a wrong date
silently widens or shortens a commitment window, which moves stock the client
has legally promised to somebody else.
"""
import datetime
import re

from agents.shared import normalise_match_key

# Header keywords, most specific first within each field. A header satisfies a
# field if it CONTAINS one of these once punctuation and case are stripped.
# One header can only ever satisfy one field.
_FIELD_KEYWORDS = {
    "customer": ("customer", "cust", "buyer", "client", "account", "company"),
    "item":     ("itemdescription", "itemname", "itemcode", "item", "product",
                 "description", "desc", "sku", "material", "article"),
    "quantity": ("tenderqty", "committedqty", "quantity", "qty", "carton",
                 "units", "volume", "committed"),
    # "expiry"/"valid till" here mean the CONTRACT's end, not a product's shelf
    # life. A tender sheet has no product expiry column; an inventory export does.
    "start":    ("startdate", "datefrom", "fromdate", "commence", "effective",
                 "periodfrom"),
    "end":      ("enddate", "dateto", "todate", "expiry", "expire", "until",
                 "validtill", "validto", "periodto"),
}

# Words short enough to appear INSIDE an unrelated header, so they must match a
# header exactly. Found the hard way: "customer" contains "to", "vendor"
# contains "end" -- as substrings those silently stole the date columns and the
# sheet imported with the wrong periods.
_FIELD_EXACT = {
    "customer": (),
    "item":     (),
    "quantity": (),
    "start":    ("start", "from"),
    "end":      ("end", "to"),
}

# Resolved in this order so a header like "date to" is claimed by "end" before
# the looser "item"/"customer" nets ever see it.
_FIELD_ORDER = ("start", "end", "quantity", "customer", "item")

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")

# Excel stores a date as days since 1899-12-30. Bare numbers outside this band
# are quantities or IDs, not dates: 20000 is 1954-10-03, 80000 is 2119-01-11.
# Narrow on purpose -- misreading a carton count as a date is worse than
# refusing it and telling the user.
_XL_EPOCH   = datetime.date(1899, 12, 30)
_XL_MIN_SER = 20000
_XL_MAX_SER = 80000

# Bound what a single bad file can write into the rejects blob.
MAX_REJECTS_STORED = 50


def _norm_header(name) -> str:
    return _NOT_ALNUM.sub("", str(name).casefold())


def detect_columns(headers):
    """Map field name -> header, for the five fields a tender row needs.

    Returns (mapping, missing). `missing` is the list of fields no header
    matched; the caller shows it to the user rather than importing a partial
    row, because a tender row missing any one of the five cannot be applied.
    """
    normed = [(h, _norm_header(h)) for h in headers]
    mapping, claimed = {}, set()
    for field in _FIELD_ORDER:
        # Exact first: "To" beats any substring guess for the same header.
        hit = next((h for h, n in normed
                    if h not in claimed and n in _FIELD_EXACT[field]), None)
        if hit is None:
            for keyword in _FIELD_KEYWORDS[field]:
                hit = next((h for h, n in normed
                            if h not in claimed and keyword in n), None)
                if hit is not None:
                    break
        if hit is not None:
            mapping[field] = hit
            claimed.add(hit)
    missing = [f for f in _FIELD_KEYWORDS if f not in mapping]
    return mapping, missing


def parse_quantity(value):
    """Positive number, or None. Accepts '1,200' and '1 200.5'."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    try:
        qty = float(text)
    except ValueError:
        return None
    # 0 is a real number but a zero-quantity tender commits nothing, so it can
    # only be a placeholder row. Negative is always an error.
    if qty <= 0:
        return None
    return qty


def parse_date(value, day_first=True):
    """Parse one date cell to a datetime.date, or None if unreadable.

    day_first matches Singapore convention (03/04/2026 is 3 April). It is a
    parameter and not a constant so a client writing month-first is one
    argument away, not a rewrite.
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value

    text = str(value).strip()
    if not text:
        return None

    # Excel serial. The ingest layer reads raw cell values and does not apply
    # number formats, so a genuine Excel date cell arrives here as "46082",
    # not as a date. Without this branch every .xlsx upload would reject 100%
    # of its rows while a .csv of the same sheet imported fine.
    try:
        serial = float(text)
    except ValueError:
        pass
    else:
        if serial.is_integer() and _XL_MIN_SER <= serial <= _XL_MAX_SER:
            return _XL_EPOCH + datetime.timedelta(days=int(serial))
        return None

    text = text.split("T")[0].split(" ")[0]
    parts = re.split(r"[/\-.]", text)
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    a, b, c = (int(p) for p in parts)

    if len(parts[0]) == 4:            # yyyy-mm-dd, never ambiguous
        year, month, day = a, b, c
    else:
        day, month = (a, b) if day_first else (b, a)
        year = c
        if year < 100:                # two-digit year: 26 -> 2026
            year += 2000
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def build_rows(records, day_first=True, mapping=None):
    """Turn raw {header: value} dicts into validated tender rows.

    Returns (rows, rejects, mapping). Each reject carries the source row number
    and a plain-English reason -- the user has to be able to fix the sheet
    without guessing which cell offended. When a required column is absent
    entirely, mapping is {"__missing__": [fields]} and nothing is imported.

    `mapping` may be passed in when the caller has already run detect_columns
    against the headers alone -- that lets it fetch only the five columns it
    needs instead of every column in the sheet.
    """
    if not records:
        return [], [], (mapping or {})

    if mapping is None:
        mapping, missing = detect_columns(list(records[0].keys()))
        if missing:
            return [], [], {"__missing__": missing}

    rows, rejects = [], []
    for i, rec in enumerate(records, start=1):
        customer = str(rec.get(mapping["customer"]) or "").strip()
        item     = str(rec.get(mapping["item"]) or "").strip()
        qty      = parse_quantity(rec.get(mapping["quantity"]))
        start    = parse_date(rec.get(mapping["start"]), day_first)
        end      = parse_date(rec.get(mapping["end"]), day_first)

        if not customer and not item and qty is None:
            continue          # blank spacer row, not an error worth reporting

        reason = None
        if not customer:
            reason = "no customer name"
        elif not item:
            reason = "no item name"
        elif qty is None:
            reason = "quantity is missing, zero or not a number"
        elif start is None:
            reason = "start date unreadable"
        elif end is None:
            reason = "end date unreadable"
        elif end < start:
            reason = "end date is before the start date"

        if reason:
            if len(rejects) < MAX_REJECTS_STORED:
                rejects.append({"row": i, "reason": reason,
                                "customer": customer[:80], "item": item[:80]})
            continue

        rows.append({
            "customer":     customer,
            "item_name":    item,
            "match_key":    normalise_match_key(item),
            "quantity":     qty,
            "period_start": start.isoformat(),
            "period_end":   end.isoformat(),
        })
    return rows, rejects, mapping


def find_overlaps(rows):
    """Tender rows for the same customer AND item whose periods overlap.

    Two live commitments on one customer+item double-count: the same stock gets
    reserved twice, so what is left to sell reads lower than it is. Reported,
    never auto-resolved -- a client CAN hold two contracts on one item, and
    only they know which is right.
    """
    buckets = {}
    for r in rows:
        buckets.setdefault(
            (normalise_match_key(r["customer"]), r["match_key"]), []
        ).append(r)

    clashes = []
    for group in buckets.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: r["period_start"])
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                # Half-open would let a contract ending 30 Jun and one starting
                # 30 Jun both claim that day. Inclusive at both ends is the
                # reading a contract actually has.
                if a["period_start"] <= b["period_end"] and b["period_start"] <= a["period_end"]:
                    clashes.append({
                        "customer": a["customer"],
                        "item":     a["item_name"],
                        "a":        a["period_start"] + " to " + a["period_end"],
                        "b":        b["period_start"] + " to " + b["period_end"],
                    })
    return clashes
