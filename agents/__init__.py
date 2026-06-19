"""berthcast — Three-Agent System.

This package replaces the old single-file agents.py. The public surface is
unchanged: callers still do `from agents import run_normalization_agent, ...`.
"""

from .normalization import run_normalization_agent
from .inventory import run_inventory_agent
from .recommendation import run_recommendation_agent
from .orchestrator import run_pipeline

__all__ = [
    "run_normalization_agent",
    "run_inventory_agent",
    "run_recommendation_agent",
    "run_pipeline",
]
