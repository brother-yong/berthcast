"""Report deadlines stay anchored to the saved analysis, across all outputs."""
import csv
import io
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="berthcast_asof_"), "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
os.environ.setdefault("SECRET_KEY", "test-secret-not-used")
stub = types.ModuleType("anthropic")
stub.Anthropic = lambda *a, **k: None
stub.AnthropicError = Exception
sys.modules["anthropic"] = stub

import database as db
import app as appmod
from rec_logic import _compute_order_by

appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
_FAILED = False


def _check(name, cond):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        _FAILED = True


REC = {"item": "BROOKVALE MILK 1L", "days_of_supply": 30,
       "lead_time_days": 20, "supplier": "NORDVIK", "approved": True,
       "suggested_quantity": "60 CTN", "avg_monthly_sales": 30,
       "confidence": "HIGH"}
uid = db.execute(
    "INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
    ("admin@brookvale.example", "unused", "BROOKVALE", "claude-sonnet-5"))
sid = db.execute(
    "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
    (uid, "BROOKVALE", "done"))
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json, created_at) "
    "VALUES (?,?,?,?)", (sid, "[]", json.dumps([REC]), "2025-09-01 12:30:00"))
client = appmod.app.test_client()
with client.session_transaction() as s:
    s.update(user_id=uid, email="admin@brookvale.example", org_name="BROOKVALE",
             model="claude-sonnet-5", tier="enterprise", role="admin", sv=0)

# Dropping the date at ANY call site must fail, even if the helper is correct.
for suffix in ("", "/print", "/export.csv"):
    response = client.get(f"/results/{sid}{suffix}")
    _check(f"{suffix or 'results'} renders", response.status_code == 200)
    body = response.get_data(as_text=True)
    if suffix == "/export.csv":
        row = next(csv.DictReader(io.StringIO(body)))
        _check("CSV uses the saved deadline", row["Order By"] == "11 Sep 2025")
    else:
        _check(f"{suffix or 'results'} uses the saved deadline", "11 Sep 2025" in body)

for dos, buffer, status, first_date, second_date in (
    (30, 10, "ok", "11 Sep 2025", "11 Oct 2025"),
    (23, 3, "urgent", "04 Sep 2025", "04 Oct 2025"),
    (18, -2, "overdue", "30 Aug 2025", "29 Sep 2025"),
):
    rec = dict(REC, days_of_supply=dos)
    try:
        first = _compute_order_by(rec, as_of=datetime(2025, 9, 1, 12, 30))
        second = _compute_order_by(rec, as_of="2025-10-01 12:30:00")
    except TypeError as exc:
        _check(f"{status}: accepts an analysis date ({exc})", False)
        continue
    _check(f"{status}: date changes with the analysis date",
           first["order_by_date"] == first_date and second["order_by_date"] == second_date)
    _check(f"{status}: buffer and status stay unchanged",
           first["buffer_days"] == second["buffer_days"] == buffer
           and first["status"] == second["status"] == status)

for value in (None, "", "not a date"):
    before = (datetime.utcnow() + timedelta(days=10)).strftime("%d %b %Y")
    try:
        result = _compute_order_by(REC, as_of=value)
        after = (datetime.utcnow() + timedelta(days=10)).strftime("%d %b %Y")
        _check(f"bad date {value!r} falls back to today",
               result["order_by_date"] in (before, after) and result["status"] == "ok")
    except Exception as exc:
        _check(f"bad date {value!r} must not raise ({exc})", False)

db.execute("UPDATE analysis_results SET created_at=? WHERE session_id=?", ("not a date", sid))
for suffix in ("", "/print", "/export.csv"):
    _check(f"bad stored date keeps {suffix or 'results'} readable",
           client.get(f"/results/{sid}{suffix}").status_code == 200)

sys.exit(1 if _FAILED else 0)
