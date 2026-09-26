"""Malformed inventory input and access boundaries for print and CSV.

Run with: python tests/test_inventory_health_sheet_break.py
"""
import copy
import csv
import html as html_lib
import io
import json
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="berth_inventory_break_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")

    class _AnthropicStub:
        def __init__(self, *args, **kwargs):
            pass

    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import database as db  # noqa: E402
import app as appmod  # noqa: E402
from rec_logic import inventory_number_display, sort_inventory_items  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

appmod.app.config["TESTING"] = True
appmod.app.config["WTF_CSRF_ENABLED"] = False
_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name
          + (f" [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _row(item, **fields):
    row = {"item": item, "category": "DRY", "stock": "38",
           "status": "CRITICAL", "spoilage_risk": "NONE", "days_of_supply": 10,
           "observation": "Check current stock."}
    row.update(fields)
    return row


A = _row("BROOKVALE PRAWN MEAT 400G", spoilage_risk="HIGH", days_of_supply=0)
B = _row("NORDVIK COD FILLET 1KG", status="LOW", spoilage_risk="MEDIUM", days_of_supply=20)
C = _row("PADIMAS JASMINE RICE 5KG", status="HEALTHY", days_of_supply=60)
BASE = [C, B, A]
DEFAULT_ITEMS = [A["item"], B["item"], C["item"]]
ORG = "Inventory Break Org"
EMAIL = "inventory-break@example.com"
MODEL = "claude-sonnet-4-6"
db.execute(
    "INSERT INTO users (email, password_hash, org_name, model, tier, email_verified, role) "
    "VALUES (?,?,?,?,?,?,?)",
    (EMAIL, generate_password_hash("x"), ORG, MODEL, "enterprise", 1, "admin"),
)
UID = db.query("SELECT id FROM users WHERE email=?", (EMAIL,))[0]["id"]
SID = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
                 (UID, ORG, "complete"))
RIVAL_SID = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
                       (UID + 999, "Rival Test Org", "complete"))
EMPTY_SID = db.execute("INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
                       (UID, ORG, "complete"))
RIVAL_ITEM = "NORDVIK RIVAL OAT DRINK 1L"
for sid, rows in ((SID, BASE), (RIVAL_SID, [_row(RIVAL_ITEM)])):
    db.execute(
        "INSERT INTO analysis_results "
        "(session_id, inventory_report, recommendations_json, created_at) VALUES (?,?,?,?)",
        (sid, json.dumps(rows), "[]", "2026-09-22 17:30:00"),
    )


def _client(tier="enterprise", trial_ends_at=None, org_name=ORG):
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=UID, email=EMAIL, org_name=org_name, model=MODEL,
                       is_admin=False, tier=tier, role="admin", sv=0,
                       trial_ends_at=trial_ends_at)
    return client


CLIENT = _client()
SUFFIXES = ("inventory/print", "inventory.csv")


def _path(suffix, sid=SID):
    return f"/results/{sid}/{suffix}"


def _save(report, raw=False):
    db.execute("UPDATE analysis_results SET inventory_report=? WHERE session_id=?",
               (report if raw else json.dumps(report), SID))


def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.get_data(as_text=True))))


def _csv_items(response):
    return [row[1] for row in _csv_rows(response)[1:] if len(row) == 8]


def _print_items(document):
    body = re.search(r"<tbody>(.*?)</tbody>", document, flags=re.DOTALL)
    if not body:
        return []
    result = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", body.group(1), flags=re.DOTALL):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.DOTALL)
        if len(cells) == 8:
            result.append(html_lib.unescape(cells[1]))
    return result


def _positive(suffix, label, client=CLIENT):
    response = client.get(_path(suffix))
    _check(label + ": own session is readable",
           response.status_code == 200 and A["item"] in response.get_data(as_text=True))
    return response


print("-- 1: invalid settings fall back to the default list --")
bad_queries = (
    ("unknown sort", [("sort", "DROP TABLE")]),
    ("unknown direction", [("dir", "sideways")]),
    ("unknown status", [("show", "EVIL")]),
    ("lower-case status", [("show", "critical")]),
    ("no statuses", []),
    ("5000 invalid statuses", [("show", "EVIL")] * 5000),
    ("script in settings", [("sort", "<script>alert(1)</script>"),
                            ("dir", "<script>alert(1)</script>"),
                            ("show", "<script>alert(1)</script>")]),
)
for label, query in bad_queries:
    print_response = CLIENT.get(_path("inventory/print"), query_string=query)
    csv_response = CLIENT.get(_path("inventory.csv"), query_string=query)
    document = print_response.get_data(as_text=True)
    _check(label + ": print returns default rows",
           print_response.status_code == 200 and _print_items(document) == DEFAULT_ITEMS)
    _check(label + ": CSV returns default rows",
           csv_response.status_code == 200 and _csv_items(csv_response) == DEFAULT_ITEMS)
    _check(label + ": header reports validated settings",
           all(text in document for text in (
               "Analysis run: 23/09/2026", "3 item(s)",
               "Sorted by: Spoilage risk, high to low",
               "Showing: Critical, Running low, Well stocked")))
    _check(label + ": unvalidated script is absent",
           "<script>alert(1)</script>" not in document
           and "&lt;script&gt;alert(1)&lt;/script&gt;" not in document)

print("-- 2: missing sort values always follow known values --")
sort_cases = (
    ("spoilage", "spoilage_risk", ["NONE", "HIGH", "SEVERE", None, ""]),
    ("status", "status", ["DEAD", "CRITICAL", "PENDING", 7, None]),
    ("days", "days_of_supply", [0, 20, "N/A", ""]),
    ("stock", "stock", [0, 20, "N/A", ""]),
    ("item", "item", ["BROOKVALE A", "PADIMAS Z", "", None]),
    ("category", "category", ["CHILL", "FROZEN", "", None]),
)
for sort_key, field, values in sort_cases:
    rows = [dict(_row(f"KESSINGTON TEST {index}"), **{field: value, "_id": index})
            for index, value in enumerate(values)]
    original = copy.deepcopy(rows)
    for direction, known in (("asc", [0, 1]), ("desc", [1, 0])):
        sorted_rows = sort_inventory_items(rows, sort_key, direction)
        ids = [row["_id"] for row in sorted_rows]
        _check(f"{sort_key} {direction}: unknown values follow known values",
               ids[:2] == known and set(ids[2:]) == set(range(2, len(values))), repr(ids))
        _check(f"{sort_key} {direction}: input contents and order stay unchanged", rows == original)
        _check(f"{sort_key} {direction}: result holds the original dict objects",
               sorted_rows is not rows and {id(row) for row in sorted_rows} == {id(row) for row in rows})

risk_rows = [_row("BROOKVALE MEDIUM", spoilage_risk="MEDIUM"),
             _row("BROOKVALE HIGH", spoilage_risk="HIGH"),
             _row("BROOKVALE BETWEEN", spoilage_risk="MEDIUM-HIGH")]
for direction, expected in (("desc", ["HIGH", "MEDIUM-HIGH", "MEDIUM"]),
                            ("asc", ["MEDIUM", "MEDIUM-HIGH", "HIGH"])):
    _check(f"MEDIUM-HIGH sits between HIGH and MEDIUM when {direction}",
           [row["spoilage_risk"] for row in sort_inventory_items(risk_rows, "spoilage", direction)]
           == expected)
for invalid_key in ("not-a-sort", None, ["days"]):
    _check(f"direct bad sort {invalid_key!r} and direction use default order",
           [row["item"] for row in sort_inventory_items(BASE, invalid_key, "sideways")]
           == DEFAULT_ITEMS)
_check("non-dict entries are omitted by the helper",
       sort_inventory_items([None, "bad", A, 7]) == [A])

print("-- 3: huge and non-finite numbers are missing, not a server error --")
bad_numbers = (("huge numeric string", "1" + "0" * 400),
               ("huge integer", 10 ** 400), ("positive infinity", float("inf")),
               ("negative infinity", float("-inf")), ("NaN", float("nan")))
for label, value in bad_numbers:
    _check(label + ": numeric display is missing", inventory_number_display(value) is None)
    invalid = _row("BROOKVALE UNKNOWN NUMBER", stock=value, days_of_supply=value)
    valid = _row("PADIMAS FINITE NUMBER", stock=38, days_of_supply=12)
    _save([invalid, valid])
    results_response = CLIENT.get(f"/results/{SID}")
    _check(label + ": results page stays readable",
           results_response.status_code == 200 and invalid["item"] in results_response.get_data(as_text=True))
    for sort_key in ("days", "stock"):
        for direction in ("asc", "desc"):
            query = [("sort", sort_key), ("dir", direction)]
            print_response = CLIENT.get(_path("inventory/print"), query_string=query)
            csv_response = CLIENT.get(_path("inventory.csv"), query_string=query)
            expected = [valid["item"], invalid["item"]]
            _check(f"{label}: print {sort_key} {direction} keeps non-finite last",
                   print_response.status_code == 200
                   and _print_items(print_response.get_data(as_text=True)) == expected)
            csv_data = _csv_rows(csv_response)
            _check(f"{label}: CSV {sort_key} {direction} keeps non-finite last with blank days",
                   csv_response.status_code == 200 and _csv_items(csv_response) == expected
                   and len(csv_data) == 3 and csv_data[2][6] == "")

print("-- 4: report shapes are handled by the new routes --")
report_cases = (
    ("wrapped report", {"report": BASE}, False, DEFAULT_ITEMS),
    ("non-list wrapped report", {"report": "x"}, False, []),
    ("NULL report", None, True, []),
    ("mixed list", ["str", 42, None, A, C, B], False, DEFAULT_ITEMS),
    ("malformed JSON", "{not valid JSON", True, []),
    ("numeric JSON", 42, False, []),
)
for label, report, raw, expected in report_cases:
    _save(report, raw=raw)
    for suffix in SUFFIXES:
        response = CLIENT.get(_path(suffix))
        items = (_print_items(response.get_data(as_text=True))
                 if suffix == "inventory/print" else _csv_items(response))
        _check(f"{label}: {suffix} lists only valid rows",
               response.status_code == 200 and items == expected, repr(items))

print("-- 5: unrecognised statuses are counted but not exported --")
pending = _row("KESSINGTON PENDING ITEM", status="PENDING")
list_status = _row("KESSINGTON LIST STATUS ITEM", status=["x"])
_save(BASE + [pending, list_status])
response = CLIENT.get(_path("inventory/print"))
document = response.get_data(as_text=True)
_check("print counts both unlisted statuses",
       response.status_code == 200 and "2 item(s) with no recognised status" in document)
_check("print leaves both unlisted rows out", _print_items(document) == DEFAULT_ITEMS)
response = CLIENT.get(_path("inventory.csv"))
_check("CSV handles a list status without exporting it",
       response.status_code == 200 and _csv_items(response) == DEFAULT_ITEMS)
# The existing results status label cannot handle a non-string status. That
# separate bug is outside this feature; PENDING must still remain visible.
_save(BASE + [pending])
response = CLIENT.get(f"/results/{SID}")
document = response.get_data(as_text=True)
start = document.find('id="tab-inv"')
end = document.find("<!-- No matched sales record", start)
_check("PENDING stays visible on the existing inventory tab",
       response.status_code == 200 and start > 0 and end > start
       and pending["item"] in document[start:end])

print("-- 6: spreadsheet cells cannot become formulas --")
formula = _row('=HYPERLINK("http://x","y")', category="+SUM(1)", stock="-2+3",
               spoilage_risk="@x", observation="=cmd|' /C calc'!A1")
clean = _row("PADIMAS CLEAN STOCK", stock="38")
_save([formula, clean])
response = CLIENT.get(_path("inventory.csv"))
data = _csv_rows(response)
formula_rows = [row for row in data[1:] if len(row) == 8 and row[1].endswith(formula["item"])]
_check("formula row is exported as inert text",
       response.status_code == 200 and len(formula_rows) == 1
       and all(formula_rows[0][index].startswith("'") for index in (1, 2, 3, 5, 7)))
_check("clean numeric stock stays 38",
       any(len(row) == 8 and row[1] == clean["item"] and row[3] == "38" for row in data[1:]))

print("-- 7: print HTML escapes uploaded text --")
xss = _row("<script>alert(1)</script>")
_save([xss])
response = CLIENT.get(_path("inventory/print"))
document = response.get_data(as_text=True)
_check("script text is escaped on the print sheet",
       response.status_code == 200 and "&lt;script&gt;alert(1)&lt;/script&gt;" in document
       and xss["item"] not in document)
with open(os.path.join(ROOT, "templates", "print_inventory.html"), encoding="utf-8") as source:
    template = source.read()
_check("print template adds no script block or escaping bypass",
       "<script" not in template.lower() and re.search(r"\|\s*safe\b", template) is None)

print("-- 8: rival organisations cannot read the new routes --")
_save(BASE)
for suffix in SUFFIXES:
    response = CLIENT.get(_path(suffix, RIVAL_SID))
    _check(suffix + ": rival session returns 403 without its item",
           response.status_code == 403 and RIVAL_ITEM not in response.get_data(as_text=True))
    _positive(suffix, "cross-org positive control")

print("-- 9: the existing free-tier export restriction is preserved --")
free_client = _client(tier="free")
response = free_client.get(_path("inventory.csv"))
_check("free-tier CSV redirects to results without a CSV body",
       response.status_code == 302 and response.headers.get("Location", "").endswith(f"/results/{SID}")
       and response.mimetype != "text/csv")
_positive("inventory/print", "free-tier print remains available", free_client)
response = _positive("inventory.csv", "enterprise CSV positive control")
_check("enterprise spreadsheet has a CSV content type", response.mimetype == "text/csv")

print("-- 10: expired trials retain read access but cannot export --")
expired_client = _client(trial_ends_at="2020-01-01")
response = expired_client.get(_path("inventory.csv"))
_check("expired-trial CSV redirects to dashboard",
       response.status_code == 302 and response.headers.get("Location", "").endswith("/dashboard"))
_positive("inventory/print", "expired-trial print remains available", expired_client)
_positive("inventory.csv", "active-account CSV positive control")

print("-- 11: logged-out requests must log in --")
logged_out = appmod.app.test_client()
for suffix in SUFFIXES:
    response = logged_out.get(_path(suffix))
    _check(suffix + ": logged-out request redirects to login",
           response.status_code == 302 and response.headers.get("Location", "").endswith("/login"))
    _positive(suffix, "logged-in positive control")

print("-- 12: an owned session without analysis returns to dashboard --")
for suffix in SUFFIXES:
    response = CLIENT.get(_path(suffix, EMPTY_SID))
    _check(suffix + ": no-analysis session redirects to dashboard",
           response.status_code == 302 and response.headers.get("Location", "").endswith("/dashboard"))
    _positive(suffix, "completed-analysis positive control")

print("-- 13: filenames cannot carry organisation punctuation --")
for label, org_name in (("punctuation", 'T\u00e9st "Org"; x'), ("non-ASCII", "\u5e93\u5b58")):
    db.execute("UPDATE users SET org_name=? WHERE id=?", (org_name, UID))
    db.execute("UPDATE upload_sessions SET org_name=? WHERE id=?", (org_name, SID))
    client = _client(org_name=org_name)
    response = client.get(_path("inventory.csv"))
    disposition = response.headers.get("Content-Disposition", "")
    _check(label + ": filename contains only safe ASCII characters",
           response.status_code == 200 and A["item"] in response.get_data(as_text=True)
           and re.fullmatch(r"attachment; filename=berthcast_inventory_[A-Za-z0-9_.-]+_\d+\.csv",
                            disposition) is not None, disposition)
    if label == "non-ASCII":
        _check("non-ASCII organisation falls back to org",
               disposition == f"attachment; filename=berthcast_inventory_org_{SID}.csv")

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll inventory health sheet break checks passed.")
