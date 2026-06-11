"""Database backups for berthcast.

The whole product — accounts, every analysis, and the a regional food distributor outcome proof
numbers — lives in one SQLite file on one Render disk. The disk survives deploys,
but a backup is a different thing from durable storage: a bad migration, an
accidental admin delete, or file corruption can still wipe data the disk happily
keeps holding. This module takes consistent snapshots so there's always a way back.

Two layers of protection:
  1. Automatic on-disk snapshots (start_backup_scheduler) — a timestamped copy
     written next to the DB every `interval_seconds`, pruned to the last `keep`.
     Covers the common cases (bad write, fat-finger, corruption) with zero setup.
  2. A manual off-disk copy — the admin "Download backup" button calls
     backup_database() and streams the file to the founder's laptop. That's the
     copy that survives total loss of the Render disk itself.

Snapshots use SQLite's `VACUUM INTO`, which writes a clean, fully-consistent copy
even while the app is mid-write (WAL is in play) — no locking the live DB.

The snapshot/prune functions are pure and side-effect-contained so they can be
unit-tested without a running app or any threads.
"""
import os
import sqlite3
import threading
import time
from datetime import datetime

_PREFIX = "berthcast-"
_SUFFIX = ".db"


def make_snapshot(db_path: str, dest_path: str) -> str:
    """Write a consistent copy of `db_path` to `dest_path` via VACUUM INTO.

    VACUUM INTO requires the destination not to exist; callers use timestamped
    names so that never collides.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("VACUUM INTO ?", (dest_path,))
    finally:
        conn.close()
    return dest_path


def backup_database(db_path: str, backups_dir: str, now: datetime = None) -> str:
    """Snapshot the DB into `backups_dir` with a timestamped name. Returns the path."""
    now = now or datetime.utcnow()
    os.makedirs(backups_dir, exist_ok=True)
    name = f"{_PREFIX}{now:%Y%m%d-%H%M%S}{_SUFFIX}"
    return make_snapshot(db_path, os.path.join(backups_dir, name))


def list_backups(backups_dir: str) -> list:
    """All snapshot filenames in `backups_dir`, oldest first.

    The timestamp format sorts chronologically as plain text, so a lexicographic
    sort is also a time sort.
    """
    if not os.path.isdir(backups_dir):
        return []
    files = [f for f in os.listdir(backups_dir)
             if f.startswith(_PREFIX) and f.endswith(_SUFFIX)]
    return sorted(files)


def prune_backups(backups_dir: str, keep: int = 14) -> list:
    """Delete all but the newest `keep` snapshots. Returns the names removed."""
    if keep <= 0:
        return []
    files = list_backups(backups_dir)
    to_remove = files[:-keep] if len(files) > keep else []
    for f in to_remove:
        try:
            os.remove(os.path.join(backups_dir, f))
        except OSError:
            pass
    return to_remove


def latest_backup(backups_dir: str) -> str:
    """Full path of the newest snapshot, or None if there are none."""
    files = list_backups(backups_dir)
    return os.path.join(backups_dir, files[-1]) if files else None


def default_backups_dir(db_path: str) -> str:
    """Where snapshots live: a `backups/` folder beside the DB (so it's on the
    same persistent disk in production)."""
    parent = os.path.dirname(db_path) or "."
    return os.path.join(parent, "backups")


def run_once(db_path: str, backups_dir: str, keep: int = 14, logger=print,
             on_failure=None) -> str:
    """One backup + prune cycle. Never raises — a backup failure must not take
    down the app; it's logged instead. Returns the snapshot path, or None on failure.

    `on_failure(error_text)` is an optional hook so the caller can alert a
    human — a log line nobody reads means every backup could silently be weeks
    old. The hook itself is guarded: a broken alerter can't break backups.
    """
    try:
        path = backup_database(db_path, backups_dir)
        prune_backups(backups_dir, keep)
        logger(f"[backup] wrote {path}")
        return path
    except Exception as e:
        logger(f"[backup] FAILED: {e}")
        if on_failure is not None:
            try:
                on_failure(str(e))
            except Exception:
                pass
        return None


def start_backup_scheduler(db_path: str, backups_dir: str,
                           interval_seconds: int = 86400, keep: int = 14,
                           logger=print, on_failure=None) -> threading.Thread:
    """Start a daemon thread that backs up immediately, then every
    `interval_seconds`. Single-worker deployment, so one scheduler is enough.
    Returns the thread (already started)."""
    def _loop():
        while True:
            run_once(db_path, backups_dir, keep, logger, on_failure)
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="backup-scheduler", daemon=True)
    t.start()
    return t
