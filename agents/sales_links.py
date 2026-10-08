"""A sales link joins one sheet line to its stock items.

Links are saved because repeated AI judgments can differ. The company switch
allows them to be disabled while retaining corrections and undo history.
Claude judges, Python does the arithmetic.
"""

import json

from database import get_sales_links, query, table_exists
from .shared import detect_inventory_columns, normalise_match_key, sg_today


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


def pick_code_column(cols, rows):
    if not rows:
        return None
    # A warehouse export may repeat one location_code on every stock row.
    # Only exact item-code headers with mostly distinct values qualify.
    for header in CODE_HEADERS:
        if header not in cols or "location" in header:
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


def stock_codes(session_id):
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
        result = {}
        for row in rows:
            raw_code, raw_name = row.get(code_col), row.get(description)
            code = str(raw_code).strip() if raw_code is not None else ""
            name = str(raw_name).strip() if raw_name is not None else ""
            if code and len(code) <= MAX_CODE_CHARS and name:
                result.setdefault(code, name)
        return result, code_col
    except Exception:
        return {}, None


def make_entry(line, members, conf, by, model=None, why="") -> dict:
    if not isinstance(line, str):
        raise ValueError("A sales line is required")
    line = line.strip()
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
        name = member.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A stock item name is required")
        name = name.strip()
        key = member.get("key")
        key = normalise_match_key(key if isinstance(key, str) else name)
        # A cut key is a different item's identity, so a long one is not kept.
        if len(key) > MAX_KEY_CHARS:
            key = ""
        cleaned.append({"code": code.strip(), "key": key, "name": name[:MAX_NAME_CHARS]})
    conf = conf.strip().lower() if isinstance(conf, str) else "low"
    if conf not in {"high", "medium", "low"}:
        conf = "low"
    by = by.strip().lower() if isinstance(by, str) else "ai"
    if by not in {"ai", "admin"}:
        by = "ai"
    return {"line": line, "members": cleaned, "conf": conf, "by": by,
            "at": sg_today().isoformat(),
            "model": model.strip()[:120] if by == "ai" and isinstance(model, str) else None,
            "why": why.strip()[:120] if isinstance(why, str) else ""}


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
    raw_by_key = {}
    for raw in sales_names or []:
        key = normalise_match_key(str(raw).strip())
        if key:
            raw_by_key.setdefault(key, []).append(raw)

    applied, missing, exact_drops, fell_back = [], [], [], []
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
            found = None
            if isinstance(code, str) and code.strip():
                code_tries += 1
                found = by_code.get(code.strip())
                code_hits += bool(found)
            # A saved key at the old 120-character cut may be the prefix of a
            # longer name, which is another item, so only shorter keys count.
            if (not found and isinstance(member_key, str)
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
            if normalise_match_key(variant) in claimed or normalise_match_key(canonical) in claimed:
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
