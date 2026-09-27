"""A sales link joins one sheet line to its stock items.

Links are saved because repeated AI judgments can differ. The company switch
allows them to be disabled while retaining corrections and undo history.
Claude judges, Python does the arithmetic.
"""

import json

from database import query, table_exists
from .shared import detect_inventory_columns, normalise_match_key, sg_today


MAX_LINES = 1000
MAX_MEMBERS = 20
MAX_LINE_CHARS = 120
MAX_CODE_CHARS = 40
MAX_NAME_CHARS = 80
MAX_KEY_CHARS = 120
MAX_NEW_LINES_PER_RUN = 300
BATCH_LINES = 20
LINK_MAX_TOKENS = 8000
LINK_TIMEOUT_S = 180
LINK_BUDGET_S = 600
LINK_MODEL = "claude-sonnet-5"  # The model the prompt was tested on.
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
        key = normalise_match_key(key if isinstance(key, str) else name)[:MAX_KEY_CHARS]
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
