"""Configuration constants for berthcast. Extracted from app.py — values unchanged."""
import os


UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"xlsx", "csv"}

FILE_SLOTS = {
    "inventory":       "inventory",
    "purchase_orders": "purchase_orders",
    "sales":           "sales",
    "suppliers":       "suppliers",
    "customers":       "customers",
}

AVAILABLE_MODELS = [
    ("claude-haiku-4-5-20251001", "Haiku — fast, lower cost (testing)"),
    ("claude-sonnet-4-6",         "Sonnet — balanced (recommended)"),
    ("claude-opus-4-8",           "Opus — most thorough (production reports)"),
]
