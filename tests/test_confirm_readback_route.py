"""POST /upload/confirm-readback: exists, login-gated, and flips a
needs_confirm sales slot to done while preserving the read-back."""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app as appmod  # noqa: E402
import database as db  # noqa: E402

F = []


def _check(c, m):
    if not c:
        F.append(m)


appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
client = appmod.app.test_client()

# 1) logged out -> login_required redirects (route exists + is gated).
r = client.post("/upload/confirm-readback", json={"slot": "sales", "session_id": 1})
_check(r.status_code != 404, f"route must exist, got {r.status_code}")
_check(r.status_code == 302, f"logged-out must redirect to login, got {r.status_code}")

# 2) owner with a needs_confirm sales slot -> flips to done, keeps read-back.
SID = 947
db.execute("DELETE FROM upload_sessions WHERE id=?", (SID,))
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)",
    (SID, 1, "V2Org", "uploading", "all", "{}"),
)
rb = {"items": 149, "months_kept": [1, 2, 3, 4, 5, 6], "months_dropped": [7, 8, 9, 10, 11, 12],
      "assumed_year": 2026, "total_units": 4015509.7, "tier": "degraded",
      "question": "We kept June as real sales..."}
db.set_conversion_status(SID, "sales", "needs_confirm", rows_count=891, readback=rb)

with client.session_transaction() as s:
    s["user_id"] = 1
    s["org_name"] = "V2Org"
    s["email"] = "u@v2org.com"

r = client.post("/upload/confirm-readback", json={"slot": "sales", "session_id": SID})
_check(r.status_code == 200, f"owner confirm should be 200, got {r.status_code}")
_check(r.get_json().get("ok") is True, "confirm should return ok:true")

after = db.get_conversion_status(SID).get("sales", {})
_check(after.get("status") == "done", f"slot should be done after confirm, got {after.get('status')}")
_check(after.get("rows") == 891, "row count preserved")
_check(after.get("readback", {}).get("question") == rb["question"], "read-back preserved")

# 3) confirming again (already done, not needs_confirm) -> 409.
r = client.post("/upload/confirm-readback", json={"slot": "sales", "session_id": SID})
_check(r.status_code == 409, f"double-confirm should be 409, got {r.status_code}")

# 4) a DIFFERENT org must NOT be able to confirm this org's slot.
SID2 = 948
db.execute("DELETE FROM upload_sessions WHERE id=?", (SID2,))
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)",
    (SID2, 1, "V2Org", "uploading", "all", "{}"),
)
db.set_conversion_status(SID2, "sales", "needs_confirm", rows_count=891, readback=rb)
# The other-org user must really exist, or login_required revokes the session
# before the cross-org guard runs (session_version check needs a live row).
db.execute("DELETE FROM users WHERE email=?", ("x@otherorg.com",))
other_uid = db.execute(
    "INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
    ("x@otherorg.com", "x", "OtherOrg", "claude-sonnet-4-6"))
with client.session_transaction() as s:
    s["user_id"] = other_uid
    s["org_name"] = "OtherOrg"
    s["email"] = "x@otherorg.com"
r = client.post("/upload/confirm-readback", json={"slot": "sales", "session_id": SID2})
_check(r.status_code in (403, 404), f"cross-org confirm must be blocked, got {r.status_code}")
still = db.get_conversion_status(SID2).get("sales", {})
_check(still.get("status") == "needs_confirm", "cross-org attempt must NOT flip the slot")
db.execute("DELETE FROM users WHERE id=?", (other_uid,))
db.execute("DELETE FROM upload_sessions WHERE id=?", (SID2,))

db.execute("DELETE FROM upload_sessions WHERE id=?", (SID,))

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All confirm-readback route tests passed.")
