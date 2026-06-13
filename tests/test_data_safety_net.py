"""The data safety net: every category of messy upload must fail safe.

berthcast is going to unseen files (prospect demos, a free tier where strangers
upload anything) with no chance to fix on the spot. The danger is not a crash —
it's a clean, confident report built on a misread file. So the promise is: never
show a trusted report built on data we couldn't read. Every upload ends OK, WARN
(shown with a caveat), or BLOCK (refused with a plain reason).

This runs the broken-file corpus (tests/corpus/) through the REAL ingestion and
the REAL gate (data_quality.assess_upload — the same call the live pipeline
makes), then checks the gate is wired into the inventory agent end to end.

Run: python tests/test_data_safety_net.py
"""
import csv
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CORPUS = os.path.join(ROOT, "tests", "corpus")

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_safetynet.db")
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

import database as db                 # noqa: E402
import data_quality as dq            # noqa: E402
import agents.inventory as inv_mod   # noqa: E402

_FAILED = False
_SID = [600]


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _new_session():
    _SID[0] += 1
    sid = _SID[0]
    db.execute(
        "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
        "VALUES (?,?,?,?,?,?)", (sid, 1, "CorpusOrg", "complete", "all", "{}"))
    return sid


def _ingest(sid, slot, path):
    res = db._csv_to_sqlite(path, slot, sid)
    if not res.get("ok"):
        # empty/header-only files legitimately produce no table — that's a BLOCK
        # case the gate handles by finding no inventory rows.
        return res
    return res


db.init_db()

# ── Corpus: each entry must produce its required gate outcome ─────────────────
# (file, sales_file_or_None, column_map_or_None, expect_level, expect_code_or_None)
CASES = [
    ("clean_inventory.csv",        "clean_sales.csv", None,                                    "ok",    None),
    ("wrong_document.csv",         None,              None,                                    "block", "no_columns"),
    ("empty_inventory.csv",        None,              None,                                    "block", "empty_file"),
    ("stock_is_text.csv",          None,              None,                                    "block", "stock_not_numeric"),
    ("euro_decimals_inventory.csv", None,             None,                                    "warn",  "number_format"),
    ("codes_inventory.csv",        "codes_sales.csv", None,                                    "warn",  "low_name_overlap"),
    ("unit_inventory.csv",         "unit_sales.csv",  None,                                    "warn",  "unit_mismatch"),
    ("suspect_stock_inventory.csv", None,             {"description": "description", "stock": "qty_on_order"}, "warn", "stock_column_suspect"),
]

for inv_file, sales_file, cmap, level, code in CASES:
    sid = _new_session()
    _ingest(sid, "inventory", os.path.join(CORPUS, inv_file))
    if sales_file:
        _ingest(sid, "sales", os.path.join(CORPUS, sales_file))
    findings = dq.assess_upload(sid, column_map=cmap)
    codes = {f["code"] for f in findings}
    blocks = [f for f in findings if f["level"] == "block"]
    warns = [f for f in findings if f["level"] == "warn"]

    if level == "ok":
        _check(f"{inv_file}: clean file produces NO findings (no false alarms)",
               not findings, detail=str(codes))
    elif level == "block":
        _check(f"{inv_file}: BLOCKED", bool(blocks), detail=str(findings))
        _check(f"{inv_file}: block code is {code}", code in {b['code'] for b in blocks},
               detail=str([b['code'] for b in blocks]))
        # A block is terminal — it must be the only finding returned.
        _check(f"{inv_file}: block stops further checks", len(findings) == 1, detail=str(codes))
    else:  # warn
        _check(f"{inv_file}: not blocked", not blocks, detail=str([b['code'] for b in blocks]))
        _check(f"{inv_file}: WARN code {code} present", code in codes, detail=str(codes))
        # every warning must carry a plain-English message
        _check(f"{inv_file}: warning has a readable message",
               all(len(f["message"]) > 30 for f in warns))

# ── Large file (generated): WARN large_file, no false BLOCK ──────────────────
sid = _new_session()
big = os.path.join(tempfile.gettempdir(), "berth_bigfile.csv")
with open(big, "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Description", "Qty On Hand"])
    for i in range(dq.LARGE_FILE_ROWS + 1):
        w.writerow([f"ITEM NUMBER {i}", 10])
db._csv_to_sqlite(big, "inventory", sid)
findings = dq.assess_upload(sid)
_check("large file: WARN large_file", "large_file" in {f["code"] for f in findings},
       detail=str({f["code"] for f in findings}))
_check("large file: not blocked", not any(f["level"] == "block" for f in findings))

# ── Edge: no sales file at all is fine (inventory-only run) ───────────────────
sid = _new_session()
db._csv_to_sqlite(os.path.join(CORPUS, "clean_inventory.csv"), "inventory", sid)
findings = dq.assess_upload(sid)
_check("inventory-only (no sales file) produces no findings", not findings, detail=str(findings))

# ── Integration: WARN flows into the inventory agent's data_notes ─────────────
captured = {"user": ""}


def _fake_claude(model, system, user, max_tokens=4096):
    """Emit one item per prompt line so the agent completes and returns a report
    (returning "[]" would make it error with 'no usable JSON', masking the data
    notes we're checking)."""
    captured["user"] = user
    items = []
    for line in user.splitlines():
        if line.startswith("Item: "):
            items.append({"item": line.split("|")[0].replace("Item:", "").strip(),
                          "category": "X", "stock": 0, "status": "HEALTHY",
                          "spoilage_risk": "NONE", "days_of_supply": 0, "observation": "t"})
    return json.dumps(items)


inv_mod._call_claude = _fake_claude
# shared._call_claude (column auto-map) -> keyword fallback
import agents.shared as shared  # noqa: E402
shared._call_claude = lambda *a, **k: "{}"

sid = _new_session()
db._csv_to_sqlite(os.path.join(CORPUS, "euro_decimals_inventory.csv"), "inventory", sid)
res = inv_mod.run_inventory_agent(sid, "claude-sonnet-4-6", [], {}, None)
notes = res.get("data_notes") if isinstance(res, dict) else None
_check("agent: euro file run is NOT blocked (report still produced)",
       isinstance(res, dict) and "report" in res, detail=str(res)[:120])
_check("agent: number_format WARN reached data_notes (results banner)",
       bool(notes) and any("comma for the decimal" in n for n in notes), detail=str(notes))

# ── Integration: BLOCK stops the agent with a plain reason ───────────────────
sid = _new_session()
db._csv_to_sqlite(os.path.join(CORPUS, "wrong_document.csv"), "inventory", sid)
res = inv_mod.run_inventory_agent(sid, "claude-sonnet-4-6", [], {}, None)
_check("agent: wrong document is blocked", isinstance(res, dict) and res.get("blocked") is True,
       detail=str(res)[:160])
_check("agent: block returns an error message, not a report",
       isinstance(res, dict) and "error" in res and "report" not in res)


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll data-safety-net tests passed.")
