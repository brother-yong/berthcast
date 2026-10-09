"""Plan 018 commit 3: the AI links new sales lines, Python checks every answer.

Only model calls are canned (the link call through agents.sales_links._call_claude).
The real inventory, recommendation and Flask paths run on invented products and
a temporary database. Every scenario reports on its own, so the pre-build RED
run names each missing behaviour.
Run: python tests/test_sales_links_ai.py
"""
import importlib
import json
import logging
import os
import re
import sys
import tempfile
import types
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.TemporaryDirectory(prefix="berthcast_sales_links_ai_", ignore_cleanup_errors=True)
os.environ["DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_tmp.name, "uploads")
os.environ.pop("RENDER", None)
for _var in ("MAIL_SENDER", "MAIL_APP_PASSWORD", "ALERT_EMAIL"):
    os.environ.pop(_var, None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
_stub = types.ModuleType("anthropic")


class _AnthropicStub:
    def __init__(self, *args, **kwargs):
        pass


_stub.Anthropic = _AnthropicStub
_stub.AnthropicError = Exception
sys.modules["anthropic"] = _stub

import database as db                                   # noqa: E402
import agents.shared as shared                          # noqa: E402
import agents.inventory as inv                          # noqa: E402
import agents.recommendation as rec                     # noqa: E402
import agents.orchestrator as orch                      # noqa: E402
import agents.sales_links as links                      # noqa: E402
from agents.orchestrator import run_pipeline            # noqa: E402
from agents.shared import normalise_match_key as nkey   # noqa: E402
import app as appmod                                    # noqa: E402

_FAILED = 0
_TOTAL = 0
_link_calls = []
_link_mode = {"answers": {}, "replies": [], "raise": None, "unit": "same", "reason": "test"}
_last_inv = {}
_serial = [0]

LINE = "SPAGHETTI 500G BROOKVALE/NORDVIK"
NECTAR_LINE = "ORANGE NECTAR 1L"
COCONUT_LINE = "COCONUT DRINK 330ML"
BRK = "BROOKVALE SPAGHETTI 500G"
NRD = "NORDVIK SPAGHETTI 500G"
NECTAR = "PADIMAS ORANGE NECTAR 1L"
JUICE = "PADIMAS ORANGE JUICE 100% 1L"
COCONUT = "KESSINGTON COCONUT DRINK 330ML"
STOCK = [("BRK-SP500", BRK, "PKT", 0, 0), ("NRD-SP500", NRD, "PKT", 400, 400),
         ("PDM-ON1L", NECTAR, "PKT", 0, 0), ("PDM-OJ1L", JUICE, "PKT", 80, 80),
         ("KES-CD330", COCONUT, "CTN", 0, 0)]
SALES = [(LINE, 300, "BROOKVALE FOODS"), (NECTAR_LINE, 40, ""), (COCONUT_LINE, 20, "")]
ANSWERS = {LINE: (["BRK-SP500", "NRD-SP500"], "high"), NECTAR_LINE: (["PDM-ON1L"], "medium"),
           COCONUT_LINE: (["KES-CD330"], "high")}


def _check(name, cond, detail=""):
    global _FAILED, _TOTAL
    _TOTAL += 1
    print(("ok: " if cond else "FAIL: ") + name + (f" [{detail}]" if detail and not cond else ""))
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


def _fake_inventory(model, system, user, **kwargs):
    rows = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            rows.append({"item": line.split(" | ")[0][6:], "stock": 0, "category": "GENERAL",
                         "status": "CRITICAL", "days_of_supply": 0,
                         "spoilage_risk": "NONE", "observation": "test"})
    return json.dumps(rows)


def _fake_recommendation(model, system, user, **kwargs):
    rows = []
    for block in user.split("---"):
        name = qty = None
        for line in block.splitlines():
            if line.startswith("Item: "):
                name = line[6:].strip()
            elif line.startswith("Pre-computed suggested order quantity: "):
                qty = line.split(": ", 1)[1].strip()
        if name:
            rows.append({"item": name, "suggested_quantity": qty,
                         "recommended_action": "REORDER", "supplier": "Unknown",
                         "supplier_risk": "NONE", "confidence": "HIGH", "flags": []})
    return json.dumps(rows)


def _text(user):
    return "\n".join(b.get("text", "") for b in user) if isinstance(user, list) else str(user)


def _sales_rows(text):
    """The 'row i: NAME | ...' lines of one link prompt's sales block."""
    block = text.split("SALES LINES to link:", 1)[-1]
    return re.findall(r"^row (\d+): (.*?) \| supplier block (.*?) \| sold in the sales file (.*)$",
                      block, re.MULTILINE)


def _fake_link(model, system, user, **kwargs):
    _link_calls.append({"model": model, "system": system, "user": user, "kwargs": kwargs})
    if _link_mode["raise"] is not None:
        raise _link_mode["raise"]
    if _link_mode["replies"]:
        return _link_mode["replies"].pop(0)
    out = []
    for row, name, _sup, _sold in _sales_rows(_text(user)):
        codes, conf = _link_mode["answers"].get(name, ([], "low"))
        obj = {"row": int(row), "codes": list(codes), "unit": _link_mode["unit"],
               "reason": _link_mode["reason"]}
        if conf is not None:
            obj["confidence"] = conf
        out.append(obj)
    return json.dumps(out)


_orig_run_inventory = orch.run_inventory_agent


def _capturing_inventory(*args, **kwargs):
    result = _orig_run_inventory(*args, **kwargs)
    _last_inv.clear()
    _last_inv.update(result)
    return result


shared._call_claude = lambda *a, **k: "{}"
shared.sg_today = lambda: date(2026, 9, 26)
inv._call_claude = _fake_inventory
rec._call_claude = _fake_recommendation
links._call_claude = _fake_link
orch.run_inventory_agent = _capturing_inventory
appmod._send_critical_alert = lambda *a, **k: None
appmod._send_run_failure_alert = lambda *a, **k: None
appmod._send_analysis_ready_email = lambda *a, **k: None
appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
db.init_db()


def _reset(answers=None):
    _link_calls.clear()
    _link_mode.update(answers=dict(ANSWERS if answers is None else answers), replies=[],
                      unit="same", reason="test")
    _link_mode["raise"] = None


def _org():
    _serial[0] += 1
    return f"BROOKVALE AI LINKS {_serial[0]}"


def _seed(org, stock, sales, status="uploading"):
    sid = db.execute("INSERT INTO upload_sessions (user_id,org_name,status,scope,context_json) "
                     "VALUES (?,?,?,?,?)", (1, org, status, "all", "{}"))
    db.execute(f"CREATE TABLE inventory_{sid} (inventory_code TEXT, location_code TEXT, "
               "description TEXT, uom TEXT, qty_on_hand TEXT, free_balance TEXT)")
    db.execute(f"CREATE TABLE sales_{sid} (date TEXT, item_description TEXT, qty_sold TEXT, "
               "supplier TEXT, lead_time_days TEXT)")
    conn = db.get_db()
    try:
        conn.executemany(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?,?,?)",
                         [(code, "WAREHOUSE", desc, uom, str(qty), str(free))
                          for code, desc, uom, qty, free in stock])
        conn.executemany(f"INSERT INTO sales_{sid} VALUES (?,?,?,?,?)",
                         [(f"2026-{month:02}-15", s[0], str(s[1]), s[2] if len(s) > 2 else "", "")
                          for s in sales for month in (6, 7, 8)])
        conn.commit()
    finally:
        conn.close()
    return sid


def _switch_on(org, entries=()):
    def put(lines):
        for entry in entries:
            lines[nkey(entry["line"])] = entry
        return True

    if entries:
        db.update_sales_links(org, put, "operator@example.com")
    db.set_sales_links_enabled(org, True, "operator@example.com")


def _pipeline(sid):
    _last_inv.clear()
    result = run_pipeline(sid, "test", [], {})
    _expect("error" not in result, f"pipeline error: {result.get('error')}")
    return result


def _rec_for(result, name):
    return next((r for r in result.get("recommendations") or []
                 if isinstance(r, dict) and r.get("item") == name), {})


def _saved(org):
    return db.get_sales_links(org)["lines"]


def _codes(entry):
    return [m.get("code") for m in (entry or {}).get("members") or []]


def _notes(result):
    return result.get("link_notes") or []


def _family_ordered(family):
    """One order for both spaghetti brands, sized on their combined free stock."""
    calc = family.get("order_calc") or {}
    return (calc.get("position") == 400 and family.get("suggested_quantity") == f"{calc.get('order')} PKT"
            and len((family.get("sales_link") or {}).get("members") or []) == 2)


def _client(org, admin=False):
    _serial[0] += 1
    email = f"buyer{_serial[0]}@example.com"
    uid = db.execute("INSERT INTO users (email,password_hash,org_name,is_admin,role,model,tier) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (email, "unused-test-hash", org, int(admin), "admin", "test", "enterprise"))
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(user_id=uid, email=email, org_name=org, is_admin=admin, role="admin",
                       sv=0, model="test", tier="enterprise")
    return client


def _admin_for(org):
    _client(org)
    return _client(_org(), admin=True)


def _entry(line, members, conf="high", by="admin"):
    return {"line": line, "members": [{"code": c, "key": nkey(n), "name": n} for c, n in members],
            "conf": conf, "by": by, "at": "2026-09-27",
            "model": "claude-sonnet-5" if by == "ai" else None, "why": "test"}


def _undo_hash(page):
    match = re.search(r'name="links_hash" value="([^"]*)"', page)
    return match.group(1) if match else None


# ── 3.0b: four carried fixes ────────────────────────────────────────────────

def stale_undo_is_refused():
    org = _org()
    admin = _admin_for(org)
    _switch_on(org, [_entry(LINE, [("BRK-SP500", BRK)])])
    db.update_sales_links(org, lambda lines: lines.update(
        {nkey(NECTAR_LINE): _entry(NECTAR_LINE, [("PDM-ON1L", NECTAR)])}) or True, "operator@example.com")
    page = admin.get("/admin/sales-links", query_string={"org": org}).get_data(as_text=True)
    rendered = _undo_hash(page)
    _expect(rendered, "undo form carries no links hash")
    # An AI run saves after the page loaded: the open tab's undo must not undo it.
    db.update_sales_links(org, lambda lines: lines.update(
        {nkey(COCONUT_LINE): _entry(COCONUT_LINE, [("KES-CD330", COCONUT)], by="ai")}) or True,
        "AI (analysis 1)")
    response = admin.post("/admin/sales-links", data={"org": org, "action": "undo", "links_hash": rendered},
                          follow_redirects=True)
    _expect("Links changed since this page loaded" in response.get_data(as_text=True), "stale undo not refused")
    _expect(nkey(COCONUT_LINE) in _saved(org), "stale undo removed the AI save")
    fresh = _undo_hash(admin.get("/admin/sales-links", query_string={"org": org}).get_data(as_text=True))
    admin.post("/admin/sales-links", data={"org": org, "action": "undo", "links_hash": fresh})
    after = _saved(org)
    _expect(nkey(COCONUT_LINE) not in after and nkey(NECTAR_LINE) in after, f"fresh undo did not swap {after}")
    # A double click replays the same form: the second POST must not swap back.
    admin.post("/admin/sales-links", data={"org": org, "action": "undo", "links_hash": fresh})
    _expect(_saved(org) == after, "replayed undo swapped back")


def separator_codes_are_never_saved():
    org = _org()
    stock = [(f"BRK-{i:02d}", f"BROOKVALE ITEM {i:02d} 1KG", "PKT", 5, 5) for i in range(17)]
    stock += [("BRK 100", "BROOKVALE PENNE 500G", "PKT", 0, 0), ("NRD,200", "NORDVIK PENNE 1KG", "PKT", 0, 0),
              ("PDM;300", "PADIMAS PENNE 250G", "PKT", 0, 0)]
    sid = _seed(org, stock, [("PENNE 500G BROOKVALE", 30)], status="complete")
    found, col = links.stock_codes(sid)
    _expect(col == "inventory_code" and not {"BRK 100", "NRD,200", "PDM;300"} & set(found),
            f"stock_codes kept separator codes {sorted(found)}")
    _reset({"PENNE 500G BROOKVALE": (["BRK 100"], "high")})
    _switch_on(org)
    _pipeline(_seed(org, stock, [("PENNE 500G BROOKVALE", 30)]))
    stock_text = _link_calls[0]["user"][0]["text"] if _link_calls else ""
    _expect("BRK 100 |" not in stock_text and "BRK-00 |" in stock_text, "separator code in the stock list")
    entry = _saved(org).get(nkey("PENNE 500G BROOKVALE")) or {}
    _expect(entry.get("members") == [] and entry.get("why") == "codes removed by checks", f"entry {entry}")


def lone_surrogates_still_save():
    org = _org()
    entry = links.make_entry("BROOKVALE \ud83d PASTA", [{"code": "BRK-SP500", "name": "BROOKVALE \ud83d PASTA 1KG"}],
                             "high", "ai", model=links.LINK_MODEL, why="odd \ud83d reason")
    result = db.update_sales_links(org, lambda lines: lines.update({nkey(entry["line"]): entry}) or True, "test")
    _expect(result.get("ok") and result.get("changed"), f"save {result}")
    admin = _admin_for(org)
    _expect(admin.get("/admin/sales-links", query_string={"org": org}).status_code == 200, "admin page broke")
    # The same character arriving in a model reply.
    org2 = _org()
    _reset()
    _link_mode["reason"] = "\ud83d"
    _switch_on(org2)
    _pipeline(_seed(org2, STOCK, SALES))
    _expect(len(_saved(org2)) == 3, f"AI answers not saved: {list(_saved(org2))}")


def shared_code_is_not_an_identity():
    cheddar, sardines = "BROOKVALE CHEDDAR 200G", "NORDVIK SARDINES 155G"
    rows = [{"inventory_code": "0", "description": cheddar}, {"inventory_code": "0", "description": sardines}]
    line = "CHEDDAR 200G BROOKVALE"
    found = links.apply_links({nkey(line): _entry(line, [("0", cheddar)], by="ai")}, rows,
                              "inventory_code", "description", [line], {})
    members = [f["member_keys"] for f in found["families"].values()]
    _expect(members == [[nkey(cheddar)]], f"shared code absorbed {members}")
    _expect(any('"0"' in n or ": 0" in n or " 0." in n for n in found["notes"]), f"notes {found['notes']}")
    unknown = _entry(line, [("0", cheddar)], by="ai")
    unknown["members"][0]["key"] = ""
    found = links.apply_links({nkey(line): unknown}, rows, "inventory_code", "description", [line], {})
    _expect(found["families"] == {}, f"keyless shared code applied {found['families']}")
    # stock_codes reports it, the admin form refuses it, and the AI cannot save it.
    org = _org()
    stock = [(f"GRF-{i:02d}", f"GREENFJORD ITEM {i:02d} 1KG", "PKT", 5, 5) for i in range(18)]
    stock += [("0", cheddar, "PKT", 0, 0), ("0", sardines, "PKT", 0, 0)]
    sid = _seed(org, stock, [(line, 30)], status="complete")
    shared_codes = set()
    links.stock_codes(sid, shared_codes)
    _expect(shared_codes == {"0"}, f"shared {shared_codes}")
    admin = _admin_for(org)
    page = admin.post("/admin/sales-links", data={"org": org, "action": "set", "line": line, "codes": "0"},
                      follow_redirects=True).get_data(as_text=True)
    _expect("more than one item" in page and _saved(org) == {}, "admin form saved a shared code")
    _reset({line: (["0"], "high")})
    _switch_on(org)
    _pipeline(_seed(org, stock, [(line, 30)]))
    entry = _saved(org).get(nkey(line)) or {}
    _expect(entry.get("members") == [], f"AI saved a shared code {entry}")


# ── 1-15: plan section 6, commit 3 ──────────────────────────────────────────

def first_run_links_new_lines():
    rl = importlib.import_module("rec_logic")
    org = _org()
    _reset()
    _switch_on(org)
    sid = _seed(org, STOCK, SALES)
    result = _pipeline(sid)
    _expect(len(_link_calls) == 1, f"{len(_link_calls)} link calls")
    call = _link_calls[0]
    _expect(call["model"] == links.LINK_MODEL and call["system"] == links.LINK_SYSTEM
            and call["kwargs"] == {"max_tokens": 8000, "timeout": 180}, f"call {call['kwargs']}")
    _expect("quantity sold vs stock on hand: same | different | unsure." in links.LINK_SYSTEM
            and links.LINK_SYSTEM.endswith(shared.UNTRUSTED_GUARD), "prompt edit or guard missing")
    stock_block, sales_block = call["user"]
    _expect(stock_block.get("cache_control") == {"type": "ephemeral"} and "cache_control" not in sales_block,
            "only the resent stock list is cached")
    _expect(stock_block["text"].startswith("STOCK LIST (code | description | UOM | category | on hand):\n"
                                           "<untrusted_data>\n")
            and "\nNRD-SP500 | NORDVIK SPAGHETTI 500G | PKT | WAREHOUSE | on hand 400\n" in stock_block["text"]
            and stock_block["text"].endswith("</untrusted_data>"), f"stock block {stock_block['text']!r}")
    _expect(sales_block["text"].startswith("SALES LINES to link:\n<untrusted_data>\n")
            and sales_block["text"].endswith("</untrusted_data>"), "sales block not fenced")
    rows = _sales_rows(sales_block["text"])
    _expect(sorted(r[1] for r in rows) == sorted(s[0] for s in SALES), f"rows {rows}")
    spag = next(r for r in rows if r[1] == LINE)
    _expect(spag[2] == "BROOKVALE FOODS" and spag[3] == "900", f"spaghetti row {spag}")
    _expect(next(r for r in rows if r[1] == NECTAR_LINE)[2] == "none", "a line with no supplier says none")
    state = db.get_sales_links(org)
    _expect(state["updated_by"] == f"AI (analysis {sid})", f"updated_by {state['updated_by']}")
    saved = state["lines"]
    _expect(set(saved) == {nkey(s[0]) for s in SALES}, f"saved {list(saved)}")
    _expect(all(e["by"] == "ai" and e["model"] == links.LINK_MODEL for e in saved.values()), "author or model")
    _expect([saved[nkey(n)]["conf"] for n in (LINE, NECTAR_LINE, COCONUT_LINE)] == ["high", "medium", "high"],
            "confidence not as replied")
    _expect(_codes(saved[nkey(LINE)]) == ["BRK-SP500", "NRD-SP500"]
            and [m["name"] for m in saved[nkey(LINE)]["members"]] == [BRK, NRD], "members not from the stock file")
    family = _rec_for(result, LINE)
    _expect(_family_ordered(family), f"family {family}")
    _expect((_rec_for(result, NECTAR).get("flags") or [None])[0] == rl.LINK_UNSURE_FLAG, "unsure link not flagged")
    _expect(any("Linked 3 new sales line(s) with AI: 1 unsure" in n for n in _notes(result)), f"{_notes(result)}")


def second_run_makes_no_calls():
    org = _org()
    _reset()
    _switch_on(org)
    first = _pipeline(_seed(org, STOCK, SALES))
    _expect(len(_link_calls) == 1, "control: the first run links the lines")
    _reset()
    second = _pipeline(_seed(org, STOCK, SALES))
    _expect(_link_calls == [], f"{len(_link_calls)} calls on the second run")
    for name in (LINE, NECTAR, COCONUT):
        a, b = _rec_for(first, name), _rec_for(second, name)
        _expect(a.get("suggested_quantity") == b.get("suggested_quantity")
                and a.get("sales_link") == b.get("sales_link"), f"{name} changed")


def only_the_new_line_is_sent():
    org = _org()
    _reset()
    _switch_on(org)
    _pipeline(_seed(org, STOCK, SALES))
    _reset({"ORANGE JUICE 1L": (["PDM-OJ1L"], "high")})
    _pipeline(_seed(org, STOCK, SALES + [("ORANGE JUICE 1L", 15, "")]))
    _expect(len(_link_calls) == 1, f"{len(_link_calls)} calls")
    rows = _sales_rows(_text(_link_calls[0]["user"]))
    _expect([(r[0], r[1]) for r in rows] == [("1", "ORANGE JUICE 1L")], f"rows {rows}")
    _expect(_codes(_saved(org).get(nkey("ORANGE JUICE 1L"))) == ["PDM-OJ1L"], "fourth line not saved")


def batches_of_twenty():
    org = _org()
    _reset({})
    _switch_on(org)
    _pipeline(_seed(org, STOCK, [(f"GREENFJORD LINE {i:02d}", 5) for i in range(45)]))
    sizes = [len(_sales_rows(_text(c["user"]))) for c in _link_calls]
    _expect(sizes == [20, 20, 5], f"batches {sizes}")
    _expect(len(_saved(org)) == 45, f"{len(_saved(org))} saved")


def one_retry_on_unreadable_reply():
    org = _org()
    _reset()
    _link_mode["replies"] = ["Sorry, I cannot help with that."]
    _switch_on(org)
    _pipeline(_seed(org, STOCK, SALES))
    _expect(len(_link_calls) == 2, f"{len(_link_calls)} calls")
    _expect(len(_saved(org)) == 3, f"saved {list(_saved(org))}")


def two_unreadable_replies_save_nothing():
    org = _org()
    _reset()
    _link_mode["replies"] = ["not json", "still not json"]
    _switch_on(org)
    result = _pipeline(_seed(org, STOCK, SALES))
    _expect(len(_link_calls) == 2 and _saved(org) == {}, f"{len(_link_calls)} calls, saved {_saved(org)}")
    _expect(any("no usable AI reply" in n and LINE in n and COCONUT_LINE in n for n in _notes(result)),
            f"notes {_notes(result)}")
    _reset()
    _pipeline(_seed(org, STOCK, SALES))
    _expect(len(_link_calls) == 1 and len(_sales_rows(_text(_link_calls[0]["user"]))) == 3,
            "the next run must send the lines again")


def call_failure_keeps_saved_links():
    org = _org()
    _reset()
    _link_mode["raise"] = RuntimeError("stub: link call failed")
    _switch_on(org, [_entry(LINE, [("BRK-SP500", BRK), ("NRD-SP500", NRD)])])
    result = _pipeline(_seed(org, STOCK, SALES))
    _expect(len(_link_calls) == 1, f"{len(_link_calls)} calls")
    _expect(_family_ordered(_rec_for(result, LINE)), "saved link not applied")
    _expect(set(_saved(org)) == {nkey(LINE)}, f"saved {list(_saved(org))}")
    _expect(any("AI linking stopped" in n and "RuntimeError" in n for n in _notes(result)), f"{_notes(result)}")


def python_checks_drop_codes():
    org = _org()
    lines = {"PASTA 500G NORDVIK": (["NRD-SP500", "INVENTED-1"], "high"),
             NECTAR_LINE: (["PDM-ON1L"], "high"), "NECTAR ORANGE PADIMAS": (["PDM-ON1L"], "medium"),
             COCONUT_LINE: (["KES-CD330"], "medium"), "KESSINGTON COCONUT 330": (["KES-CD330"], "medium")}
    _reset(lines)
    _switch_on(org, [_entry(LINE, [("BRK-SP500", BRK), ("NRD-SP500", NRD)])])
    result = _pipeline(_seed(org, STOCK, [(LINE, 300)] + [(n, 10) for n in lines]))
    saved = _saved(org)
    _expect(_codes(saved[nkey(NECTAR_LINE)]) == ["PDM-ON1L"], "high must keep a contested code")
    for name in ("PASTA 500G NORDVIK", "NECTAR ORANGE PADIMAS", COCONUT_LINE, "KESSINGTON COCONUT 330"):
        entry = saved[nkey(name)]
        _expect(entry["members"] == [] and entry["why"] == "codes removed by checks", f"{name}: {entry}")
    _expect(_codes(saved[nkey(LINE)]) == ["BRK-SP500", "NRD-SP500"] and saved[nkey(LINE)]["by"] == "admin",
            "the saved line changed")
    notes = " | ".join(_notes(result))
    for text in ("1 AI code(s) were not in this stock file", "1 AI code(s) were already on a saved line",
                 "2 AI code(s) were claimed by two new lines"):
        _expect(text in notes, f"missing {text!r} in {notes}")


def over_the_item_cap():
    org = _org()
    stock = [(f"GRF-{i:02d}", f"GREENFJORD ITEM {i:02d} 1KG", "PKT", 0, 0) for i in range(21)]
    _reset({"GREENFJORD MIX 1KG": ([s[0] for s in stock], "high")})
    _switch_on(org)
    _pipeline(_seed(org, stock, [("GREENFJORD MIX 1KG", 10)]))
    entry = _saved(org).get(nkey("GREENFJORD MIX 1KG")) or {}
    _expect(entry.get("members") == [] and entry.get("why") == "over the item cap", f"entry {entry}")


def confidence_is_normalised():
    org = _org()
    names = ["BROOKVALE A", "NORDVIK B", "PADIMAS C", "KESSINGTON D"]
    stock = [(f"C-{i}", f"{n} ITEM 1KG", "PKT", 0, 0) for i, n in enumerate(names)]
    replies = ["HIGH", "Medium", "sure", None]
    _reset({n: ([f"C-{i}"], c) for i, (n, c) in enumerate(zip(names, replies))})
    _switch_on(org)
    _pipeline(_seed(org, stock, [(n, 10) for n in names]))
    saved = _saved(org)
    _expect([saved[nkey(n)]["conf"] for n in names] == ["high", "medium", "low", "low"],
            f"conf {[saved[nkey(n)]['conf'] for n in names]}")


def unit_field_is_ignored():
    outcomes = []
    for unit in ("same", "different"):
        org = _org()
        _reset()
        _link_mode["unit"] = unit
        _switch_on(org)
        result = _pipeline(_seed(org, STOCK, SALES))
        outcomes.append((result["recommendations"], _saved(org)))
    _expect(outcomes[0] == outcomes[1], "the unit field changed the recs or the saved entries")
    _expect(not any("unit" in e for e in outcomes[0][1].values()), "a unit was saved")
    _expect(len(outcomes[0][1]) == 3, "control: the entries were saved")


def switch_off_or_no_code_column():
    org = _org()
    _reset()
    _pipeline(_seed(org, STOCK, SALES))
    _expect(_link_calls == [] and _saved(org) == {}, "switch off still called or saved")
    _switch_on(org)
    no_codes = [("SAME", d, u, q, f) for _c, d, u, q, f in STOCK]
    result = _pipeline(_seed(org, no_codes, SALES))
    _expect(_link_calls == [] and _saved(org) == {}, "no code column still called or saved")
    _expect(any("no usable item code column" in n for n in _notes(result)), f"notes {_notes(result)}")
    _pipeline(_seed(org, STOCK, SALES))
    _expect(len(_link_calls) == 1, "control: the same company with codes links its lines")


def per_run_line_cap():
    org = _org()
    _reset({})
    _switch_on(org)
    result = _pipeline(_seed(org, STOCK, [(f"GREENFJORD LINE {i:03d}", 1) for i in range(310)]))
    _expect(len(_saved(org)) == 300 and len(_link_calls) == 15, f"{len(_saved(org))} saved, {len(_link_calls)} calls")
    _expect(any("10 new sales line(s) wait" in n for n in _notes(result)), f"notes {_notes(result)}")


def time_budget_stops_new_batches():
    org = _org()
    _reset({})
    had = hasattr(links, "_now")
    original = getattr(links, "_now", None)
    links._now = lambda: 0 if not _link_calls else links.LINK_BUDGET_S + 1
    try:
        _switch_on(org)
        result = _pipeline(_seed(org, STOCK, [(f"GREENFJORD LINE {i:02d}", 5) for i in range(25)]))
    finally:
        if had:
            links._now = original
        else:
            del links._now
    _expect(len(_link_calls) == 1 and len(_saved(org)) == 20, f"{len(_link_calls)} calls, {len(_saved(org))} saved")
    _expect(any("5 new sales line(s) wait" in n and "time limit" in n for n in _notes(result)),
            f"notes {_notes(result)}")


def staff_note_variant_is_not_sent():
    # Not in the plan's list: added with the new-line filter that keeps the
    # 018-2 spelling GUARD green (a line plus "<- out of stock" is that line).
    org = _org()
    noted = LINE + " <- out of stock"
    _reset()
    _switch_on(org)
    _pipeline(_seed(org, STOCK, SALES + [(noted, 10)]))
    sent = [r[1] for c in _link_calls for r in _sales_rows(_text(c["user"]))]
    _expect(sorted(sent) == sorted(s[0] for s in SALES), f"sent {sent}")
    _expect(_codes(_saved(org).get(nkey(LINE))) == ["BRK-SP500", "NRD-SP500"], "the line lost its codes")
    _expect(nkey(noted) not in _saved(org), "the noted variant was saved")


def admin_counter():
    org = _org()
    admin = _admin_for(org)
    ai = {"line": LINE, "sure": True, "ai": True, "label": "Free stock", "members": [], "more": 0}

    def run(created, recs, status="complete"):
        sid = db.execute("INSERT INTO upload_sessions (user_id,org_name,status,created_at) VALUES (?,?,?,?)",
                         (1, org, status, created))
        db.execute("INSERT INTO analysis_results (session_id,inventory_report,recommendations_json) "
                   "VALUES (?,?,?)", (sid, "[]", json.dumps(recs)))

    run("2026-09-01 00:00:00", [{"item": f"OLD {i}", "sales_link": ai, "dismissed": True} for i in range(5)])
    run("2026-09-10 00:00:00", [{"item": "A", "sales_link": ai, "dismissed": True}])
    run("2026-09-20 00:00:00", [
        {"item": "B", "sales_link": ai, "dismissed": True, "suggested_quantity": "650 PKT"},
        {"item": "C", "sales_link": ai, "approved": True, "suggested_quantity": "650 PKT",
         "edited_quantity": "100 PKT"},
        {"item": "D", "sales_link": ai, "approved": True, "suggested_quantity": "40 PKT",
         "edited_quantity": "40 PKT"},
        {"item": "E", "sales_link": dict(ai, ai=False), "dismissed": True},
        {"item": "F", "error": "x", "sales_link": ai, "dismissed": True},
        "junk"])
    run("2026-09-30 00:00:00", [{"item": "G", "sales_link": ai, "dismissed": True}], status="running")
    page = admin.get("/admin/sales-links", query_string={"org": org}).get_data(as_text=True)
    _expect("Last 2 runs: 4 AI-linked orders, 2 dismissed, 1 quantity changed." in page,
            f"counter missing: {re.findall(r'Last 2 runs:[^<]*', page)}")
    _expect("Kill switch: more than 1 in 5 rejected means switch off." in page, "kill switch line missing")


_run("RED 3.0b-1: a stale or replayed undo is refused inside the lock", stale_undo_is_refused)
_run("RED 3.0b-2: codes with a space, comma or semicolon are never offered or saved", separator_codes_are_never_saved)
_run("RED 3.0b-3: a lone surrogate in saved text still saves and renders", lone_surrogates_still_save)
_run("RED 3.0b-4: a code shared by different items is not an identity", shared_code_is_not_an_identity)
_run("RED 1: three new lines, one call, fenced prompt, saved and applied", first_run_links_new_lines)
_run("RED 2: a second run with the same lines makes no call", second_run_makes_no_calls)
_run("RED 3: a new fourth line is the only line sent", only_the_new_line_is_sent)
_run("RED 4: 45 new lines go in batches of 20, 20 and 5", batches_of_twenty)
_run("RED 5: one retry after an unreadable reply", one_retry_on_unreadable_reply)
_run("RED 6: two unreadable replies save nothing and the lines are sent again", two_unreadable_replies_save_nothing)
_run("RED 7: a failing call keeps the run and the saved links", call_failure_keeps_saved_links)
_run("RED 8: invented, saved-line and contested codes are dropped and counted", python_checks_drop_codes)
_run("RED 9: more than 20 codes saves no family", over_the_item_cap)
_run("RED 10: confidence is normalised", confidence_is_normalised)
_run("RED 11: the unit field changes nothing and is never saved", unit_field_is_ignored)
_run("RED 12: switch off or no code column makes no call", switch_off_or_no_code_column)
_run("RED 13: at most 300 new lines per run", per_run_line_cap)
_run("RED 14: no new batch after the time budget", time_budget_stops_new_batches)
_run("RED 15: the admin page counts AI-linked orders over the last 2 runs", admin_counter)
_run("RED 16: a line plus a staff note is not sent as a line of its own", staff_note_variant_is_not_sent)

logging.shutdown()
_tmp.cleanup()
print(f"\n{_TOTAL - _FAILED} passed, {_FAILED} failed")
sys.exit(1 if _FAILED else 0)
