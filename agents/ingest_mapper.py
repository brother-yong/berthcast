"""The one AI call of smart sales ingestion: propose a layout recipe from a
small sample. Layout ONLY — the model never sees the full file and never
outputs quantities. Output is schema-validated by ingest_recipe.validate_recipe;
anything unparseable or invalid becomes a refusal upstream."""
import json
import os
import re

from agents.shared import _call_claude, wrap_untrusted, UNTRUSTED_GUARD

# Infrastructure model, not the org-facing chat model: fixed and cheap.
MAPPER_MODEL = os.environ.get("INGEST_MAPPER_MODEL", "claude-haiku-4-5-20251001")
MAPPER_MAX_TOKENS = 600

_SYSTEM = (
    "You analyse the LAYOUT of a spreadsheet sample from a sales report "
    "where months are spread across columns.\n"
    + UNTRUSTED_GUARD + "\n\n"
    "Reply with ONLY a JSON object (no prose):\n"
    "{\n"
    '  "layout": "wide_matrix",     // or "unknown" if this is not a months-as-columns grid\n'
    '  "header_row": <1-based row number of the row naming the months>,\n'
    '  "item_col": <1-based column number of item/product names>,\n'
    '  "month_cols": {"<1-based column number>": <month 1-12>, ...},\n'
    '  "supplier_col": <1-based column number or null>,\n'
    '  "leadtime_col": <1-based column number or null>\n'
    "}\n"
    "Rules: columns and rows are 1-based. Only include month columns you are "
    "sure about. If the layout is unclear, return {\"layout\": \"unknown\"}."
)


def build_mapper_prompts(sample_text: str):
    """(system, user) prompt pair. The sample is untrusted file content and
    is fenced; the fence rule lives in the system prompt."""
    user = (
        "Here is the sample (one line per row, cells separated by ' | ', "
        "row numbers prefixed):\n"
        + wrap_untrusted(sample_text)
        + "\nReturn the JSON object now."
    )
    return _SYSTEM, user


def parse_recipe_response(text):
    """Extract the first JSON object from the model reply. None if absent —
    the caller treats None as a refusal."""
    m = re.search(r"\{.*\}", str(text or ""), re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group())
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def propose_recipe(sample_text: str):
    """Call the model. Returns a raw dict (unvalidated) or None."""
    system, user = build_mapper_prompts(sample_text)
    try:
        reply = _call_claude(MAPPER_MODEL, system, user, max_tokens=MAPPER_MAX_TOKENS)
    except Exception:
        return None
    return parse_recipe_response(reply)
