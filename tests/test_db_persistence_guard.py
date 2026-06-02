"""Tests for the production storage guard in database.py.

The guard refuses to start the app on Render if the database would be written
to throwaway storage — either because DB_PATH isn't set, or because the
persistent disk isn't actually mounted. This is what stops the silent
data-loss bug (accounts wiped on every deploy) from ever returning unnoticed.

The guard logic is a pure function so we can test all four situations without
spinning up a real Render container.

Dependency-free: run with `python tests/test_db_persistence_guard.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys
import tempfile

# Use a temp DB path and make sure we're NOT seen as "on Render" while importing,
# so importing database doesn't trip its own guard or touch the repo folder.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "guard_test.db"))
os.environ.pop("RENDER", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database as db


_FAILED = False


def _check(name, cond):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        _FAILED = True


def _raises(**kwargs):
    """True if the guard refuses to start (raises) for the given situation."""
    try:
        db._verify_persistent_storage(**kwargs)
        return False
    except RuntimeError:
        return True


# 1. On Render, DB_PATH not set -> would write to throwaway cwd -> must shout.
_check("render + no DB_PATH -> refuses to start",
       _raises(db_path="berthcast.db", on_render=True,
               dir_exists=lambda d: False, db_path_is_explicit=False))

# 2. On Render, DB_PATH set but its folder is missing (disk not mounted) -> shout.
_check("render + DB_PATH set but disk not mounted -> refuses to start",
       _raises(db_path="/var/data/berthai.db", on_render=True,
               dir_exists=lambda d: False, db_path_is_explicit=True))

# 3. On Render, DB_PATH set and the disk is mounted (folder exists) -> boots fine.
_check("render + DB_PATH set + disk mounted -> starts normally",
       not _raises(db_path="/var/data/berthai.db", on_render=True,
                   dir_exists=lambda d: True, db_path_is_explicit=True))

# 4. Local dev (not on Render) -> never blocks, even with a missing folder.
_check("local dev -> never blocks",
       not _raises(db_path="berthcast.db", on_render=False,
                   dir_exists=lambda d: False, db_path_is_explicit=False))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll storage-guard tests passed.")
