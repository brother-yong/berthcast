"""19 Jul 2026: silently-dropped AI batches must never render as a complete report.

Both agents batch items to Claude (inventory 300/batch, recommendation
150/batch). When a batch's reply can't be parsed as JSON, the old code
skipped it with only an ephemeral progress-log warning — no flag was
persisted, so a report missing up to a whole batch of items still rendered
as complete and trustworthy. This is the same class of bug as the 14 Jul
2026 "empty-but-successful" recommendation-step failure.

Covers:
  1. Inventory agent: a shortfall between items-sent and items-returned sets
     the existing `partial` flag, even when nothing was "repaired" (e.g. a
     dropped batch parses as nothing at all, not a truncated one).
  2. Inventory agent: a complete reply does NOT set partial.
  3. Recommendation agent: a shortfall appends to the data_notes list the
     orchestrator already threads to the results-page banner.
  4. Orchestrator: passes its data_notes list into the recommendation agent
     and returns whatever notes came back.

Run: python tests/test_partial_batch_flag.py
"""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_partialbatchflag.db")
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

import database as db                      # noqa: E402
import agents.shared as shared             # noqa: E402
import agents.inventory as inv_mod         # noqa: E402
import agents.recommendation as rec_mod    # noqa: E402
import agents.orchestrator as orch_mod     # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1+2. Inventory agent: shortfall sets partial, complete reply doesn't ────
db.init_db()
SID = 981
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID, 1, "PartialBatchOrg", "complete", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{SID} ('
           '"description" TEXT, "qty_on_hand" TEXT, "category" TEXT, "uom" TEXT)')
_INV_ITEMS = [
    ("BROOKVALE UHT MILK 1L",        "100", "DAIRY", "CTN"),
    ("BROOKVALE UHT MILK 500ML",     "80",  "DAIRY", "CTN"),
    ("HARVEST OAT CEREAL 750G",      "50",  "DRY",   "PKT"),
]
for r in _INV_ITEMS:
    db.execute(f"INSERT INTO inventory_{SID} VALUES (?,?,?,?)", r)

# Keyword fallback for the auto-mapper's LLM call — "{}" means no override,
# so the plain keyword guess (description/qty_on_hand/category/uom) is used.
shared._call_claude = lambda *a, **k: "{}"


def _one_item_reply(model, system, user, max_tokens=4096):
    """Simulates a batch where only one item's health dict came back —
    the other two were silently dropped (e.g. an unparseable tail)."""
    return json.dumps([
        {"item": "BROOKVALE UHT MILK 1L", "status": "HEALTHY", "days_of_supply": 30,
         "observation": "x", "category": "DAIRY", "spoilage_risk": "NONE"},
    ])


def _full_reply(model, system, user, max_tokens=4096):
    return json.dumps([
        {"item": name, "status": "HEALTHY", "days_of_supply": 30,
         "observation": "x", "category": cat, "spoilage_risk": "NONE"}
        for (name, _stock, cat, _uom) in _INV_ITEMS
    ])


inv_mod._call_claude = _one_item_reply
res_short = inv_mod.run_inventory_agent(SID, "m", [], {}, None)
_check("inventory shortfall (1 of 3 items returned) sets partial",
       res_short.get("partial") is True, detail=str(res_short.get("partial")))
_check("shortfall report only has the 1 item that came back",
       res_short.get("items_analysed") == 1, detail=str(res_short.get("items_analysed")))

inv_mod._call_claude = _full_reply
res_full = inv_mod.run_inventory_agent(SID, "m", [], {}, None)
_check("complete reply (3 of 3 items) does NOT set partial",
       not res_full.get("partial"), detail=str(res_full.get("partial")))
_check("complete report has all 3 items",
       res_full.get("items_analysed") == 3, detail=str(res_full.get("items_analysed")))


# ── 3. Recommendation agent: shortfall appends a data note ───────────────────
_REC_REPORT = [
    {"item": "BROOKVALE UHT MILK 1L", "category": "DAIRY", "stock": 0,
     "status": "CRITICAL", "spoilage_risk": "NONE", "days_of_supply": 0, "observation": "t"},
    {"item": "BROOKVALE UHT MILK 500ML", "category": "DAIRY", "stock": 0,
     "status": "CRITICAL", "spoilage_risk": "NONE", "days_of_supply": 0, "observation": "t"},
    {"item": "HARVEST OAT CEREAL 750G", "category": "DRY", "stock": 0,
     "status": "CRITICAL", "spoilage_risk": "NONE", "days_of_supply": 0, "observation": "t"},
]


def _short_rec_reply(model, system, user, max_tokens=64000):
    """Only one of the three items' recommendations came back."""
    return json.dumps([
        {"item": "BROOKVALE UHT MILK 1L", "supplier": "Unknown", "supplier_type": "other",
         "lead_time_days": None, "days_of_supply": 0, "recommended_action": "REORDER",
         "suggested_quantity": "Verify with team", "confidence": "LOW",
         "consequence_if_acting": "a", "consequence_if_not_acting": "b",
         "supplier_risk": "HIGH", "mitigation": "m", "flags": [], "reason": "r"},
    ])


rec_mod._call_claude = _short_rec_reply
notes = []
rec_mod.run_recommendation_agent(SID, "m", list(_REC_REPORT), {}, None, data_notes=notes)
_check("recommendation shortfall appends exactly one data note",
       len(notes) == 1, detail=str(notes))
_check("the note mentions missing recommendations",
       bool(notes) and "missing" in notes[0], detail=str(notes))


# ── 4. Orchestrator threads data_notes into the recommendation agent ────────
_orig_inv, _orig_rec = orch_mod.run_inventory_agent, orch_mod.run_recommendation_agent
try:
    orch_mod.run_inventory_agent = lambda *a, **k: {
        "report": list(_REC_REPORT), "partial": False,
    }

    def _fake_rec_appends_note(session_id, model, inventory_report, context,
                                progress_emit=None, data_notes=None):
        if data_notes is not None:
            data_notes.append("X")
        return []

    orch_mod.run_recommendation_agent = _fake_rec_appends_note
    result = orch_mod.run_pipeline(SID, "m", [], {}, emit=lambda *a, **k: None,
                                    mark=lambda *a, **k: None)
    _check("orchestrator threads data_notes through to the recommendation agent",
           "X" in (result.get("data_notes") or []), detail=str(result.get("data_notes")))
finally:
    orch_mod.run_inventory_agent, orch_mod.run_recommendation_agent = _orig_inv, _orig_rec


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll partial-batch-flag tests passed.")
