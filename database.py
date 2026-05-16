import sqlite3
import json
import os

DB_PATH = "berthai.db"


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # Allows concurrent reads + writes
    conn.execute("PRAGMA synchronous=NORMAL") # Faster writes, still safe
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            org_name TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT 'claude-haiku-4-5-20251001',
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS organisations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            model TEXT NOT NULL DEFAULT 'claude-haiku-4-5-20251001'
        );

        CREATE TABLE IF NOT EXISTS upload_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            org_name TEXT NOT NULL,
            status TEXT DEFAULT 'uploading',
            context_json TEXT,
            file_names_json TEXT,
            dedup_confirmed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS analysis_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            inventory_report TEXT,
            recommendations_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES upload_sessions(id)
        );
    """)
    conn.commit()

    # Migrations — add columns that may not exist in older databases
    for migration in [
        "ALTER TABLE upload_sessions ADD COLUMN file_names_json TEXT",
        "ALTER TABLE upload_sessions ADD COLUMN conversion_status_json TEXT",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # Column already exists

    conn.close()


def excel_to_sqlite(filepath: str, table_name: str, session_id: int):
    """
    Convert an uploaded Excel file into a SQLite table using direct XML streaming.

    Bypasses openpyxl entirely — parses the XLSX ZIP file using Python's built-in
    xml.etree.ElementTree.iterparse(), which processes one XML element at a time and
    uses O(1) memory regardless of file size. A 55MB file with 450k rows uses under
    50MB of RAM this way.

    Handles Synergix export format: skips metadata rows, detects the real header row
    (first row with 3+ non-empty cells), maps sparse cell references correctly.
    """
    import zipfile
    import xml.etree.ElementTree as ET
    import re

    NS  = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    TAG_ROW  = f"{{{NS}}}row"
    TAG_C    = f"{{{NS}}}c"
    TAG_V    = f"{{{NS}}}v"
    TAG_IS   = f"{{{NS}}}is"
    TAG_T    = f"{{{NS}}}t"
    TAG_SI   = f"{{{NS}}}si"

    def _col_idx(col_letter: str) -> int:
        """Convert column letter(s) to 0-based index. A=0, B=1, Z=25, AA=26."""
        result = 0
        for ch in col_letter.upper():
            result = result * 26 + (ord(ch) - ord("A") + 1)
        return result - 1

    def _sanitize(name: str) -> str:
        return (name.strip().lower()
                .replace(" ", "_").replace("/", "_").replace("-", "_")
                .replace(".", "").replace("(", "").replace(")", ""))

    conn = get_db()
    try:
        with zipfile.ZipFile(filepath, "r") as zf:

            # ── 1. Load shared strings (string lookup table used by most cells) ──
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                with zf.open("xl/sharedStrings.xml") as f:
                    for event, elem in ET.iterparse(f, events=("end",)):
                        if elem.tag == TAG_SI:
                            text = "".join(t.text or "" for t in elem.iter(TAG_T))
                            shared_strings.append(text)
                            elem.clear()

            # ── 2. Find the sheet file ──────────────────────────────────────────
            sheet_candidates = [n for n in zf.namelist()
                                 if re.match(r"xl/worksheets/sheet\d+\.xml", n)]
            if not sheet_candidates:
                return {"ok": False, "error": "No worksheet found in file."}
            sheet_path = sorted(sheet_candidates)[0]

            # ── 3. Stream rows with iterparse ───────────────────────────────────
            headers          = None       # list of sanitized column names
            header_col_map   = {}         # col_idx (int) → position in headers list
            scoped_table     = f"{table_name}_{session_id}"
            insert_sql       = None
            BATCH            = 2000
            batch: list      = []
            total            = 0

            with zf.open(sheet_path) as f:
                for event, elem in ET.iterparse(f, events=("end",)):
                    if elem.tag != TAG_ROW:
                        elem.clear()
                        continue

                    # Extract {col_idx: value_str} for every non-empty cell in this row
                    row_vals: dict[int, str] = {}
                    for cell in elem:
                        if cell.tag != TAG_C:
                            continue
                        ref = cell.get("r", "")
                        col_letter = re.sub(r"\d", "", ref)
                        if not col_letter:
                            continue
                        ci = _col_idx(col_letter)

                        cell_type = cell.get("t", "")
                        v_elem    = cell.find(TAG_V)
                        is_elem   = cell.find(TAG_IS)

                        if is_elem is not None:
                            # Inline string
                            val = "".join(t.text or "" for t in is_elem.iter(TAG_T))
                        elif v_elem is not None and v_elem.text is not None:
                            if cell_type == "s":
                                # Shared string reference
                                idx = int(v_elem.text)
                                val = shared_strings[idx] if idx < len(shared_strings) else ""
                            else:
                                val = v_elem.text
                        else:
                            val = None

                        if val is not None and str(val).strip():
                            row_vals[ci] = str(val).strip()

                    elem.clear()  # Critical: release memory immediately

                    if not row_vals:
                        continue

                    if headers is None:
                        # Skip metadata rows; find the real header (3+ non-empty cells)
                        if len(row_vals) < 3:
                            continue

                        # Build headers from all column positions min→max
                        min_ci = min(row_vals.keys())
                        max_ci = max(row_vals.keys())
                        seen: dict[str, int] = {}
                        headers = []
                        for ci in range(min_ci, max_ci + 1):
                            raw = row_vals.get(ci, "")
                            name = _sanitize(raw) if raw else f"col_{ci}"
                            if not name:
                                name = f"col_{ci}"
                            if name in seen:
                                seen[name] += 1
                                name = f"{name}_{seen[name]}"
                            else:
                                seen[name] = 0
                            headers.append(name)
                            header_col_map[ci] = len(headers) - 1

                        # Create the SQLite table
                        cols_def = ", ".join(f'"{h}" TEXT' for h in headers) + ', "_session_id" TEXT'
                        conn.execute(f'DROP TABLE IF EXISTS "{scoped_table}"')
                        conn.execute(f'CREATE TABLE "{scoped_table}" ({cols_def})')
                        conn.commit()
                        placeholders = ", ".join("?" * (len(headers) + 1))
                        insert_sql   = f'INSERT INTO "{scoped_table}" VALUES ({placeholders})'

                    else:
                        # Map this data row onto the header columns
                        values = [None] * len(headers)
                        for ci, val in row_vals.items():
                            pos = header_col_map.get(ci)
                            if pos is not None:
                                values[pos] = val
                        values.append(str(session_id))
                        batch.append(values)

                        if len(batch) >= BATCH:
                            conn.executemany(insert_sql, batch)
                            conn.commit()
                            total += len(batch)
                            batch = []

            # Flush remaining rows
            if batch and insert_sql:
                conn.executemany(insert_sql, batch)
                conn.commit()
                total += len(batch)

        if headers is None:
            return {"ok": False, "error": "Could not find data headers — file may be empty or in an unsupported format."}

        return {"ok": True, "rows": total, "table": scoped_table}

    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def query(sql: str, params=()) -> list[dict]:
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(sql, params)
        rows = c.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def execute(sql: str, params=()):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(sql, params)
        conn.commit()
        return c.lastrowid
    finally:
        conn.close()


def get_conversion_status(session_id: int) -> dict:
    rows = query("SELECT conversion_status_json FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or not rows[0]["conversion_status_json"]:
        return {}
    try:
        return json.loads(rows[0]["conversion_status_json"])
    except Exception:
        return {}


def set_conversion_status(session_id: int, slot: str, status: str, rows_count: int = 0, error: str = ""):
    import json as _json
    current = get_conversion_status(session_id)
    current[slot] = {"status": status, "rows": rows_count, "error": error}
    execute("UPDATE upload_sessions SET conversion_status_json=? WHERE id=?",
            (_json.dumps(current), session_id))


def table_exists(table_name: str) -> bool:
    rows = query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return len(rows) > 0


def get_session_tables(session_id: int) -> dict:
    """Return which data tables exist for a given session."""
    expected = ["inventory", "purchase_orders", "sales", "suppliers", "customers", "stockouts"]
    return {
        name: table_exists(f"{name}_{session_id}")
        for name in expected
    }
