"""Proof for two correctness/security fixes:

  1. Comma bug — uploaded values are stored as TEXT, and SQLite's CAST stops at
     the first non-digit, so CAST("1,200" AS REAL) wrongly returned 1.0. Every
     sales/velocity/revenue figure downstream was silently corrupted. The agents
     now read numbers via agents.shared._num_sql, which strips commas first.

  2. Column-name injection — a crafted spreadsheet header could smuggle a quote
     or semicolon into the CREATE TABLE statement. _sanitize_name now whitelists
     [a-z0-9_] only.

Dependency-free: run with `python tests/test_number_and_sanitize_fixes.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys
import sqlite3
import tempfile

# Throwaway DB + not-on-Render BEFORE importing app modules (database reads
# DB_PATH at import time and runs its storage guard).
_TMPDIR = tempfile.mkdtemp(prefix="berth_numfix_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
from agents.shared import _num_sql


_FAILED = False


def _check(name, cond):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        _FAILED = True


# ---------------------------------------------------------------------------
# 1. The comma bug — run the ACTUAL SQL the agents now build, against text
#    values exactly as they are stored on upload.
# ---------------------------------------------------------------------------
def _test_comma_numbers():
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE TABLE t ("amount" TEXT)')
    # Values as a real ERP export stores them: text, with thousands commas.
    for v in ["1,200", "2,000", "500", "1,000.50"]:
        conn.execute('INSERT INTO t VALUES (?)', (v,))

    # The OLD, broken expression — proves the bug was real.
    old = conn.execute('SELECT SUM(CAST("amount" AS REAL)) FROM t').fetchone()[0]
    _check("old CAST mis-reads commas (proves the bug existed: 1+2+500+1 = 504)",
           old == 504.0)

    # The NEW expression the agents build via _num_sql.
    new = conn.execute(f'SELECT SUM({_num_sql("amount")}) FROM t').fetchone()[0]
    # 1200 + 2000 + 500 + 1000.50
    _check("new _num_sql reads commas correctly (1200+2000+500+1000.5 = 4700.5)",
           new == 4700.5)
    conn.close()


# ---------------------------------------------------------------------------
# 2. The sanitizer — dangerous characters can no longer reach an identifier.
# ---------------------------------------------------------------------------
def _test_sanitize():
    _check("normal header stays readable",
           db._sanitize_name("Qty On Hand") == "qty_on_hand")

    evil = db._sanitize_name('foo" TEXT); DROP TABLE users;--')
    _check("no double-quote survives", '"' not in evil)
    _check("no semicolon survives", ";" not in evil)
    _check("no parenthesis/space survives", ")" not in evil and " " not in evil)
    _check("only [a-z0-9_] remain", all(c.isalnum() or c == "_" for c in evil))

    _check("all-symbol header collapses to empty (caller falls back to col_i)",
           db._sanitize_name(";;;\"'") == "")


# ---------------------------------------------------------------------------
# 3. End-to-end ingest: a CSV with comma-numbers AND a malicious header goes
#    through the real _csv_to_sqlite path without breaking, and reads back right.
# ---------------------------------------------------------------------------
def _test_ingest_end_to_end():
    csv_path = os.path.join(_TMPDIR, "sales.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write('Description,Net Amount,evil" TEXT); DROP TABLE x;--\n')
        f.write('White Bread,1,200,x\n')          # note: comma inside the number
        f.write('Frozen Salmon,2,000,y\n')

    # NOTE: a raw comma inside an unquoted CSV number splits into extra columns —
    # that's a *file formatting* reality, not our bug. Use quoted values to model
    # how spreadsheets actually export thousands separators.
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write('Description,Net Amount,"evil"" TEXT); DROP TABLE x;--"\n')
        f.write('White Bread,"1,200",x\n')
        f.write('Frozen Salmon,"2,000",y\n')

    res = db.excel_to_sqlite(csv_path, "sales", 9991)
    _check("ingest succeeded without SQL error", res.get("ok") is True)
    _check("ingest stored 2 rows", res.get("rows") == 2)

    cols = [r["name"] for r in db.query('PRAGMA table_info("sales_9991")')]
    _check("malicious header neutralised to a safe identifier",
           any(c.startswith("evil") for c in cols)
           and all('"' not in c and ";" not in c for c in cols))

    total = db.query(f'SELECT SUM({_num_sql("net_amount")}) as s FROM sales_9991')[0]["s"]
    _check("comma-formatted revenue reads back correctly (1200+2000 = 3200)",
           total == 3200.0)


def main():
    _test_comma_numbers()
    _test_sanitize()
    _test_ingest_end_to_end()
    if _FAILED:
        print("\nSOME TESTS FAILED")
        sys.exit(1)
    print("\nAll number-parsing and sanitizer tests passed.")


if __name__ == "__main__":
    main()
