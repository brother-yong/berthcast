"""Configuration constants for berthcast. Extracted from app.py — values unchanged."""
import os


UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"xlsx", "csv"}

# Valid upload slots. This was a dict mapping each slot to a table name, but
# every name mapped to itself — so the mapping carried no information. It's just
# the set of slot names, used directly as the per-session table prefix.
FILE_SLOTS = ("inventory", "purchase_orders", "sales", "suppliers", "customers")

AVAILABLE_MODELS = [
    # 8 Oct 2026: Sonnet stays the default. On a 200-item real-data sample Haiku 5.5
    # (thinking off) matched every quantity but missed cover-vs-lead-time calls;
    # with its thinking on it matched Sonnet on every stock status at ~1/12 the cost.
    # Before any client account runs on Haiku: the 150-item recommendation batch can
    # overflow max_tokens 64000 with thinking on (76 items used ~37K).
    ("claude-sonnet-5-5", "Sonnet: most accurate (recommended)"),
    ("claude-haiku-5-5",  "Haiku: testing only, not for client accounts yet"),
]
