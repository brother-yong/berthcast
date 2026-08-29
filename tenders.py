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
import math
import re

from agents.shared import normalise_match_key

# Header keywords, most specific first within each field. A header satisfies a
# field if it CONTAINS one of these once punctuation and case are stripped.
# One header can only ever satisfy one field.
#
# Only two fields are read off the sheet. The customer and the period are asked
# for on the upload form instead: the client's real tender sheets carry the
# customer in the filename or a chat message, never in a column.
_FIELD_KEYWORDS = {
    "quantity": ("tenderqty", "committedqty", "monthlyconsumption", "consumption",
                 "quantity", "qty", "volume", "carton", "units", "usage",
                 "offtake", "committed"),
    "item":     ("itemdescription", "itemname", "stkname", "stockname",
                 "productname", "materialdescription", "itemcode", "item",
                 "product", "description", "desc", "sku", "material", "article"),
}

# quantity first: a "SIX MONTH VOLUME" or "Est. Monthly Consumption" column must
# be claimed before item's looser nets ever see it.
_FIELD_ORDER = ("quantity", "item")

# A code column must lose to a name column. match_key is how a later version
# joins these rows to inventory, and inventory is keyed on names, so a sheet
# carrying both "Item number" and "Product name" has to store the name.
# Checked against the whole normalised header. Substrings for the long ones;
# EXACT for the short ones, or "no" and "id" would match half the sheet.
_CODEISH_SUBSTR = ("code", "number", "barcode", "itemno", "stkno", "partno", "refno")
_CODEISH_EXACT  = ("no", "id", "sn", "sku", "ref", "itemid", "stkid", "serialno")

# Exact, not substring, and that is the whole point: the real sheets carry
# headers like "<company> UPDATE" and "Business Entity", and a substring net on
# "company" would refuse every genuine file. A miss here is harmless (the form
# value is used, which is what the user asked for); a false hit blocks a real
# upload, so this errs silent.
_CONFLICT_EXACT = {
    "customer": ("customer", "customers", "customername", "customercode",
                 "cust", "custname", "buyer", "buyername", "client",
                 "clientname", "soldto", "shipto", "company", "companyname",
                 "account", "accountname"),
    "start":    ("start", "startdate", "datestart", "from", "fromdate",
                 "datefrom", "periodfrom", "periodstart", "validfrom",
                 "effectivedate", "commencementdate", "contractstart"),
    "end":      ("end", "enddate", "dateend", "to", "todate", "dateto",
                 "periodto", "periodend", "validto", "validtill", "validuntil",
                 "expiry", "expirydate", "expirationdate", "until", "contractend"),
}
_CONFLICT_ORDER = ("customer", "start", "end")

# What the sheet's number means. The file never says -- one live sheet is a
# six-month total, another is a monthly rate -- so the uploader picks it on the
# form. The template hardcodes these two strings as <option value>s.
BASIS_PER_MONTH    = "per_month"
BASIS_PERIOD_TOTAL = "period_total"
QTY_BASES          = (BASIS_PER_MONTH, BASIS_PERIOD_TOTAL)
BASIS_LABELS       = {BASIS_PER_MONTH:    "Per month",
                      BASIS_PERIOD_TOTAL: "Total for the period"}

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

# Bound the overlap scan. Both a memory and a CPU guard: see find_overlaps.
MAX_OVERLAPS_REPORTED = 50


def _norm_header(name) -> str:
    return _NOT_ALNUM.sub("", str(name).casefold())


def _is_codeish(normed_name) -> bool:
    return (normed_name in _CODEISH_EXACT
            or any(s in normed_name for s in _CODEISH_SUBSTR))


def _first_keyword_hit(normed, claimed, keywords):
    """First unclaimed header containing the earliest keyword that hits."""
    for keyword in keywords:
        hit = next((h for h, n in normed
                    if h not in claimed and keyword in n), None)
        if hit is not None:
            return hit
    return None


def detect_columns(headers):
    """Map field -> header for the two columns a tender sheet must supply.

    Returns (mapping, missing, conflicts).
      mapping    {"item": <header>, "quantity": <header>}
      missing    required fields no header matched -- import nothing, name them
      conflicts  [(field, header), ...] in _CONFLICT_ORDER: the sheet carries
                 its own per-row customer or period. Refused, never overwritten:
                 stamping one customer over a sheet naming several would
                 mislabel a contract with no warning.
    """
    normed = [(h, _norm_header(h)) for h in headers]
    mapping, claimed = {}, set()
    for field in _FIELD_ORDER:
        if field == "item":
            # Two passes: name columns first, then everything, so a sheet with
            # only a code column ("SKU", "Qty") still imports. Demoting the
            # code column is a preference, not a ban.
            hit = _first_keyword_hit(
                [p for p in normed if not _is_codeish(p[1])],
                claimed, _FIELD_KEYWORDS[field])
            if hit is None:
                hit = _first_keyword_hit(normed, claimed, _FIELD_KEYWORDS[field])
        else:
            hit = _first_keyword_hit(normed, claimed, _FIELD_KEYWORDS[field])
        if hit is not None:
            mapping[field] = hit
            claimed.add(hit)
    missing = [f for f in _FIELD_ORDER if f not in mapping]

    # Conflicts are read off every header, claimed or not: one offending column
    # is enough to refuse the file, and the user sees the header from their own
    # sheet rather than the normalised form.
    conflicts = []
    for field in _CONFLICT_ORDER:
        hit = next((h for h, n in normed if n in _CONFLICT_EXACT[field]), None)
        if hit is not None:
            conflicts.append((field, hit))
    return mapping, missing, conflicts


def months_in_period(start, end) -> int:
    """Whole calendar months a period covers, floored at 1.

    Rule, stated once so nobody has to reverse it out of the code:
    (year, month) difference, plus one when the end day reaches the start day.
    1 Jul -> 31 Dec is 6. 1 Jul -> 14 Aug is 1, not 2. Floored at 1 so a
    same-day period can never divide by zero or turn a period total into a
    wildly inflated monthly rate.
    """
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day >= start.day:
        months += 1
    return max(1, months)


def monthly_rate(quantity, basis, period_start, period_end):
    """Per-month figure for one row, or None when the basis was never stated.

    Accepts datetime.date or ISO strings for the dates (rows come back from
    SQLite as ISO text). Returns None -- never a guess -- for an unknown or
    NULL basis, an unparseable date, or a missing quantity.
    """
    if basis not in QTY_BASES or quantity is None:
        return None
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        return None
    if basis == BASIS_PER_MONTH:
        return qty
    start = parse_date(period_start)
    end   = parse_date(period_end)
    if start is None or end is None:
        return None
    return qty / months_in_period(start, end)


def format_qty(value) -> str:
    """1800.0 -> '1,800'; 12.5 -> '12.5'; 0.08 -> '0.08'; None -> ''."""
    if value is None:
        return ""
    try:
        text = f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return ""
    # rstrip stops at the '.', so '1,800.00' -> '1,800' and never '1,8'.
    return text.rstrip("0").rstrip(".")


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
    #
    # NaN and Infinity have to be refused explicitly: float() accepts "nan",
    # "inf" and any overflowing literal like "1e400", and neither survives a
    # `<= 0` test -- NaN compares False against everything, Infinity really is
    # greater than zero. Both reach real sheets. A NaN is the worse one: SQLite
    # stores it as NULL, quantity is NOT NULL, so the whole executemany aborts
    # and one junk cell throws away every good row beside it.
    if not math.isfinite(qty) or qty <= 0:
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


def build_rows(records, customer, period_start, period_end, qty_basis, mapping=None):
    """Turn raw {header: value} dicts into validated tender rows.

    customer / period_start / period_end / qty_basis come from the upload form,
    not from the sheet: none of the client's real tender sheets carry them.
    They are applied to every row. period_start and period_end are
    datetime.date and are validated by the caller; qty_basis is one of
    QTY_BASES.

    Returns (rows, rejects, mapping). Each reject carries the source row number
    and a plain-English reason -- the user has to be able to fix the sheet
    without guessing which cell offended. When a required column is absent
    entirely, mapping is {"__missing__": [fields]} and nothing is imported.

    `mapping` may be passed in when the caller has already run detect_columns
    against the headers alone -- that lets it fetch only the two columns it
    needs instead of every column in the sheet.
    """
    if not records:
        return [], [], (mapping or {})

    if mapping is None:
        mapping, missing, _conflicts = detect_columns(list(records[0].keys()))
        if missing:
            return [], [], {"__missing__": missing}

    start_iso, end_iso = period_start.isoformat(), period_end.isoformat()
    rows, rejects = [], []
    for i, rec in enumerate(records, start=1):
        item = str(rec.get(mapping["item"]) or "").strip()
        qty  = parse_quantity(rec.get(mapping["quantity"]))

        # The customer is no longer a per-row value, so it cannot be part of
        # the blank test any more: it is always present, and testing it would
        # turn every trailing spacer row in a long sheet into a reject.
        if not item and qty is None:
            continue          # blank spacer row, not an error worth reporting

        reason = None
        if not item:
            reason = "no item name"
        elif qty is None:
            reason = "quantity is missing, zero or not a number"

        if reason:
            # Every reject is collected, and the caller trims the list before
            # storing it. Capping HERE made len(rejects) stop at 50, so a sheet
            # with 63 bad rows told the user "3 imported, 50 skipped" and ten
            # rows vanished with no record. In a product whose whole job is
            # arithmetic, a count that does not add up is worse than the file.
            rejects.append({"row": i, "reason": reason,
                            "customer": customer[:80], "item": item[:80]})
            continue

        rows.append({
            "customer":     customer,
            "item_name":    item,
            "match_key":    normalise_match_key(item),
            "quantity":     qty,
            "period_start": start_iso,
            "period_end":   end_iso,
            "qty_basis":    qty_basis,
        })
    return rows, rejects, mapping


def find_overlaps(rows, limit=MAX_OVERLAPS_REPORTED):
    """Tender rows for the same customer AND item whose periods overlap.

    Two live commitments on one customer+item double-count: the same stock gets
    reserved twice, so what is left to sell reads lower than it is. Reported,
    never auto-resolved -- a client CAN hold two contracts on one item, and
    only they know which is right.

    Bounded on purpose. Pairing is quadratic, and since the customer and the
    period now come from the upload form, every row in one sheet shares both --
    so a sheet listing one item 200 times produces 19,900 clashes and megabytes
    of HTML, recomputed on every page load, on a 512 MB single-worker box. The
    first `limit` clashes tell the client the same thing the full list would.
    """
    buckets = {}
    for r in rows:
        # The route validates end >= start, but nothing in the schema enforces
        # it, and rows written by the previous version took their period from
        # sheet columns. A reversed row can neither break the scan nor clash,
        # so it would quietly drag its bucket back toward quadratic.
        if r["period_end"] < r["period_start"]:
            continue
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
                # Sorted by period_start, so once one b starts after a ends,
                # every later b does too. Nothing after this point can clash
                # with THIS a.
                if b["period_start"] > a["period_end"]:
                    break
                # Half-open would let a contract ending 30 Jun and one starting
                # 30 Jun both claim that day. Inclusive at both ends is the
                # reading a contract actually has.
                if a["period_start"] <= b["period_end"]:
                    clashes.append({
                        "customer": a["customer"],
                        "item":     a["item_name"],
                        "a":        a["period_start"] + " to " + a["period_end"],
                        "b":        b["period_start"] + " to " + b["period_end"],
                    })
                    if len(clashes) >= limit:
                        return clashes
    return clashes
