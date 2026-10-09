"""Plan 018 commit 2: saved sales links shape the order (no AI call).

Only model calls are canned. The real inventory, recommendation and Flask
paths run on invented products and a temporary database. Every scenario
reports on its own, so the pre-build RED run names each missing behaviour.
Run: python tests/test_sales_links_apply.py
"""
import csv
import importlib
import io
import json
import logging
import os
import sys
import tempfile
import types
from datetime import date
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.TemporaryDirectory(prefix="berthcast_sales_links_apply_", ignore_cleanup_errors=True)
os.environ["DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_tmp.name, "uploads")
os.environ.pop("RENDER", None)
# Inline threads below would otherwise really send mail from a machine that
# happens to hold the mail settings.
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
import rate_limit                                       # noqa: E402
import agents.shared as shared                          # noqa: E402
import agents.inventory as inv                          # noqa: E402
import agents.recommendation as rec                     # noqa: E402
import agents.orchestrator as orch                      # noqa: E402
from agents.orchestrator import run_pipeline            # noqa: E402
from agents.shared import normalise_match_key as nkey   # noqa: E402
import app as appmod                                    # noqa: E402

_FAILED = 0
_TOTAL = 0
_inv_prompts, _rec_prompts = [], []
_last_inv = {}
_critical_calls, _failure_calls = [], []
_serial = [0]

LINE = "SPAGHETTI 500G BROOKVALE/NORDVIK"
BRK = "BROOKVALE SPAGHETTI 500G"
NRD = "NORDVIK SPAGHETTI 500G"
NECTAR = "PADIMAS ORANGE NECTAR 1L"
SPAG_STOCK = [("BRK-SP500", BRK, "PKT", 0, 0), ("NRD-SP500", NRD, "PKT", 400, 400)]
SPAG_SALES = [(LINE, 300)]
SPAG_MEMBERS = [("BRK-SP500", BRK), ("NRD-SP500", NRD)]
STOCK_TEXT = f"Free stock by item: {BRK} 0 PKT; {NRD} 400 PKT."
LONG = "BROOKVALE SPAGHETTI " + "EXTRA FINE DURUM WHEAT " * 6 + "500G"


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
    _inv_prompts.append(user)
    rows = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            rows.append({"item": line.split(" | ")[0][6:], "stock": 0, "category": "GENERAL",
                         "status": "CRITICAL", "days_of_supply": 0,
                         "spoilage_risk": "NONE", "observation": "test"})
    return json.dumps(rows)


def _fake_recommendation(model, system, user, **kwargs):
    _rec_prompts.append(user)
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
orch.run_inventory_agent = _capturing_inventory
appmod._send_critical_alert = lambda *a, **k: _critical_calls.append(a)
appmod._send_run_failure_alert = lambda *a, **k: _failure_calls.append(a)
appmod._send_analysis_ready_email = lambda *a, **k: None
appmod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
db.init_db()


def _org():
    _serial[0] += 1
    return f"BROOKVALE LINKS {_serial[0]}"


def _seed(org, stock, sales, status="uploading"):
    sid = db.execute("INSERT INTO upload_sessions (user_id,org_name,status,scope,context_json) "
                     "VALUES (?,?,?,?,?)", (1, org, status, "all", "{}"))
    db.execute(f"CREATE TABLE inventory_{sid} (inventory_code TEXT, location_code TEXT, "
               "description TEXT, uom TEXT, qty_on_hand TEXT, free_balance TEXT)")
    db.execute(f"CREATE TABLE sales_{sid} (date TEXT, item_description TEXT, qty_sold TEXT, "
               "supplier TEXT, lead_time_days TEXT)")
    for code, desc, uom, qty, free in stock:
        db.execute(f"INSERT INTO inventory_{sid} VALUES (?,?,?,?,?,?)",
                   (code, "WAREHOUSE", desc, uom, str(qty), str(free)))
    for name, monthly in sales:
        for month in (6, 7, 8):
            db.execute(f"INSERT INTO sales_{sid} VALUES (?,?,?,?,?)",
                       (f"2026-{month:02}-15", name, str(monthly), "", ""))
    return sid


def _entry(line, members, conf="high", by="ai"):
    return {"line": line, "members": [{"code": c, "key": nkey(n), "name": n} for c, n in members],
            "conf": conf, "by": by, "at": "2026-09-27",
            "model": "claude-sonnet-5" if by == "ai" else None, "why": "test"}


def _save_links(org, entries, enabled=True):
    def put(lines):
        for entry in entries:
            lines[nkey(entry["line"])] = entry
        return True

    db.update_sales_links(org, put, "operator@example.com")
    db.set_sales_links_enabled(org, enabled, "operator@example.com")


def _pipeline(sid, groups=None):
    _inv_prompts.clear()
    _rec_prompts.clear()
    _last_inv.clear()
    result = run_pipeline(sid, "test", groups or [], {})
    _expect("error" not in result, f"pipeline error: {result.get('error')}")
    return result, dict(_last_inv.get("row_numbers") or {})


def _item_lines():
    return [line for prompt in _inv_prompts for line in prompt.splitlines() if line.startswith("Item: ")]


def _rec_for(result, name):
    return next((r for r in result.get("recommendations") or []
                 if isinstance(r, dict) and r.get("item") == name), {})


def _report_row(result, name):
    return next((r for r in result.get("inventory_report") or []
                 if isinstance(r, dict) and r.get("item") == name), {})


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
    return client, uid


def _store(sid, result):
    db.execute("DELETE FROM analysis_results WHERE session_id=?", (sid,))
    db.execute("INSERT INTO analysis_results (session_id,inventory_report,recommendations_json,data_notes) "
               "VALUES (?,?,?,?)", (sid, json.dumps(result["inventory_report"]),
                                    json.dumps(result["recommendations"]),
                                    json.dumps(result.get("data_notes") or [])))
    db.execute("UPDATE upload_sessions SET status='complete' WHERE id=?", (sid,))


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _analyse(client, sid):
    db.execute("DELETE FROM analysis_results WHERE session_id=?", (sid,))
    db.execute("INSERT INTO analysis_results (session_id,inventory_report,recommendations_json) "
               "VALUES (?,?,?)", (sid, json.dumps({"confirmed_groups": []}), "[]"))
    rate_limit._hits.clear()
    original = appmod.threading.Thread
    appmod.threading.Thread = _InlineThread
    try:
        response = client.get(f"/analyse/{sid}")
    finally:
        appmod.threading.Thread = original
    _expect(response.status_code == 200, f"/analyse status {response.status_code}")
    status = db.query("SELECT status FROM upload_sessions WHERE id=?", (sid,))[0]["status"]
    _expect(status == "complete", f"analysis ended as {status}")


class _CapturingAnthropic:
    prompts = []

    def __init__(self, **kwargs):
        pass

    class _Messages:
        def stream(self, **kwargs):
            _CapturingAnthropic.prompts.append(kwargs["messages"][0]["content"])
            raise RuntimeError("stub: tests never call the real API")

    @property
    def messages(self):
        return self._Messages()


# ── 1-16: plan section 6, commit 2 ──────────────────────────────────────────

def multi_brand_family():
    rl = importlib.import_module("rec_logic")
    org = _org()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS)])
    result, stamps = _pipeline(_seed(org, SPAG_STOCK, SPAG_SALES))
    lines = _item_lines()
    _expect(sum(line.startswith(f"Item: {LINE} |") for line in lines) == 1, f"family prompt lines {lines}")
    _expect(not any(line.startswith((f"Item: {BRK} |", f"Item: {NRD} |")) for line in lines),
            "brand rows must not reach the prompt")
    _expect((stamps.get(nkey(LINE)) or {}).get("position") == 400, f"stamp {stamps.get(nkey(LINE))}")
    family = _rec_for(result, LINE)
    _expect(family.get("suggested_quantity") == "650 PKT", f"family rec {family}")
    members = (family.get("sales_link") or {}).get("members") or []
    _expect([(m.get("name"), m.get("free")) for m in members] == [(BRK, 0), (NRD, 400)],
            f"members {members}")
    shown = rl._link_display(family) or {}
    _expect(shown.get("stock") == STOCK_TEXT, f"stock line {shown}")
    _expect(shown.get("warning") == "" and rl.LINK_UNSURE_FLAG not in (family.get("flags") or []),
            "a sure link must not warn")
    row = _report_row(result, LINE)
    _expect(row.get("link_members") == [BRK, NRD], f"report row {row}")
    _expect(STOCK_TEXT in str(row.get("observation")), f"observation {row.get('observation')}")


def unsure_family_warning():
    rl = importlib.import_module("rec_logic")
    flag = rl.LINK_UNSURE_FLAG
    org = _org()
    client, _ = _client(org)
    _save_links(org, [_entry(LINE, SPAG_MEMBERS, conf="medium")])
    sid = _seed(org, SPAG_STOCK, SPAG_SALES)
    result, _ = _pipeline(sid)
    family = _rec_for(result, LINE)
    _expect((family.get("flags") or [None])[0] == flag, f"flags {family.get('flags')}")
    _store(sid, result)
    page = client.get(f"/results/{sid}").get_data(as_text=True)
    _expect('<span class="rec-row-lowconf">check match</span>' in page, "check match tag missing")
    _expect(f'<p class="rec-qty-basis"><strong>{flag}.</strong></p>' in page, "card warning missing")
    _expect(f'<p class="rec-qty-basis">{STOCK_TEXT}</p>' in page, "card stock line missing")
    printed = client.get(f"/results/{sid}/print").get_data(as_text=True)
    _expect(f'<div class="note"><strong>{flag}.</strong></div>' in printed, "print warning missing")
    _expect(f'<div class="note">{STOCK_TEXT}</div>' in printed, "print stock line missing")


def single_item_link():
    rl = importlib.import_module("rec_logic")
    oyster_line, oyster = "BRKVL OYSTR SCE 510G", "BROOKVALE OYSTER SAUCE 510G"
    org = _org()
    _save_links(org, [_entry(oyster_line, [("BRK-OY510", oyster)])])
    result, _ = _pipeline(_seed(org, [("BRK-OY510", oyster, "BTL", 0, 0)], [(oyster_line, 100)]))
    item = _rec_for(result, oyster)
    _expect(item, f"rec must keep the stock item's name: {result.get('recommendations')}")
    _expect((item.get("order_calc") or {}).get("sales_from") == [oyster_line], f"calc {item.get('order_calc')}")
    _expect(isinstance(item.get("sales_link"), dict), "single-item rec lost its link")
    _expect((rl._link_display(item) or {}).get("stock") is None, "one item needs no stock-by-item line")


def admin_low_is_sure():
    rl = importlib.import_module("rec_logic")
    org = _org()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS, conf="low", by="admin")])
    result, _ = _pipeline(_seed(org, SPAG_STOCK, SPAG_SALES))
    family = _rec_for(result, LINE)
    _expect((family.get("sales_link") or {}).get("sure") is True, f"family {family}")
    _expect(rl.LINK_UNSURE_FLAG not in (family.get("flags") or []), "admin link flagged unsure")
    _expect((rl._link_display(family) or {}).get("warning") == "", "admin link warned")


def switch_off_guard():
    # Two fresh companies: a second run in one company would see the first
    # run's outcome history ("Past recs") in its prompt, links or not.
    stock = SPAG_STOCK + [("PDM-ON1L", NECTAR, "PKT", 0, 0)]
    sales = SPAG_SALES + [(NECTAR, 40)]
    first, first_stamps = _pipeline(_seed(_org(), stock, sales))
    first_prompts, first_keys = (list(_inv_prompts), list(_rec_prompts)), set(_last_inv)
    org = _org()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS, conf="medium")], enabled=False)
    second, second_stamps = _pipeline(_seed(org, stock, sales))
    _expect(first_prompts == (list(_inv_prompts), list(_rec_prompts)), "prompts changed with the switch off")
    _expect(first_stamps == second_stamps, "stamps changed with the switch off")
    _expect(first == second, "report or recommendations changed with the switch off")
    _expect(first_keys == set(_last_inv) and "link_notes" not in second, "extra return keys with the switch off")
    _expect(bool(first.get("recommendations")), "guard needs at least one order to compare")


def staff_group_dropped():
    org = _org()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS)])
    groups = [{"canonical": NRD, "variants": ["NORDVIK SPAG 500G PROMO"]}]
    result, stamps = _pipeline(_seed(org, SPAG_STOCK, SPAG_SALES), groups)
    stamp = stamps.get(nkey(LINE)) or {}
    _expect(stamp.get("position") == 400 and stamp.get("sales_sources") == [LINE], f"stamp {stamp}")
    _expect(_rec_for(result, LINE).get("suggested_quantity") == "650 PKT", "family changed")
    notes = result.get("link_notes") or []
    _expect(any(n.startswith("1 staff duplicate group") and NRD in n for n in notes), f"notes {notes}")


def exact_name_rule():
    org = _org()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS)])
    result, stamps = _pipeline(_seed(org, SPAG_STOCK, SPAG_SALES + [(NRD, 50)]))
    lines = _item_lines()
    _expect(not any(line.startswith(f"Item: {LINE} |") for line in lines), f"lines {lines}")
    brookvale = _rec_for(result, BRK)
    _expect(isinstance(brookvale.get("sales_link"), dict), f"single family rec {brookvale}")
    _expect((brookvale.get("order_calc") or {}).get("sales_from") == [LINE], "family must be fed by the line")
    _expect((stamps.get(nkey(NRD)) or {}).get("sales_sources") == [NRD], f"NORDVIK stamp {stamps.get(nkey(NRD))}")


def code_on_two_lines():
    other = "PASTA 500G NORDVIK"
    org = _org()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS), _entry(other, [("NRD-SP500", NRD)])])
    result, stamps = _pipeline(_seed(org, SPAG_STOCK, SPAG_SALES + [(other, 20)]))
    members = (_rec_for(result, BRK).get("sales_link") or {}).get("members") or []
    _expect([m.get("name") for m in members] == [BRK], f"members {members}")
    _expect((stamps.get(nkey(NRD)) or {}).get("sales_sources") == [], "shared code must leave both lines")
    notes = result.get("link_notes") or []
    _expect(any("more than one linked line" in n and NRD in n for n in notes), f"notes {notes}")


def mixed_units_review():
    org = _org()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS)])
    stock = [("BRK-SP500", BRK, "PKT", 0, 0), ("NRD-SP500", NRD, "CTN", 400, 400)]
    result, _ = _pipeline(_seed(org, stock, SPAG_SALES))
    rows = [r for r in result["inventory_report"] if r.get("item") in (f"{BRK} (PKT)", f"{NRD} (CTN)")]
    _expect(len(rows) == 2 and all(r.get("status") == "REVIEW" for r in rows), f"rows {result['inventory_report']}")
    _expect(result.get("recommendations") == [], f"recs {result.get('recommendations')}")
    notes = result.get("link_notes") or []
    _expect(any("mix pack units" in n and LINE in n for n in notes), f"notes {notes}")


def recoded_member():
    org = _org()
    entry = _entry(LINE, SPAG_MEMBERS)
    entry["members"][1]["code"] = "NRD-OLD"
    _save_links(org, [entry])
    stock = [("BRK-SP500", BRK, "PKT", 0, 0), ("NRD-NEW", NRD, "PKT", 400, 400)]
    result, stamps = _pipeline(_seed(org, stock, SPAG_SALES))
    _expect((stamps.get(nkey(LINE)) or {}).get("position") == 400, f"stamp {stamps.get(nkey(LINE))}")
    notes = result.get("link_notes") or []
    _expect(any("Only 50% of saved item codes were found in this stock file" in n for n in notes),
            f"notes {notes}")


def unresolved_entries_fall_back():
    rl = importlib.import_module("rec_logic")
    stock = [("BRK-SP500", BRK, "PKT", 0, 0), ("NRD-SP500", NRD, "PKT", 0, 0)]
    sales = SPAG_SALES + [(NRD, 50)]
    plain, plain_stamps = _pipeline(_seed(_org(), stock, sales))
    plain_prompts = (list(_inv_prompts), list(_rec_prompts))
    org = _org()
    _save_links(org, [_entry(LINE, [("GONE-1", "GREENFJORD GHOST PASTA 1KG")]),
                      _entry(NRD, [], conf="low")])
    linked, linked_stamps = _pipeline(_seed(org, stock, sales))
    _expect(plain_prompts == (list(_inv_prompts), list(_rec_prompts)), "unresolved links changed the prompts")
    _expect(plain_stamps == linked_stamps and plain["recommendations"] == linked["recommendations"],
            "unresolved links changed the orders")
    nordvik = _rec_for(linked, NRD)
    _expect(nordvik and "sales_link" not in nordvik
            and rl.LINK_UNSURE_FLAG not in (nordvik.get("flags") or []), f"no-family rec {nordvik}")
    notes = linked.get("link_notes") or []
    _expect(any("fell back to name matching" in n and LINE in n for n in notes), f"notes {notes}")


def exactly_once():
    oyster_line, oyster = "BRKVL OYSTR SCE 510G", "BROOKVALE OYSTER SAUCE 510G"
    org = _org()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS), _entry(oyster_line, [("BRK-OY510", oyster)])])
    stock = SPAG_STOCK + [("BRK-OY510", oyster, "BTL", 0, 0)]
    _, stamps = _pipeline(_seed(org, stock, SPAG_SALES + [(oyster_line, 100)]))
    for raw, owner in ((LINE, LINE), (oyster_line, oyster)):
        fed = [key for key, stamp in stamps.items() if raw in (stamp.get("sales_sources") or [])]
        _expect(fed == [nkey(owner)], f"{raw} fed {fed}")


def tender_reaches_family():
    rl = importlib.import_module("rec_logic")
    org = _org()
    client, _ = _client(org)
    _save_links(org, [_entry(LINE, SPAG_MEMBERS)])
    sid = _seed(org, SPAG_STOCK, SPAG_SALES)
    result, _ = _pipeline(sid)
    _expect(_rec_for(result, LINE).get("suggested_quantity") == "650 PKT", "family order missing")
    tender_line = "NORDVIK SPAG 500G HOTEL PACK"
    upload = db.create_tender_upload(org, "tenders.csv")
    db.save_tender_rows(org, upload, [{
        "customer": "KESSINGTON HOTELS", "item_name": tender_line, "match_key": nkey(tender_line),
        "quantity": 50, "period_start": "2020-01-01", "period_end": "2099-12-31",
        "qty_basis": "per_month"}])
    db.save_tender_match(org, nkey(tender_line), tender_line, NRD, nkey(NRD))
    for item in result["recommendations"]:
        item["approved"] = True
    _store(sid, result)
    page = client.get(f"/results/{sid}").get_data(as_text=True)
    _expect('<span class="qty-tender">+ 50</span>' in page and "Order 700 PKT in total." in page,
            "results page lost the member's tender")
    _expect("Contracted, but not on this order list" not in page, "family listed as not recommended")
    printed = client.get(f"/results/{sid}/print").get_data(as_text=True)
    _expect("<strong>700 PKT</strong>" in printed and "50 tender" in printed, "print lost the tender")
    exported = client.get(f"/results/{sid}/export.csv").get_data(as_text=True)
    row = next((r for r in csv.DictReader(io.StringIO(exported)) if r.get("Item") == LINE), {})
    _expect(row.get("Qty To Order") == "700 PKT" and row.get("Tender Add-On") == "50", f"CSV row {row}")
    covered = {"suggested_quantity": "", "uom_label": " PKT",
               "order_calc": {"state": "covered", "spare": 350},
               "sales_link": {"members": [{"name": BRK, "key": nkey(BRK), "free": 1000},
                                          {"name": NRD, "key": nkey(NRD), "free": 400}], "more": 0}}
    split = rl._tender_split(covered, {"qty": 50}) or {}
    _expect(split.get("total") == "50 PKT" and not split.get("stock_covers"), f"covered split {split}")


def newly_critical_through_members():
    org = _org()
    client, uid = _client(org)
    previous = db.execute("INSERT INTO upload_sessions (user_id,org_name,status,created_at) VALUES (?,?,?,?)",
                          (uid, org, "complete", "2026-09-01 00:00:00"))
    db.execute("INSERT INTO analysis_results (session_id,inventory_report,recommendations_json) VALUES (?,?,?)",
               (previous, json.dumps([{"item": NRD, "status": "CRITICAL"}]), "[]"))
    _save_links(org, [_entry(LINE, SPAG_MEMBERS)])
    stock = [("BRK-SP500", BRK, "PKT", 0, 0), ("NRD-SP500", NRD, "PKT", 0, 0),
             ("PDM-ON1L", NECTAR, "PKT", 0, 0)]
    sid = _seed(org, stock, SPAG_SALES + [(NECTAR, 40)])
    _critical_calls.clear()
    _analyse(client, sid)
    saved = json.loads(db.query("SELECT inventory_report FROM analysis_results WHERE session_id=?",
                                (sid,))[0]["inventory_report"])
    family = next((r for r in saved if r.get("item") == LINE), {})
    _expect(family.get("status") == "CRITICAL" and NRD in (family.get("link_members") or []),
            f"family row {family}")
    emailed = [[i.get("item") for i in call[2]] for call in _critical_calls]
    _expect(emailed == [[NECTAR]], f"critical emails {emailed}")


def link_notice_email():
    org = _org()
    client, _ = _client(org)
    _save_links(org, [_entry(LINE, SPAG_MEMBERS)])
    clean = _seed(org, SPAG_STOCK, SPAG_SALES)
    _failure_calls.clear()
    _analyse(client, clean)
    _expect(not [c for c in _failure_calls if c[2] == "sales_links"], f"clean run emailed {_failure_calls}")
    noisy = _seed(org, SPAG_STOCK, SPAG_SALES + [("GREENFJORD MYSTERY MIX 1KG", 5)])
    _failure_calls.clear()
    _analyse(client, noisy)
    calls = [c for c in _failure_calls if c[2] == "sales_links"]
    _expect(len(calls) == 1 and calls[0][0] == org and calls[0][1] == noisy, f"calls {_failure_calls}")
    detail = calls[0][3]
    _expect(f"/admin/sales-links?org={quote(org)}" in detail and "GREENFJORD MYSTERY MIX 1KG" in detail,
            f"detail {detail!r}")


def dedup_leaves_linked_names_out():
    org = _org()
    client, _ = _client(org)
    stock = SPAG_STOCK + [("PDM-ON1L", NECTAR, "PKT", 50, 50)]
    sales = SPAG_SALES + [(NECTAR, 40)]

    def scan():
        sid = _seed(org, stock, sales)
        rate_limit._hits.clear()
        appmod.normalization_cache.pop(sid, None)
        _CapturingAnthropic.prompts.clear()
        original = appmod._anthropic.Anthropic
        appmod._anthropic.Anthropic = _CapturingAnthropic
        try:
            response = client.get(f"/dedup/stream/{sid}")
            response.get_data()
            response.close()
        finally:
            appmod._anthropic.Anthropic = original
        _expect(len(_CapturingAnthropic.prompts) == 1, "dedup scan did not reach the model")
        return _CapturingAnthropic.prompts[0]

    no_row = scan()
    _save_links(org, [_entry(LINE, SPAG_MEMBERS)], enabled=False)
    off = scan()
    _expect(off == no_row and all(n in off for n in (LINE, BRK, NRD, NECTAR)), "switch-off scan changed")
    db.set_sales_links_enabled(org, True, "operator@example.com")
    names = scan().split("Item names:\n", 1)[-1].splitlines()
    _expect(NECTAR in names and not any(n in names for n in (LINE, BRK, NRD)), f"names {names}")


# ── 2.0b carried from the 018-1 review ──────────────────────────────────────

def cut_member_key_is_not_saved():
    links = importlib.import_module("agents.sales_links")
    _expect(121 <= len(nkey(LONG)) <= 200 and len(LONG) <= 200, "fixture must have a 121-200 char key")
    entry = links.make_entry("BRKVL LONG PASTA", [{"code": "BRK-LONG", "name": LONG}], "high", "admin")
    _expect(entry["members"][0]["key"] == "", f"make_entry saved {entry['members'][0]['key']!r}")
    org = _org()
    _client(org)
    admin, _ = _client(_org(), admin=True)
    _seed(org, [("BRK-LONG", LONG, "PKT", 5, 5)], [], status="complete")
    response = admin.post("/admin/sales-links",
                          data={"org": org, "action": "set", "line": "BRKVL LONG PASTA", "codes": "BRK-LONG"})
    _expect(response.status_code == 302, f"POST status {response.status_code}")
    saved = db.get_sales_links(org)["lines"].get(nkey("BRKVL LONG PASTA")) or {}
    _expect([m.get("key") for m in saved.get("members") or []] == [""], f"route saved {saved}")


def key_fallback_ignores_cut_keys():
    links = importlib.import_module("agents.sales_links")
    prefix = nkey(LONG)[:120]
    # A different item whose whole key equals the old 120-character cut.
    rows = [{"inventory_code": "BRK-DECOY", "description": prefix.upper()}]
    lines = {nkey("BRKVL LONG PASTA"): _entry("BRKVL LONG PASTA", [("BRK-GONE", LONG)]),
             nkey("BRKVL CUT PASTA"): _entry("BRKVL CUT PASTA", [("BRK-GONE-2", LONG)])}
    lines[nkey("BRKVL LONG PASTA")]["members"][0]["key"] = prefix
    lines[nkey("BRKVL CUT PASTA")]["members"][0]["key"] = ""
    found = links.apply_links(lines, rows, "inventory_code", "description",
                              ["BRKVL LONG PASTA", "BRKVL CUT PASTA"], {})
    _expect(found["families"] == {}, f"cut key matched {found['families']}")
    control = links.apply_links({nkey(LINE): _entry(LINE, [("NRD-OLD", NRD)])},
                                [{"inventory_code": "NRD-NEW", "description": NRD}],
                                "inventory_code", "description", [LINE], {})
    _expect(len(control["families"]) == 1, "an uncut key must still find a recoded item")


def admin_help_text():
    org = _org()
    _client(org)
    admin, _ = _client(_org(), admin=True)
    page = admin.get("/admin/sales-links", query_string={"org": org}).get_data(as_text=True)
    _expect("do not use them yet" not in page and "analysis integration" not in page, "stale help text")
    _expect("analysis runs use the saved links" in page and "linked by the AI once" in page, "new help text missing")


_run("RED 1: multi-brand line becomes one order with each brand's free stock", multi_brand_family)
_run("RED 2: unsure link warns on the card, the tag and the printed sheet", unsure_family_warning)
_run("RED 3: single-item link keeps the stock item's own name", single_item_link)
_run("RED 4: an admin link is sure even at low confidence", admin_low_is_sure)
_run("GUARD 5: switch OFF is identical to having no links row", switch_off_guard)
_run("RED 6: a staff group touching a linked name is dropped and counted", staff_group_dropped)
_run("RED 7: exact-name stock row stays with its own sales line", exact_name_rule)
_run("RED 8: one code on two saved lines is dropped from both", code_on_two_lines)
_run("RED 9: mixed pack units send the family to review", mixed_units_review)
_run("RED 10: a recoded member is found by name and the code rate is noted", recoded_member)
_run("RED 11: unresolved or no-family entries fall back to plain name matching", unresolved_entries_fall_back)
_run("RED 12: every linked sales line feeds exactly one stock row", exactly_once)
_run("RED 13: a member's tender reaches the family order on results, print and CSV", tender_reaches_family)
_run("RED 14: newly-critical email compares through linked member names", newly_critical_through_members)
_run("RED 15: link notes send one operator email with the admin link", link_notice_email)
_run("RED 16: the duplicate scan leaves linked names out only when links are on", dedup_leaves_linked_names_out)
_run("RED 17: a member key longer than 120 characters is saved blank, never cut", cut_member_key_is_not_saved)
_run("RED 18: the key fallback ignores blank and possibly-cut saved keys", key_fallback_ignores_cut_keys)
_run("RED 19: admin page help text describes what analysis runs now do", admin_help_text)

logging.shutdown()
_tmp.cleanup()
print(f"\n{_TOTAL - _FAILED} passed, {_FAILED} failed")
sys.exit(1 if _FAILED else 0)
