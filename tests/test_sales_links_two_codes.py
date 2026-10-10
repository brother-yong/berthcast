"""Plan 018-3 follow-up: one stock item can carry two item codes (one per
warehouse). apply_links treats an item as its name key, so the AI-step checks
"saved lines win" and "a contested item stays with the surer line" must compare
items, not code text, or a second code takes the item off a saved line.
Eight checks fail on the shipped 7708b8a code. "The surer new line keeps the
item", 3d, 3g and 3h guard against reading saved lines differently from
apply_links; 4 guards one-code-per-item files.
Only the link call is canned. Invented brands. Run: python tests/test_sales_links_two_codes.py
"""
import json
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.TemporaryDirectory(prefix="berthcast_two_codes_", ignore_cleanup_errors=True)
os.environ["DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")
_stub = types.ModuleType("anthropic")
_stub.Anthropic = lambda *args, **kwargs: None
_stub.AnthropicError = Exception
sys.modules["anthropic"] = _stub

import database as db                                   # noqa: E402
import agents.sales_links as links                      # noqa: E402
from agents.shared import normalise_match_key as nkey   # noqa: E402

_FAILED = 0
LINE = "SPAGHETTI 500G BROOKVALE/NORDVIK"
OLD = "SPAGHETTI BROOKVALE OLD CODE"
PLAIN = "SPAGHETTI BROOKVALE"
BRK = "BROOKVALE SPAGHETTI 500G"
NRD = "NORDVIK SPAGHETTI 500G"
GRF = "GREENFJORD PENNE 500G"
# The same BROOKVALE item under two codes, as in a two-warehouse export. The
# last two rows have names with no letters or digits (an empty name key).
ROWS = [{"inventory_code": c, "location_code": loc, "description": d, "uom": "PKT", "free_balance": f}
        for c, loc, d, f in (("BRK-SP500", "WH1", BRK, "100"), ("BRK-SP500-B", "WH2", BRK, "100"),
                             ("NRD-SP500", "WH1", NRD, "400"), ("GRF-PN500", "WH1", GRF, "50"),
                             ("PX-1", "WH1", "!!!", "5"), ("PX-2", "WH1", "???", "5"),
                             ("0", "WH1", "KESSINGTON RICE 1KG", "5"), ("0", "WH2", "PADIMAS OATS 500G", "5"))]
ANSWERS = {}


def _fake_link(model, system, user, **kwargs):
    text = user[-1]["text"] if isinstance(user, list) else str(user)
    reply = []
    for row, name in re.findall(r"row (\d+): (.+?) \| supplier block", text):
        codes, conf = ANSWERS.get(name, ([], "low"))
        reply.append({"row": int(row), "codes": codes, "confidence": conf, "unit": "same", "reason": "test"})
    return json.dumps(reply)


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f" [{detail}]" if detail and not cond else ""))
    _FAILED += not cond


def _link(org, lines, saved):
    sales = {line: {"total_qty": 300} for line in lines}
    return links.link_new_lines(org, 1, ROWS, "inventory_code", "description", "uom", None,
                                "free_balance", sales, saved)


def _item_keys(entry):
    return {m["key"] for m in (entry or {}).get("members") or []}


def _codes(entry):
    return {m["code"] for m in (entry or {}).get("members") or []}


db.init_db()
links._call_claude = _fake_link

# 1. A saved admin line holds BROOKVALE by one code; a new line names it by the other.
saved = {nkey(LINE): links.make_entry(LINE, [{"code": "BRK-SP500", "name": BRK},
                                             {"code": "NRD-SP500", "name": NRD}], "high", "admin")}
ANSWERS.clear()
ANSWERS[OLD] = (["BRK-SP500-B"], "high")
out = _link("TWO CODES SAVED", [LINE, OLD], saved)
new = out["entries"].get(nkey(OLD))
_check("a new line never takes an item off a saved line through its second code",
       new is not None and nkey(BRK) not in _item_keys(new), str(out["entries"]))
ctx = links.apply_links({**saved, **out["entries"]}, ROWS, "inventory_code", "description", [LINE, OLD], {})
family = ctx["families"].get(nkey(LINE)) or {}
_check("the saved family keeps both brands this run",
       set(family.get("member_keys") or []) == {nkey(BRK), nkey(NRD)}, str(ctx["families"]))

# 2. Two new lines name one item through two codes: the surer line keeps it.
ANSWERS.clear()
ANSWERS[PLAIN] = (["BRK-SP500"], "high")
ANSWERS[OLD] = (["BRK-SP500-B"], "medium")
out = _link("TWO CODES SURER", [PLAIN, OLD], {})
_check("the surer new line keeps the item", nkey(BRK) in _item_keys(out["entries"].get(nkey(PLAIN))),
       str(out["entries"]))
_check("the less sure new line drops the item",
       nkey(OLD) in out["entries"] and nkey(BRK) not in _item_keys(out["entries"][nkey(OLD)]),
       str(out["entries"]))

# 3. A tie drops the item from both new lines.
ANSWERS.clear()
ANSWERS[PLAIN] = (["BRK-SP500"], "medium")
ANSWERS[OLD] = (["BRK-SP500-B"], "medium")
out = _link("TWO CODES TIE", [PLAIN, OLD], {})
_check("a tie drops the item from both new lines",
       len(out["entries"]) == 2 and not any(nkey(BRK) in _item_keys(e) for e in out["entries"].values()),
       str(out["entries"]))

# 3b. The less sure line named BOTH codes of the item: it loses both.
ANSWERS.clear()
ANSWERS[PLAIN] = (["BRK-SP500"], "high")
ANSWERS[OLD] = (["BRK-SP500", "BRK-SP500-B", "NRD-SP500"], "medium")
out = _link("TWO CODES BOTH", [PLAIN, OLD], {})
_check("a losing line keeps no code of the contested item",
       _codes(out["entries"].get(nkey(OLD))) == {"NRD-SP500"}, str(out["entries"]))

# 3c. The saved member's code is gone from this file: its saved key still protects the item.
saved = {nkey(LINE): links.make_entry(LINE, [{"code": "GONE-1", "name": BRK}], "high", "admin")}
ANSWERS.clear()
ANSWERS[OLD] = (["BRK-SP500-B"], "high")
out = _link("TWO CODES GONE", [LINE, OLD], saved)
_check("a saved member whose code is gone still protects its item by key",
       nkey(OLD) in out["entries"] and nkey(BRK) not in _item_keys(out["entries"][nkey(OLD)]),
       str(out))

# 3d. A stale saved key (the item was renamed) does not block an unrelated item:
# apply_links reads the code, so the saved line holds NORDVIK, not GREENFJORD.
saved = {nkey(LINE): links.make_entry(LINE, [{"code": "NRD-SP500", "key": nkey(GRF), "name": NRD}],
                                      "high", "admin")}
ANSWERS.clear()
ANSWERS[OLD] = (["GRF-PN500"], "high")
out = _link("STALE KEY", [LINE, OLD], saved)
_check("a stale saved key never blocks the item that now carries that name",
       _codes(out["entries"].get(nkey(OLD))) == {"GRF-PN500"}, str(out))

# 3e. A name with no letters or digits can never be linked, so it is never saved.
ANSWERS.clear()
ANSWERS[PLAIN] = (["PX-1"], "high")
ANSWERS[OLD] = (["PX-2"], "medium")
out = _link("EMPTY KEYS", [PLAIN, OLD], {})
_check("empty-name items are never saved as members",
       len(out["entries"]) == 2 and not any(_codes(e) for e in out["entries"].values()), str(out["entries"]))

# 3f. The saved member's code now has an empty name, so the run reads its saved
# key (apply_links does): that item stays protected.
saved = {nkey(LINE): links.make_entry(LINE, [{"code": "PX-1", "key": nkey(GRF), "name": GRF}], "high", "admin")}
ANSWERS.clear()
ANSWERS[OLD] = (["GRF-PN500"], "high")
out = _link("EMPTY NAME SAVED", [LINE, OLD], saved)
_check("a saved key read through an empty-name code still protects its item",
       nkey(OLD) in out["entries"] and nkey(GRF) not in _item_keys(out["entries"][nkey(OLD)]), str(out))

# 3g. The saved member's code is a placeholder on two items and its saved key
# names neither: apply_links leaves the line unlinked, so nothing is protected.
saved = {nkey(LINE): links.make_entry(LINE, [{"code": "0", "key": nkey(GRF), "name": GRF}], "high", "admin")}
ANSWERS.clear()
ANSWERS[OLD] = (["GRF-PN500"], "high")
out = _link("PLACEHOLDER SAVED", [LINE, OLD], saved)
_check("a placeholder code's unrelated saved key does not block an item",
       _codes(out["entries"].get(nkey(OLD))) == {"GRF-PN500"}, str(out))

# 3h. An item on two saved lines (apply_links strips it from both) is still
# claimed by them: a new line naming it through either code does not take it.
TWO = "BROOKVALE PASTA OFFER"
saved = {nkey(LINE): links.make_entry(LINE, [{"code": "BRK-SP500", "name": BRK},
                                             {"code": "NRD-SP500", "name": NRD}], "high", "admin"),
         nkey(TWO): links.make_entry(TWO, [{"code": "BRK-SP500-B", "name": BRK}], "high", "admin")}
ANSWERS.clear()
ANSWERS[OLD] = (["BRK-SP500-B", "GRF-PN500"], "high")
out = _link("TWO SAVED LINES", [LINE, TWO, OLD], saved)
_check("an item two saved lines claim is never taken by a new line",
       _codes(out["entries"].get(nkey(OLD))) == {"GRF-PN500"}, str(out["entries"]))

# 4. Guard: one code per item behaves as before (the surer line keeps its code).
ANSWERS.clear()
ANSWERS[PLAIN] = (["NRD-SP500"], "high")
ANSWERS[OLD] = (["NRD-SP500"], "low")
out = _link("ONE CODE", [PLAIN, OLD], {})
_check("GUARD: a single code contested by two lines stays with the surer line",
       nkey(NRD) in _item_keys(out["entries"].get(nkey(PLAIN)))
       and nkey(NRD) not in _item_keys(out["entries"].get(nkey(OLD))), str(out["entries"]))

print("RESULT: " + ("FAIL" if _FAILED else "ALL OK"))
sys.exit(1 if _FAILED else 0)
