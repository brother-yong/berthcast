"""A sales link joins one sheet line to its stock items.

Links are saved because repeated AI judgments can differ. The company switch
allows them to be disabled while retaining corrections and undo history.
Claude judges, Python does the arithmetic.
"""

import json
import math
import re
import time

from database import get_sales_links, query, table_exists, update_sales_links
from .shared import (
    UNTRUSTED_GUARD,
    SalesNameIndex,
    _call_claude,
    _emit,
    _extract_json_array,
    _to_num,
    detect_inventory_columns,
    normalise_match_key,
    sg_today,
    wrap_untrusted,
)


MAX_LINES = 1000
MAX_MEMBERS = 20
MAX_LINE_CHARS = 120
MAX_CODE_CHARS = 40
MAX_NAME_CHARS = 80
MAX_SHOWN_NAME_CHARS = 60  # member names on recs and report rows
MAX_KEY_CHARS = 120
MAX_NEW_LINES_PER_RUN = 300
BATCH_LINES = 20
LINK_MAX_TOKENS = 8000
LINK_TIMEOUT_S = 180
LINK_BUDGET_S = 600
LINK_MODEL = "claude-sonnet-5-5"  # Prompt was tuned on claude-sonnet-5; kept on the Sonnet line for accuracy.
CODE_HEADERS = ("inventory_code", "item_code", "stock_code", "stk_code",
                "product_code", "sku", "stk_id", "item_no", "material_code",
                "article_code", "code")

# The prompt tested in plan 017 (names no client, product or brand). One edit:
# the unit sentence judges from the quantity sold, because the monthly average
# is not known at this point of the run; the unit answer is ignored anyway.
LINK_SYSTEM = (
    "You link lines from a food distributor's hand-kept SALES sheet to items in its STOCK list.\n"
    "A sales line may cover several stock items: two brands written together (e.g. 'BRANDA/BRANDB PASTA'), "
    "or a generic name with no brand that covers every brand of that same product and pack size. "
    "Carton multipliers such as 'x12', 'X 24', '24'S' or '(24 cups/tray)' describe the carton, not the item; "
    "ignore them when matching sizes.\n"
    "Rules: link only the SAME product, flavour/variant and pack size. Never link different products "
    "(e.g. different flavours, different sizes, sliced vs whole). 100% juice, nectar and plain juice/drink are "
    "DIFFERENT products: a line saying NECTAR links only nectar items, a line saying 100% links only 100% juice "
    "items. Sizes within 5% (e.g. 500ML vs 510ML) count as the same size. Each sales line shows the supplier "
    "block it sits under; use it to narrow the brand when it clearly points to one. Use only codes that appear "
    "in the stock list. If nothing matches, return an empty list.\n"
    "confidence: high = you would bet on it; medium = likely, a buyer should glance; low = a guess.\n"
    "unit: does the sales quantity count the same unit as the stock item's UOM? Judge from the names, "
    "the pack wording and the quantity sold vs stock on hand: same | different | unsure.\n"
    "Reply with ONLY a JSON array, one object per sales line: "
    '{"row": <row>, "codes": ["<code>", ...], "confidence": "high"|"medium"|"low", '
    '"unit": "same"|"different"|"unsure", "reason": "<under 20 words>"}\n\n' + UNTRUSTED_GUARD
)
# The admin page re-splits a row's saved codes on these, so a code containing
# one could never be saved back as itself.
_CODE_SEPARATORS = re.compile(r"[\s,;]")
_CONF_RANK = {"low": 1, "medium": 2, "high": 3}
_now = time.monotonic  # a module attribute so tests can pin the clock
# Staff-note markers only. "(" and "*" also start product detail here
# ("(DOUBLE)", "(1L)"), so a split there may name a different product.
_STAFF_NOTE_RE = re.compile(r"←|<-|->|//")


def _noted_head_key(text):
    """Key of the line a noted spelling ("X <- out of stock") names, else ""."""
    text = str(text).strip()
    head = _STAFF_NOTE_RE.split(text)[0].strip()
    return normalise_match_key(head) if head and head != text else ""


def pick_code_column(cols, rows):
    if not rows:
        return None
    # A warehouse export may repeat one location_code on every stock row.
    # Only exact item-code headers with mostly distinct values qualify.
    for header in CODE_HEADERS:
        if header not in cols:
            continue
        values = []
        for row in rows:
            value = row.get(header)
            value = str(value).strip() if value is not None else ""
            if value:
                values.append(value)
        if (len(values) * 10 >= len(rows) * 9
                and len(set(values)) * 10 >= len(values) * 9):
            return header
    return None


def inventory_desc_col(session_id, cols):
    try:
        rows = query("SELECT column_map_json FROM upload_sessions WHERE id=?",
                     (session_id,))
        saved = json.loads(rows[0]["column_map_json"]) if rows else None
        description = saved.get("description") if isinstance(saved, dict) else None
        if isinstance(description, str) and description in cols:
            return description
    except (TypeError, ValueError, RecursionError):
        pass
    return detect_inventory_columns(cols)["description"]


def sales_desc_col(cols):
    return next((c for c in cols if c in
                 ("inventory_desc", "item_description", "description", "product_name")), None) or next(
        (c for c in cols if any(k in c.lower() for k in
                               ("desc", "item_name", "product_name", "item"))
         and "supplier" not in c.lower()), None)


def _code_names(rows, code_col, desc_col):
    """{code: first description} for codes a link may save, and the set of
    codes found on two or more different items (a placeholder such as "0")."""
    names, keys = {}, {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw_code, raw_name = row.get(code_col), row.get(desc_col)
        code = str(raw_code).strip() if raw_code is not None else ""
        name = str(raw_name).strip() if raw_name is not None else ""
        if code and len(code) <= MAX_CODE_CHARS and name and not _CODE_SEPARATORS.search(code):
            names.setdefault(code, name)
            keys.setdefault(code, set()).add(normalise_match_key(name))
    return names, {code for code, found in keys.items() if len(found) > 1}


def stock_codes(session_id, shared=None):
    """{code: first description} of a session's stock file, and its code column.

    When `shared` is a set, codes found on two or more different items are
    added to it, so the caller can refuse them: such a code is not an identity.
    """
    try:
        table = f"inventory_{int(session_id)}"
        if not table_exists(table):
            return {}, None
        rows = query(f"SELECT * FROM {table} LIMIT 3000")
        if not rows:
            return {}, None
        cols = list(rows[0])
        description = inventory_desc_col(session_id, cols)
        code_col = pick_code_column(cols, rows)
        if not description or not code_col:
            return {}, None
        result, on_two = _code_names(rows, code_col, description)
        if isinstance(shared, set):
            shared.update(on_two)
        return result, code_col
    except Exception:
        return {}, None


def _clean_text(text):
    # A lone surrogate (model text can carry one) cannot be encoded as UTF-8,
    # so the save would fail and the line be asked again every run.
    return text.encode("utf-8", "replace").decode("utf-8")


def make_entry(line, members, conf, by, model=None, why="") -> dict:
    if not isinstance(line, str):
        raise ValueError("A sales line is required")
    line = _clean_text(line).strip()
    line_key = normalise_match_key(line)
    if (not line_key or len(line) > MAX_LINE_CHARS
            or len(line_key) > MAX_KEY_CHARS):
        raise ValueError("The sales line must have a valid bounded name")
    if not isinstance(members, list) or len(members) > MAX_MEMBERS:
        raise ValueError("Too many or invalid stock items")
    cleaned = []
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("Invalid stock item")
        code = member.get("code")
        if not isinstance(code, str) or not code.strip() or len(code.strip()) > MAX_CODE_CHARS:
            raise ValueError("Invalid stock item code")
        code = _clean_text(code)
        name = member.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A stock item name is required")
        name = _clean_text(name).strip()
        key = member.get("key")
        key = normalise_match_key(_clean_text(key) if isinstance(key, str) else name)
        # A cut key is a different item's identity, so a long one is not kept.
        if len(key) > MAX_KEY_CHARS:
            key = ""
        cleaned.append({"code": code.strip(), "key": key, "name": name[:MAX_NAME_CHARS]})
    conf = _clean_text(conf).strip().lower() if isinstance(conf, str) else "low"
    if conf not in {"high", "medium", "low"}:
        conf = "low"
    by = _clean_text(by).strip().lower() if isinstance(by, str) else "ai"
    if by not in {"ai", "admin"}:
        by = "ai"
    return {"line": line, "members": cleaned, "conf": conf, "by": by,
            "at": sg_today().isoformat(),
            "model": _clean_text(model).strip()[:120] if by == "ai" and isinstance(model, str) else None,
            "why": _clean_text(why).strip()[:120] if isinstance(why, str) else ""}


def note_names(names):
    """Up to five names for an operator note, each short, the rest counted."""
    # Uploaded names land in a plain-text email: whitespace (newlines too) is
    # collapsed, so a name can never start a line of its own there.
    names = list(dict.fromkeys(" ".join(str(n).split()) for n in names))
    shown = ", ".join(n[:MAX_SHOWN_NAME_CHARS] for n in names[:5])
    return shown + (f" and {len(names) - 5} more" if len(names) > 5 else "")


def apply_links(lines, rows, code_col, desc_col, sales_names, alias_map):
    """Saved links as alias entries built from THIS upload's own strings.

    Pure: no database, no model. The combine loop, SalesNameIndex, the scope
    filter and the canonical name all read the alias map, so a linked family
    reaches every one of them as a single item with no change of their own.
    A family is {"line", "sure", "ai", "canonical", "multi", "raw_lines",
    "member_keys"}, keyed by the normalised canonical name.
    """
    notes = []
    out = {"alias_map": dict(alias_map or {}), "families": {}, "claimed_keys": set(),
           "notes": notes, "dropped_groups": 0}
    if not code_col:
        notes.append("Links not applied: no usable item code column in this stock file.")
        return out

    # Name keys stand for items here: the combine loop buckets rows by the same
    # key, so two spellings of one key are one item and can sit on one line only.
    by_code, by_key = {}, {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        desc = str(row.get(desc_col) or "").strip()
        key = normalise_match_key(desc)
        # No length cut: a long spelling of a member left out here would
        # split off as its own row and its stock would leave the family.
        if not key:
            continue
        by_key.setdefault(key, {})[desc] = None
        raw_code = row.get(code_col)
        code = str(raw_code).strip() if raw_code is not None else ""
        if code and len(code) <= MAX_CODE_CHARS:
            by_code.setdefault(code, {})[key] = None
    raw_by_key, rerouted = {}, {}
    saved_keys = lines if isinstance(lines, dict) else {}
    for raw in sales_names or []:
        text = str(raw).strip()
        key = normalise_match_key(text)
        if key:
            raw_by_key.setdefault(key, []).append(raw)
        # Staff type notes ("<- out of stock") onto the items most likely to run
        # out, so a month's sheet may hold only the noted spelling of a saved
        # line. Read it as that line, as SalesNameIndex does when the full name
        # matches nothing; the filter in link_new_lines never sends it to the AI.
        if key not in saved_keys and key not in by_key:
            head = _noted_head_key(text)
            if head and head != key and head in saved_keys:
                raw_by_key.setdefault(head, []).append(raw)
                rerouted[text.lower()] = head

    applied, missing, exact_drops, fell_back, shared_codes = [], [], [], [], []
    code_tries = code_hits = 0
    for line_key, entry in (lines.items() if isinstance(lines, dict) else ()):
        if (not isinstance(entry, dict) or not isinstance(entry.get("line"), str)
                or not isinstance(entry.get("members"), list)):
            continue
        line = entry["line"].strip()
        # A longer line is never linked, and one line has one entry only.
        if (not line_key or len(line) > MAX_LINE_CHARS
                or normalise_match_key(line) != line_key or line_key not in raw_by_key):
            continue
        if not entry["members"]:
            continue  # a saved "no family" decision: plain name matching, no warning
        keys = {}
        for member in entry["members"][:MAX_MEMBERS]:
            member = member if isinstance(member, dict) else {}
            code, member_key = member.get("code"), member.get("key")
            found, shared = None, False
            if isinstance(code, str) and code.strip():
                code_tries += 1
                found = by_code.get(code.strip())
                if found and len(found) > 1:
                    # A code on two or more items (a placeholder such as "0")
                    # is not an identity: only the saved key can say which.
                    shared = True
                    shared_codes.append(code.strip())
                    found = {member_key: None} if isinstance(member_key, str) and member_key in found else None
                code_hits += bool(found)
            # A saved key at the old 120-character cut may be the prefix of a
            # longer name, which is another item, so only shorter keys count.
            if (not found and not shared and isinstance(member_key, str)
                    and 0 < len(member_key) < MAX_KEY_CHARS and member_key in by_key):
                found = {member_key: None}
            if not found:
                name = member.get("name")
                missing.append(name if isinstance(name, str) and name.strip()
                               else str(code or "unreadable item"))
                continue
            keys.update(found)
        # A stock row named exactly like another sales line belongs to that line.
        for key in [k for k in keys if k != line_key and k in raw_by_key]:
            exact_drops.append(next(iter(by_key[key])))
            del keys[key]
        applied.append({"key": line_key, "line": line, "entry": entry, "keys": keys})

    claims = {}
    for item in applied:
        for key in item["keys"]:
            claims[key] = claims.get(key, 0) + 1
    on_two = [key for key, count in claims.items() if count > 1]
    families = out["families"]
    for item in applied:
        keys = item["keys"]
        for key in [k for k in keys if claims[k] > 1]:
            del keys[key]
        # Needs a saved member: an exact-name row alone is plain name matching
        # already, and must not carry an "AI not sure" warning.
        if not keys:
            fell_back.append(item["line"])
            continue
        if item["key"] in by_key:
            keys[item["key"]] = None
        member_keys = list(keys)
        multi = len(member_keys) >= 2
        canonical = item["line"] if multi else next(iter(by_key[member_keys[0]]))
        conf = item["entry"].get("conf")
        conf = conf.strip().lower() if isinstance(conf, str) else ""
        by = item["entry"].get("by")
        families[normalise_match_key(canonical)] = {
            "line": item["line"], "sure": by == "admin" or conf == "high", "ai": by == "ai",
            "canonical": canonical, "multi": multi,
            "raw_lines": list(raw_by_key[item["key"]]), "member_keys": member_keys}

    # Exactly-once tripwire: every linked sales line must land on its family.
    # A family that fails is removed and the map rebuilt without it, so for
    # that line the run is the unlinked one: the staff groups it displaced
    # come back and it claims nothing.
    staff, withheld = out["alias_map"], []
    while True:
        claimed = set()
        for fkey, family in families.items():
            claimed.update([fkey, normalise_match_key(family["line"])] + family["member_keys"])
        kept, dropped = {}, {}
        for variant, canonical in staff.items():
            # A noted spelling routed into a family overrides a staff group on it: count it.
            if (normalise_match_key(variant) in claimed or normalise_match_key(canonical) in claimed
                    or rerouted.get(str(variant).strip().lower()) in claimed):
                dropped[canonical] = None
            else:
                kept[variant] = canonical
        for family in families.values():
            for raw in family["raw_lines"]:
                kept[str(raw).strip().lower()] = family["canonical"]
            if family["multi"]:
                for key in family["member_keys"]:
                    for desc in by_key[key]:
                        kept[desc.lower()] = family["canonical"]
        failed = [fkey for fkey, family in families.items()
                  if any(normalise_match_key(kept.get(str(raw).strip().lower(), str(raw).strip())) != fkey
                         for raw in family["raw_lines"])]
        if not failed:
            break
        for fkey in failed:
            withheld.append(families.pop(fkey)["line"])
    out.update(alias_map=kept, claimed_keys=claimed, dropped_groups=len(dropped))

    if missing:
        notes.append(f"{len(missing)} saved stock item(s) were not in this stock file: "
                     f"{note_names(missing)}.")
    if shared_codes:
        notes.append(f"{len(set(shared_codes))} saved item code(s) are on more than one stock item "
                     f"in this file, so only the saved item name can match them: {note_names(shared_codes)}.")
    if on_two:
        notes.append(f"{len(on_two)} stock item(s) were on more than one linked line, so they "
                     f"were left out of those lines: {note_names(next(iter(by_key[k])) for k in on_two)}.")
    if exact_drops:
        notes.append(f"{len(exact_drops)} stock item(s) have a sales line of their own name, so "
                     f"they stay with that line: {note_names(exact_drops)}.")
    if fell_back:
        notes.append(f"{len(fell_back)} linked line(s) had no stock item left in this file and "
                     f"fell back to name matching: {note_names(fell_back)}.")
    if dropped:
        notes.append(f"{len(dropped)} staff duplicate group(s) touched linked items and were "
                     f"left out: {note_names(dropped)}.")
    if code_tries and code_hits * 10 < code_tries * 9:
        notes.append(f"Only {code_hits * 100 // code_tries}% of saved item codes were found in "
                     "this stock file, so item codes may have changed; the rest were matched "
                     "by item name where possible.")
    if withheld:
        notes.append(f"{len(withheld)} linked line(s) were withheld because a sales line would "
                     f"have fed two stock rows: {note_names(withheld)}.")
    return out


def groups_from_alias_map(alias_map) -> list:
    """Staff-group shape of an alias map; alias_map_from_groups rebuilds it."""
    groups = {}
    for variant, canonical in (alias_map or {}).items():
        groups.setdefault(canonical, []).append(variant)
    return [{"canonical": c, "variants": v} for c, v in groups.items()]


def linked_name_keys(org_name, session_id) -> set:
    """Every name key a switched-on company's saved links claim in one upload.

    Empty when the switch is off, the company is blank, or anything fails:
    callers only hide names with it, so failing open shows today's list.
    """
    try:
        if not isinstance(org_name, str) or not org_name.strip():
            return set()
        state = get_sales_links(org_name)
        if not state.get("enabled"):
            return set()
        sid = int(session_id)
        inv_table, sal_table = f"inventory_{sid}", f"sales_{sid}"
        if not table_exists(inv_table):
            return set()
        rows = query(f"SELECT * FROM {inv_table} LIMIT 3000")
        if not rows:
            return set()
        cols = list(rows[0])
        desc_col = inventory_desc_col(sid, cols)
        code_col = pick_code_column(cols, rows)
        if not desc_col or not code_col:
            return set()
        names = []
        sample = query(f"SELECT * FROM {sal_table} LIMIT 1") if table_exists(sal_table) else []
        sales_col = sales_desc_col(list(sample[0])) if sample else None
        if sales_col:
            names = [r["n"] for r in query(
                f'SELECT DISTINCT "{sales_col}" AS n FROM {sal_table} LIMIT 5000') if r["n"]]
        return apply_links(state["lines"], rows, code_col, desc_col, names, {})["claimed_keys"]
    except Exception:
        return set()


def _one_line(value):
    # Collapsed so an uploaded name can never start a prompt line of its own.
    return " ".join(str(value).split())


def _line_suppliers(session_id):
    """{line key: set of supplier names} from the session's own sales rows."""
    suppliers = {}
    try:
        table = f"sales_{int(session_id)}"
        sample = query(f"SELECT * FROM {table} LIMIT 1") if table_exists(table) else []
        cols = list(sample[0]) if sample else []
        desc_col = sales_desc_col(cols)
        sup_col = next((c for c in cols if "supplier" in c.lower() or "vendor" in c.lower()), None)
        if desc_col and sup_col and sup_col != desc_col:
            for r in query(f'SELECT DISTINCT "{desc_col}" AS d, "{sup_col}" AS s FROM {table} LIMIT 5000'):
                key = normalise_match_key(str(r["d"] or "").strip())
                name = _one_line(r["s"] or "")
                if key and name:
                    suppliers.setdefault(key, set()).add(name)
    except Exception:
        return {}
    return suppliers


def link_new_lines(org_name, session_id, rows, code_col, desc_col, uom_col, cat_col, qty_col,
                   sales_by_item, saved_lines, progress_emit=None):
    """Ask the AI once about each sales line with no saved entry, check every
    answer in Python, and save the results (add-only) for later runs.

    Returns {"entries": {line_key: entry}, "notes": [str], "calls": int}. The
    run applies the entries even when the save is refused.
    """
    notes = []
    out = {"entries": {}, "notes": notes, "calls": 0}
    saved_lines = saved_lines if isinstance(saved_lines, dict) else {}
    sales_by_item = sales_by_item if isinstance(sales_by_item, dict) else {}
    firsts = {}
    for raw in sales_by_item:
        line = str(raw).strip() if raw is not None else ""
        key = normalise_match_key(line)
        if key and len(line) <= MAX_LINE_CHARS and key not in saved_lines:
            firsts.setdefault(key, (line, raw))
    # A line that is another line plus a staff note ("<- out of stock") is
    # matched as that line by SalesNameIndex. Asked about separately, both
    # would claim the same codes and the tie rule would strip them from both.
    known = set(saved_lines) | set(firsts)
    for key, (line, _raw) in list(firsts.items()):
        head = normalise_match_key(SalesNameIndex._ANNOT_SPLIT_RE.split(line)[0])
        if head and head != key and head in known:
            del firsts[key]
    new = sorted(firsts.items())
    limit = min(MAX_NEW_LINES_PER_RUN, max(0, MAX_LINES - len(saved_lines)))
    if len(new) > limit:
        if limit < MAX_NEW_LINES_PER_RUN:
            notes.append(f"{len(new) - limit} new sales line(s) were not linked: this company already "
                         f"has the most saved links allowed ({MAX_LINES}).")
        else:
            notes.append(f"{len(new) - limit} new sales line(s) wait for the next run (at most "
                         f"{MAX_NEW_LINES_PER_RUN} are linked per run).")
        new = new[:limit]
    if not new:
        return out

    # The AI may only pick codes it was shown: codes of THIS upload that are
    # bounded, have no separator and belong to exactly one item.
    names, on_two = _code_names(rows, code_col, desc_col)
    index, stock_lines = {}, {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw_code, raw_desc = row.get(code_col), row.get(desc_col)
        code = str(raw_code).strip() if raw_code is not None else ""
        desc = str(raw_desc).strip() if raw_desc is not None else ""
        if code not in names or code in on_two or not desc or len(desc) > 200:
            continue
        on_hand = _to_num(row.get(qty_col), None) if qty_col else None
        on_hand = f"{on_hand:g}" if on_hand is not None and math.isfinite(on_hand) else "unknown"
        uom = _one_line(row.get(uom_col) or "")[:10] if uom_col else ""
        cat = _one_line(row.get(cat_col) or "")[:20] if cat_col else ""
        stock_lines[f"{code} | {_one_line(desc)[:80]} | {uom} | {cat} | on hand {on_hand}"] = None
        index[code] = names[code]
    if not index:
        notes.append("New sales lines were not linked: no stock item has an item code the AI can use.")
        return out

    suppliers = _line_suppliers(session_id)

    def supplier(key):
        found = suppliers.get(key)
        return next(iter(found))[:60] if found and len(found) == 1 else "none"

    def sold(raw):
        info = sales_by_item.get(raw)
        total = info.get("total_qty") if isinstance(info, dict) else None
        usable = (isinstance(total, (int, float)) and not isinstance(total, bool)
                  and math.isfinite(total))
        return str(round(total)) if usable else "unknown"

    # The whole stock list goes out again with every batch of 20 lines, so it
    # is marked for the prompt cache: later batches read it at a tenth.
    stock_block = {"type": "text", "cache_control": {"type": "ephemeral"},
                   "text": "STOCK LIST (code | description | UOM | category | on hand):\n"
                           + wrap_untrusted("\n".join(stock_lines))}
    total = len(new)
    _emit(progress_emit, f"Linking {total} new sales-sheet lines to stock items "
                         "(first run takes a minute or two)")
    answered, unread = [], []
    start = _now()
    for b in range(0, total, BATCH_LINES):
        if _now() - start > LINK_BUDGET_S:
            notes.append(f"{total - b} new sales line(s) wait for the next run: the AI step reached "
                         f"its time limit ({LINK_BUDGET_S // 60} minutes).")
            break
        batch = new[b:b + BATCH_LINES]
        text = "\n".join(f"row {i}: {_one_line(line)} | supplier block {supplier(key)} | "
                         f"sold in the sales file {sold(raw)}"
                         for i, (key, (line, raw)) in enumerate(batch, 1))
        user = [stock_block, {"type": "text", "text": "SALES LINES to link:\n" + wrap_untrusted(text)}]
        arr = None
        try:
            for _attempt in range(2):  # one retry for an unreadable reply, as tested
                out["calls"] += 1
                reply = _call_claude(LINK_MODEL, LINK_SYSTEM, user, max_tokens=LINK_MAX_TOKENS,
                                     timeout=LINK_TIMEOUT_S)
                arr, _repaired = _extract_json_array(reply)
                if isinstance(arr, list) and arr:
                    break
                arr = None
        except Exception as e:
            notes.append(f"AI linking stopped this run ({type(e).__name__}); {total - b} new sales "
                         "line(s) wait for the next run.")
            break
        if arr is None:
            unread.extend(line for _key, (line, _raw) in batch)
        else:
            answered.append((batch, arr))
        _emit(progress_emit, f"Linked {min(b + BATCH_LINES, total)} of {total} new sales-sheet lines")

    # Python checks (plan 018 section 5.1). Model text is untrusted: a code
    # counts only by exact membership in this upload's index, and nothing the
    # model writes reaches a quantity, a unit or a name shown to staff.
    in_use = set()
    for raw in sales_by_item:
        line = str(raw).strip() if raw is not None else ""
        in_use.update((normalise_match_key(line), _noted_head_key(line)))
    saved_codes = set()
    for key, entry in saved_lines.items():
        # A saved line absent from this upload must not strip a respelled line's codes.
        if key not in in_use:
            continue
        members = entry.get("members") if isinstance(entry, dict) else None
        for member in members if isinstance(members, list) else ():
            code = member.get("code") if isinstance(member, dict) else None
            if isinstance(code, str):
                saved_codes.add(code.strip())
    picks, ignored, not_in_file, on_saved = {}, 0, 0, 0
    for batch, arr in answered:
        seen = set()
        for obj in arr:
            row = obj.get("row") if isinstance(obj, dict) else None
            if type(row) is not int or not 1 <= row <= len(batch) or row in seen:
                ignored += 1
                continue
            seen.add(row)
            codes = obj.get("codes")
            if not isinstance(codes, list):
                ignored += 1
                continue
            valid = {}
            for code in codes:
                code = code.strip() if isinstance(code, str) else ""
                if code in index:
                    valid[code] = None
                else:
                    not_in_file += 1
            conf = obj.get("confidence")
            conf = conf.strip().lower() if isinstance(conf, str) else "low"
            reason = obj.get("reason")
            key, (line, _raw) = batch[row - 1]
            picks[key] = {"line": line, "conf": conf if conf in _CONF_RANK else "low",
                          "why": reason[:120] if isinstance(reason, str) else "",
                          "codes": list(valid), "asked": bool(codes), "over": False}
            if len(valid) > MAX_MEMBERS:
                # Never cut: a partial family would under-count its stock.
                picks[key].update(codes=[], why="over the item cap", over=True)
    unanswered = [line for batch, _arr in answered for key, (line, _raw) in batch if key not in picks]
    for pick in picks.values():
        kept = [code for code in pick["codes"] if code not in saved_codes]
        on_saved += len(pick["codes"]) - len(kept)
        pick["codes"] = kept
    claims = {}
    for key, pick in picks.items():
        for code in pick["codes"]:
            claims.setdefault(code, []).append(key)
    contested = [code for code, keys in claims.items() if len(keys) > 1]
    for code in contested:
        keys = sorted(claims[code], key=lambda k: _CONF_RANK[picks[k]["conf"]], reverse=True)
        top = _CONF_RANK[picks[keys[0]]["conf"]]
        winner = keys[0] if _CONF_RANK[picks[keys[1]]["conf"]] < top else None
        for key in keys:
            if key != winner:
                picks[key]["codes"].remove(code)

    entries, unsure, no_match, removed, over = {}, 0, [], [], []
    for key, pick in picks.items():
        members = [{"code": c, "key": normalise_match_key(index[c]), "name": index[c]}
                   for c in pick["codes"]]
        why = pick["why"]
        if pick["over"]:
            over.append(pick["line"])
        elif not members and pick["asked"]:
            why = "codes removed by checks"
            removed.append(pick["line"])
        elif not members:
            no_match.append(pick["line"])
        try:
            entries[key] = make_entry(pick["line"], members, pick["conf"], "ai",
                                      model=LINK_MODEL, why=why)
        except ValueError:
            continue
        unsure += bool(members) and pick["conf"] != "high"

    if entries:
        def add_new(lines):
            changed = False
            for key, entry in entries.items():
                # Add-only: an admin edit made while this run was linking wins.
                if key not in lines and len(lines) < MAX_LINES:
                    lines[key] = entry
                    changed = True
            return changed

        try:
            saved = update_sales_links(org_name, add_new, f"AI (analysis {int(session_id)})")
        except Exception as e:
            saved = {"ok": False, "error": type(e).__name__}
        if not saved.get("ok"):
            notes.append("Links not saved (limit reached); this run uses them anyway."
                         if saved.get("error") == "too_big" else
                         f"Links not saved ({saved.get('error')}); this run uses them anyway.")
        notes.append(f"Linked {len(entries)} new sales line(s) with AI: {unsure} unsure (medium or low).")
    if no_match:
        notes.append(f"{len(no_match)} new sales line(s) had no match from the AI: {note_names(no_match)}.")
    if removed:
        notes.append(f"{len(removed)} new sales line(s) were saved with no match because the checks "
                     f"removed every code: {note_names(removed)}.")
    if over:
        notes.append(f"{len(over)} new sales line(s) matched more than {MAX_MEMBERS} stock items and "
                     f"were saved with no match: {note_names(over)}.")
    if not_in_file:
        notes.append(f"{not_in_file} AI code(s) were not in this stock file and were dropped.")
    if on_saved:
        notes.append(f"{on_saved} AI code(s) were already on a saved line and were left there.")
    if contested:
        notes.append(f"{len(contested)} AI code(s) were claimed by two new lines: kept by the more "
                     "sure line, or by neither on a tie.")
    if ignored:
        notes.append(f"{ignored} AI answer(s) could not be read and were ignored.")
    if unanswered:
        notes.append(f"{len(unanswered)} new sales line(s) got no AI answer and will be asked again "
                     f"next run: {note_names(unanswered)}.")
    if unread:
        notes.append(f"{len(unread)} new sales line(s) got no usable AI reply and will be tried again "
                     f"next run: {note_names(unread)}.")
    out["entries"] = entries
    return out
