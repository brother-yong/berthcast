"""Proof for the .xlsx decompression-bomb guard (audit finding #8).

An .xlsx is a ZIP. The 100 MB upload cap bounds the *compressed* size, but a few-KB
file can decompress to gigabytes and OOM the single 512 MB worker. The worst sink
was xl/sharedStrings.xml, which is read into a Python list in memory.

The fix counts the bytes the decompressor ACTUALLY produces (database._LimitedReader)
and aborts past a cap — it never trusts the size the file claims, which a malicious
file can forge.

This test proves:
  1. The reader stops exactly at its byte cap (the core mechanism).
  2. A real expansion bomb (~50 MB of shared strings, set against a 2 MB cap) is
     rejected AND memory stays bounded — measured with tracemalloc, so it's the
     actual allocation, not a guess. Without the guard this peak would exceed the
     50 MB the file expands to.
  3. A normal small .xlsx still parses correctly under the same caps (no false
     positive).
  4. A file with too many rows is rejected (disk/time bound).

Dependency-free:  python tests/test_upload_limits.py
"""
import os
import sys
import io
import zipfile
import tempfile
import tracemalloc

# Throwaway DB + not-on-Render BEFORE importing database (it reads DB_PATH and runs
# its storage guard at import time).
_TMPDIR = tempfile.mkdtemp(prefix="berth_uplimit_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _make_xlsx(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in members.items():
            z.writestr(name, content)


# ---------------------------------------------------------------------------
# 1. The core mechanism: _LimitedReader stops at its byte cap.
# ---------------------------------------------------------------------------
r = db._LimitedReader(io.BytesIO(b"x" * 1000), 100)
got = b""
raised = False
try:
    got += r.read(50)   # 50  total  -> ok
    got += r.read(50)   # 100 total  -> ok (boundary is inclusive)
    got += r.read(50)   # 150 total  -> must raise
except db._DecompressionLimitExceeded:
    raised = True
_check("LimitedReader raises once the byte cap is exceeded", raised)
_check("LimitedReader hands back no more than the cap before raising", len(got) <= 100)

# ---------------------------------------------------------------------------
# 2. A real expansion bomb is rejected with bounded memory.
# ---------------------------------------------------------------------------
_block = "<si><t>" + ("A" * 990) + "</t></si>"          # ~1 KB per shared string
_bomb_xml = f'<sst xmlns="{_NS}">' + (_block * 50_000) + "</sst>"   # ~50 MB uncompressed
_bomb_path = os.path.join(_TMPDIR, "bomb.xlsx")
_make_xlsx(_bomb_path, {
    "xl/sharedStrings.xml": _bomb_xml,
    "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_NS}"><sheetData></sheetData></worksheet>',
})
del _bomb_xml   # free the builder's copy so it can't muddy the measurement

db.MAX_XLSX_SHARED_STRINGS_BYTES = 2 * 1024 * 1024      # 2 MB cap for the test
tracemalloc.start()
res = db.excel_to_sqlite(_bomb_path, "sales", 70001)
_cur, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

_check("expansion bomb is rejected", res.get("ok") is False, detail=str(res))
_check("rejection message explains the file is oversized",
       "expands" in (res.get("error") or "").lower(), detail=str(res.get("error")))
_check("memory stayed bounded (< 20 MB) even though the file expands to ~50 MB",
       peak < 20 * 1024 * 1024, detail=f"peak={peak // 1024 // 1024}MB")

# ---------------------------------------------------------------------------
# 3. No false positive: a normal small .xlsx still parses correctly.
# ---------------------------------------------------------------------------
_shared_ok = (
    f'<sst xmlns="{_NS}">'
    '<si><t>Description</t></si>'
    '<si><t>Qty</t></si>'
    '<si><t>Supplier</t></si>'
    '<si><t>White Bread</t></si>'
    '<si><t>Local SG</t></si>'
    '</sst>'
)
_sheet_ok = (
    f'<worksheet xmlns="{_NS}"><sheetData>'
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
    '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2"><v>10</v></c><c r="C2" t="s"><v>4</v></c></row>'
    '</sheetData></worksheet>'
)
_ok_path = os.path.join(_TMPDIR, "ok.xlsx")
_make_xlsx(_ok_path, {"xl/sharedStrings.xml": _shared_ok, "xl/worksheets/sheet1.xml": _sheet_ok})

res_ok = db.excel_to_sqlite(_ok_path, "sales", 70002)   # 2 MB cap still in effect
_check("a normal small file parses successfully under the same caps", res_ok.get("ok") is True, detail=str(res_ok))
_check("it stored the one data row", res_ok.get("rows") == 1, detail=str(res_ok))
_row = db.query('SELECT description, qty, supplier FROM "sales_70002"')
_check("the data reads back intact",
       bool(_row) and _row[0]["description"] == "White Bread"
       and _row[0]["qty"] == "10" and _row[0]["supplier"] == "Local SG",
       detail=str(_row))

# ---------------------------------------------------------------------------
# 4. Row cap: a file with more rows than allowed is rejected (disk/time bound).
# ---------------------------------------------------------------------------
db.MAX_XLSX_ROWS = 5
_rows = '<row r="1"><c r="A1" t="inlineStr"><is><t>c1</t></is></c>' \
        '<c r="B1" t="inlineStr"><is><t>c2</t></is></c>' \
        '<c r="C1" t="inlineStr"><is><t>c3</t></is></c></row>'
for i in range(2, 12):   # 10 data rows
    _rows += f'<row r="{i}"><c r="A{i}"><v>{i}</v></c><c r="B{i}"><v>{i}</v></c><c r="C{i}"><v>{i}</v></c></row>'
_rowbomb_path = os.path.join(_TMPDIR, "rows.xlsx")
_make_xlsx(_rowbomb_path, {"xl/worksheets/sheet1.xml": f'<worksheet xmlns="{_NS}"><sheetData>{_rows}</sheetData></worksheet>'})

res_rows = db.excel_to_sqlite(_rowbomb_path, "sales", 70003)
_check("a file over the row cap is rejected", res_rows.get("ok") is False, detail=str(res_rows))
_check("rejection message mentions rows", "rows" in (res_rows.get("error") or "").lower(), detail=str(res_rows.get("error")))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll upload-limit tests passed.")
