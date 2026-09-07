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
import difflib
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

# Longest item name we will accept off a tender sheet. A CPU guard first: the
# ingest layer allows a 100,000-character cell, propose_matches does work that
# grows with the token count of the name it is matching, and the confirm screen
# runs it for every row on the page. One pasted blob in an item column would
# hold the single production worker for minutes and take every other tenant
# down with it. Real item names on these sheets are short, so 200 is already
# generous, and a longer value means the wrong column was mapped.
# Refused with a reason rather than truncated, the same choice the customer
# name makes at MAX_TENDER_CUSTOMER_CHARS: a silently shortened item name is a
# mis-match waiting to happen.
MAX_TENDER_ITEM_CHARS = 200

# Candidates offered per tender row on the confirm screen.
MATCH_TOP_N = 3

# Below this, offer nothing rather than noise. Measured on real sheets, the
# correct answers score 0.300, 0.314 and 0.329 while garbage scores 0.28, 0.253
# and 0.242: the two bands OVERLAP, so no threshold separates them and lowering
# this globally would inject confident nonsense into the top three. It is not a
# tuning knob. The one caller that needs a lower floor passes it per call, at
# render time only, and never on a path that writes.
MATCH_MIN_SCORE = 0.30

# difflib runs on at most this many candidates per tender row. Without the
# prescreen, 200 rows against 5,000 names is a million SequenceMatcher calls on
# one 512 MB worker.
_MATCH_PRESCREEN = 25

# Contracts named on the expanded card before it says "and N more".
MAX_ADDON_SOURCES = 3


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
        # Sliced before normalising so an oversized cell cannot make even this
        # regex expensive. On an accepted row the slice is a no-op, because the
        # length check below has already passed.
        key    = normalise_match_key(item[:MAX_TENDER_ITEM_CHARS + 1])
        if not item:
            reason = "no item name"
        elif len(item) > MAX_TENDER_ITEM_CHARS:
            reason = (f"item name is too long (over {MAX_TENDER_ITEM_CHARS} "
                      "characters), check the column is the right one")
        elif not key:
            # A name with no letters or digits ("***", "-----") normalises to
            # an empty match key. Stored, that row can never be matched to one
            # of the client's items and can never even be decided: the confirm
            # screen skips empty keys, while the unmatched count still counts
            # the row. The banner would then sit at "1 not matched" forever
            # with nothing the client could do about it, and a warning that
            # cannot reach zero stops being read at all. Refuse it here, the
            # same way a blank name is refused.
            reason = "item name has no letters or numbers"
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
            "match_key":    key,
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


# ── Matching a tender line to one of the client's own items ──────────────────
# The two files carry the same products in a different word order:
# "SAUCE OYSTER_500GRM/BTL." against "OYSTER SAUCE 500ML". normalise_match_key
# preserves order, so on a real sheet it matches nothing at all. Token-set
# overlap is what fixes that. stdlib difflib only, no new dependency.
#
# Nothing here decides anything. It produces a shortlist a human confirms:
# measured against real sheets, most top-1 guesses are right, a handful are
# ambiguous on pack size (400g against 425g) and a couple are confidently WRONG
# (one mapped a 400g bottle to a 20kg bag). That last group is why nothing is
# auto-applied and nothing is pre-selected.

def _match_tokens(name):
    """Lowercase alphanumeric runs of two characters or more.

    Digits are KEPT on purpose: "500" against "425" is exactly the
    discriminator a pack-size mismatch needs.
    """
    return [t for t in re.split(r"[^a-z0-9]+", str(name).casefold()) if len(t) >= 2]


def build_match_index(inventory_names):
    """One reusable index of the org's item names, built ONCE per page.

    Rebuilding it per tender row would re-tokenise the whole item list 25 times
    over for a single screen.
    """
    names, tokens, by_token = [], [], {}
    for name in inventory_names:
        toks = set(_match_tokens(name))
        for t in toks:
            by_token.setdefault(t, []).append(len(names))
        names.append(name)
        tokens.append(toks)
    return {"names": names, "tokens": tokens, "by_token": by_token}


def propose_matches(tender_item, index, top_n=MATCH_TOP_N, min_score=MATCH_MIN_SCORE):
    """Best guesses at which item a tender line refers to, best first.

    Returns [{"name": str, "score": float}, ...], or [] for empty input, an
    empty index or a name with no usable tokens. Never raises: this runs while
    rendering a page and a stray cell in the client's sheet must not 500 it.

    min_score is a parameter for exactly ONE caller, the confirm screen's
    zero-candidate fallback, which lowers it to show the nearest names to a
    human who is looking at the row. Nothing that stores a decision may pass it.
    """
    if not isinstance(index, dict) or not index.get("names"):
        return []
    wanted = set(_match_tokens(tender_item))
    if not wanted:
        return []

    # Candidate set = rows sharing at least one token, with the overlap count
    # already counted on the way in.
    overlap = {}
    for t in wanted:
        for idx in index["by_token"].get(t, ()):
            overlap[idx] = overlap.get(idx, 0) + 1
    if not overlap:
        return []

    def _jaccard(idx):
        union = len(wanted | index["tokens"][idx])
        return (overlap[idx] / union) if union else 0.0

    # Prescreen on the cheap measures, then run difflib on the survivors only.
    ranked = sorted(overlap,
                    key=lambda i: (-overlap[i], -_jaccard(i), index["names"][i]))
    # Sorting the tokens before the ratio is what makes word order stop
    # mattering: two sheets naming the same product in a different order
    # produce the same string here. Built ONCE: it does not vary with the
    # candidate, and rebuilding it inside the loop sorted the same token set
    # 25 times per row for nothing.
    wanted_str = " ".join(sorted(wanted))
    scored = []
    for idx in ranked[:_MATCH_PRESCREEN]:
        ratio = difflib.SequenceMatcher(
            None, wanted_str,
            " ".join(sorted(index["tokens"][idx]))).ratio()
        score = round(0.65 * _jaccard(idx) + 0.35 * ratio, 3)
        if score >= min_score:
            scored.append({"name": index["names"][idx], "score": score})
    scored.sort(key=lambda c: (-c["score"], c["name"]))
    return scored[:top_n]


def tender_addons(matched_rows, today_iso):
    """Confirmed monthly tender volume per item: {inventory_key: {...}}.

    matched_rows is get_matched_tender_commitments output, so every row here
    already carries a human decision. Order of operations matters and it is
    filter, then de-duplicate, then sum.

    Each value is {"qty": float, "sources": [...], "count": int}, where sources
    is capped at MAX_ADDON_SOURCES and count is the number of contracts behind
    the figure, so the card can say "and N more" truthfully.
    """
    out, seen = {}, set()
    for row in matched_rows:
        start, end = row["period_start"], row["period_end"]
        # 1. Live only. Ended and not-yet-started contribute zero.
        if not (start <= today_iso <= end):
            continue

        # 2. De-duplicate, first row wins. save_tender_rows is a plain INSERT
        # and tender_commitments has no UNIQUE constraint, so uploading the
        # same sheet twice appends a second full set of rows. Both sets join to
        # the ONE mapping row, and a 100/month contract would render as
        # "200 + 200" on the results page, the printed PO and the CSV.
        # Keyed on match_key, not inventory_key: a re-upload carries identical
        # tender text so it still de-dupes, while two genuinely different
        # tender lines a human mapped to the same item still sum correctly.
        # customer is deliberately OUT of the key, or a re-upload typed
        # "NORDVIK" once and "NORDVIK PTE LTD" the next time would evade the
        # dedupe entirely.
        # The trade: a genuine SECOND customer with an identical item, period,
        # quantity and basis is under-counted. That is the right way to be
        # wrong here. Under-counting fails visible (a stockout she can see and
        # react to); over-counting fails expensive (a doubled PO she has
        # already paid for).
        try:
            qty_part = round(float(row["quantity"]), 4)
        except (TypeError, ValueError):
            qty_part = row["quantity"]
        fingerprint = (row["match_key"], start, end, qty_part, row.get("qty_basis"))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        # 3. Sum. monthly_rate is the ONLY source of the per-month figure, and
        # it returns None for an unstated basis or an unreadable date. None
        # contributes ZERO and names no contract: a row we cannot rate is not
        # evidence of a quantity, and guessing one is the whole failure this
        # feature exists to avoid.
        rate = monthly_rate(row["quantity"], row.get("qty_basis"), start, end)
        if rate is None:
            continue

        # 4. count and sources come from the SURVIVING rows only, so "and N
        # more" reports contracts rather than duplicate uploads.
        entry = out.setdefault(row["inventory_key"],
                               {"qty": 0.0, "sources": [], "count": 0})
        entry["qty"] += rate
        entry["count"] += 1
        if len(entry["sources"]) < MAX_ADDON_SOURCES:
            # "item" is the name as the ORG knows it, not the text on the
            # customer's sheet: the only reader of it is the "contracted but
            # not recommended" list, and a human looks that up in their own
            # system.
            entry["sources"].append({"customer":   row.get("customer") or "",
                                     "item":       row.get("inventory_item") or "",
                                     "period_end": end})
    return out
