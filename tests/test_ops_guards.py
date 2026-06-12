"""Regression tests for the ops guards + prompt adaptability (Tier 1C + 2).

Covers, from the client-#2 readiness audit:
  - Disk-full guard: uploads and analyses are refused with a plain message
    when the data disk is nearly full (a full disk mid-analysis used to die
    with a cryptic SQLite I/O error).
  - Stale upload-chunk sweep: tmp_* litter from interrupted uploads is
    removed at boot once it's a day old; fresh chunks and other files stay.
  - Backup failure alerting: run_once calls the on_failure hook (guarded —
    a broken alerter can't break backups), and the app-side email alert
    sends at most once per day and only when configured.
  - Settings: admins can set industry + company description; non-admins and
    bogus industries are rejected.
  - Prompt adaptability: the column-mapping prompt no longer says "food";
    the recommendation example matches the org's industry; no client name
    is baked into any other org's prompt.

Run: python tests/test_ops_guards.py
"""
import os
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_opsguards.db")
for ext in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp_db + ext)
    except FileNotFoundError:
        pass
os.environ["DB_PATH"] = _tmp_db
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db                # noqa: E402
import backup                        # noqa: E402
import rate_limit                    # noqa: E402
import agents.shared as shared       # noqa: E402
import agents.recommendation as rec_mod   # noqa: E402
import app as appmod                 # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


db.init_db()
appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
os.makedirs(appmod.UPLOAD_FOLDER, exist_ok=True)


# ── 1. Disk guard: unit ──────────────────────────────────────────────────────
_orig_disk_usage = appmod.shutil.disk_usage
try:
    appmod.shutil.disk_usage = lambda p: types.SimpleNamespace(
        total=10**9, used=10**9 - 10 * 1024 * 1024, free=10 * 1024 * 1024)
    _check("10MB free -> no room", appmod._disk_has_room() is False)
    appmod.shutil.disk_usage = lambda p: types.SimpleNamespace(
        total=10**9, used=0, free=10**9)
    _check("1GB free -> room", appmod._disk_has_room() is True)

    def _boom(p):
        raise OSError("stat failed")
    appmod.shutil.disk_usage = _boom
    _check("measurement failure never blocks users", appmod._disk_has_room() is True)
finally:
    appmod.shutil.disk_usage = _orig_disk_usage

# ── 2. Disk guard: routes refuse work when full ──────────────────────────────
SID = 701
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "DiskOrg", "uploading", "all", "{}"))

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"] = 1
    s["email"] = "u@diskorg.com"
    s["org_name"] = "DiskOrg"
    s["model"] = "claude-sonnet-4-6"
    s["is_admin"] = True
    s["tier"] = "enterprise"
    s["role"] = "admin"

_orig_room = appmod._disk_has_room
appmod._disk_has_room = lambda: False
try:
    r = client.post("/upload", data={"slot": "inventory"})
    body = r.get_json() or {}
    _check("full disk: upload refused with plain message",
           body.get("ok") is False and "storage is full" in body.get("error", ""),
           detail=str(body))
    rate_limit._hits.clear()
    r = client.get(f"/analyse/{SID}", follow_redirects=False)
    _check("full disk: analyse refused (redirect, no run started)",
           r.status_code == 302 and "/dashboard" in r.headers.get("Location", ""),
           detail=str(r.status_code))
finally:
    appmod._disk_has_room = _orig_room

# ── 3. Stale chunk sweep ─────────────────────────────────────────────────────
_old_chunk  = os.path.join(appmod.UPLOAD_FOLDER, "tmp_testupload_0")
_new_chunk  = os.path.join(appmod.UPLOAD_FOLDER, "tmp_testupload_1")
_real_file  = os.path.join(appmod.UPLOAD_FOLDER, "9999_inventory_keep.xlsx")
for p in (_old_chunk, _new_chunk, _real_file):
    with open(p, "w") as f:
        f.write("x")
_two_days_ago = time.time() - 2 * 86400
os.utime(_old_chunk, (_two_days_ago, _two_days_ago))
os.utime(_real_file, (_two_days_ago, _two_days_ago))

swept = appmod._sweep_stale_chunks()
_check("old tmp chunk swept", swept == 1 and not os.path.exists(_old_chunk),
       detail=f"swept={swept}")
_check("fresh tmp chunk kept", os.path.exists(_new_chunk))
_check("old NON-chunk upload file untouched", os.path.exists(_real_file))
for p in (_new_chunk, _real_file):
    os.remove(p)

# ── 4. Backup on_failure hook ────────────────────────────────────────────────
_fail_msgs = []
_bad_db = os.path.join(tempfile.gettempdir(), "no_such_dir_xyz")  # a directory path, not a db
os.makedirs(_bad_db, exist_ok=True)
res = backup.run_once(_bad_db, os.path.join(tempfile.gettempdir(), "bk_out"),
                      logger=lambda m: None, on_failure=_fail_msgs.append)
_check("backup failure calls on_failure with the error", res is None and len(_fail_msgs) == 1,
       detail=str(_fail_msgs))


def _broken_alert(msg):
    raise RuntimeError("alerter is down")


res = backup.run_once(_bad_db, os.path.join(tempfile.gettempdir(), "bk_out"),
                      logger=lambda m: None, on_failure=_broken_alert)
_check("broken alerter never breaks the backup loop", res is None)

_ok_dir = os.path.join(tempfile.gettempdir(), "bk_ok_src")
_ok_db  = os.path.join(_ok_dir, "src.db")
os.makedirs(_ok_dir, exist_ok=True)
import sqlite3 as _sq
_c = _sq.connect(_ok_db); _c.execute("CREATE TABLE IF NOT EXISTS t (x)"); _c.commit(); _c.close()
_fail_msgs.clear()
# force=True: the temp output dir persists between test runs, and a leftover
# fresh snapshot would (correctly) make a scheduled run skip — we're proving
# the write path here, not the restart-storm guard (tests/test_backup.py does).
res = backup.run_once(_ok_db, os.path.join(tempfile.gettempdir(), "bk_out2"),
                      logger=lambda m: None, on_failure=_fail_msgs.append, force=True)
_check("successful backup never calls on_failure", res is not None and not _fail_msgs)

# ── 5. App-side backup alert: configured, throttled ──────────────────────────
_sent = []
_orig_deliver = appmod._deliver_email
appmod._deliver_email = lambda msg, sender, pw, to: _sent.append((msg["Subject"], to)) or True
_orig_env = {k: os.environ.get(k) for k in ("ALERT_EMAIL", "MAIL_SENDER", "MAIL_APP_PASSWORD")}
try:
    os.environ["ALERT_EMAIL"] = "founder@example.com"
    os.environ["MAIL_SENDER"] = "alerts@example.com"
    os.environ["MAIL_APP_PASSWORD"] = "pw"
    appmod._last_backup_alert["t"] = 0.0
    appmod._backup_failure_alert("disk full")
    appmod._backup_failure_alert("disk full again")
    _check("alert emailed once then throttled for the day",
           len(_sent) == 1 and _sent[0][1] == "founder@example.com", detail=str(_sent))

    os.environ.pop("ALERT_EMAIL")
    appmod._last_backup_alert["t"] = 0.0
    _sent.clear()
    appmod._backup_failure_alert("disk full")
    _check("no ALERT_EMAIL configured -> no send, no crash", _sent == [])
finally:
    appmod._deliver_email = _orig_deliver
    for k, v in _orig_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# ── 6. Settings: industry + description ──────────────────────────────────────
r = client.post("/settings", data={"action": "save_company",
                                   "industry": "fmcg",
                                   "company_description": "Distributes household goods in SG."},
                follow_redirects=False)
cfg = db.get_company_config("DiskOrg")
_check("admin saves industry", cfg.get("industry") == "fmcg", detail=str(cfg.get("industry")))
_check("admin saves description", "household goods" in (cfg.get("company_description") or ""))

r = client.post("/settings", data={"action": "save_company", "industry": "lol",
                                   "company_description": "x"})
cfg = db.get_company_config("DiskOrg")
_check("bogus industry rejected", cfg.get("industry") == "fmcg", detail=str(cfg.get("industry")))

with client.session_transaction() as s:
    s["role"] = "reviewer"
r = client.post("/settings", data={"action": "save_company", "industry": "general",
                                   "company_description": "hijack"})
cfg = db.get_company_config("DiskOrg")
_check("non-admin cannot change company details", cfg.get("industry") == "fmcg")
with client.session_transaction() as s:
    s["role"] = "admin"

# ── 7. Prompt adaptability ───────────────────────────────────────────────────
_captured = {"system": ""}


def _fake_claude(model, system, user, max_tokens=4096):
    _captured["system"] = system
    return "[]"


shared._call_claude = _fake_claude
shared.propose_inventory_columns(["item_name", "qty"], [{"item_name": "EDAM", "qty": "5"}], "m")
_check("column-mapping prompt no longer says food",
       "food" not in _captured["system"].lower())
_check("column-mapping prompt says distributors",
       "distributor" in _captured["system"].lower())

# Recommendation example follows the org's industry; never names another client.
rec_mod._call_claude = _fake_claude
_REPORT = [{"item": "WIDGET", "category": "PARTS", "stock": 0, "status": "CRITICAL",
            "spoilage_risk": "NONE", "days_of_supply": 2, "observation": "t"}]

for sid, org, industry in ((702, "FoodOrg", "food_distribution"),
                           (703, "GenOrg", "general")):
    db.execute(
        "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
        "VALUES (?,?,?,?,?,?)", (sid, 1, org, "complete", "all", "{}"))
    db.execute(f'CREATE TABLE inventory_{sid} ("item_name" TEXT, "qty" TEXT, "uom" TEXT)')
    db.execute(f'CREATE TABLE sales_{sid} ("item_name" TEXT, "qty" TEXT, "date" TEXT)')
    db.upsert_company_config(org, industry=industry)

_captured["system"] = ""
rec_mod.run_recommendation_agent(702, "m", list(_REPORT), {}, None)
_food_sys = _captured["system"]
_check("food org keeps the food-flavoured example", "frozen salmon" in _food_sys)
_check("food org example does not name Cool Link", "Cool Link" not in _food_sys)

_captured["system"] = ""
rec_mod.run_recommendation_agent(703, "m", list(_REPORT), {}, None)
_gen_sys = _captured["system"]
_check("general org gets a neutral example (no salmon)", "salmon" not in _gen_sys)
_check("general org prompt never names Cool Link", "Cool Link" not in _gen_sys)
_check("general org prompt still demands consequences",
       "consequence_if_acting" in _gen_sys)

# ── 8. Confidence is data quality, not supplier presence ────────────────────
# 12 June: orgs with no supplier records (every demo/dummy run) got N/A on
# EVERY recommendation, because the prompt forced INSUFFICIENT_DATA whenever
# the supplier was unknown. Unknown supplier = supplier_risk, not data gap.
import rec_logic  # noqa: E402

_check("prompt no longer forces INSUFFICIENT_DATA on unknown supplier",
       "INSUFFICIENT_DATA if supplier is not known" not in _gen_sys)
_check("prompt ties confidence to stock/velocity data quality",
       "does NOT force INSUFFICIENT_DATA" in _gen_sys)
_check("unknown supplier still raises supplier_risk",
       "supplier_risk = HIGH" in _gen_sys and "supplier unknown" in _gen_sys)

# End-to-end: model answers HIGH for an item whose supplier is NOT in the
# system — the pipeline must deliver HIGH to the page, not N/A.
_HIGH_REC = ('[{"item": "WIDGET", "supplier": "Unknown", "supplier_type": "other", '
             '"lead_time_days": null, "days_of_supply": 2, '
             '"recommended_action": "REORDER", "suggested_quantity": 100, '
             '"confidence": "HIGH", "consequence_if_acting": "a", '
             '"consequence_if_not_acting": "b", "supplier_risk": "HIGH", '
             '"mitigation": "verify supplier", "flags": [], "reason": "r"}]')
rec_mod._call_claude = lambda model, system, user, max_tokens=4096: _HIGH_REC
_recs = rec_mod.run_recommendation_agent(703, "m", list(_REPORT), {}, None)
_check("agent returns the rec", len(_recs) == 1 and _recs[0].get("item") == "WIDGET",
       detail=str(_recs)[:200])
rec_logic._normalise_confidence(_recs[0])
_check("HIGH confidence survives the pipeline despite unknown supplier",
       _recs[0].get("confidence") == "HIGH", detail=str(_recs[0].get("confidence")))
_check("results page would NOT show N/A for this rec",
       _recs[0].get("confidence") != "INSUFFICIENT_DATA")
_junk = {"confidence": "garbage"}
rec_logic._normalise_confidence(_junk)
_check("junk confidence still normalises to INSUFFICIENT_DATA (guard intact)",
       _junk["confidence"] == "INSUFFICIENT_DATA")


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll ops-guard tests passed.")
