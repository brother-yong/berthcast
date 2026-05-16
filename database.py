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

    def _sanitize(name: str) -> s