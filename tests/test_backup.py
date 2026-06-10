"""Proof for the database backup module.

berthcast's entire state lives in one SQLite file on one Render disk. backup.py
takes consistent snapshots (VACUUM INTO) so a bad migration, accidental delete,
or corruption isn't fatal. This proves a snapshot is a real, readable copy of the
data, that pruning keeps the newest N, and that a backup failure never raises.

Dependency-free:
    python tests/test_backup.py
"""
import os
import sys
import sqlite3
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backup

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


_TMP = tempfile.mkdtemp(prefix="berth_backup_")
_DB = os.path.join(_TMP, "live.db")
_BACKUPS = os.path.join(_TMP, "backups")

# A live DB with some data.
conn = sqlite3.connect(_DB)
conn.execute("CREATE TABLE t (id INTEGER, v TEXT)")
conn.execute("INSERT INTO t VALUES (1, 'hello')")
conn.commit()
conn.close()

# ── A snapshot is a real, readable copy ─────────────────────────────────────
p1 = backup.backup_database(_DB, _BACKUPS, now=datetime(2026, 6, 10, 1, 0, 0))
_check("snapshot file exists", os.path.exists(p1))
c = sqlite3.connect(p1)
row = c.execute("SELECT v FROM t WHERE id=1").fetchone()
c.close()
_check("snapshot contains the live data", bool(row) and row[0] == "hello", detail=str(row))

# ── Listing is chronological ────────────────────────────────────────────────
for h in range(2, 8):
    backup.backup_database(_DB, _BACKUPS, now=datetime(2026, 6, 10, h, 0, 0))
files = backup.list_backups(_BACKUPS)
_check("seven snapshots present", len(files) == 7, detail=str(len(files)))
_check("list is sorted oldest-to-newest", files == sorted(files))

# ── Pruning keeps the newest N ──────────────────────────────────────────────
removed = backup.prune_backups(_BACKUPS, keep=3)
remaining = backup.list_backups(_BACKUPS)
_check("prune removed the 4 oldest", len(removed) == 4, detail=str(removed))
_check("three snapshots kept", len(remaining) == 3, detail=str(remaining))
_check("the newest snapshot was kept", remaining[-1] == files[-1])
_check("an oldest snapshot was removed", files[0] in removed)

# ── Helpers ─────────────────────────────────────────────────────────────────
_check("latest_backup points to the newest file",
       os.path.basename(backup.latest_backup(_BACKUPS)) == remaining[-1])
_check("default dir sits beside the DB",
       backup.default_backups_dir(os.path.join("/var/data", "berthai.db"))
       == os.path.join("/var/data", "backups"))

# ── run_once never raises ───────────────────────────────────────────────────
ok_path = backup.run_once(_DB, _BACKUPS, keep=10, logger=lambda m: None)
_check("run_once returns a path on success", bool(ok_path) and os.path.exists(ok_path))
# Pointing at a directory (not a file) makes SQLite fail to open — run_once must
# swallow it and return None rather than crash the app.
fail = backup.run_once(_TMP, _BACKUPS, keep=10, logger=lambda m: None)
_check("run_once swallows errors and returns None on failure", fail is None, detail=str(fail))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll backup tests passed.")
