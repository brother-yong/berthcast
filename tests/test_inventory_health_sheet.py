"""Inventory health sorting, print settings and spreadsheet parity.

Run with: python tests/test_inventory_health_sheet.py
"""
import csv
import html as html_lib
import io
import json
import os
import random
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_inventory_sheet_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

# The client is constructed during import; these routes never call Claude.
if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:
        def __init__(self, *args, **kwargs):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

from rec_logic import (  # noqa: E402
    inventory_number_display,
    inventory_view_params,
    sort_inventory_items,
)
import database as db  # noqa: E402
import app as appmod  # noqa: E402
from chat_logic import PRODUCT_GUIDE  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

appmod.app.config["TESTING"] = True
appmod.app.config["WTF_CSRF_ENABLED"] = False
_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name
          + (f" [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


ROWS = {
    "A": {"item": "BROOKVALE PRAWN MEAT 400G", "category": "FROZEN",
          "stock": "0 CTN", "status": "CRITICAL", "spoilage_risk": "HIGH",
          "days_of_supply": 0, "observation": "Check stock now."},
    "B": {"item": "NORDVIK COD FILLET 1KG", "category": "FROZEN",
          "stock": 400, "status": "LOW", "spoilage_risk": "MEDIUM",
          "days_of_supply": 20, "observation": "Review the next order."},
    "C": {"item": "PADIMAS JASMINE RICE 5KG", "category": "DRY",
          "stock": "300", "status": "HEALTHY", "spoilage_risk": "NONE",
          "days_of_supply": 180, "observation": "Stock covers demand."},
    "D": {"item": "KESSINGTON OAT DRINK 1L", "category": "CHILL",
          "stock": "unreadable", "status": "HEALTHY", "spoilage_risk": "LOW",
          "days_of_supply": None, "observation": "Confirm stock units."},
    "E": {"item": "brookvale butter 250g", "category": "chill",
          "stock": 5.0, "status": "CRITICAL", "spoilage_risk": "HIGH",
          "days_of_supply": 12, "observation": "Check shelf life."},
    "H": {"item": "PADIMAS HONEY 500G", "category": "",
          "stock": 30, "status": "LOW", "spoilage_risk": "",
          "days_of_supply": 45, "observation": "Confirm storage category."},
    "F": {"item": "KESSINGTON GHEE 800G", "category": "DRY",
          "stock": 0.0, "status": "REVIEW", "spoilage_risk": "NONE",
          "days_of_supply": None, "observation": "Match the sales name."},
    "G": {"item": "NORDVIK SARDINES 155G", "category": "DRY",
          "stock": 40, "status": "DEAD", "spoilage_risk": "NONE",
          "days_of_supply": None, "observation": "Check whether it still sells."},
}
PIPELINE_ROWS = [ROWS[code] for code in "HCDBEAFG"]
SHOWN_ROWS = [ROWS[code] for code in "HCDBEA"]
ITEM_CODES = {row["item"]: code for code, row in ROWS.items()}

# Literal orders from the agreed fixture, independent of the sorting helper.
EXPECTED = {
    ("spoilage", "desc"): "AEBDCH",
    ("spoilage", "asc"): "CDBAEH",
    ("status", "desc"): "AEBHCD",
    ("status", "asc"): "CDBHAE",
    ("days", "desc"): "CHBEAD",
    ("days", "asc"): "AEBHCD",
    ("stock", "desc"): "BCHEAD",
    ("stock", "asc"): "AEHCBD",
    ("item", "desc"): "CHBDAE",
    ("item", "asc"): "EADBHC",
    ("category", "desc"): "ABCEDH",
    ("category", "asc"): "EDCABH",
}
CSV_HEADER = ["#", "Item", "Category", "Stock at analysis", "Status",
              "Spoilage risk", "Days of supply", "Note"]


def _codes(rows):
    return "".join(ITEM_CODES[row["item"]] for row in rows)


def _html_order(document):
    positions = [(document.find(row["item"]), code) for code, row in ROWS.items()]
    return "".join(code for pos, code in sorted(positions) if pos >= 0)


def _csv_order(rows):
    return "".join(ITEM_CODES.get(row[1], "?") for row in rows[1:] if len(row) > 1)


def _item_row(document, code):
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", document, flags=re.DOTALL):
        if ROWS[code]["item"] in row:
            return row
    return ""


print("-- inventory helpers --")
for (sort_key, direction), expected in EXPECTED.items():
    actual = _codes(sort_inventory_items(SHOWN_ROWS, sort_key, direction))
    _check(f"helper {sort_key} {direction}: {expected}", actual == expected, actual)
    rng = random.Random(16)
    shuffled_orders = []
    for _ in range(20):
        shuffled = list(SHOWN_ROWS)
        rng.shuffle(shuffled)
        shuffled_orders.append(_codes(sort_inventory_items(shuffled, sort_key, direction)))
    _check(f"helper {sort_key} {direction} retains tie order through 20 shuffles",
           all(order == expected for order in shuffled_orders), repr(shuffled_orders))
    _check(f"valid {sort_key} {direction} settings pass through",
           inventory_view_params(sort_key, direction, ["LOW", "CRITICAL"])
           == (sort_key, direction, ("CRITICAL", "LOW")))

_check("missing settings use spoilage descending and the three default statuses",
       inventory_view_params(None, None, [])
       == ("spoilage", "desc", ("CRITICAL", "LOW", "HEALTHY")))
_check("show settings are deduplicated into canonical order",
       inventory_view_params("item", "asc", ["DEAD", "CRITICAL", "DEAD"])
       == ("item", "asc", ("CRITICAL", "DEAD")))
for value, expected in ((0, "0"), (0.0, "0"), (12.0, "12"),
                        (12.5, "12.5"), ("45", "45"), (None, None)):
    _check(f"number display {value!r} gives {expected!r}",
           inventory_number_display(value) == expected)

db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role) "
    "VALUES (?,?,?,?,?,?,?)",
    ("inventory-sheet@example.com", generate_password_hash("x"), "Inventory Test Org",
     "claude-sonnet-4-6", "enterprise", 1, "admin"),
)
uid = db.query("SELECT id FROM users WHERE email=?", ("inventory-sheet@example.com",))[0]["id"]
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "Inventory Test Org", "complete"),
)
db.execute(
    "INSERT INTO analysis_results "
    "(session_id, inventory_report, recommendations_json, created_at) VALUES (?,?,?,?)",
    (sid, json.dumps(PIPELINE_ROWS), "[]", "2026-09-22 17:30:00"),
)

client = appmod.app.test_client()
with client.session_transaction() as session:
    session["user_id"] = uid
    session["email"] = "inventory-sheet@example.com"
    session["org_name"] = "Inventory Test Org"
    session["model"] = "claude-sonnet-4-6"
    session["is_admin"] = False
    session["tier"] = "enterprise"
    session["role"] = "admin"
    session["sv"] = 0

PRINT_PATH = f"/results/{sid}/inventory/print"
CSV_PATH = f"/results/{sid}/inventory.csv"


def _print_page(query=()):
    response = client.get(PRINT_PATH, query_string=query)
    return response, response.get_data(as_text=True)


def _spreadsheet(query=()):
    response = client.get(CSV_PATH, query_string=query)
    return response, list(csv.reader(io.StringIO(response.get_data(as_text=True))))


print("-- default print sheet --")
response, document = _print_page()
_check("default print page returns 200", response.status_code == 200, str(response.status_code))
for phrase in (
    "Analysis run: 23/09/2026",
    "6 item(s)",
    "Sorted by: Spoilage risk, high to low",
    "Showing: Critical, Running low, Well stocked",
    "Stock is a snapshot from this analysis. Check current ERP stock before ordering.",
):
    _check(f"print header contains {phrase!r}", phrase in document)
_check("default print order excludes Review and Dead", _html_order(document) == "AEBDCH")

print("-- print and spreadsheet parity for all sort settings --")
for (sort_key, direction), expected in EXPECTED.items():
    query = [("sort", sort_key), ("dir", direction)]
    print_response, print_html = _print_page(query)
    csv_response, csv_rows = _spreadsheet(query)
    label = f"{sort_key} {direction}"
    _check(f"print {label} returns 200", print_response.status_code == 200)
    _check(f"CSV {label} returns 200", csv_response.status_code == 200)
    _check(f"print {label} order is {expected}", _html_order(print_html) == expected,
           _html_order(print_html))
    _check(f"CSV {label} order is {expected}", _csv_order(csv_rows) == expected,
           _csv_order(csv_rows))
    _check(f"CSV {label} has the eight agreed columns", bool(csv_rows) and csv_rows[0] == CSV_HEADER)

query = [("sort", "days"), ("dir", "asc"), ("show", "CRITICAL"), ("show", "LOW")]
response, linked_html = _print_page(query)
match = re.search(r'href="([^"]*inventory\.csv[^"]*)"', linked_html)
_check("custom sheet has a spreadsheet download link", match is not None)
if match:
    download = client.get(html_lib.unescape(match.group(1)))
    download_rows = list(csv.reader(io.StringIO(download.get_data(as_text=True))))
    _check("download link keeps both status filters and ascending days order",
           download.status_code == 200
           and _html_order(linked_html) == "AEBH"
           and _csv_order(download_rows) == "AEBH")

print("-- chosen statuses --")
for statuses, expected in (
    (("REVIEW", "DEAD"), "FG"),
    (("CRITICAL",), "AE"),
    (("CRITICAL", "LOW", "HEALTHY", "REVIEW", "DEAD"), "AEBDFCGH"),
):
    query = [("show", status) for status in statuses]
    print_response, print_html = _print_page(query)
    csv_response, csv_rows = _spreadsheet(query)
    label = ", ".join(statuses)
    _check(f"print Show {label} contains exactly {expected}",
           print_response.status_code == 200 and _html_order(print_html) == expected)
    _check(f"CSV Show {label} contains exactly {expected}",
           csv_response.status_code == 200 and _csv_order(csv_rows) == expected)

response, csv_rows = _spreadsheet()
csv_by_item = {row[1]: row for row in csv_rows[1:] if len(row) == 8}
_check("print preserves real zero days", "<td>0</td>" in _item_row(document, "A"))
_check("print distinguishes unknown days with a placeholder",
       "\u2014" in _item_row(document, "D"))
_check("CSV preserves real zero days", csv_by_item.get(ROWS["A"]["item"], [""] * 8)[6] == "0")
_check("CSV leaves unknown days blank",
       ROWS["D"]["item"] in csv_by_item and csv_by_item[ROWS["D"]["item"]][6] == "")

print("-- on-screen inventory tab --")
response = client.get(f"/results/{sid}")
results_html = response.get_data(as_text=True)
start = results_html.find('id="tab-inv"')
end = results_html.find('id="tab-review"')
_check("results page returns 200", response.status_code == 200, str(response.status_code))
_check("inventory and review tab boundaries are present", start > 0 and end > start)
inventory_html = results_html[start:end] if start > 0 and end > start else ""
_check("on-screen inventory order matches default print and excludes Review and Dead",
       _html_order(inventory_html) == "AEBDCH", _html_order(inventory_html))
_check("on-screen inventory preserves zero days", "<td>0</td>" in _item_row(inventory_html, "A"))
_check("on-screen inventory marks missing days", "\u2014" in _item_row(inventory_html, "D"))
_check("inventory tab links to its print sheet", PRINT_PATH in inventory_html)
_check("inventory tab links to its spreadsheet", CSV_PATH in inventory_html)
_check("results header uses the Singapore run date", "Generated \u00b7 23/09/2026" in results_html)
_check("results header does not expose the prior UTC date", "2026-09-22" not in results_html)

print("-- product guide --")
guide_start = PRODUCT_GUIDE.find("Inventory health tab:")
guide_end = PRODUCT_GUIDE.find("Getting the order sheet out")
_check("guide explains the inventory spreadsheet download", "Download spreadsheet" in PRODUCT_GUIDE)
_check("inventory guidance is before order-sheet guidance", 0 <= guide_start < guide_end)
guide_paragraph = PRODUCT_GUIDE[guide_start:guide_end] if 0 <= guide_start < guide_end else ""
_check("inventory guidance keeps CSV wording reserved for the order sheet",
       bool(guide_paragraph) and "CSV" not in guide_paragraph)

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll inventory health sheet feature checks passed.")
