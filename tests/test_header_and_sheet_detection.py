"""Regression tests for title-row and multi-sheet handling (probe fixes 4-5).

The adversarial probe showed two LOUD failures (clear error, no wrong answer,
but staff friction):

  4. A wide company-title line above the real headers ("Cool Link Pte Ltd |
     Inventory Report | June 2026") was taken as the header row, naming the
     table's columns after the title and dropping every data column wider
     than it. Analysis then refused to run.
  5. A workbook whose first sheet is a cover page hid the real data sheet —
     only sheet 1 was ever read.

Fixes under test (database.py):
  - _choose_header_row: the first rows are buffered and each candidate is
    scored by how many cells contain column vocabulary (item/stock/qty/...).
    A later row only displaces an earlier one by winning >= 2, so files with
    no title line keep the old behavior exactly.
  - _xlsx_to_sqlite: sheets tried in numeric order (fixes sheet10-before-
    sheet2 lexicographic ordering too); first sheet with a confident header
    (score >= 2) and data wins; single-sheet files accept any parse as
    before; nothing usable anywhere stays a loud error.

Run: python tests/test_header_and_sheet_detection.py
"""
import os
import sys
import tempfile
import types
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_headersheet.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                                # noqa: E402
from agents.shared import detect_inventory_columns   # noqa: E402

db.init_db()
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _sheet_xml(rows):
    cells_xml = []
    for ri, row in enumerate(rows, 1):
        cs = []
        for ci, val in enumerate(row):
            if val is None or str(val) == "":
                continue
            col = chr(ord("A") + ci)
            cs.append(f'<c r="{col}{ri}" t="inlineStr"><is><t>{val}</t></is></c>')
        cells_xml.append(f'<row r="{ri}">' + "".join(cs) + "</row>")
    return (f'<worksheet xmlns="{NS}"><sheetData>'
            + "".join(cells_xml) + "</sheetData></worksheet>")


def make_xlsx(*sheets):
    """Build a minimal real .xlsx; each arg is one sheet's rows."""
    path = tempfile.mktemp(suffix=".xlsx")
    with zipfile.ZipFile(path, "w") as zf:
        for i, rows in enumerate(sheets, 1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
    return path


def make_csv(text):
    path = tempfile.mktemp(suffix=".csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


def _table_cols(table):
    rows = db.query(f"SELECT * FROM {table} LIMIT 1")
    return [c for c in rows[0].keys() if c != "_session_id"] if rows else []


_HEADERS = ["Item Name", "Category", "Current Stock", "UOM"]
_DATA = [["EDAM", "CHEESE", "120", "KG"], ["GOUDA", "CHEESE", "150", "KG"]]


# ── 1. CSV: wide title row above real headers ────────────────────────────────
p = make_csv("Cool Link Pte Ltd,Inventory Report,June 2026\n"
             "Item Name,Category,Current Stock,UOM\n"
             "EDAM,CHEESE,120,KG\nGOUDA,CHEESE,150,KG\n")
r = db._csv_to_sqlite(p, "inventory", 401)
cols = _table_cols("inventory_401")
_check("csv wide title: parse ok with 2 rows", r.get("ok") and r.get("rows") == 2, detail=str(r))
_check("csv wide title: real headers chosen", cols == ["item_name", "category", "current_stock", "uom"],
       detail=str(cols))
_check("csv wide title: downstream detection now works",
       detect_inventory_columns(cols)["stock"] == "current_stock")
_check("csv wide title: data intact (EDAM stock 120)",
       db.query("SELECT * FROM inventory_401 WHERE item_name='EDAM'")[0]["current_stock"] == "120")

# ── 2. CSV: single-cell title above headers ──────────────────────────────────
p = make_csv("Cool Link Inventory June 2026\n"
             "Item Name,Category,Current Stock,UOM\n"
             "EDAM,CHEESE,120,KG\n")
r = db._csv_to_sqlite(p, "inventory", 402)
cols = _table_cols("inventory_402")
_check("csv 1-cell title: real headers chosen (no 1-column table)",
       cols == ["item_name", "category", "current_stock", "uom"], detail=str(cols))
_check("csv 1-cell title: row count right", r.get("rows") == 1, detail=str(r))

# ── 3. CSV: normal file unchanged (regression) ───────────────────────────────
p = make_csv("Item Name,Category,Current Stock,UOM\nEDAM,CHEESE,120,KG\n")
r = db._csv_to_sqlite(p, "inventory", 403)
_check("csv normal file: unchanged", r.get("ok") and r.get("rows") == 1
       and _table_cols("inventory_403")[0] == "item_name")

# ── 4. CSV: zero-score headers keep first-row-wins (regression) ──────────────
p = make_csv("alpha,beta,gamma\nEDAM,CHEESE,120\nGOUDA,CHEESE,150\n")
r = db._csv_to_sqlite(p, "inventory", 404)
cols = _table_cols("inventory_404")
_check("csv zero-score headers: first row still wins",
       cols == ["alpha", "beta", "gamma"] and r.get("rows") == 2, detail=str(cols))

# ── 5. CSV: sales file with title row uses sales vocabulary ──────────────────
p = make_csv("Cool Link Pte Ltd,Sales Report,June 2026\n"
             "Date,Item Description,Qty,Net Amount\n"
             "2026-06-01,EDAM,5,100\n")
r = db._csv_to_sqlite(p, "sales", 405)
cols = _table_cols("sales_405")
_check("csv sales title: real headers chosen",
       cols == ["date", "item_description", "qty", "net_amount"], detail=str(cols))

# ── 6. XLSX: wide 3-cell title row above headers (was LOUD, now works) ───────
p = make_xlsx([["Cool Link Pte Ltd", "Inventory Report", "June 2026"],
               _HEADERS] + _DATA)
r = db._xlsx_to_sqlite(p, "inventory", 406)
cols = _table_cols("inventory_406")
_check("xlsx wide title: parse ok with 2 rows", r.get("ok") and r.get("rows") == 2, detail=str(r))
_check("xlsx wide title: real headers chosen",
       cols == ["item_name", "category", "current_stock", "uom"], detail=str(cols))
_check("xlsx wide title: no data column dropped (UOM survived)",
       db.query("SELECT * FROM inventory_406 WHERE item_name='EDAM'")[0]["uom"] == "KG")

# ── 7. XLSX: normal single-sheet file unchanged (regression) ─────────────────
p = make_xlsx([_HEADERS] + _DATA)
r = db._xlsx_to_sqlite(p, "inventory", 407)
_check("xlsx normal file: unchanged", r.get("ok") and r.get("rows") == 2
       and _table_cols("inventory_407")[0] == "item_name")

# ── 8. XLSX: short file (fewer rows than lookahead) with title ───────────────
p = make_xlsx([["Stock List"], [], ["Item Name", "Category", "Current Stock"],
               ["EDAM", "CHEESE", "120"]])
r = db._xlsx_to_sqlite(p, "inventory", 408)
cols = _table_cols("inventory_408")
_check("xlsx short file + title: header found at EOF path",
       r.get("ok") and r.get("rows") == 1 and cols == ["item_name", "category", "current_stock"],
       detail=f"{r} {cols}")

# ── 9. XLSX: cover sheet first, data on sheet 2 (was LOUD, now works) ────────
p = make_xlsx([["Cover Page"], ["prepared by ops team"]],
              [_HEADERS] + _DATA)
r = db._xlsx_to_sqlite(p, "inventory", 409)
cols = _table_cols("inventory_409")
_check("xlsx cover sheet: data found on sheet 2", r.get("ok") and r.get("rows") == 2, detail=str(r))
_check("xlsx cover sheet: headers from the data sheet",
       cols == ["item_name", "category", "current_stock", "uom"], detail=str(cols))

# ── 10. XLSX: cover sheet WITH a small junk table, real data on sheet 2 ──────
# The cover's 3-cell row would have been taken as headers before; its zero
# vocabulary score now sends us on to the confident sheet.
p = make_xlsx([["Section", "Page", "Notes"], ["Overview", "1", "n/a"]],
              [_HEADERS] + _DATA)
r = db._xlsx_to_sqlite(p, "inventory", 410)
cols = _table_cols("inventory_410")
_check("xlsx junk-table cover: real data sheet wins",
       r.get("ok") and cols == ["item_name", "category", "current_stock", "uom"],
       detail=f"{r} {cols}")

# ── 11. XLSX: nothing usable anywhere stays a loud error ─────────────────────
p = make_xlsx([["Cover Page"], ["just a note"]])
r = db._xlsx_to_sqlite(p, "inventory", 411)
_check("xlsx all-cover workbook: still errors loudly",
       not r.get("ok") and "header" in r.get("error", "").lower(), detail=str(r))

# ── 12. XLSX: single sheet with zero-score table still accepted (regression) ─
p = make_xlsx([["alpha", "beta", "gamma"], ["EDAM", "CHEESE", "120"]])
r = db._xlsx_to_sqlite(p, "inventory", 412)
_check("xlsx single zero-score sheet: accepted as before",
       r.get("ok") and r.get("rows") == 1 and _table_cols("inventory_412") == ["alpha", "beta", "gamma"],
       detail=str(r))

# ── 13. Two-row title block (company line + address line) ────────────────────
p = make_xlsx([["Cool Link Pte Ltd", "Inventory Report", "June 2026"],
               ["Blk 1 Example Rd", "Singapore", "079903"],
               _HEADERS] + _DATA)
r = db._xlsx_to_sqlite(p, "inventory", 413)
cols = _table_cols("inventory_413")
_check("xlsx two title lines: real headers still found",
       cols == ["item_name", "category", "current_stock", "uom"], detail=str(cols))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll header/sheet detection tests passed.")
