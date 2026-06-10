import sqlite3
import json
import os
import re

# DB lives on the Render persistent disk in prod (DB_PATH env var points to a
# file on the mounted disk, e.g. /var/data/berthai.db). For local dev, defaults
# to a file in the cwd.
# IMPORTANT: never put this under the project source dir on Render — that folder
# is overwritten on every deploy and the DB gets wiped.
DB_PATH = os.environ.get("DB_PATH", "berthcast.db")


def _verify_persistent_storage(db_path, on_render, dir_exists=os.path.isdir,
                               db_path_is_explicit=None):
    """Refuse to start if, in production, the DB would land on throwaway storage.

    Previously the code silently created a missing folder and wrote there — so if
    the persistent disk wasn't mounted (or DB_PATH wasn't set) the database lived
    on the ephemeral container filesystem and every deploy wiped all accounts,
    with no error. This makes that situation loud instead of silent.

    Only enforced on Render (on_render). Locally it does nothing, so dev is
    unaffected. Kept as a pure function (deps injected) so it is fully testable.
    """
    if not on_render:
        return
    if db_path_is_explicit is None:
        db_path_is_explicit = bool(os.environ.get("DB_PATH"))

    problems = []
    if not db_path_is_explicit:
        problems.append("DB_PATH is not set (so the DB would be written to the "
                        "deploy folder, which Render wipes on every deploy)")
    else:
        parent = os.path.dirname(db_path)
        if parent and not dir_exists(parent):
            problems.append(f"the folder {parent} does not exist — the persistent "
                            "disk is probably not mounted")

    if problems:
        raise RuntimeError(
            "REFUSING TO START: the database would be saved to throwaway storage "
            "and lost on the next deploy.\n"
            "  Problem: " + "; ".join(problems) + ".\n"
            "  Fix: in Render, attach a persistent disk (e.g. mounted at /var/data) "
            "and set the DB_PATH env var to a file on it (e.g. /var/data/berthai.db)."
        )


# Fail loudly in production if storage isn't persistent; stay convenient locally.
_verify_persistent_storage(DB_PATH, bool(os.environ.get("RENDER")))

# Local/dev only: create the parent dir if missing so sqlite can open the file.
# In production the persistent disk is already mounted, so we never create it
# here (a missing dir there means trouble — handled by the guard above).
if not os.environ.get("RENDER"):
    _db_dir = os.path.dirname(DB_PATH)
    if _db_dir and not os.path.exists(_db_dir):
        try:
            os.makedirs(_db_dir, exist_ok=True)
        except Exception:
            pass


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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

        CREATE TABLE IF NOT EXISTS contact_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            company TEXT,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chat_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT DEFAULT 'New conversation',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id)
        );

        CREATE TABLE IF NOT EXISTS company_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_name TEXT UNIQUE NOT NULL,
            stockout_cost_per_unit REAL DEFAULT 50.0,
            holding_cost_per_unit_per_day REAL DEFAULT 0.5,
            service_level_target REAL DEFAULT 0.95,
            default_lead_time_days INTEGER DEFAULT 56,
            lead_time_variance_days INTEGER DEFAULT 14,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS supplier_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_name TEXT NOT NULL,
            supplier_name TEXT NOT NULL,
            delay_probability REAL DEFAULT 0.2,
            avg_lead_time_days INTEGER DEFAULT 56,
            data_quality_score REAL DEFAULT 0.5,
            notes TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_name, supplier_name)
        );

        CREATE TABLE IF NOT EXISTS recommendation_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            action_recommended TEXT,
            user_action TEXT,
            predicted_loss_no_act REAL,
            predicted_cost_act REAL,
            net_benefit REAL,
            confidence TEXT,
            actual_outcome TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES upload_sessions(id)
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.commit()

    for migration in [
        "ALTER TABLE upload_sessions ADD COLUMN file_names_json TEXT",
        "ALTER TABLE upload_sessions ADD COLUMN conversion_status_json TEXT",
        "ALTER TABLE upload_sessions ADD COLUMN scope TEXT DEFAULT 'all'",
        "ALTER TABLE company_config ADD COLUMN industry TEXT DEFAULT 'general'",
        "ALTER TABLE company_config ADD COLUMN company_description TEXT",
        "ALTER TABLE supplier_profiles ADD COLUMN supplier_type TEXT DEFAULT 'other'",
        "ALTER TABLE chat_conversations ADD COLUMN pinned INTEGER DEFAULT 0",
        # Tier & verification — default 1/enterprise so existing users are unaffected
        "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'enterprise'",
        "ALTER TABLE users ADD COLUMN analyses_used INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN chat_messages_used INTEGER NOT NULL DEFAULT 0",
        # Role column: admin / reviewer / viewer. Existing users default to admin.
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'",
        # Org-scoped chat: add org_name to chat_conversations so all org members share them.
        "ALTER TABLE chat_conversations ADD COLUMN org_name TEXT",
        """CREATE TABLE IF NOT EXISTS email_verification_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
        # ── Performance indexes ──────────────────────────────────────────
        "CREATE INDEX IF NOT EXISTS idx_upload_sessions_user_id ON upload_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_upload_sessions_status ON upload_sessions(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_upload_sessions_org ON upload_sessions(org_name, status)",
        "CREATE INDEX IF NOT EXISTS idx_analysis_results_session ON analysis_results(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_chat_conversations_user ON chat_conversations(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_conv ON chat_messages(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_contact_requests_status ON contact_requests(status)",
        "CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_session ON recommendation_outcomes(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_token ON password_reset_tokens(token)",
        "CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_token ON email_verification_tokens(token)",
        # ── Supplier reliability score (persists across sessions) ────────
        "ALTER TABLE supplier_profiles ADD COLUMN reliability_score REAL DEFAULT 50.0",
        "ALTER TABLE supplier_profiles ADD COLUMN total_recs INTEGER DEFAULT 0",
        "ALTER TABLE supplier_profiles ADD COLUMN orders_placed INTEGER DEFAULT 0",
        "ALTER TABLE supplier_profiles ADD COLUMN stockouts_avoided INTEGER DEFAULT 0",
        "ALTER TABLE supplier_profiles ADD COLUMN stockouts_happened INTEGER DEFAULT 0",
        "ALTER TABLE supplier_profiles ADD COLUMN last_scored_at TIMESTAMP",
        "CREATE INDEX IF NOT EXISTS idx_supplier_profiles_org ON supplier_profiles(org_name)",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass

    conn.close()


# ── Upload safety limits ─────────────────────────────────────────────────────
# An .xlsx is a ZIP, so the 100 MB upload cap (MAX_CONTENT_LENGTH) does NOT bound
# how much data it expands to: a few-KB file can decompress to gigabytes ("zip
# bomb") and OOM the single 512 MB worker — a full outage triggered by one upload.
# These cap the *decompressed* work. The byte caps are enforced by counting the
# bytes the decompressor actually produces (see _LimitedReader), NOT by trusting
# the sizes the file declares in its own header — a malicious file can lie about
# those, but it cannot fake bytes it never produced.
MAX_XLSX_SHARED_STRINGS_BYTES = 64 * 1024 * 1024     # RAM-critical: accumulated in a list
MAX_XLSX_WORKSHEET_BYTES      = 1024 * 1024 * 1024    # streamed + discarded; final stop only
MAX_XLSX_ROWS                 = 2_000_000             # bounds disk use + processing time
MAX_COLUMNS                   = 16_384                # Excel's own hard column ceiling
MAX_CELLS_PER_ROW             = 16_384
MAX_CELL_CHARS                = 100_000               # one cell can't be a multi-MB blob

_OVERSIZE_MSG = (
    "This file expands to far more data than expected when opened — it may be "
    "corrupted or malformed. Please re-export it from your system and try again."
)
_TOO_MANY_ROWS_MSG = (
    f"This file has more than {MAX_XLSX_ROWS:,} rows. Please split it into smaller "
    "files and upload them one at a time."
)
_TOO_MANY_COLS_MSG = (
    "This file has an unusual number of columns — more than a spreadsheet can hold. "
    "Please check the export and try again."
)


class _DecompressionLimitExceeded(Exception):
    """Raised when a zip member produces more decompressed bytes than allowed."""


class _LimitedReader:
    """Wrap a binary stream and stop once more than `limit` bytes have actually
    been read from it.

    ElementTree pulls XML through .read(size); by counting the real bytes it
    returns we bound memory and CPU regardless of what the zip's headers claim the
    uncompressed size is. The overshoot is at most one read chunk (ElementTree
    uses 16 KB), so the bound is tight.
    """

    def __init__(self, fp, limit):
        self._fp = fp
        self._limit = limit
        self._read = 0

    def read(self, size=-1):
        chunk = self._fp.read(size)
        self._read += len(chunk)
        if self._read > self._limit:
            raise _DecompressionLimitExceeded()
        return chunk

    def close(self):
        pass


def excel_to_sqlite(filepath: str, table_name: str, session_id: int):
    """Dispatch by extension. CSV stream-parses (low RAM). XLSX uses iterparse."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        return _csv_to_sqlite(filepath, table_name, session_id)
    return _xlsx_to_sqlite(filepath, table_name, session_id)


def _sanitize_name(name: str) -> str:
    name = (name.strip().lower()
            .replace(" ", "_").replace("/", "_").replace("-", "_")
            .replace(".", "").replace("(", "").replace(")", ""))
    # Whitelist: only letters, digits, and underscore may reach a SQL identifier.
    # A header like  foo" TEXT); DROP TABLE users;--  would otherwise smuggle a
    # quote/semicolon into the CREATE TABLE statement. Callers fall back to
    # "col_<i>" when this returns "".
    return re.sub(r"[^a-z0-9_]", "", name)


def _csv_to_sqlite(filepath: str, table_name: str, session_id: int):
    import csv

    conn = get_db()
    try:
        scoped_table = f"{table_name}_{session_id}"
        with open(filepath, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            sample = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel

            reader = csv.reader(f, dialect)

            headers = None
            insert_sql = None
            BATCH = 2000
            batch = []
            total = 0

            for raw_row in reader:
                if not any((c or "").strip() for c in raw_row):
                    continue

                if headers is None:
                    seen = {}
                    headers = []
                    for i, raw in enumerate(raw_row):
                        name = _sanitize_name(raw) if raw and raw.strip() else f"col_{i}"
                        if not name:
                            name = f"col_{i}"
                        if name in seen:
                            seen[name] += 1
                            name = f"{name}_{seen[name]}"
                        else:
                            seen[name] = 0
                        headers.append(name)

                    cols_def = ", ".join(f'"{h}" TEXT' for h in headers) + ', "_session_id" TEXT'
                    conn.execute(f'DROP TABLE IF EXISTS "{scoped_table}"')
                    conn.execute(f'CREATE TABLE "{scoped_table}" ({cols_def})')
                    conn.commit()
                    placeholders = ", ".join("?" * (len(headers) + 1))
                    insert_sql = f'INSERT INTO "{scoped_table}" VALUES ({placeholders})'
                    continue

                vals = [(c or "").strip() if c is not None else None for c in raw_row]
                if len(vals) < len(headers):
                    vals = vals + [None] * (len(headers) - len(vals))
                elif len(vals) > len(headers):
                    vals = vals[:len(headers)]
                vals.append(str(session_id))
                batch.append(vals)

                if total + len(batch) > MAX_XLSX_ROWS:
                    return {"ok": False, "error": _TOO_MANY_ROWS_MSG}

                if len(batch) >= BATCH:
                    conn.executemany(insert_sql, batch)
                    conn.commit()
                    total += len(batch)
                    batch = []

            if batch and insert_sql:
                conn.executemany(insert_sql, batch)
                conn.commit()
                total += len(batch)

        if headers is None:
            return {"ok": False, "error": "CSV looks empty or has no header row."}
        return {"ok": True, "rows": total, "table": scoped_table}

    except Exception as e:
        return {"ok": False, "error": f"CSV parse failed: {e}"}
    finally:
        conn.close()


def _xlsx_to_sqlite(filepath: str, table_name: str, session_id: int):
    import zipfile
    import xml.etree.ElementTree as ET
    import re

    NS      = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    TAG_ROW = f"{{{NS}}}row"
    TAG_C   = f"{{{NS}}}c"
    TAG_V   = f"{{{NS}}}v"
    TAG_IS  = f"{{{NS}}}is"
    TAG_T   = f"{{{NS}}}t"
    TAG_SI  = f"{{{NS}}}si"

    def _col_idx(col_letter: str) -> int:
        result = 0
        for ch in col_letter.upper():
            result = result * 26 + (ord(ch) - ord("A") + 1)
        return result - 1

    conn = get_db()
    try:
        with zipfile.ZipFile(filepath, "r") as zf:

            shared_strings = []
            if "xl/sharedStrings.xml" in zf.namelist():
                with zf.open("xl/sharedStrings.xml") as f:
                    # Cap the decompressed size of the shared-strings table — it is
                    # accumulated in memory, so this is the main zip-bomb sink.
                    for event, elem in ET.iterparse(
                            _LimitedReader(f, MAX_XLSX_SHARED_STRINGS_BYTES),
                            events=("end",)):
                        if elem.tag == TAG_SI:
                            text = "".join(t.text or "" for t in elem.iter(TAG_T))
                            if len(text) > MAX_CELL_CHARS:
                                text = text[:MAX_CELL_CHARS]
                            shared_strings.append(text)
                            elem.clear()

            sheet_candidates = [n for n in zf.namelist()
                                 if re.match(r"xl/worksheets/sheet\d+\.xml", n)]
            if not sheet_candidates:
                return {"ok": False, "error": "No worksheet found in file."}
            sheet_path = sorted(sheet_candidates)[0]

            headers        = None
            header_col_map = {}
            scoped_table   = f"{table_name}_{session_id}"
            insert_sql     = None
            BATCH          = 2000
            batch          = []
            total          = 0

            with zf.open(sheet_path) as f:
                ws_root = None
                for event, elem in ET.iterparse(
                        _LimitedReader(f, MAX_XLSX_WORKSHEET_BYTES),
                        events=("start", "end")):
                    if event == "start":
                        if elem.tag == f"{{{NS}}}worksheet":
                            ws_root = elem
                        continue

                    if elem.tag != TAG_ROW:
                        continue

                    row_vals = {}
                    for cell in elem:
                        if cell.tag != TAG_C:
                            continue
                        if len(row_vals) >= MAX_CELLS_PER_ROW:
                            break
                        ref = cell.get("r", "")
                        col_letter = re.sub(r"\d", "", ref)
                        if not col_letter:
                            continue
                        ci = _col_idx(col_letter)

                        cell_type = cell.get("t", "")
                        v_elem    = cell.find(TAG_V)
                        is_elem   = cell.find(TAG_IS)

                        if is_elem is not None:
                            val = "".join(t.text or "" for t in is_elem.iter(TAG_T))
                        elif v_elem is not None and v_elem.text is not None:
                            if cell_type == "s":
                                idx = int(v_elem.text)
                                val = shared_strings[idx] if idx < len(shared_strings) else ""
                            else:
                                val = v_elem.text
                        else:
                            val = None

                        if val is not None and str(val).strip():
                            sval = str(val).strip()
                            if len(sval) > MAX_CELL_CHARS:
                                sval = sval[:MAX_CELL_CHARS]
                            row_vals[ci] = sval

                    if ws_root is not None:
                        ws_root.clear()

                    if not row_vals:
                        continue

                    if headers is None:
                        if len(row_vals) < 3:
                            continue
                        min_ci = min(row_vals.keys())
                        max_ci = max(row_vals.keys())
                        if max_ci - min_ci + 1 > MAX_COLUMNS:
                            return {"ok": False, "error": _TOO_MANY_COLS_MSG}
                        seen   = {}
                        headers = []
                        for ci in range(min_ci, max_ci + 1):
                            raw  = row_vals.get(ci, "")
                            name = _sanitize_name(raw) if raw else f"col_{ci}"
                            if not name:
                                name = f"col_{ci}"
                            if name in seen:
                                seen[name] += 1
                                name = f"{name}_{seen[name]}"
                            else:
                                seen[name] = 0
                            headers.append(name)
                            header_col_map[ci] = len(headers) - 1

                        cols_def = ", ".join(f'"{h}" TEXT' for h in headers) + ', "_session_id" TEXT'
                        conn.execute(f'DROP TABLE IF EXISTS "{scoped_table}"')
                        conn.execute(f'CREATE TABLE "{scoped_table}" ({cols_def})')
                        conn.commit()
                        placeholders = ", ".join("?" * (len(headers) + 1))
                        insert_sql   = f'INSERT INTO "{scoped_table}" VALUES ({placeholders})'

                    else:
                        values = [None] * len(headers)
                        for ci, val in row_vals.items():
                            pos = header_col_map.get(ci)
                            if pos is not None:
                                values[pos] = val
                        values.append(str(session_id))
                        batch.append(values)

                        if total + len(batch) > MAX_XLSX_ROWS:
                            return {"ok": False, "error": _TOO_MANY_ROWS_MSG}

                        if len(batch) >= BATCH:
                            conn.executemany(insert_sql, batch)
                            conn.commit()
                            total += len(batch)
                            batch = []

            if batch and insert_sql:
                conn.executemany(insert_sql, batch)
                conn.commit()
                total += len(batch)

        if headers is None:
            return {"ok": False, "error": "Could not find data headers in xlsx."}
        return {"ok": True, "rows": total, "table": scoped_table}

    except _DecompressionLimitExceeded:
        return {"ok": False, "error": _OVERSIZE_MSG}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def query(sql: str, params=()) -> list:
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


def update_recommendations(session_id, mutator):
    """Atomically read → modify → write analysis_results.recommendations_json.

    Every approve/dismiss/edit/outcome action used to do this as three separate
    connections (SELECT, then json.loads in Python, then UPDATE) with a gap in
    between. Two teammates acting on the same results page at the same time would
    both read the same blob and both write it back — the second write silently
    wiped the first (lost update). Outcome tracking, the proof data, was the most
    likely casualty.

    This runs the whole read-modify-write inside ONE connection under
    BEGIN IMMEDIATE, which takes SQLite's write lock up front. A second caller
    blocks (up to the 30s busy timeout) until the first commits, so it always
    reads the latest committed blob before changing it. No lost updates.

    `mutator(recs)` receives the parsed list and MUST mutate it in place. Whatever
    it returns is handed back to the caller as the "result" payload (e.g. a list of
    items it touched, or a flag saying the target item wasn't found).

    Returns one of:
      {"ok": True,  "result": <mutator return value>}
      {"ok": False, "code": 404, "error": "No results for this session"}
      {"ok": False, "code": 500, "error": "Corrupt data"}
    """
    conn = get_db()
    try:
        conn.isolation_level = None          # we drive the transaction by hand
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT recommendations_json FROM analysis_results WHERE session_id=?",
            (session_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return {"ok": False, "code": 404, "error": "No results for this session"}
        try:
            recs = json.loads(row["recommendations_json"] or "[]")
        except Exception:
            conn.execute("ROLLBACK")
            return {"ok": False, "code": 500, "error": "Corrupt data"}

        payload = mutator(recs)

        conn.execute(
            "UPDATE analysis_results SET recommendations_json=? WHERE session_id=?",
            (json.dumps(recs), session_id)
        )
        conn.execute("COMMIT")
        return {"ok": True, "result": payload}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
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
    current = get_conversion_status(session_id)
    current[slot] = {"status": status, "rows": rows_count, "error": error}
    execute("UPDATE upload_sessions SET conversion_status_json=? WHERE id=?",
            (json.dumps(current), session_id))


def table_exists(table_name: str) -> bool:
    rows = query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return len(rows) > 0


def get_session_tables(session_id: int) -> dict:
    expected = ["inventory", "purchase_orders", "sales", "suppliers", "customers", "stockouts"]
    return {
        name: table_exists(f"{name}_{session_id}")
        for name in expected
    }


# ---------------------------------------------------------------------------
# Company config helpers
# ---------------------------------------------------------------------------

def get_company_config(org_name: str) -> dict:
    rows = query("SELECT * FROM company_config WHERE org_name=?", (org_name,))
    if rows:
        return dict(rows[0])
    # Return sensible defaults if not yet configured
    return {
        "org_name": org_name,
        "stockout_cost_per_unit": 50.0,
        "holding_cost_per_unit_per_day": 0.5,
        "service_level_target": 0.95,
        "default_lead_time_days": 56,
        "lead_time_variance_days": 14,
    }


def upsert_company_config(org_name: str, **kwargs):
    existing = query("SELECT id FROM company_config WHERE org_name=?", (org_name,))
    allowed = {
        "stockout_cost_per_unit", "holding_cost_per_unit_per_day",
        "service_level_target", "default_lead_time_days", "lead_time_variance_days",
        "industry", "company_description",
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    if existing:
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=CURRENT_TIMESTAMP"
        execute(f"UPDATE company_config SET {sets} WHERE org_name=?",
                tuple(fields.values()) + (org_name,))
    else:
        cols = "org_name, " + ", ".join(fields.keys())
        placeholders = ", ".join("?" * (len(fields) + 1))
        execute(f"INSERT INTO company_config ({cols}) VALUES ({placeholders})",
                (org_name,) + tuple(fields.values()))


# ---------------------------------------------------------------------------
# Supplier profile helpers
# ---------------------------------------------------------------------------

def get_supplier_profiles(org_name: str) -> list:
    return query("SELECT * FROM supplier_profiles WHERE org_name=? ORDER BY supplier_name",
                 (org_name,))


def get_supplier_profile(org_name: str, supplier_name: str) -> dict:
    rows = query(
        "SELECT * FROM supplier_profiles WHERE org_name=? AND supplier_name=?",
        (org_name, supplier_name)
    )
    if rows:
        return dict(rows[0])
    return {
        "org_name": org_name,
        "supplier_name": supplier_name,
        "delay_probability": 0.2,
        # None (not 56) so callers fall through to the supplier-type lead time
        # (import 112 / local 21). A hardcoded number here silently shadowed that
        # and made every item use a flat 56-day horizon. Truly unknown suppliers
        # still land on config.default_lead_time_days as the final fallback.
        "avg_lead_time_days": None,
        "data_quality_score": 0.3,  # Low — unknown supplier
        "notes": "No profile. Using defaults.",
    }


def upsert_supplier_profile(org_name: str, supplier_name: str, **kwargs):
    existing = query(
        "SELECT id FROM supplier_profiles WHERE org_name=? AND supplier_name=?",
        (org_name, supplier_name)
    )
    allowed = {"delay_probability", "avg_lead_time_days", "data_quality_score", "notes", "supplier_type"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if existing:
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=CURRENT_TIMESTAMP"
        execute(
            f"UPDATE supplier_profiles SET {sets} WHERE org_name=? AND supplier_name=?",
            tuple(fields.values()) + (org_name, supplier_name)
        )
    else:
        cols = "org_name, supplier_name, " + ", ".join(fields.keys())
        placeholders = ", ".join("?" * (len(fields) + 2))
        execute(
            f"INSERT INTO supplier_profiles ({cols}) VALUES ({placeholders})",
            (org_name, supplier_name) + tuple(fields.values())
        )


def seed_cool_link_defaults():
    """Pre-seed Cool Link supplier profiles from known data.
    Only writes if Cool Link has no config row yet — safe to call multiple times."""
    org = "Cool Link"
    existing = query("SELECT id FROM company_config WHERE org_name=?", (org,))
    if not existing:
        upsert_company_config(org,
            stockout_cost_per_unit=80.0,
            holding_cost_per_unit_per_day=0.4,
            service_level_target=0.95,
            default_lead_time_days=56,
            lead_time_variance_days=21,
            industry="food_distribution",
            company_description=(
                "Food distribution company in Singapore. "
                "Imports chilled, frozen, and dry goods. "
                "Key risk: import lead times of 90–120 days with unreliable suppliers."
            ),
        )
    known_suppliers = [
        ("Local SG",    0.10,  21, 0.9, "Local supplier. Reliable. Fast lead time."),
        ("Import Other",0.25,  56, 0.5, "Generic import. Moderate risk."),
    ]
    for name, delay_p, lead, quality, notes in known_suppliers:
        upsert_supplier_profile(org, name,
            delay_probability=delay_p,
            avg_lead_time_days=lead,
            data_quality_score=quality,
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Outcome tracking helpers
# ---------------------------------------------------------------------------

def save_recommendation_outcome(session_id: int, item: str, action_recommended: str,
                                  predicted_loss_no_act: float, predicted_cost_act: float,
                                  net_benefit: float, confidence: str):
    execute(
        """INSERT OR IGNORE INTO recommendation_outcomes
           (session_id, item, action_recommended, predicted_loss_no_act,
            predicted_cost_act, net_benefit, confidence)
           VALUES (?,?,?,?,?,?,?)""",
        (session_id, item, action_recommended, predicted_loss_no_act,
         predicted_cost_act, net_benefit, confidence)
    )



def get_supplier_accuracy(org_name: str, supplier_name: str, days: int = 90) -> dict:
    """Return historical prediction accuracy for a supplier across recent sessions."""
    rows = query(
        "SELECT ro.action_recommended, ro.user_action, ro.actual_outcome, "
        "       ro.predicted_loss_no_act, ro.confidence "
        "FROM recommendation_outcomes ro "
        "JOIN upload_sessions us ON ro.session_id = us.id "
        "WHERE us.org_name=? "
        "  AND ro.created_at >= datetime('now', ?) "
        "  AND ro.item LIKE ?",
        (org_name, f"-{days} days", f"%{supplier_name}%")
    )
    total = len(rows)
    dismissed = sum(1 for r in rows if r.get("user_action") == "dismissed")
    approved  = sum(1 for r in rows if r.get("user_action") == "approved")
    return {
        "total_recs": total,
        "approved": approved,
        "dismissed": dismissed,
        "days_window": days,
    }


def get_outcome_stats(org_name: str) -> dict:
    """Aggregate outcome tracking stats across all sessions for an org.
    Returns counts for the ROI dashboard card."""
    rows = query(
        "SELECT ar.recommendations_json FROM analysis_results ar "
        "JOIN upload_sessions us ON ar.session_id = us.id "
        "WHERE us.org_name=? AND us.status='complete' "
        "ORDER BY us.created_at DESC",
        (org_name,)
    )
    total_recs = 0
    total_approved = 0
    total_order_placed = 0
    total_stockout_avoided = 0
    total_stockout_happened = 0
    total_outcome_pending = 0
    for row in rows:
        try:
            recs = json.loads(row["recommendations_json"] or "[]")
        except Exception:
            continue
        for r in recs:
            if not isinstance(r, dict) or r.get("error"):
                continue
            total_recs += 1
            if r.get("approved"):
                total_approved += 1
            if r.get("order_placed"):
                total_order_placed += 1
                outcome = r.get("outcome_status", "")
                if outcome == "stockout_avoided":
                    total_stockout_avoided += 1
                elif outcome == "stockout_happened":
                    total_stockout_happened += 1
                elif not outcome:
                    total_outcome_pending += 1
    return {
        "total_recs": total_recs,
        "total_approved": total_approved,
        "total_order_placed": total_order_placed,
        "stockout_avoided": total_stockout_avoided,
        "stockout_happened": total_stockout_happened,
        "outcome_pending": total_outcome_pending,
        "follow_through_pct": round(total_order_placed / total_approved * 100) if total_approved else 0,
        "success_rate_pct": round(total_stockout_avoided / (total_stockout_avoided + total_stockout_happened) * 100) if (total_stockout_avoided + total_stockout_happened) else 0,
    }


def update_supplier_scores(org_name: str):
    """Recalculate reliability scores for all suppliers in an org based on
    outcome history across all sessions. Score formula:
      - Base: 50
      - +20 max from follow-through rate (orders placed / approved)
      - +30 max from stockout avoidance rate
      - -10 if known unreliable (delay_probability > 0.3)
      - Clamped to 0-100
    """
    rows = query(
        "SELECT ar.recommendations_json FROM analysis_results ar "
        "JOIN upload_sessions us ON ar.session_id = us.id "
        "WHERE us.org_name=? AND us.status='complete'",
        (org_name,)
    )
    supplier_stats = {}
    for row in rows:
        try:
            recs = json.loads(row["recommendations_json"] or "[]")
        except Exception:
            continue
        for r in recs:
            if not isinstance(r, dict) or r.get("error"):
                continue
            sup = (r.get("edited_supplier") or r.get("supplier") or "Unknown").strip()
            if sup not in supplier_stats:
                supplier_stats[sup] = {"recs": 0, "approved": 0, "placed": 0, "avoided": 0, "happened": 0}
            s = supplier_stats[sup]
            s["recs"] += 1
            if r.get("approved"):
                s["approved"] += 1
            if r.get("order_placed"):
                s["placed"] += 1
                if r.get("outcome_status") == "stockout_avoided":
                    s["avoided"] += 1
                elif r.get("outcome_status") == "stockout_happened":
                    s["happened"] += 1

    for sup_name, stats in supplier_stats.items():
        profile = get_supplier_profile(org_name, sup_name)
        delay_prob = profile.get("delay_probability", 0.2)

        score = 50.0
        if stats["approved"] > 0:
            ft_rate = stats["placed"] / stats["approved"]
            score += ft_rate * 20
        outcomes_total = stats["avoided"] + stats["happened"]
        if outcomes_total > 0:
            avoid_rate = stats["avoided"] / outcomes_total
            score += avoid_rate * 30
        if delay_prob > 0.3:
            score -= 10
        score = max(0, min(100, round(score, 1)))

        upsert_supplier_profile(org_name, sup_name,
            delay_probability=delay_prob,
        )
        execute(
            "UPDATE supplier_profiles "
            "SET reliability_score=?, total_recs=?, orders_placed=?, "
            "    stockouts_avoided=?, stockouts_happened=?, "
            "    last_scored_at=CURRENT_TIMESTAMP "
            "WHERE org_name=? AND supplier_name=?",
            (score, stats["recs"], stats["placed"],
             stats["avoided"], stats["happened"],
             org_name, sup_name)
        )


def get_supplier_scores(org_name: str) -> list:
    """Return all supplier profiles with scores for an org, sorted by score descending."""
    return query(
        "SELECT supplier_name, supplier_type, reliability_score, "
        "       total_recs, orders_placed, stockouts_avoided, stockouts_happened, "
        "       delay_probability, avg_lead_time_days, last_scored_at, notes "
        "FROM supplier_profiles "
        "WHERE org_name=? "
        "ORDER BY reliability_score DESC, supplier_name ASC",
        (org_name,)
    )
