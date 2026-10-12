"""Plan 018 follow-up: the site-admin "set" correction on /admin/sales-links must
compare stock ITEMS, not code text. One item can carry two codes (one per
warehouse) and apply_links treats an item as its name key, so an item put on a
second line through its other code is dropped from BOTH lines and its stock
silently leaves the family. Same rule as tests/test_sales_links_two_codes.py.
Four checks fail on f7368ed; the last fails if the code-text fallback is removed. No model calls; invented brands.
Run: python tests/test_sales_links_admin_two_codes.py
"""
import os
import sys
import tempfile
import types
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.TemporaryDirectory(prefix="berthcast_admin_two_codes_", ignore_cleanup_errors=True)
os.environ["DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_tmp.name, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
_stub = types.ModuleType("anthropic")
_stub.Anthropic = lambda *args, **kwargs: None
_stub.AnthropicError = Exception
sys.modules["anthropic"] = _stub

import database as db                                   # noqa: E402
import app as appmod                                    # noqa: E402
import agents.sales_links as links                      # noqa: E402
from agents.shared import normalise_match_key as nkey   # noqa: E402

appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
_FAILED = 0
LINE = "SPAGHETTI 500G BROOKVALE/NORDVIK"
OFFER = "BROOKVALE PASTA OFFER"
BRK = "BROOKVALE SPAGHETTI 500G"
NRD = "NORDVIK SPAGHETTI 500G"
GRF = "GREENFJORD PENNE 500G"
# The same BROOKVALE item under two codes, as in a two-warehouse export.
STOCK = [("WH1", "BRK-SP500", BRK, "100", "PKT"), ("WH2", "BRK-SP500-B", BRK, "100", "PKT"),
         ("WH1", "NRD-SP500", NRD, "400", "PKT"), ("WH1", "GRF-PN500", GRF, "50", "PKT")]
_serial = 0


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f" [{detail}]" if detail and not cond else ""))
    _FAILED += not cond


def _fixture():
    """A company with one complete stock upload, and a logged-in site admin."""
    global _serial
    _serial += 1
    org = f"BROOKVALE ADMIN TEST {_serial}"
    db.execute("INSERT INTO users (email,password_hash,org_name,is_admin,role) VALUES (?,?,?,?,?)",
               (f"staff{_serial}@example.com", "unused-test-hash", org, 0, "admin"))
    sid = db.execute("INSERT INTO upload_sessions (user_id,org_name,status,created_at) VALUES (?,?,?,?)",
                     (1, org, "complete", "2026-10-10 01:00:00"))
    db.execute(f"CREATE TABLE inventory_{int(sid)} (location_code TEXT, inventory_code TEXT, "
               "description TEXT, qty_on_hand TEXT, uom TEXT)")
    conn = db.get_db()
    try:
        conn.executemany(f"INSERT INTO inventory_{int(sid)} VALUES (?,?,?,?,?)", STOCK)
        conn.commit()
    finally:
        conn.close()
    email = f"siteadmin{_serial}@example.com"
    uid = db.execute("INSERT INTO users (email,password_hash,org_name,is_admin,role) VALUES (?,?,?,?,?)",
                     (email, "unused-test-hash", f"OPERATOR {_serial}", 1, "admin"))
    client = appmod.app.test_client()
    with client.session_transaction() as s:
        s.update(user_id=uid, email=email, org_name=f"OPERATOR {_serial}", is_admin=True,
                 role="admin", sv=0, model="claude-sonnet-5", tier="enterprise")
    return org, client


def _set(client, org, line, codes):
    response = client.post("/admin/sales-links", data={"org": org, "action": "set", "line": line, "codes": codes})
    assert response.status_code == 302 and urlparse(response.location).path == "/admin/sales-links"
    return client.get(response.location).get_data(as_text=True)


def _save(org, line, members):
    def put(lines):
        lines[nkey(line)] = links.make_entry(line, members, "high", "admin")
        return True
    db.update_sales_links(org, put, "operator@example.com")


def _family_keys(org, sales_names):
    ctx = links.apply_links(db.get_sales_links(org)["lines"],
                            [{"inventory_code": c, "description": d} for _, c, d, _, _ in STOCK],
                            "inventory_code", "description", sales_names, {})
    return {f["line"]: set(f["member_keys"]) for f in ctx["families"].values()}


db.init_db()

# 1. The bug: line A holds BROOKVALE by one code; the admin puts the other code on line B.
org, client = _fixture()
_set(client, org, LINE, "BRK-SP500 NRD-SP500")
before = db.get_sales_links(org)["lines"]
page = _set(client, org, OFFER, "BRK-SP500-B")
_check("the second code of an item already on another line is refused",
       "Already on another line:" in page and "BRK-SP500-B" in page and "same item as BRK-SP500" in page,
       page[page.find("flash"):][:300])
_check("the refused correction writes nothing", db.get_sales_links(org)["lines"] == before)
_check("the saved family keeps both brands",
       _family_keys(org, [LINE, OFFER]).get(LINE) == {nkey(BRK), nkey(NRD)}, str(_family_keys(org, [LINE, OFFER])))

# 2. A saved member whose code is gone from the latest stock file still claims its item by key
# (apply_links reads the saved key when the code is missing).
org, client = _fixture()
_save(org, LINE, [{"code": "GONE-1", "name": BRK}])
before = db.get_sales_links(org)["lines"]
page = _set(client, org, OFFER, "BRK-SP500-B")
_check("an item held through a gone code's saved key is refused",
       "Already on another line:" in page and db.get_sales_links(org)["lines"] == before)

# 3. GUARD: a stale saved key (item renamed) does not block the item that now has that name:
# apply_links reads the code, so line A holds NORDVIK, not GREENFJORD.
org, client = _fixture()
_save(org, LINE, [{"code": "NRD-SP500", "key": nkey(GRF), "name": NRD}])
_set(client, org, OFFER, "GRF-PN500")
saved = db.get_sales_links(org)["lines"].get(nkey(OFFER)) or {}
_check("GUARD: a stale saved key never blocks an unrelated item",
       [m["code"] for m in saved.get("members") or []] == ["GRF-PN500"], str(saved))

# 4. GUARD: one line may hold both codes of its own item, and other items stay free.
org, client = _fixture()
_set(client, org, LINE, "BRK-SP500 NRD-SP500")
_set(client, org, LINE, "BRK-SP500 BRK-SP500-B NRD-SP500")
_set(client, org, OFFER, "GRF-PN500")
lines = db.get_sales_links(org)["lines"]
_check("GUARD: editing a line to add its item's second code is accepted",
       [m["code"] for m in lines[nkey(LINE)]["members"]] == ["BRK-SP500", "BRK-SP500-B", "NRD-SP500"])
_check("GUARD: an unrelated item on another line is accepted",
       [m["code"] for m in (lines.get(nkey(OFFER)) or {}).get("members") or []] == ["GRF-PN500"])

# 5. Saved members of any JSON type on another line never crash the check or block an
# unrelated item; a "-----" stock row's code (no item key) is still refused by its text.
STOCK.append(("WH1", "SPACER-1", "-----", "0", "PKT"))
org, client = _fixture()
STOCK.pop()


def _corrupt(lines):
    lines[nkey(LINE)] = {"line": LINE, "conf": "high", "by": "admin",
                         "members": [7, None, {"code": None, "key": ["x"]}, {"code": ["GRF-PN500"], "key": 5},
                                     {"code": "  ", "key": None}, {"code": "SPACER-1", "key": ""}]}
    return True


db.update_sales_links(org, _corrupt, "operator@example.com")
_set(client, org, OFFER, "GRF-PN500")
saved = db.get_sales_links(org)["lines"].get(nkey(OFFER)) or {}
_check("malformed saved members neither crash the check nor block an unrelated item",
       [m["code"] for m in saved.get("members") or []] == ["GRF-PN500"], str(saved))
before = db.get_sales_links(org)["lines"]
page = _set(client, org, "SPACER LINE", "SPACER-1")
_check("a code whose stock name has no item key is still refused by its text",
       "Already on another line: SPACER-1" in page and db.get_sales_links(org)["lines"] == before)

print("RESULT: " + ("FAIL" if _FAILED else "ALL OK"))
sys.exit(1 if _FAILED else 0)
