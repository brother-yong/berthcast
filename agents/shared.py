"""Shared building blocks for the agents: the Claude client, lead-time/spoilage
constants, supplier resolution, and the small JSON/parsing helpers every agent uses.

Moved verbatim from the old single-file agents.py — no logic changes.
"""

import json
import os
import re
from database import (
    query,
    get_supplier_profile,
)

import anthropic

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

LEAD_TIME_DAYS = {
    "import": 16 * 7,
    "local":   3 * 7,
    # "other" deliberately omitted — no reliable default, treat as unknown
}

SPOILAGE_THRESHOLD_DAYS = {
    "chill":   14,
    "frozen":  60,
    "dry":     180,
}

# Category-based supplier type inference — shared between inventory and rec agents.
# Used as a last-resort fallback when the supplier listing and PO table don't
# have a match. Food-distribution defaults; non-food clients should populate
# their own supplier profiles via the settings page.
CATEGORY_SUPPLIER_TYPE = {
    "bread": "local", "bun": "local", "hotdog": "local", "prata": "local",
    "eggs": "local", "egg": "local", "water": "local",
    "coca-cola": "local", "fanta": "local", "sprite": "local", "pepsi": "local",
    "7up": "local", "carbonated": "local",
    "spring roll skin": "local", "spring roll": "local",
    "milk": "import", "cheese": "import", "butter": "import",
    "cream": "import", "yoghurt": "import", "yogurt": "import",
    "ice cream": "import", "muesli": "import", "cereal": "import",
    "pasta": "import", "noodle": "import", "flour": "import",
    "biscuit": "import", "cracker": "import", "cookie": "import",
    "juice": "import", "coffee": "import", "sauce": "import",
    "ketchup": "import", "canned": "import", "tortilla": "import",
    "pizza": "import", "pastry": "import", "puff": "import",
    "mozzarella": "import", "parmesan": "import", "edam": "import",
    "cheddar": "import", "feta": "import", "gouda": "import",
    "cottage cheese": "import", "emmenthal": "import",
}

LEAD_TIME_BY_TYPE = {"import": 112, "local": 21, "other": 56}


def _infer_supplier_type(item_name: str) -> str:
    """Guess import vs local from product keywords. Last-resort fallback."""
    name_lower = item_name.lower()
    for keyword, stype in CATEGORY_SUPPLIER_TYPE.items():
        if keyword in name_lower:
            return stype
    return "other"


# Words that disqualify a column from being CURRENT stock on hand: they
# describe movement (sold/sale), rates (avg), money (value/amount/price), or
# reservations (allocated) — not what's sitting in the warehouse right now.
_STOCK_EXCLUDE = ("sold", "sale", "avg", "allocated", "value", "amount", "price")


def _pick_stock_column(cols):
    """Pick the inventory column that holds CURRENT stock on hand.

    Three passes, strongest signal first:
      1. exact well-known headers;
      2. stock-meaning words (balance / on hand / stock) minus disqualifiers —
         catches hand-made headers like "Current System balance";
      3. generic qty/quantity minus disqualifiers.

    The disqualifier list is the point: a single first-match-wins fuzzy pass
    once read stock from a "Qty Sold" column (it appeared earlier in the sheet
    than the real balance column), which made months-of-supply identical for
    every item and the whole catalogue look HEALTHY.
    """
    STOCK_EXACT = ("qty_on_hand", "qty", "quantity", "stock_on_hand", "on_hand",
                   "stock_qty", "balance", "stock_balance", "closing_stock",
                   "current_system_balance", "system_balance", "current_balance",
                   "current_stock")
    col = next((k for k in cols if k in STOCK_EXACT), None)
    if col:
        return col

    def _clean(k):
        return not any(x in k for x in _STOCK_EXCLUDE)

    col = next((k for k in cols
                if ("balance" in k or "on_hand" in k or "stock" in k) and _clean(k)), None)
    if col:
        return col
    return next((k for k in cols
                 if ("qty" in k or "quantity" in k) and _clean(k)), None)


_DESC_EXACT = ("description", "item_description", "inventory_desc", "product_description",
               "item_name", "product_name", "stock_description", "item_desc")
_CAT_EXACT  = ("category", "cat", "class", "item_category", "product_category", "storage_type")
_UOM_EXACT  = ("uom", "unit_of_measure", "unit", "uom_code", "uom_description",
               "base_uom", "purchase_uom", "sales_uom", "stock_uom")


def detect_inventory_columns(cols) -> dict:
    """Keyword best-guess for the inventory table's key columns.

    Returns {"description","stock","category","uom"} (any value may be None).
    This is the offline fallback used when there's no confirmed/LLM mapping.
    """
    desc = next((k for k in cols if k in _DESC_EXACT), None) or \
           next((k for k in cols if ("desc" in k or "item_name" in k or "product_name" in k)
                 and "supplier" not in k), None)
    cat  = next((k for k in cols if k in _CAT_EXACT), None) or \
           next((k for k in cols if "cat" in k or "class" in k or "storage" in k), None)
    uom  = next((k for k in cols if k.lower() in _UOM_EXACT), None) or \
           next((k for k in cols if "uom" in k.lower() or "unit_of" in k.lower()), None)
    return {"description": desc, "stock": _pick_stock_column(cols),
            "category": cat, "uom": uom}


# Currency markers seen in SEA spreadsheets. Only stripped from the FRONT of a
# cell, so a currency word inside an item name can never be touched.
_CURRENCY_RE = re.compile(
    r"^\s*(?:US\$|S\$|HK\$|NT\$|SGD|USD|MYR|RM|IDR|PHP|THB|VND|\$|€|£|¥)\s*",
    re.IGNORECASE,
)
# Exponent allowed: xlsx stores large numbers as "1.234567E6" in the raw XML.
_NUM_AT_START = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _to_num(val, default=0.0):
    """Parse a spreadsheet cell as a number, tolerating what ERP exports and
    hand-made sheets actually contain: thousands separators ("1,200"), currency
    prefixes ("S$1,200"), accounting negatives ("(50)"), and a trailing unit
    word ("120 KG").

    The number must sit at the START of the cell and nothing after it may
    contain digits — "ABC123" and "12-34" return `default` rather than a
    digit-fished guess. Fabricating a number from junk is the same
    silent-wrong-answer class the column-mapping work exists to kill.
    """
    s = str(val if val is not None else "").strip()
    if not s:
        return default
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1].strip()
    s = _CURRENCY_RE.sub("", s).replace(",", "").strip()
    m = _NUM_AT_START.match(s)
    if not m:
        return default
    rest = s[m.end():]
    if any(ch.isdigit() for ch in rest):
        return default
    try:
        n = float(m.group(0))
    except ValueError:
        return default
    return -n if neg else n


# ── Sales-date month counting ────────────────────────────────────────────────
# months-of-data drives ALL velocity math (avg monthly sales = total / months),
# so misreading the date format silently shifts every health label. SQLite's
# strftime only understands ISO dates; real exports also arrive as 15/06/2026
# (Singapore's own standard), 15-Jun-26, or Excel's internal serial numbers.

_MONTH_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
                "nov": 11, "dec": 12}
_ISO_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})(?:-\d{1,2})?")
_NUM_DATE_RE = re.compile(r"^\s*(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
_TXT_DATE_RE = re.compile(r"^\s*(\d{1,2})[\s\-/]+([A-Za-z]{3,9})[\s\-/,]+(\d{2,4})\b")


def _norm_year(y: int) -> int:
    return 2000 + y if y < 100 else y


def count_sales_months(raw_values):
    """Count distinct (year, month) pairs in a sales-date column.

    Handles ISO dates, numeric D/M/Y or M/D/Y (day-vs-month decided ONCE for
    the whole column: any first token > 12 means day-first, any second token
    > 12 means month-first, otherwise day-first — the SEA convention), textual
    15-Jun-26 styles, and Excel serial numbers (xlsx stores dates as days
    since 1899-12-30; our parser keeps the raw number).

    Returns (months, format_label) — or None when fewer than half the
    non-empty values parse, because counting months from junk would fabricate
    the velocity denominator (same rule as _to_num: never guess).
    """
    import datetime as _dt

    vals = [str(v).strip() for v in (raw_values or []) if str(v or "").strip()]
    if not vals:
        return None

    # Column-level day/month decision for ambiguous numeric dates.
    day_first = True
    saw_month_first = False
    for s in vals:
        m = _NUM_DATE_RE.match(s)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a >= 1000:          # year-first like 2026/06/15 — no evidence either way
            continue
        if a > 12:
            day_first, saw_month_first = True, False
            break
        if b > 12:
            saw_month_first = True
    if saw_month_first:
        day_first = False

    months = set()
    parsed = 0
    fmt_counts = {}

    def _hit(y, mo, label):
        nonlocal parsed
        if 1 <= mo <= 12 and 1900 <= y <= 2200:
            months.add((y, mo))
            parsed += 1
            fmt_counts[label] = fmt_counts.get(label, 0) + 1
            return True
        return False

    for s in vals:
        m = _ISO_DATE_RE.match(s)
        if m and _hit(int(m.group(1)), int(m.group(2)), "ISO (YYYY-MM-DD)"):
            continue
        m = _NUM_DATE_RE.match(s)
        if m:
            a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if a >= 1000:
                if _hit(a, b, "YYYY/MM/DD"):
                    continue
            elif day_first:
                if _hit(_norm_year(c), b, "DD/MM/YYYY"):
                    continue
            else:
                if _hit(_norm_year(c), a, "MM/DD/YYYY"):
                    continue
        m = _TXT_DATE_RE.match(s)
        if m:
            mo = _MONTH_NAMES.get(m.group(2)[:3].lower())
            if mo and _hit(_norm_year(int(m.group(3))), mo, "DD-Mon-YYYY"):
                continue
        # Excel serial: days since 1899-12-30. 20000–80000 spans 1954–2119.
        try:
            f = float(s)
        except ValueError:
            continue
        if 20000 <= f <= 80000:
            d = _dt.date(1899, 12, 30) + _dt.timedelta(days=int(f))
            _hit(d.year, d.month, "Excel serial dates")

    if parsed < max(1, len(vals) / 2):
        return None
    label = max(fmt_counts, key=fmt_counts.get)
    if len(fmt_counts) > 1:
        label += " + mixed"
    return max(1, len(months)), label


_MATCH_KEY_RE = re.compile(r"[^a-z0-9]+")


def normalise_match_key(name) -> str:
    """Key for matching item names across files: casefold and keep only
    letters/digits, so spacing, punctuation and case can never break a match."""
    return _MATCH_KEY_RE.sub("", str(name).casefold())


class SalesNameIndex:
    """Resolves inventory item names to aggregated sales rows, tolerating the
    ways the two files disagree about a name.

    12 June 2026: Cool Link's staff type annotations ("<- out of stock") into
    the sales sheet's item-name column — on exactly the items that most need a
    reorder. Exact-name matching therefore lost the sales history for those
    items, they were classified DEAD (stock 0, "sold" 0), and DEAD items never
    reach the recommendation agent: a zero-stock item with real sales produced
    no recommendation at all.

    Resolution per sales row, against `claimed_keys` (the normalised names of
    every inventory item):
      1. exact normalised match -> that item;
      2. the sales name EXTENDS an inventory name (an annotation was appended)
         -> the longest matching inventory item;
      3. an inventory name extends the sales name (sales sheet truncated it)
         -> only when exactly one inventory item matches (never double-count);
      4. otherwise the row stays under its own key (an item we don't stock, or
         a junk row such as a report total).
    Prefix rules need >= MIN_PREFIX_CHARS normalised characters so a generic
    short name can never swallow another item's sales. Numeric fields of rows
    resolving to the same item are summed.
    """

    MIN_PREFIX_CHARS = 10

    def __init__(self, sales_by_raw_name: dict, alias_map: dict = None, claimed_keys=None):
        claimed = {k for k in (claimed_keys or ()) if k}
        long_claimed = sorted((k for k in claimed if len(k) >= self.MIN_PREFIX_CHARS),
                              key=len, reverse=True)
        self._by_key = {}
        for raw, info in (sales_by_raw_name or {}).items():
            raw_s = str(raw).strip()
            if not raw_s:
                continue
            aliased = (alias_map or {}).get(raw_s.lower(), raw_s)
            key = normalise_match_key(aliased)
            if not key:
                continue
            if key not in claimed:
                # 2. annotation appended in the sales sheet — longest wins
                ext = next((c for c in long_claimed if key.startswith(c)), None)
                if ext is not None:
                    key = ext
                elif len(key) >= self.MIN_PREFIX_CHARS:
                    # 3. truncated sales name — only an unambiguous match counts
                    trunc = [c for c in claimed if c.startswith(key)]
                    if len(trunc) == 1:
                        key = trunc[0]
                    elif trunc:
                        continue  # ambiguous: crediting any one item would lie
            self._fold(key, info)

    def _fold(self, key, info):
        ex = self._by_key.get(key)
        if ex is None:
            self._by_key[key] = dict(info)
            return
        for f, v in info.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                prev = ex.get(f)
                ex[f] = (prev if isinstance(prev, (int, float)) else 0) + v
            elif f not in ex:
                ex[f] = v

    def __contains__(self, norm_key) -> bool:
        return norm_key in self._by_key

    def get(self, name):
        """Aggregated sales entry for an (inventory) item name, or None when
        the sales data genuinely does not cover the item."""
        return self._by_key.get(normalise_match_key(name))


def _looks_numeric(values) -> bool:
    """True if most non-empty values parse as numbers (via _to_num, so
    currency-prefixed and unit-suffixed cells count as numeric too)."""
    seen = numeric = 0
    for v in values:
        s = str(v).strip() if v is not None else ""
        if not s:
            continue
        seen += 1
        if _to_num(s, default=None) is not None:
            numeric += 1
    return seen > 0 and numeric >= max(1, seen // 2)


def propose_inventory_columns(headers, sample_rows, model) -> dict:
    """LLM-assisted column mapping for the inventory table, validated in Python.

    Starts from the keyword guess, then lets Claude override each field when its
    pick is valid: the column must exist, and a 'stock' pick must look numeric
    and not be a movement/money word. The result is a PROPOSAL shown to the user
    for confirmation on the context page — it is never trusted blindly. Falls
    back to the keyword guess on any error, so callers always get a usable map.
    """
    result = detect_inventory_columns(headers)
    if not headers:
        return result

    sample_lines = []
    for r in (sample_rows or [])[:8]:
        sample_lines.append(" | ".join(f'{h}={str(r.get(h, "")).strip()[:30]}' for h in headers))

    system = (
        "You map spreadsheet columns to inventory fields for an inventory analysis "
        "tool used by product distributors. Given column headers and a few sample "
        "rows, identify which column holds each field:\n"
        "- description: the item / product name\n"
        "- stock: the CURRENT quantity on hand in the warehouse right now (a balance). "
        "NOT quantity sold, NOT an average, NOT a money value.\n"
        "- category: the product category / group\n"
        "- uom: the unit of measure (e.g. KG, CTN, PCS)\n\n"
        "Return ONLY a JSON object with keys description, stock, category, uom. Each "
        "value must be EXACTLY one of the given headers, or null if no column fits. "
        "No text outside the JSON."
    )
    user = "Headers:\n" + ", ".join(headers) + "\n\nSample rows:\n" + "\n".join(sample_lines)

    try:
        raw = _call_claude(model, system, user, max_tokens=400)
    except Exception:
        return result

    llm = {}
    try:
        s = raw.strip()
        if s.startswith("```"):
            nl = s.find("\n")
            if nl != -1:
                s = s[nl + 1:]
            if s.endswith("```"):
                s = s[:-3]
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b != -1:
            llm = json.loads(s[a:b + 1])
    except Exception:
        llm = {}

    for field in ("description", "stock", "category", "uom"):
        val = llm.get(field) if isinstance(llm, dict) else None
        if not isinstance(val, str) or val not in headers:
            continue
        if field == "stock":
            if any(x in val for x in _STOCK_EXCLUDE):
                continue
            if not _looks_numeric([r.get(val) for r in (sample_rows or [])]):
                continue
        result[field] = val
    return result


def _resolve_item_suppliers(session_id: int, org_name: str, config: dict,
                            alias_map: dict = None, progress_emit=None):
    """Build per-item supplier context: supplier name, type, lead time, risk.

    Returns two dicts:
      item_supplier_map:  {item_name: supplier_name}
      item_lead_time_map: {item_name: {"supplier": str, "type": str,
                                        "lead_time_days": int|None,
                                        "delay_prob": float, "high_risk": bool}}
    """
    alias_map = alias_map or {}
    sup_table = f"suppliers_{session_id}"
    po_table  = f"purchase_orders_{session_id}"

    # 1. Build supplier_type_map from the Supplier Listing upload
    supplier_type_map = {}
    try:
        sup_rows = query(f"SELECT * FROM {sup_table} LIMIT 1000")
        for row in sup_rows:
            name_col = next((k for k in row if "name" in k or "supplier" in k), None)
            type_col = next((k for k in row if "type" in k or "category" in k or "class" in k), None)
            if name_col and type_col:
                sname = str(row[name_col] or "").strip()
                stype = str(row[type_col] or "").strip().lower()
                if "import" in stype:
                    supplier_type_map[sname] = "import"
                elif "local" in stype:
                    supplier_type_map[sname] = "local"
                else:
                    supplier_type_map[sname] = "other"
    except Exception:
        pass

    # 2. Build item→supplier from Purchase Orders (most recent PO per item)
    item_supplier_map = {}
    try:
        sample = query(f"SELECT * FROM {po_table} LIMIT 1")
        if sample:
            cols = list(sample[0].keys())
            desc_col = next((c for c in cols if c in (
                "inventory_desc", "item_description", "description", "product_name")), None)
            sup_col = next((c for c in cols if "supplier" in c and "name" in c), None) or \
                      next((c for c in cols if "supplier" in c), None)
            if desc_col and sup_col:
                po_rows = query(
                    f'SELECT "{desc_col}" as item_name, "{sup_col}" as sup_name '
                    f'FROM {po_table} WHERE "{desc_col}" IS NOT NULL '
                    f'ORDER BY rowid DESC LIMIT 3000'
                )
                for row in po_rows:
                    item = row.get("item_name", "")
                    sup  = row.get("sup_name", "")
                    if item and item not in item_supplier_map:
                        item_supplier_map[item] = sup
                    # Also try canonical name so inventory names match
                    if item and alias_map:
                        canonical = alias_map.get(str(item).strip().lower())
                        if canonical and canonical not in item_supplier_map:
                            item_supplier_map[canonical] = sup
    except Exception:
        pass

    _emit(progress_emit,
          f"Mapped {len(supplier_type_map)} suppliers, {len(item_supplier_map)} item→supplier links")

    # 3. For each known item, resolve lead time from profile → type default → config default
    item_lead_time_map = {}
    for item_name, supplier in item_supplier_map.items():
        stype = supplier_type_map.get(supplier, "other")
        if stype == "other" and (not supplier or supplier == "Unknown"):
            stype = _infer_supplier_type(item_name)

        sup_profile = get_supplier_profile(org_name, supplier)
        if not supplier or supplier == "Unknown":
            lt_days = None
        else:
            lt_days = (sup_profile.get("avg_lead_time_days")
                       or LEAD_TIME_BY_TYPE.get(stype)
                       or config.get("default_lead_time_days")
                       or None)
        delay_prob = sup_profile.get("delay_probability", 0.2)
        quality    = sup_profile.get("data_quality_score", 0.3)
        high_risk  = delay_prob > 0.30 or quality < 0.50

        item_lead_time_map[item_name] = {
            "supplier":       supplier,
            "type":           stype,
            "lead_time_days": lt_days,
            "delay_prob":     delay_prob,
            "high_risk":      high_risk,
        }

    return item_supplier_map, item_lead_time_map, supplier_type_map


# ---------------------------------------------------------------------------
# Consequence engine — pure Python, no LLM involvement
# ---------------------------------------------------------------------------

# Opus 4.7+ and Fable removed the temperature parameter — sending it is a hard
# 400 error on those models. Older models keep temperature=0 so the same file
# produces the same report run after run.
_NO_TEMPERATURE_PREFIXES = ("claude-opus-4-7", "claude-opus-4-8", "claude-fable")


def sampling_kwargs(model: str) -> dict:
    if model.startswith(_NO_TEMPERATURE_PREFIXES):
        return {}
    return {"temperature": 0}


def _call_claude(model: str, system: str, user: str, max_tokens: int = 4096) -> str:
    # Use streaming internally — Anthropic requires it for large max_tokens values.
    # Callers receive the complete text string exactly as before.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        **sampling_kwargs(model),
    ) as stream:
        return stream.get_final_text()


def _num_sql(col: str) -> str:
    """SQL expression that reads a TEXT column as a number, tolerating thousands
    separators.

    Uploaded values are all stored as TEXT, and SQLite's CAST stops at the first
    non-digit character — so CAST("1,200" AS REAL) wrongly yields 1.0, silently
    corrupting every sales/velocity/revenue figure downstream. Stripping commas,
    currency prefixes (US$/S$/$) and spaces first makes "S$1,200" -> 1200.0.
    US$ and S$ must be stripped before the bare $, or "S$1200" degrades to
    "S1200" which CASTs to 0.
    """
    expr = f'"{col}"'
    for token in (",", "US$", "S$", "$", " "):
        expr = f"REPLACE({expr}, '{token}', '')"
    return f"CAST({expr} AS REAL)"


def _emit(progress_emit, msg: str) -> None:
    """Safely call optional progress callback. Never raise into agent flow."""
    if progress_emit is None:
        return
    try:
        progress_emit(msg)
    except Exception:
        pass


def _extract_json_array(raw: str):
    if not raw:
        return None, False
    s = raw.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3].rstrip()
    start = s.find("[")
    if start == -1:
        return None, False
    end = s.rfind("]")
    if end > start:
        try:
            return json.loads(s[start:end + 1]), False
        except json.JSONDecodeError:
            pass
    body = s[start + 1:]
    for needle in ("},", "}"):
        idx = body.rfind(needle)
        if idx == -1:
            continue
        repaired = "[" + body[:idx + 1].rstrip().rstrip(",") + "]"
        try:
            return json.loads(repaired), True
        except json.JSONDecodeError:
            continue
    return None, False


def _format_context(context: dict) -> str:
    if not context:
        return "No additional context provided."
    lines = []
    if context.get("delayed_suppliers"):
        lines.append(f"Delayed/uncontactable suppliers: {context['delayed_suppliers']}")
    if context.get("large_orders"):
        lines.append(f"Large upcoming orders: {context['large_orders']}")
    if context.get("discontinue"):
        lines.append(f"Items to be discontinued: {context['discontinue']}")
    if context.get("other"):
        lines.append(f"Other notes: {context['other']}")
    return "\n".join(lines) if lines else "No additional context provided."
