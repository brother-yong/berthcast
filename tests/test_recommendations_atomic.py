"""Proof for the concurrent-write fix.

Every approve/dismiss/edit/outcome action used to read the whole
recommendations_json blob, change one thing in Python, then write the blob back —
on three separate DB connections with a gap in between. Two teammates acting on
the same results page at once would both read the same blob and both write it,
and the second write silently wiped the first (a lost update). The outcome
tracking — the proof data — was the most likely thing to vanish.

database.update_recommendations now does the read-modify-write inside one
BEGIN IMMEDIATE transaction, so concurrent callers serialise instead of clobber.

This test fires many concurrent updates, each touching a different item, and
proves every change survives. Dependency-free:
    python tests/test_recommendations_atomic.py
"""
import os
import sys
import json
import tempfile
import threading

# Throwaway DB + not-on-Render BEFORE importing app modules.
_TMPDIR = tempfile.mkdtemp(prefix="berth_atomic_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


db.init_db()

# ---------------------------------------------------------------------------
# 1. Concurrency — N threads each approve a different item; none must be lost.
# ---------------------------------------------------------------------------
N = 25
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (1, "Test Org", "complete")
)
recs = [{"item": f"Item {i}", "approved": False} for i in range(N)]
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid, "[]", json.dumps(recs))
)


def _approve(i):
    def _m(rs):
        for r in rs:
            if r.get("item") == f"Item {i}":
                r["approved"] = True
                return {"updated": True}
        return {"updated": False}
    db.update_recommendations(sid, _m)


threads = [threading.Thread(target=_approve, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()

final = json.loads(
    db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (sid,))[0]["recommendations_json"]
)
approved = sum(1 for r in final if r.get("approved"))
_check(f"all {N} concurrent approvals survived (no lost update)", approved == N, detail=f"got {approved}")

# ---------------------------------------------------------------------------
# 2. Error handling — missing session and corrupt JSON.
# ---------------------------------------------------------------------------
res = db.update_recommendations(999999, lambda rs: {"updated": False})
_check("missing session returns ok=False, code=404",
       res["ok"] is False and res["code"] == 404, detail=str(res))

sid2 = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (1, "Test Org", "complete")
)
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
    (sid2, "[]", "{not valid json")
)
res = db.update_recommendations(sid2, lambda rs: {"updated": True})
_check("corrupt recommendations_json returns ok=False, code=500",
       res["ok"] is False and res["code"] == 500, detail=str(res))

# ---------------------------------------------------------------------------
# 3. Success path passes the mutator's payload straight back to the caller.
# ---------------------------------------------------------------------------
res = db.update_recommendations(sid, lambda rs: {"updated": True, "n": len(rs)})
_check("success returns ok=True and the mutator payload",
       res["ok"] and res["result"]["n"] == N, detail=str(res))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll atomic-recommendations tests passed.")
