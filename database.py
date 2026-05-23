import sqlite3
import json
import os

DB_PATH = "berthai.db"


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
        """CREATE TABLE IF NOT EXISTS email_verification_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )""",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass

    conn.close()


def excel_to_sqlite(filepath: str, table_name: str, session_id: int):
    """Dispatch by extension. CSV stream-parses (low RAM). XLSX uses iterparse."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        return _csv_to_sqlite(filepath, table_name, session_id)
    return _xlsx_to_sqlite(filepath, table_name, session_id)


def _sanitize_name(name: str) -> str:
    return (name.strip().lower()
            .replace(" ", "_").replace("/", "_").replace("-", "_")
            .replace(".", "").replace("(", "").replace(")", ""))


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
                    for event, elem in ET.iterparse(f, events=("end",)):
                        if elem.tag == TAG_SI:
                            text = "".join(t.text or "" for t in elem.iter(TAG_T))
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
                for event, elem in ET.iterparse(f, events=("start", "end")):
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
                            row_vals[ci] = str(val).strip()

                    if ws_root is not None:
                        ws_root.clear()

                    if not row_vals:
                        continue

                    if headers is None:
                        if len(row_vals) < 3:
                            continue
                        min_ci = min(row_vals.keys())
                        max_ci = max(row_vals.keys())
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
        "avg_lead_time_days": 56,
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
        ("El Sabah",    0.55, 112, 0.7, "Flagged unreliable. Import. High delay history."),
        ("ABD Khan",    0.50, 112, 0.6, "Flagged unreliable. Import. Frequent delays."),
        ("Nhan Tu",     0.45, 112, 0.6, "Flagged unreliable. Import. Inconsistent lead times."),
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
        """SELECT ro.action_recommended, ro.user_action, ro.actual_outcome,
                  ro.predicted_loss_no_act, ro.confidence
           FROM recommendation_outcomes ro
           JOIN upload_sessions us ON ro.session_id = us.id
           WHERE us.org_name=?
             AND ro.created_at >= datetime('now', ?)
             AND ro.item LIKE ?""",
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
