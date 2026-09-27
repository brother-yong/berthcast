"""Independent hostile-input checks for plan 018 commit 1, no model calls."""
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import types
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_temp = tempfile.TemporaryDirectory(prefix="berthcast_sales_links_break_")
os.environ["DB_PATH"] = os.path.join(_temp.name, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_temp.name, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
_stub = types.ModuleType("anthropic")
_stub.Anthropic = lambda *args, **kwargs: None
_stub.AnthropicError = Exception
sys.modules["anthropic"] = _stub


def _source_hashes():
    paths = [ROOT / name for name in ("app.py", "database.py", "rec_logic.py",
                                     "templates/admin.html", "templates/admin_sales_links.html")]
    paths += list((ROOT / "agents").glob("*.py"))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


_before_hashes = _source_hashes()
import database as db  # noqa: E402
import app as appmod  # noqa: E402
from agents.shared import normalise_match_key  # noqa: E402

appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
_TOTAL = _FAILED = _serial = 0
LINE = "BROOKVALE SPAGHETTI 500G"
CODE = "BRK-SP500"
OTHER_CODE = "NRD-SP500"


def _check(name, cond, detail=""):
    global _TOTAL, _FAILED
    _TOTAL += 1
    _FAILED += int(not cond)
    print(("ok: " if cond else "FAIL: ") + name
          + (f" [{detail}]" if detail and not cond else ""))


def _expect(cond, detail):
    if not cond:
        raise AssertionError(detail)


def _run(name, function):
    try:
        function()
    except Exception as exc:
        _check(name, False, f"{type(exc).__name__}: {exc}")
    else:
        _check(name, True)


def _org():
    global _serial
    _serial += 1
    return f"BROOKVALE BREAK {_serial}"


def _entry(line=LINE, code=CODE):
    return {"line": line, "members": [{"code": code, "name": LINE,
                                       "key": normalise_match_key(LINE)}],
            "conf": "high", "by": "admin", "model": None,
            "at": "2026-09-27", "why": ""}


def _seed(org, lines, prev=None):
    raw = json.dumps({"v": 1, "lines": lines}, ensure_ascii=False)
    db.execute("INSERT INTO sales_line_links "
               "(org_name,enabled,links_json,prev_json,updated_by,updated_at) "
               "VALUES (?,1,?,?,?,?) ON CONFLICT(org_name) DO UPDATE SET "
               "links_json=excluded.links_json,prev_json=excluded.prev_json",
               (org, raw, prev, "before@example.com", "2026-09-01 01:02:03"))


def _snapshot():
    return db.query("SELECT * FROM sales_line_links ORDER BY id")


def _user(org, admin=False, role="admin"):
    email = f"break{_serial}-{int(admin)}-{role}@example.com"
    uid = db.execute("INSERT INTO users (email,password_hash,org_name,is_admin,role) "
                     "VALUES (?,?,?,?,?)", (email, "unused", org, int(admin), role))
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=uid, email=email, org_name=org, is_admin=admin,
                       role=role, sv=0, model="claude-sonnet-5", tier="enterprise")
    return client, uid


def _fixture():
    org = _org()
    _user(org)
    client, uid = _user(_org(), admin=True)
    sid = int(db.execute("INSERT INTO upload_sessions (user_id,org_name,status) "
                         "VALUES (?,?,'complete')", (uid, org)))
    db.execute(f"CREATE TABLE inventory_{sid} "
               "(inventory_code TEXT,location_code TEXT,description TEXT,qty_on_hand TEXT)")
    conn = db.get_db()
    try:
        conn.executemany(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?)", [
            (CODE, "WAREHOUSE", LINE, "0"),
            (OTHER_CODE, "WAREHOUSE", "NORDVIK SPAGHETTI 500G", "400"),
        ])
        conn.commit()
    finally:
        conn.close()
    return org, client, uid


class _Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.tags = []
        self.script_text = []
        self.in_script = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "script":
            self.in_script = True
        if tag == "form":
            self.current = {}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and attrs.get("name"):
            self.current[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        elif tag == "script":
            self.in_script = False

    def handle_data(self, text):
        if self.in_script:
            self.script_text.append(text)


def _get(client, org):
    response = client.get("/admin/sales-links", query_string={"org": org})
    _expect(response.status_code == 200, f"GET returned {response.status_code}")
    return response.get_data(as_text=True)


def corrupt_envelopes():
    org, client, _ = _fixture()
    for raw in (None, "not JSON", "[]", '"x"', '{"lines": []}', "null", "{}"):
        _seed(org, {})
        db.execute("UPDATE sales_line_links SET links_json=? WHERE org_name=?", (raw, org))
        before = _snapshot()
        _expect(db.get_sales_links(org)["lines"] == {}, f"unsafe read of {raw!r}")
        _get(client, org)
        _expect(_snapshot() == before, "corrupt read changed stored data")


def corrupt_entries_and_members():
    org, client, _ = _fixture()
    lines = {"string": "bad", "none": None, "list": [], "line": {"line": 4, "members": []},
             "membersdict": {"line": "BROOKVALE A", "members": {}},
             "membersnone": {"line": "BROOKVALE B", "members": None},
             "membersstring": {"line": "BROOKVALE C", "members": "bad"}}
    mixed = _entry("NORDVIK MIXED")
    mixed.update(members=[None, 3, "bad", [], {}, {"code": None, "name": []},
                          {"code": CODE, "name": LINE}], conf=[], by={}, at=12, why=[])
    lines[normalise_match_key(mixed["line"])] = mixed
    _seed(org, lines)
    before = _snapshot()
    _expect(db.get_sales_links(org)["lines"] == lines, "DAL should return stored entries intact")
    html = _get(client, org)
    _expect("unreadable entries" in html, "bad entries were not reported")
    _expect(_snapshot() == before, "rendering corrupt entries wrote data")


def _oversize_assert():
    org = _org()
    _seed(org, {normalise_match_key(LINE): _entry()}, prev='{"v":1,"lines":{}}')
    before = _snapshot()
    value = "\u754c" * 400_000
    serialized = json.dumps({"v": 1, "lines": {"oversize": value}}, ensure_ascii=False)
    _expect(len(serialized) < 1_000_000 < len(serialized.encode("utf-8")),
            "fixture must cross byte limit without crossing character limit")

    def grow(lines):
        lines["oversize"] = value
        return True

    result = db.update_sales_links(org, grow, "after@example.com")
    _expect(result == {"ok": False, "error": "too_big"}, f"oversize result: {result}")
    _expect(_snapshot() == before, "oversize refusal changed the old row or undo snapshot")


def cap_mutation_probe():
    original = db.SALES_LINKS_MAX_JSON
    detected = False
    try:
        db.SALES_LINKS_MAX_JSON = 2_000_000
        try:
            _oversize_assert()
        except AssertionError as exc:
            detected = "oversize result" in str(exc)
    finally:
        db.SALES_LINKS_MAX_JSON = original
    _expect(detected, "byte-limit assertion did not catch an intentionally disabled limit")


def junk_posts():
    org, client, _ = _fixture()
    _seed(org, {normalise_match_key(LINE): _entry()})
    valid = {"org": org, "action": "set", "line": "NORDVIK NEW", "codes": OTHER_CODE}
    cases = [
        ("missing org", {k: v for k, v in valid.items() if k != "org"}),
        ("unknown org", {**valid, "org": "PADIMAS UNKNOWN"}),
        ("10k org", {**valid, "org": "O" * 10_000}),
        ("unknown action", {**valid, "action": "destroy"}),
        ("missing action", {k: v for k, v in valid.items() if k != "action"}),
        ("missing line", {k: v for k, v in valid.items() if k != "line"}),
        ("empty line", {**valid, "line": ""}),
        ("10k line", {**valid, "line": "A" * 10_000}),
        ("punctuation line", {**valid, "line": " ,;!@#$%^&*() "}),
        ("missing codes", {k: v for k, v in valid.items() if k != "codes"}),
        ("empty codes", {**valid, "codes": ""}),
        ("separators only", {**valid, "codes": ",,,"}),
        ("21 codes", {**valid, "codes": ",".join(f"B{i}" for i in range(21))}),
        ("41-char code", {**valid, "codes": "B" * 41}),
        ("script code", {**valid, "codes": "<script>"}),
        ("SQL code", {**valid, "codes": "'; DROP TABLE users;--"}),
        ("unknown clear key", {"org": org, "action": "clear", "line_key": "missing"}),
        ("unknown relink key", {"org": org, "action": "relink", "line_key": "missing"}),
    ]
    for name, payload in cases:
        before = _snapshot()
        response = client.post("/admin/sales-links", data=payload)
        _expect(response.status_code == 302, f"{name}: status {response.status_code}")
        _expect(_snapshot() == before, f"{name}: wrote saved state")
        _expect(client.get(response.location).status_code == 200, f"{name}: redirect page failed")
    _expect(bool(db.query("SELECT COUNT(*) AS n FROM users")), "SQL-shaped code damaged users")


def escaped_xss():
    org, client, _ = _fixture()
    script = "<script>alert(1)</script>"
    image = '\"><img src=x onerror=1>'
    entry = _entry(script)
    entry.update(why=script)
    entry["members"][0]["name"] = image
    _seed(org, {normalise_match_key(script): entry})
    before = _snapshot()
    html = _get(client, org)
    parsed = _Page(html)
    _expect("&lt;script&gt;alert(1)&lt;/script&gt;" in html, "line is not escaped text")
    _expect("&lt;img src=x onerror=1&gt;" in html, "member name is not escaped text")
    _expect(script not in html and image not in html, "raw injected markup reached HTML")
    _expect(not any(tag == "img" and attrs.get("src") == "x" for tag, attrs in parsed.tags),
            "member name created an image element")
    _expect(not any("alert(1)" in text for text in parsed.script_text), "line entered a script")
    _expect(not any(name.startswith("on") and ("alert(1)" in value or value == "1")
                    for _, attrs in parsed.tags for name, value in attrs.items() if value),
            "payload created an event attribute")
    _expect(_snapshot() == before, "XSS read wrote data")


def denied_writes(kind):
    org, admin, uid = _fixture()
    _seed(org, {normalise_match_key(LINE): _entry()}, prev='{"v":1,"lines":{}}')
    if kind == "logged-out":
        client = appmod.app.test_client()
    elif kind == "viewer":
        client, _ = _user(_org(), role="viewer")
    else:
        client = admin
        db.bump_session_version(uid)
    before = _snapshot()
    response = client.get("/admin/sales-links", query_string={"org": org})
    _expect(response.status_code == 302, f"{kind} GET was not denied")
    for action in ("toggle", "set", "clear", "relink", "undo"):
        response = client.post("/admin/sales-links", data={"org": org, "action": action,
                               "enabled": "0", "line": "NORDVIK NEW", "codes": OTHER_CODE,
                               "line_key": normalise_match_key(LINE)})
        _expect(response.status_code == 302, f"{kind} {action}: status {response.status_code}")
        _expect(_snapshot() == before, f"{kind} {action} wrote data")


def missing_csrf():
    org, client, _ = _fixture()
    _seed(org, {normalise_match_key(LINE): _entry()})
    before = _snapshot()
    original = appmod.app.config["WTF_CSRF_ENABLED"]
    try:
        appmod.app.config["WTF_CSRF_ENABLED"] = True
        response = client.post("/admin/sales-links", data={"org": org, "action": "toggle", "enabled": "0"})
        _expect(response.status_code == 400, f"missing CSRF status {response.status_code}")
        _expect(_snapshot() == before, "missing CSRF wrote data")
    finally:
        appmod.app.config["WTF_CSRF_ENABLED"] = original


def undo_without_previous():
    org, client, _ = _fixture()
    before = _snapshot()
    _expect(db.undo_sales_links(org, "after@example.com") is False, "undo created a missing row")
    _expect(_snapshot() == before, "undo missing row wrote data")
    _seed(org, {normalise_match_key(LINE): _entry()})
    before = _snapshot()
    _expect(db.undo_sales_links(org, "after@example.com") is False, "undo succeeded without prev")
    response = client.post("/admin/sales-links", data={"org": org, "action": "undo"})
    _expect(response.status_code == 302, "undo without prev did not redirect")
    _expect("Nothing to undo." in client.get(response.location).get_data(as_text=True),
            "undo no-op did not explain itself")
    _expect(_snapshot() == before, "undo without previous changed the row")


def concurrent_mutators():
    org = _org()
    _seed(org, {})
    first_inside = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    results, errors = [], []

    def first_mutator(lines):
        first_inside.set()
        if not release_first.wait(5):
            raise RuntimeError("first mutator was not released")
        lines["brookvale"] = _entry("BROOKVALE")
        return True

    def second_mutator(lines):
        lines["nordvik"] = _entry("NORDVIK", OTHER_CODE)
        return True

    def worker(mutator, started=None):
        try:
            if started:
                started.set()
            results.append(db.update_sales_links(org, mutator, "worker@example.com"))
        except Exception as exc:
            errors.append(str(exc))

    first = threading.Thread(target=worker, args=(first_mutator,))
    second = threading.Thread(target=worker, args=(second_mutator, second_started))
    first.start()
    try:
        _expect(first_inside.wait(5), "first thread never entered its mutator")
        second.start()
        _expect(second_started.wait(5), "second thread never started update")
        # Let the second call contend while the first holds its read/write transaction.
        time.sleep(0.15)
    finally:
        release_first.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    _expect(not first.is_alive() and not second.is_alive(), "concurrent writes did not finish")
    _expect(not errors, f"concurrent exceptions: {errors}")
    _expect(len(results) == 2 and all(r == {"ok": True, "changed": True} for r in results),
            f"concurrent results: {results}")
    _expect(set(db.get_sales_links(org)["lines"]) == {"brookvale", "nordvik"},
            "one concurrent mutator lost the other key")


def malformed_identity(kind):
    org, client, _ = _fixture()
    entry = _entry()
    if kind == "long line":
        entry["line"] = "BROOKVALE " + "A" * 111 + " Z"
        key = normalise_match_key(entry["line"])
        entry["members"] = []
    elif kind == "mismatched key":
        key = "nordvikother"
        entry["members"] = []
    elif kind == "long key":
        key = "n" * 121
        entry["members"] = []
    elif kind == "long member code":
        key = normalise_match_key(entry["line"])
        entry["members"][0]["code"] = "B" * 41
        sid = int(db.latest_complete_session_id(org))
        db.execute(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?)",
                   ("B" * 40, "WAREHOUSE", "BROOKVALE OTHER ITEM", "8"))
    else:
        key = normalise_match_key(entry["line"])
        entry["members"] = [{"code": f"BRK{i}", "name": LINE, "key": normalise_match_key(LINE)}
                            for i in range(21)]
        sid = int(db.latest_complete_session_id(org))
        conn = db.get_db()
        try:
            conn.executemany(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?)",
                             [(f"BRK{i}", "WAREHOUSE", f"BROOKVALE ITEM {i}", "8")
                              for i in range(21)])
            conn.commit()
        finally:
            conn.close()
    _seed(org, {key: entry})
    before = _snapshot()
    html = _get(client, org)
    forms = [f for f in _Page(html).forms
             if f.get("action") in ("clear", "relink")
             or (f.get("action") == "set" and f.get("line"))]
    detail = f"{kind}: corrupt identity rendered {len(forms)} correction forms"
    correction = next((f for f in forms if f.get("action") == "set"), None)
    if correction is not None:
        # A real form submission proves why cropping an identity is unsafe.
        submitted = copy.deepcopy(correction)
        if kind in ("long line", "mismatched key", "long key"):
            submitted["codes"] = OTHER_CODE
        response = client.post("/admin/sales-links", data=submitted)
        detail += (f"; Save status {response.status_code}, wrote={_snapshot() != before}, "
                   f"new_identity={normalise_match_key(submitted['line']) not in {key}}")
        _expect(_snapshot() == before, detail)
    _expect(not forms and "unreadable entries" in html, detail)


if __name__ == "__main__":
    _run("corrupt JSON envelopes read empty and render safely", corrupt_envelopes)
    _run("corrupt entries and member values render safely", corrupt_entries_and_members)
    _run("UTF-8 save over 1 MB is refused with the old row intact", _oversize_assert)
    _run("byte-ceiling assertion catches an in-process mutant", cap_mutation_probe)
    _run("all prescribed junk POSTs refuse without writes or crashes", junk_posts)
    _run("saved hostile line and member names render as escaped text", escaped_xss)
    for kind in ("logged-out", "viewer", "revoked cookie"):
        _run(f"{kind} GET and all POST actions are denied", lambda kind=kind: denied_writes(kind))
    _run("missing CSRF is rejected without writes", missing_csrf)
    _run("undo without a previous version is a no-op", undo_without_previous)
    _run("two concurrent mutators preserve both keys", concurrent_mutators)
    for kind in ("long line", "mismatched key", "long key", "long member code", "too many members"):
        _run(f"{kind} renders unreadable with no lossy correction form",
             lambda kind=kind: malformed_identity(kind))
    _check("tester left app-code hashes unchanged", _source_hashes() == _before_hashes)
    print(f"\n{_TOTAL - _FAILED} passed, {_FAILED} failed")
    sys.exit(1 if _FAILED else 0)
