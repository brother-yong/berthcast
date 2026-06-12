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
import shutil
import sqlite3
import threading
import time
from datetime import datetime

_PREFIX = "berthcast-"
_SUFFIX = ".db"

# 12 June 2026: boot snapshots from a string of restarts filled the 1GB disk
# and starved SQLite of working space — the live site hung while every backup
# "succeeded". A backup that kills the site protects nothing, hence:
#   - a snapshot younger than this means a scheduled run is skipped (each boot
#     takes one; a crash-loop must not stack a pile of full DB copies);
MIN_SNAPSHOT_AGE_SECONDS = 6 * 3600
#   - a snapshot is only written when the disk can hold it with this much
#     room left over for the live DB's WAL and uploads.
FREE_SPACE_MARGIN_BYTES = 100 * 1024 * 1024


def _db_size(db_path: str) -> int:
    """Bytes a fresh snapshot needs, estimated from the live DB + its WAL."""
    total = 0
    for p in (db_path, db_path + "-wal"):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def _free_bytes(path: str):
    """Free bytes on the disk holding `path`, or None when unmeasurable —
    a failed measurement must never block backups."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def newest_snapshot_age_seconds(backups_dir: str, now_ts: float = None):
    """Seconds since the newest snapshot was written, or None if there are none."""
    latest = latest_backup(backups_dir)
    if latest is None:
        return None
    try:
        mtime = os.path.getmtime(latest)
    except OSError:
        return None
    return (time.time() if now_ts is None else now_ts) - mtime


def make_room(backups_dir: str, needed_bytes: int, min_keep: int = 1) -> list:
    """Delete oldest snapshots until `needed_bytes` fit or only `min_keep`
    remain. Never chases room by deleting the last copy — if one snapshot
    plus the live DB don't fit together, the disk is simply too small and
    the caller should alert, not destroy the only backup. Returns names removed."""
    removed = []
    files = list_backups(backups_dir)
    while len(files) > min_keep:
        free = _free_bytes(backups_dir)
        if free is None or free >= needed_bytes:
            break
        victim = files.pop(0)
        try:
            os.remove(os.path.join(backups_dir, victim))
            removed.append(victim)
        except OSError:
            break
    return removed


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
    base = f"{_PREFIX}{now:%Y%m%d-%H%M%S}"
    dest = os.path.join(backups_dir, base + _SUFFIX)
    if os.path.exists(dest):
        # Same-second collision (admin double-click). Refine with microseconds —
        # digits sort after '.', so name order stays time order for list_backups.
        dest = os.path.join(backups_dir, f"{base}{now.microsecond:06d}{_SUFFIX}")
    return make_snapshot(db_path, dest)


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
             on_failure=None, force: bool = False) -> str:
    """One backup + prune cycle. Never raises — a backup failure must not take
    down the app; it's logged instead. Returns the snapshot path, or None when
    skipped or failed.

    Scheduled runs (force=False) skip when a recent snapshot already exists,
    and every run refuses to write into a disk that can't hold the snapshot —
    pruning oldest copies first to make room, alerting when even that isn't
    enough. `on_failure(error_text)` is an optional hook so the caller can
    alert a human — a log line nobody reads means every backup could silently
    be weeks old. The hook itself is guarded: a broken alerter can't break
    backups.
    """
    def _alert(msg):
        if on_failure is not None:
            try:
                on_failure(msg)
            except Exception:
                pass

    try:
        if not force:
            age = newest_snapshot_age_seconds(backups_dir)
            if age is not None and age < MIN_SNAPSHOT_AGE_SECONDS:
                logger(f"[backup] skipped: newest snapshot is {age / 3600:.1f}h old "
                       f"(under {MIN_SNAPSHOT_AGE_SECONDS // 3600}h) — restarts don't stack copies")
                return None

        os.makedirs(backups_dir, exist_ok=True)
        needed = _db_size(db_path) + FREE_SPACE_MARGIN_BYTES
        free = _free_bytes(backups_dir)
        if free is not None and free < needed:
            removed = make_room(backups_dir, needed)
            if removed:
                logger(f"[backup] pruned {len(removed)} old snapshot(s) to free disk space")
            free = _free_bytes(backups_dir)
            if free is not None and free < needed:
                msg = (f"not enough disk space for a snapshot — need ~{needed // 1048576}MB, "
                       f"{free // 1048576}MB free. Grow the Render disk.")
                logger(f"[backup] SKIPPED: {msg}")
                _alert(msg)
                return None

        path = backup_database(db_path, backups_dir)
        prune_backups(backups_dir, keep)
        logger(f"[backup] wrote {path}")
        return path
    except Exception as e:
        logger(f"[backup] FAILED: {e}")
        _alert(str(e))
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
