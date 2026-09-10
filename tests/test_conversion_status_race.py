"""Concurrent slot updates must all survive in the real SQLite status row."""
import os
import queue
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="berthcast_conversion_"), "test.db")
os.environ.pop("RENDER", None)

import database as db

db.init_db()
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (1, "BROOKVALE", "uploading"))
db.set_conversion_status(sid, "existing", "done", rows_count=5)
_FAILED = False


def _check(name, cond):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        _FAILED = True


N = 8
start = threading.Barrier(N)
after_read = threading.Barrier(N)
real_get = db.get_conversion_status
errors = queue.SimpleQueue()


def _overlapping_read(session_id):
    current = real_get(session_id)
    # Without serialization, all writers read the same snapshot before writing.
    # With the lock, the first wait expires and later writers each read fresh data.
    try:
        after_read.wait(timeout=2)
    except threading.BrokenBarrierError:
        pass
    return current


def _write_slot(i):
    try:
        start.wait(timeout=5)
        db.set_conversion_status(sid, f"slot_{i}", "done", rows_count=i + 10,
                                 token=f"token_{i}", readback={"label": f"file_{i}"})
    except Exception as exc:
        errors.put(str(exc))


with patch.object(db, "get_conversion_status", _overlapping_read):
    threads = [threading.Thread(target=_write_slot, args=(i,), daemon=True) for i in range(N)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

_check("all writers finish", all(not thread.is_alive() for thread in threads))
_check("no writer raises", errors.empty())
current = real_get(sid)
present = sum(f"slot_{i}" in current for i in range(N))
_check(f"all concurrent slots survive ({present}/{N})", present == N)
_check("pre-existing slot survives", current.get("existing") == {
    "status": "done", "rows": 5, "error": ""})
_check("each slot retains its own rows, token and readback", all(
    current.get(f"slot_{i}") == {"status": "done", "rows": i + 10, "error": "",
                               "token": f"token_{i}", "readback": {"label": f"file_{i}"}}
    for i in range(N)))

sys.exit(1 if _FAILED else 0)
