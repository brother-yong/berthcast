"""berthcast — Three-Agent System.

This package replaces the old single-file agents.py. The public surface is
unchanged: callers still do `from agents import run_normalization_agent, ...`.
Internal helpers/constants are re-exported too for backward-compatible access.
"""

from .normalization import run_normalization_agent
from .inventory import run_inventory_agent
from .recommendation import run_recommendation_agent

# Re-export internals so existing references like `agents._extract_json_array`
# keep working (and so tests can reach them through the package root).
from .shared import (
    client,
    _call_claude,
    _emit,
    _extract_json_array,
    _format_context,
    _infer_supplier_type,
    _resolve_item_suppliers,
    LEAD_TIME_DAYS,
    SPOILAGE_THRESHOLD_DAYS,
    CATEGORY_SUPPLIER_TYPE,
    LEAD_TIME_BY_TYPE,
)

__all__ = [
    "run_normalization_agent",
    "run_inventory_agent",
    "run_recommendation_agent",
]
