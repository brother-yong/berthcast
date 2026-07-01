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
    ("claude-haiku-4-5-20251001", "Haiku — fast, lower cost (testing)"),
    ("claude-sonnet-5",           "Sonnet — balanced (recommended)"),
    ("claude-opus-4-8",           "Opus — most thorough (production reports)"),
]
