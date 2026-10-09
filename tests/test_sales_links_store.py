"""Plan 018 commit 1: saved sales links and the site-admin correction page.

Uses an isolated database and invented products, with no model calls. Every
scenario reports independently so an absent new module does not hide the other
missing behaviors during the required pre-build RED run.
Run: python tests/test_sales_links_store.py
"""
import importlib
import json
import os
import sys
import tempfile
import types
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_temp = tempfile.TemporaryDirectory(prefix="berthcast_sales_links_store_")
os.environ["DB_PATH"] = os.path.join(_temp.name, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_temp.name, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

_stub = types.ModuleType("anthropic")


class _AnthropicStub:
    def __init__(self, *args, **kwargs):
        pass


_stub.Anthropic = _AnthropicStub
_stub.AnthropicError = Exception
sys.modules["anthropic"] = _stub

import database as db                                  # noqa: E402
import app as appmod                                   # noqa: E402
from agents.shared import normalise_match_key, sg_today  # noqa: E402

appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
_FAILED = 0
_TOTAL = 0
_serial = 0
LINE = "SPAGHETTI 500G BROOKVALE/NORDVIK"
KEY = normalise_match_key(LINE)
MEMBERS = [
    {"code": "BRK-SP500", "name": "BROOKVALE SPAGHETTI 500G",
     "key": normalise_match_key("BROOKVALE SPAGHETTI 500G")},
    {"code": "NRD-SP500", "name": "NORDVIK SPAGHETTI 500G",
     "key": normalise_match_key("NORDVIK SPAGHETTI 500G")},
]
OFF = {"enabled": False, "lines": {}, "has_prev": False,
       "updated_by": None, "updated_at": None}


def _check(name, cond, detail=""):
    global _FAILED, _TOTAL
    _TOTAL += 1
    print(("ok: " if cond else "FAIL: ") + name
          + (f" [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED += 1


def _run(name, test):
    try:
        test()
    except Exception as exc:
        _check(name, False, f"{type(exc).__name__}: {exc}")
    else:
        _check(name, True)


def _expect(cond, detail):
    if not cond:
        raise AssertionError(detail)


def _links():
    return importlib.import_module("agents.sales_links")


def _org():
    global _serial
    _serial += 1
    return f"BROOKVALE TEST {_serial}"


def _entry():
    return {"line": LINE, "members": [dict(m) for m in MEMBERS],
            "conf": "high", "by": "admin", "model": None,
            "at": "2026-09-27", "why": ""}


def _save(org, key=KEY, entry=None, by="operator@example.com"):
    entry = _entry() if entry is None else entry

    def put(lines):
        lines[key] = entry
        return True

    return db.update_sales_links(org, put, by)


def _session(org, status="complete", created="2026-09-27 01:00:00", mapping=None):
    return db.execute(
        "INSERT INTO upload_sessions (user_id,org_name,status,created_at,column_map_json) "
        "VALUES (?,?,?,?,?)", (1, org, status, created, json.dumps(mapping) if mapping else None))


def _stock(sid, rows=None):
    sid = int(sid)
    db.execute(f"CREATE TABLE inventory_{sid} (location_code TEXT, inventory_code TEXT, "
               "description TEXT, stock_label TEXT, qty_on_hand TEXT, uom TEXT)")
    rows = rows if rows is not None else [
        ("WAREHOUSE", "BRK-SP500", MEMBERS[0]["name"], "BROOKVALE CUSTOM LABEL", "0", "PKT"),
        ("WAREHOUSE", "NRD-SP500", MEMBERS[1]["name"], "NORDVIK CUSTOM LABEL", "400", "PKT"),
        ("WAREHOUSE", "PAD-RICE", "PADIMAS RICE 5KG", "PADIMAS CUSTOM LABEL", "20", "BAG"),
    ]
    conn = db.get_db()
    try:
        conn.executemany(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()


def _user(org, admin=False, role="admin"):
    email = f"user{_serial}-{int(admin)}@example.com"
    uid = db.execute("INSERT INTO users (email,password_hash,org_name,is_admin,role) "
                     "VALUES (?,?,?,?,?)", (email, "unused-test-hash", org, int(admin), role))
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=uid, email=email, org_name=org, is_admin=admin,
                       role=role, sv=0, model="claude-sonnet-5", tier="enterprise")
    return client, uid, email


def _admin_fixture(stock=True):
    org = _org()
    _user(org)
    admin_org = _org()
    client, uid, email = _user(admin_org, admin=True)
    if stock:
        _stock(_session(org))
    return org, client, uid, email


def _post(client, org, action, **data):
    response = client.post("/admin/sales-links", data={"org": org, "action": action, **data})
    _expect(response.status_code == 302, f"POST {action} status {response.status_code}")
    target = urlparse(response.location)
    _expect(target.path == "/admin/sales-links" and parse_qs(target.query).get("org") == [org],
            "POST must redirect back to the chosen company")
    return client.get(response.location)


class _Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"method": attrs.get("method", "get").lower(), "fields": {}}
            self.forms.append(self.current)
        elif tag in ("input", "button", "textarea") and self.current is not None:
            if attrs.get("name"):
                self.current["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def fresh_defaults():
    _expect(db.get_sales_links("BROOKVALE TRADING") == OFF, "fresh org must be OFF")
    _expect(db.table_exists("sales_line_links"), "additive table is missing")
    _expect(db.query("SELECT * FROM sales_line_links WHERE org_name=?", ("BROOKVALE TRADING",)) == [],
            "reading must not create a row")


def invalid_org_read():
    get_links = db.get_sales_links
    original_query, original_get_db = db.query, db.get_db

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid org read opened the database")

    db.query = db.get_db = forbidden
    try:
        for org in ("", "   ", None, 12, [], {}):
            _expect(get_links(org) == OFF, f"invalid org {org!r} must return OFF defaults")
    finally:
        db.query, db.get_db = original_query, original_get_db


def first_save():
    org = _org()
    _expect(_save(org) == {"ok": True, "changed": True}, "first save result")
    state = db.get_sales_links(org)
    _expect(state["lines"] == {KEY: _entry()}, "entry must round-trip without losing fields")
    _expect(state["enabled"] is False and state["has_prev"] is False, "new row must stay OFF")
    _expect(state["updated_by"] == "operator@example.com" and bool(state["updated_at"]),
            "change attribution missing")
    stored = db.query("SELECT links_json,prev_json FROM sales_line_links WHERE org_name=?", (org,))[0]
    _expect(json.loads(stored["links_json"]) == {"v": 1, "lines": {KEY: _entry()}}, "versioned JSON")
    _expect(stored["prev_json"] is None, "first save has no previous row")


def undo_and_redo():
    org = _org()
    _save(org)
    first = db.get_sales_links(org)["lines"]
    _save(org, "rice", {"line": "PADIMAS RICE 5KG", "members": []})
    second = db.get_sales_links(org)["lines"]
    _expect(db.get_sales_links(org)["has_prev"] is True and len(second) == 2, "second change saves history")
    _expect(db.undo_sales_links(org, "undo@example.com") is True, "undo must report the swap")
    _expect(db.get_sales_links(org)["lines"] == first, "undo must restore first version")
    _expect(db.get_sales_links(org)["updated_by"] == "undo@example.com", "undo attribution")
    _expect(db.undo_sales_links(org, "redo@example.com") is True, "second undo must work")
    _expect(db.get_sales_links(org)["lines"] == second, "second undo must restore second version")


def unchanged_mutator():
    org = _org()
    _expect(db.update_sales_links(org, lambda lines: False, "idle@example.com") ==
            {"ok": True, "changed": False}, "unchanged result")
    _expect(db.query("SELECT * FROM sales_line_links WHERE org_name=?", (org,)) == [],
            "no-op must not create a row")
    _save(org)
    before = db.query("SELECT * FROM sales_line_links WHERE org_name=?", (org,))

    def cancel(lines):
        lines["discarded"] = {"line": "PADIMAS", "members": []}
        return False

    db.update_sales_links(org, cancel, "idle@example.com")
    _expect(db.query("SELECT * FROM sales_line_links WHERE org_name=?", (org,)) == before,
            "False must roll back content, previous JSON and attribution")


def org_isolation():
    org_a, org_b = _org(), _org()
    _save(org_a)
    _save(org_b, "rice", {"line": "PADIMAS RICE 5KG", "members": []})
    before_b = db.get_sales_links(org_b)
    db.set_sales_links_enabled(org_a, True, "operator@example.com")
    _save(org_a, "new", {"line": "BROOKVALE OATS 1KG", "members": []})
    db.undo_sales_links(org_a, "operator@example.com")
    _expect(db.get_sales_links(org_b) == before_b, "writes, toggle and undo leaked between orgs")


def toggle_keeps_links():
    org = _org()
    db.set_sales_links_enabled(org, True, "on@example.com")
    state = db.get_sales_links(org)
    _expect(state["enabled"] is True and state["lines"] == {}, "switch-on must create missing row")
    _save(org)
    before = db.query("SELECT links_json,prev_json FROM sales_line_links WHERE org_name=?", (org,))
    db.set_sales_links_enabled(org, False, "off@example.com")
    _expect(db.get_sales_links(org)["enabled"] is False, "switch-off missing")
    _expect(db.get_sales_links(org)["updated_by"] == "off@example.com", "toggle attribution")
    _expect(db.query("SELECT links_json,prev_json FROM sales_line_links WHERE org_name=?", (org,)) == before,
            "toggle must preserve saved links and undo history")
    db.set_sales_links_enabled(org, True, "on@example.com")
    _expect(db.get_sales_links(org)["lines"] == {KEY: _entry()}, "switch-on lost links")


def latest_complete():
    org_a, org_b = _org(), _org()
    _expect(db.latest_complete_session_id(org_a) is None, "fresh org must have no complete run")
    first = _session(org_a, created="2026-09-25 01:00:00")
    _session(org_a, "analyzing", "2026-09-29 01:00:00")
    _session(org_b, created="2026-09-30 01:00:00")
    _expect(db.latest_complete_session_id(org_a) == first, "exclude incomplete and other-org sessions")
    newer = _session(org_a, created="2026-09-27 01:00:00")
    _session(org_a, created="2026-09-24 01:00:00")
    _expect(db.latest_complete_session_id(org_a) == newer, "created date must outrank id")
    tied = _session(org_a, created="2026-09-27 01:00:00")
    _expect(db.latest_complete_session_id(org_a) == tied, "same date must choose greatest id")


def pick_columns(kind):
    links = _links()
    if kind == "warehouse":
        rows = [{"location_code": "WAREHOUSE", "code": f"ALT-{i}", "inventory_code": f"BRK-{i}"}
                for i in range(100)]
        result = links.pick_code_column(["location_code", "code", "inventory_code"], rows)
        _expect(result == "inventory_code", "preferred header must beat warehouse and generic code")
    elif kind == "constant":
        rows = [{"code": f"BRK-{i % 3}"} for i in range(100)]
        _expect(links.pick_code_column(["code"], rows) is None, "three values are not item identifiers")
    elif kind == "generic":
        _expect(links.pick_code_column(["code"], [{"code": "BRK-1"}, {"code": "NRD-2"}]) == "code",
                "unique generic code should work")
    elif kind == "thresholds":
        rows = [{"inventory_code": f"BRK-{i}" if i < 90 else " "} for i in range(100)]
        _expect(links.pick_code_column(["inventory_code"], rows) == "inventory_code", "90% filled qualifies")
        rows[89]["inventory_code"] = ""
        _expect(links.pick_code_column(["inventory_code"], rows) is None, "89% filled must fail")
        rows = [{"code": f"BRK-{i % 90}"} for i in range(100)]
        _expect(links.pick_code_column(["code"], rows) == "code", "90% distinct qualifies")
        rows[-1]["code"] = "BRK-0"
        rows[89]["code"] = "BRK-0"
        _expect(links.pick_code_column(["code"], rows) is None, "89% distinct must fail")
    else:
        _expect(links.pick_code_column(["inventory_code"], []) is None, "empty rows have no code column")
        _expect(links.pick_code_column(["location_code"], [{"location_code": "A"}]) is None,
                "location must never qualify even when unique")


def description_helpers():
    links = _links()
    sid = _session(_org(), mapping={"description": "stock_label"})
    cols = ["inventory_code", "description", "stock_label"]
    _expect(links.inventory_desc_col(sid, cols) == "stock_label", "saved mapping must win")
    db.execute("UPDATE upload_sessions SET column_map_json=? WHERE id=?", ('{"description":"gone"}', sid))
    _expect(links.inventory_desc_col(sid, cols) == "description", "stale mapping must fall back")
    db.execute("UPDATE upload_sessions SET column_map_json=? WHERE id=?", ("bad-json", sid))
    _expect(links.inventory_desc_col(sid, cols) == "description", "bad map must fall back")
    _expect(links.sales_desc_col(["supplier_description", "inventory_desc", "qty"]) == "inventory_desc",
            "exact sales description wins")
    _expect(links.sales_desc_col(["supplier_description", "item_name", "qty"]) == "item_name",
            "sales fallback excludes supplier descriptions")
    _expect(links.sales_desc_col(["supplier_description", "qty"]) is None, "missing sales name stays missing")


def stock_code_lookup():
    links = _links()
    sid = _session(_org(), mapping={"description": "stock_label"})
    _stock(sid)
    found, col = links.stock_codes(sid)
    _expect(col == "inventory_code" and found["BRK-SP500"] == "BROOKVALE CUSTOM LABEL",
            "stock lookup must use code detection and saved description mapping")
    _expect(links.stock_codes(sid + 100000) == ({}, None), "missing table must return empty lookup")
    _expect(links.stock_codes(None) == ({}, None), "invalid session must fail closed")


def stock_code_cleaning():
    links = _links()
    sid = _session(_org())
    rows = [("WAREHOUSE", f"BRK-{i}", f"BROOKVALE ITEM {i}", "", "1", "PKT") for i in range(20)]
    rows[0] = ("WAREHOUSE", " BRK-0 ", " BROOKVALE FIRST ", "", "1", "PKT")
    rows[1] = ("WAREHOUSE", "BRK-0", "BROOKVALE SECOND", "", "1", "PKT")
    rows[2] = ("WAREHOUSE", "B" * 41, "BROOKVALE LONG CODE", "", "1", "PKT")
    rows[3] = ("WAREHOUSE", "BRK-3", " ", "", "1", "PKT")
    _stock(sid, rows)
    found, col = links.stock_codes(sid)
    _expect(col == "inventory_code" and found.get("BRK-0") == "BROOKVALE FIRST", "strip and keep first name")
    _expect("B" * 41 not in found and "BRK-3" not in found, "long codes and blank descriptions must be skipped")


def stock_code_cap():
    links = _links()
    sid = _session(_org())
    rows = [("WAREHOUSE", f"BRK-{i}", f"BROOKVALE ITEM {i}", "", "1", "PKT") for i in range(3001)]
    _stock(sid, rows)
    found, col = links.stock_codes(sid)
    _expect(col == "inventory_code" and len(found) == 3000, "lookup must stop at 3000 rows")
    _expect("BRK-3000" not in found, "row after cap was read")


def entry_shape():
    links = _links()
    entry = links.make_entry(LINE, MEMBERS, "HIGH", "ai", model=links.LINK_MODEL, why="Same size")
    _expect(entry == {"line": LINE, "members": MEMBERS, "conf": "high", "by": "ai",
                      "at": sg_today().isoformat(), "model": "claude-sonnet-5-5", "why": "Same size"},
            "entry shape, confidence normalization or Singapore date differs")
    admin = links.make_entry(LINE, [], "sure", "admin")
    _expect(admin["members"] == [] and admin["conf"] == "low" and admin["model"] is None,
            "no-family decision and unknown confidence must be preserved safely")


def entry_caps():
    links = _links()
    members = [{"code": "B" * 40, "key": "k" * 140, "name": "N" * 100}]
    entry = links.make_entry("L" * 120, members, "Medium", "ai", why="W" * 150)
    _expect(len(entry["line"]) == 120 and len(entry["members"][0]["code"]) == 40,
            "valid boundary values must survive")
    _expect(len(entry["members"][0]["name"]) <= 80 and len(entry["members"][0]["key"]) <= 120
            and len(entry["why"]) == 120 and entry["conf"] == "medium", "display fields must be capped")
    for line, rows in (("L" * 121, MEMBERS), ("!!!", MEMBERS), ("", MEMBERS),
                       (LINE, [{"code": f"BRK-{i}", "name": "BROOKVALE", "key": "brookvale"}
                               for i in range(21)])):
        try:
            links.make_entry(line, rows, "high", "admin")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid line or oversized family was silently accepted")
    _expect(links.MAX_LINES == 1000 and links.MAX_MEMBERS == 20 and links.MAX_LINE_CHARS == 120
            and links.MAX_CODE_CHARS == 40 and links.MAX_NAME_CHARS == 80
            and db.SALES_LINKS_MAX_JSON == 1_000_000, "storage caps differ from the approved contract")


def admin_access():
    org = _org()
    client, _, _ = _user(org)
    response = client.get("/admin/sales-links")
    _expect(response.status_code == 302 and "/admin/sales-links" not in response.location,
            "ordinary account must be redirected away")
    _expect(b"Links are OFF" not in response.data, "ordinary account received the links page")


def admin_chooser():
    org, client, _, _ = _admin_fixture()
    response = client.get("/admin/sales-links")
    _expect(response.status_code == 200 and org.encode() in response.data, "known org missing from chooser")
    response = client.get("/admin/sales-links", query_string={"org": org})
    _expect(response.status_code == 200 and b"Links are OFF for this company." in response.data,
            "new company must render the OFF state")
    _expect(LINE.encode() not in response.data and db.get_sales_links(org)["lines"] == {},
            "new company must show no saved link rows")


def admin_toggle():
    org, client, _, email = _admin_fixture()
    response = _post(client, org, "toggle", enabled="1")
    _expect(response.status_code == 200 and b"Links are ON for this company." in response.data, "ON state missing")
    _expect(db.get_sales_links(org)["enabled"] is True and db.get_sales_links(org)["updated_by"] == email,
            "toggle must save the selected org and logged-in admin")
    response = _post(client, org, "toggle", enabled="0")
    _expect(b"Links are OFF for this company." in response.data and db.get_sales_links(org)["enabled"] is False,
            "OFF state missing")


def admin_set():
    org, client, _, email = _admin_fixture()
    response = _post(client, org, "set", line=f" {LINE} ", codes="BRK-SP500, NRD-SP500; BRK-SP500")
    state = db.get_sales_links(org)
    _expect(response.status_code == 200 and len(state["lines"]) == 1, "set must create one entry")
    entry = state["lines"][KEY]
    _expect(entry["line"] == LINE and entry["members"] == MEMBERS, "members must use table names in code order")
    _expect(entry["by"] == "admin" and entry["conf"] == "high" and entry["model"] is None,
            "manual edits must be recorded as sure admin choices")
    _expect(state["updated_by"] == email and not state["enabled"], "manual save must not silently switch on")
    _expect(all(m["name"].encode() in response.data and m["code"].encode() in response.data for m in MEMBERS),
            "saved stock names and codes must render")


def admin_clear_relink_undo():
    org, client, _, _ = _admin_fixture()
    _post(client, org, "set", line=LINE, codes="BRK-SP500 NRD-SP500")
    _post(client, org, "clear", line_key=KEY)
    cleared = db.get_sales_links(org)["lines"][KEY]
    _expect(cleared["line"] == LINE and cleared["members"] == [] and cleared["by"] == "admin",
            "clear must keep a no-family decision")
    response = _post(client, org, "relink", line_key=KEY)
    _expect(KEY not in db.get_sales_links(org)["lines"], "relink must remove the decision")
    _expect(response.status_code == 200, "empty list after relink must render")
    _post(client, org, "undo", links_hash=db.sales_links_hash(db.get_sales_links(org)["lines"]))
    _expect(db.get_sales_links(org)["lines"][KEY] == cleared, "undo must restore the removed decision")


def admin_unknown_code():
    org, client, _, _ = _admin_fixture()
    _post(client, org, "set", line=LINE, codes="BRK-SP500")
    before = db.get_sales_links(org)
    response = _post(client, org, "set", line=LINE, codes="BRK-SP500 INVENTED-CODE")
    _expect(b"Not in the latest stock file:" in response.data, "unknown code must explain refusal")
    _expect(db.get_sales_links(org) == before, "unknown code must not partially replace the entry")


def admin_overlap():
    org, client, _, _ = _admin_fixture()
    _post(client, org, "set", line=LINE, codes="BRK-SP500")
    before = db.get_sales_links(org)
    response = _post(client, org, "set", line="BROOKVALE PASTA", codes="BRK-SP500")
    _expect(b"Already on another line:" in response.data and b"BRK-SP500" in response.data,
            "overlap must identify the existing claim")
    _expect(db.get_sales_links(org) == before, "overlap must not write another family")
    _post(client, org, "set", line=LINE, codes="BRK-SP500 NRD-SP500")
    _expect(db.get_sales_links(org)["lines"][KEY]["members"] == MEMBERS, "editing the same line may keep its code")


def admin_latest_stock():
    org, client, _, _ = _admin_fixture(stock=False)
    old = _session(org, created="2026-09-25 01:00:00")
    _stock(old)
    latest = _session(org, created="2026-09-27 01:00:00")
    _stock(latest, [("WAREHOUSE", "NEW-1", "GREENFJORD NEW ITEM", "", "1", "PKT")])
    unfinished = _session(org, "uploading", "2026-09-29 01:00:00")
    _stock(unfinished)
    other = _session(_org(), created="2026-09-30 01:00:00")
    _stock(other)
    response = _post(client, org, "set", line=LINE, codes="BRK-SP500")
    _expect(b"Not in the latest stock file:" in response.data and db.get_sales_links(org)["lines"] == {},
            "old, incomplete or other-company stock must not validate the code")
    _post(client, org, "set", line="GREENFJORD ITEM", codes="NEW-1")
    saved = db.get_sales_links(org)["lines"][normalise_match_key("GREENFJORD ITEM")]
    _expect(saved["members"][0]["name"] == "GREENFJORD NEW ITEM", "latest complete stock not used")


def admin_no_stock():
    org, client, _, _ = _admin_fixture(stock=False)
    response = _post(client, org, "set", line=LINE, codes="BRK-SP500")
    _expect(b"latest stock file has no item code column" in response.data, "missing stock must explain refusal")
    _expect(db.get_sales_links(org)["lines"] == {}, "missing stock must not write")


def admin_forms():
    org, client, _, _ = _admin_fixture()
    _post(client, org, "set", line=LINE, codes="BRK-SP500")
    response = _post(client, org, "set", line=LINE, codes="BRK-SP500 NRD-SP500")
    forms = [form for form in _Forms(response.get_data(as_text=True)).forms if form["method"] == "post"]
    _expect(forms, "no POST forms found")
    _expect(all(form["fields"].get("org") == org and form["fields"].get("csrf_token") for form in forms),
            "every POST form needs the selected org and a nonempty CSRF token")
    actions = {form["fields"].get("action") for form in forms}
    _expect({"toggle", "set", "clear", "relink", "undo"} <= actions, "one or more correction actions missing")
    row_forms = [form for form in forms if form["fields"].get("action") in ("clear", "relink")]
    _expect(all(form["fields"].get("line_key") == KEY for form in row_forms), "row actions lost the saved key")
    edit = [form for form in forms if form["fields"].get("action") == "set"
            and form["fields"].get("line") == LINE]
    _expect(len(edit) == 1 and "BRK-SP500" in edit[0]["fields"].get("codes", "")
            and "NRD-SP500" in edit[0]["fields"].get("codes", ""), "row edit must prefill current codes")
    _expect(b"No match means" in response.data and b"plain name matching" in response.data,
            "no-match fallback explanation missing")


def admin_row_sort():
    links = _links()
    org, client, _, _ = _admin_fixture()
    names = ["ZZZ BROOKVALE UNSURE", "MMM NORDVIK SURE", "AAA PADIMAS MANUAL"]
    for line, conf, by in zip(names, ("medium", "high", "low"), ("ai", "ai", "admin")):
        _save(org, normalise_match_key(line), links.make_entry(line, [], conf, by))
    response = client.get("/admin/sales-links", query_string={"org": org})
    html = response.get_data(as_text=True)
    _expect(response.status_code == 200, "saved rows did not render")
    positions = [html.find(name) for name in names]
    _expect(all(pos >= 0 for pos in positions) and positions == sorted(positions),
            "row order must be AI unsure, AI sure, then admin")


def admin_guard(card=False):
    org, client, _, _ = _admin_fixture(stock=False)
    response = client.get("/admin")
    _expect(response.status_code == 200, "existing admin dashboard stopped rendering")
    if card:
        _expect(b"/admin/sales-links" in response.data and b"Sales-line links" in response.data,
                "new admin card missing")


_run("RED: fresh company defaults OFF without inserting a row", fresh_defaults)
_run("RED: blank and non-string org reads never query", invalid_org_read)
_run("RED: first saved entry round-trips with version and attribution", first_save)
_run("RED: second change preserves one-step undo and redo", undo_and_redo)
_run("RED: unchanged mutator rolls back without writing history", unchanged_mutator)
_run("RED: save, toggle and undo stay isolated by company", org_isolation)
_run("RED: toggle inserts missing row and retains links and history", toggle_keeps_links)
_run("RED: latest complete lookup respects company, status, date and id", latest_complete)
for _kind in ("warehouse", "constant", "generic", "thresholds", "empty"):
    _run(f"RED: code-column selection { _kind }", lambda kind=_kind: pick_columns(kind))
_run("RED: description helpers respect saved map and name fallbacks", description_helpers)
_run("RED: stock-code lookup respects mapping and missing tables", stock_code_lookup)
_run("RED: stock-code lookup strips values, keeps first name and skips invalid rows", stock_code_cleaning)
_run("RED: stock-code lookup reads at most 3000 rows", stock_code_cap)
_run("RED: entry factory records normalized confidence, author and Singapore date", entry_shape)
_run("RED: entry factory enforces identity and display caps", entry_caps)
_run("RED: ordinary account cannot GET the site-admin links page", admin_access)
_run("RED: admin chooser lists companies and new company renders OFF", admin_chooser)
_run("RED: admin switch saves ON and OFF with attribution", admin_toggle)
_run("RED: admin set resolves and deduplicates codes using stock names", admin_set)
_run("RED: admin clear, relink and undo preserve the intended decision", admin_clear_relink_undo)
_run("RED: unknown stock code refuses the whole manual change", admin_unknown_code)
_run("RED: another line cannot claim an already-linked code", admin_overlap)
_run("RED: manual links validate only against the selected company's latest complete stock", admin_latest_stock)
_run("RED: missing stock refuses manual linking without writing", admin_no_stock)
_run("RED: correction forms carry CSRF, org and row identity", admin_forms)
_run("RED: admin rows sort AI unsure first, then AI sure, then manual", admin_row_sort)
_run("GUARD: existing admin dashboard still renders", admin_guard)
_run("RED: existing admin dashboard links to sales-line settings", lambda: admin_guard(card=True))

print(f"\n{_TOTAL - _FAILED} passed, {_FAILED} failed")
sys.exit(1 if _FAILED else 0)
