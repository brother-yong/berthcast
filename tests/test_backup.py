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
import time
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

# ── Restart-storm guard: boot backups don't stack on every restart ──────────
# The snapshots above were just written, so a scheduled (non-forced) run skips.
skipped = backup.run_once(_DB, _BACKUPS, keep=10, logger=lambda m: None)
_check("run_once skips while a fresh snapshot exists (12 June: restarts filled the disk)",
       skipped is None, detail=str(skipped))

ok_path = backup.run_once(_DB, _BACKUPS, keep=10, logger=lambda m: None, force=True)
_check("force=True writes regardless (admin button path)",
       bool(ok_path) and os.path.exists(ok_path))

# Age the snapshots past the guard — the daily scheduled run must still work.
_old = time.time() - (backup.MIN_SNAPSHOT_AGE_SECONDS + 60)
for f in backup.list_backups(_BACKUPS):
    os.utime(os.path.join(_BACKUPS, f), (_old, _old))
ok_aged = backup.run_once(_DB, _BACKUPS, keep=10, logger=lambda m: None)
_check("run_once writes once the newest snapshot is old enough",
       bool(ok_aged) and os.path.exists(ok_aged))

# ── run_once never raises ───────────────────────────────────────────────────
# Pointing at a directory (not a file) makes SQLite fail to open — run_once must
# swallow it and return None rather than crash the app.
fail = backup.run_once(_TMP, _BACKUPS, keep=10, logger=lambda m: None, force=True)
_check("run_once swallows errors and returns None on failure", fail is None, detail=str(fail))

# ── Disk-room guard: a backup must never fill the disk ──────────────────────
_orig_free = backup._free_bytes
_orig_size = backup._db_size

# make_room deletes oldest-first and stops as soon as there's room.
_readings = iter([5, 5, 10**15])           # low, low, plenty
backup._free_bytes = lambda p: next(_readings)
_before = backup.list_backups(_BACKUPS)
_removed = backup.make_room(_BACKUPS, needed_bytes=100)
_check("make_room deletes oldest first, stops when room appears",
       _removed == _before[:2], detail=f"removed={_removed}")

# make_room never deletes the last snapshot chasing room that won't appear.
backup._free_bytes = lambda p: 0
backup.make_room(_BACKUPS, needed_bytes=100, min_keep=1)
_check("make_room keeps at least one snapshot on a hopelessly small disk",
       len(backup.list_backups(_BACKUPS)) == 1)

# run_once on a full disk: prunes what it may, then SKIPS + alerts — never writes.
_alerts = []
_count_before = len(backup.list_backups(_BACKUPS))
res = backup.run_once(_DB, _BACKUPS, keep=10, logger=lambda m: None,
                      on_failure=_alerts.append, force=True)
_check("run_once refuses to write into a full disk", res is None, detail=str(res))
_check("the too-full disk fires the failure alert (ALERT_EMAIL path)",
       len(_alerts) == 1 and "disk" in _alerts[0], detail=str(_alerts))
_check("no snapshot appeared on the full disk",
       len(backup.list_backups(_BACKUPS)) == _count_before)

# An unmeasurable disk must never block backups.
backup._free_bytes = lambda p: None
ok_unmeasured = backup.run_once(_DB, _BACKUPS, keep=10, logger=lambda m: None, force=True)
_check("unmeasurable free space never blocks a backup", bool(ok_unmeasured))

backup._free_bytes = _orig_free
backup._db_size = _orig_size
ok_restored = backup.run_once(_DB, _BACKUPS, keep=10, logger=lambda m: None, force=True)
_check("normal backups resume with real measurements", bool(ok_restored))

# ── Same-second snapshots don't collide (admin double-click) ────────────────
n1 = backup.backup_database(_DB, _BACKUPS, now=datetime(2026, 6, 11, 1, 0, 0))
n2 = backup.backup_database(_DB, _BACKUPS, now=datetime(2026, 6, 11, 1, 0, 0))
_check("two snapshots in the same second both exist under distinct names",
       os.path.exists(n1) and os.path.exists(n2) and n1 != n2,
       detail=f"{os.path.basename(n1)} / {os.path.basename(n2)}")


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll backup tests passed.")
