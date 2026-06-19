"""Proof for the operator usage view (run health + visibility).

Covers:
  1. usage.classify_complete_run / classify_session names the real outcome of a
     run (healthy / blank / incomplete / recs_failed / failed) — including the
     silent "blank" case the DB calls 'complete'.
  2. usage.should_alert pages the operator on real fires (blank, failed) but NOT
     on a polite 'refused' (bad data declined) or partial results.
  3. usage.org_run_summary aggregates per-org counts.
  4. emails._send_run_failure_alert composes + sends only when configured, and
     goes to ALERT_EMAIL (the operator).
  5. End-to-end: the /admin/usage page renders, classifies seeded runs correctly,
     records last_login on sign-in, and shows "logged in, never ran" clients.

Dependency-free:  python tests/test_usage_insights.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_usage.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

# app.py imports anthropic at module load — stub it so the import is side-effect free.
if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import json                              # noqa: E402
import database as db                    # noqa: E402
import usage                             # noqa: E402
import emails                            # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. Classification of a 'complete' run ────────────────────────────────────
healthy = usage.classify_complete_run(
    [{"item": "A", "status": "OK"}, {"item": "B", "status": "LOW"}],
    [{"item": "A", "qty": 5}],
    [],
)
_check("healthy run -> healthy", healthy["category"] == usage.HEALTHY, detail=str(healthy))
_check("healthy run counts items + recs", healthy["items"] == 2 and healthy["recs"] == 1, detail=str(healthy))

# Items present, zero recommendations, no errors = legitimately well stocked.
well_stocked = usage.classify_complete_run([{"item": "A"}], [], [])
_check("items + no recs needed -> healthy (NOT a failure)",
       well_stocked["category"] == usage.HEALTHY, detail=str(well_stocked))

blank = usage.classify_complete_run([], [], [])
_check("zero items -> blank (the silent failure)", blank["category"] == usage.BLANK, detail=str(blank))

incomplete = usage.classify_complete_run(
    [{"item": "A"}],
    [{"item": "A"}],
    ["The AI's reply was cut short on at least one batch, so some items may be missing."],
)
_check("truncated reply -> incomplete", incomplete["category"] == usage.INCOMPLETE, detail=str(incomplete))

recs_failed = usage.classify_complete_run(
    [{"item": "A"}, {"item": "B"}],
    [{"item": "A", "error": "boom"}, {"item": "B", "error": "boom"}],
    [],
)
_check("items but every rec errored -> recs_failed",
       recs_failed["category"] == usage.RECS_FAILED, detail=str(recs_failed))

# Dict-wrapped inventory ({"report": [...]}) is handled.
wrapped = usage.classify_complete_run({"report": [{"item": "A"}]}, [], [])
_check("dict-wrapped inventory is unwrapped", wrapped["items"] == 1, detail=str(wrapped))

# classify_session reads raw DB values and survives NULL / garbage JSON.
_check("status=failed -> failed",
       usage.classify_session("failed", None, None, None)["category"] == usage.FAILED)
_check("status=analyzing -> running",
       usage.classify_session("analyzing", None, None, None)["category"] == usage.RUNNING)
_check("status=uploading -> pending",
       usage.classify_session("uploading", None, None, None)["category"] == usage.PENDING)
_check("garbage JSON doesn't crash -> blank",
       usage.classify_session("complete", "{not json", "also bad", None)["category"] == usage.BLANK)


# ── 2. Alert gating ──────────────────────────────────────────────────────────
_check("alert on blank",   usage.should_alert(usage.BLANK) is True)
_check("alert on failed",  usage.should_alert(usage.FAILED) is True)
_check("NO alert on refused (bad data is a nudge, not a fire)",
       usage.should_alert(usage.REFUSED) is False)
_check("NO alert on healthy", usage.should_alert(usage.HEALTHY) is False)
_check("NO alert on incomplete (partial, not nothing)",
       usage.should_alert(usage.INCOMPLETE) is False)
_check("NO alert on recs_failed", usage.should_alert(usage.RECS_FAILED) is False)


# ── 3. Per-org aggregation ───────────────────────────────────────────────────
summary = usage.org_run_summary([
    {"category": usage.HEALTHY}, {"category": usage.HEALTHY},
    {"category": usage.BLANK}, {"category": usage.FAILED},
    {"category": usage.INCOMPLETE},
])
_check("summary totals", summary["total"] == 5, detail=str(summary))
_check("summary counts each bucket",
       summary["healthy"] == 2 and summary["blank"] == 1
       and summary["failed"] == 1 and summary["incomplete"] == 1, detail=str(summary))


# ── 4. Operator failure-alert email ──────────────────────────────────────────
_sent = []
_orig_deliver = emails._deliver


def _capture(msg, sender, pw, to):
    # get_payload(decode=True) base64-decodes the body; the email lib encodes it
    # automatically because the message contains a non-ASCII em dash.
    body = msg.get_payload(decode=True)
    body = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    _sent.append((msg["Subject"], to, body))
    return True


emails._deliver = _capture
_orig_env = {k: os.environ.get(k) for k in ("ALERT_EMAIL", "MAIL_SENDER", "MAIL_APP_PASSWORD")}
try:
    os.environ["ALERT_EMAIL"]       = "founder@example.com"
    os.environ["MAIL_SENDER"]       = "alerts@example.com"
    os.environ["MAIL_APP_PASSWORD"] = "pw"

    emails._send_run_failure_alert("a regional food distributor", 7, "blank", "", "https://berthcast.com")
    _check("blank alert sends to ALERT_EMAIL",
           len(_sent) == 1 and _sent[0][1] == "founder@example.com", detail=str(_sent))
    _check("blank alert names the client + reason",
           "a regional food distributor" in _sent[0][0] and "BLANK" in _sent[0][2], detail=str(_sent[0]))

    _sent.clear()
    os.environ.pop("ALERT_EMAIL")
    emails._send_run_failure_alert("a regional food distributor", 7, "failed", "kaboom", "https://berthcast.com")
    _check("no ALERT_EMAIL configured -> no send, no crash", _sent == [], detail=str(_sent))
finally:
    emails._deliver = _orig_deliver
    for k, v in _orig_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── 5. End-to-end: the /admin/usage page ─────────────────────────────────────
from werkzeug.security import generate_password_hash   # noqa: E402
import app as appmod                                    # noqa: E402

db.init_db()
appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True


def _mk_user(email, org, is_admin=0):
    return db.execute(
        "INSERT INTO users (email, password_hash, org_name, model, is_admin, email_verified, role) "
        "VALUES (?,?,?,?,?,1,'admin')",
        (email, generate_password_hash("password123"), org, "claude-haiku-4-5-20251001", is_admin),
    )


def _mk_run(org, status, inventory=None, recs=None, notes=None):
    sid = db.execute(
        "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
        (1, org, status),
    )
    if status == "complete":
        db.execute(
            "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json, data_notes) "
            "VALUES (?,?,?,?)",
            (sid, json.dumps(inventory or []), json.dumps(recs or []), json.dumps(notes or [])),
        )
    return sid


_admin_id = _mk_user("ops@berthcast.com", "BerthAI Ops", is_admin=1)
_mk_user("buyer@example.com", "a regional food distributor")
_mk_user("nobody@dormant.com", "DormantCo")     # logs in, never runs anything

# a regional food distributor: one healthy, one blank, one failed.
_mk_run("a regional food distributor", "complete",
        inventory=[{"item": "Frozen Peas", "status": "OK"}, {"item": "Rice", "status": "LOW"}],
        recs=[{"item": "Rice", "qty": 100}])
_mk_run("a regional food distributor", "complete", inventory=[], recs=[])      # blank
_mk_run("a regional food distributor", "failed")

# DupCo: ONE complete analysis that ended up with TWO analysis_results rows —
# what a double-submitted dedup form leaves behind (INSERT OR REPLACE can't
# replace without a UNIQUE on session_id). The page must count it as ONE run.
_mk_user("ops2@dup.com", "DupCo")               # never logs in -> sorts to the bottom
_dup_sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (1, "DupCo", "complete"),
)
for _ in range(2):
    db.execute(
        "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json, data_notes) "
        "VALUES (?,?,?,?)",
        (_dup_sid, json.dumps([{"item": "X", "status": "OK"}]), json.dumps([{"item": "X"}]), json.dumps([])),
    )

client = appmod.app.test_client()

# Admin signs in — this should also stamp last_login.
r = client.post("/login", data={"email": "ops@berthcast.com", "password": "password123"},
                follow_redirects=False)
_check("admin login succeeds (redirect)", r.status_code in (302, 303), detail=str(r.status_code))
_ll = db.query("SELECT last_login FROM users WHERE id=?", (_admin_id,))
_check("login records last_login", bool(_ll and _ll[0]["last_login"]), detail=str(_ll))

# DormantCo signs in but never runs anything.
client.get("/logout")
client.post("/login", data={"email": "nobody@dormant.com", "password": "password123"})
client.get("/logout")
# Back to admin.
client.post("/login", data={"email": "ops@berthcast.com", "password": "password123"})

page = client.get("/admin/usage")
_check("usage page loads (200)", page.status_code == 200, detail=str(page.status_code))
body = page.get_data(as_text=True)
_check("page shows the client org", "a regional food distributor" in body)
_check("page surfaces a blank run", "Blank" in body, detail="expected a Blank badge")
_check("page surfaces a failed run", "Failed" in body)
_check("page surfaces a healthy run", "Healthy" in body)
_check("page flags 'logged in, never ran' client",
       "DormantCo" in body and "never ran an analysis" in body)

# Issue #1 fix: a session with duplicate analysis_results rows counts as ONE run.
# Isolate DupCo's card (from its name to the next org card, or end of page).
_start = body.find("DupCo")
_next = body.find("usage-org-name", _start + 1)
_dup_card = body[_start:_next] if _next != -1 else body[_start:]
_check("duplicate result rows counted as ONE run (not two)",
       "1 run" in _dup_card and "2 runs" not in _dup_card,
       detail=_dup_card[:160])

# A non-admin must not reach it.
client.get("/logout")
client.post("/login", data={"email": "buyer@example.com", "password": "password123"})
denied = client.get("/admin/usage", follow_redirects=False)
_check("non-admin is blocked from the usage page",
       denied.status_code in (302, 303, 403), detail=str(denied.status_code))


print()
if _FAILED:
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS")
