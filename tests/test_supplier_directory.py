"""Supplier directory: duplicate detection, rebuild, merge resolution, archive.

The supplier-profile machinery (delay rate -> high-risk flag -> safety buffer)
shipped long ago but sat unused: the list started empty and had to be typed by
hand, and get_supplier_profile matches names EXACTLY, so a hand-typed profile
silently missed the spelling in the client's files. This covers the fix --
building the list from the uploads -- and the duplicate handling on top of it.

Run: python tests/test_supplier_directory.py
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_supdir.db")
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

import database as db          # noqa: E402
import supplier_directory as sd  # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _group_of(groups, name):
    """Return the member->reason dict of the group containing `name`, or None."""
    for g in groups:
        if name in g["members"]:
            return g
    return None


# ---------------------------------------------------------------------------
# Pass 1 — suffix strip
# ---------------------------------------------------------------------------
groups = sd.detect_duplicate_groups(
    ["NORDVIK", "NORDVIK TRADING PTE LTD", "BROOKVALE FOODS"]
)
g = _group_of(groups, "NORDVIK")
_check("suffix pass groups NORDVIK with NORDVIK TRADING PTE LTD",
       g is not None and "NORDVIK TRADING PTE LTD" in g["members"])
_check("unrelated name is not pulled into the group",
       g is not None and "BROOKVALE FOODS" not in g["members"])
_check("suffix group carries a reason",
       g is not None and bool(g["members"]["NORDVIK"]))

# ---------------------------------------------------------------------------
# Pass 2 — fuzzy (typos the suffix rule is blind to)
# ---------------------------------------------------------------------------
groups = sd.detect_duplicate_groups(["BROOKVALE", "BROKVALE", "PADIMAS RICE"])
g = _group_of(groups, "BROOKVALE")
_check("fuzzy pass groups BROOKVALE with BROKVALE",
       g is not None and "BROKVALE" in g["members"])
_check("fuzzy pass leaves an unrelated name alone",
       g is not None and "PADIMAS RICE" not in g["members"])

# ---------------------------------------------------------------------------
# Names that merely share a first word must NOT fuzzy-match. They DO collide
# under the suffix pass (both strip to "padimas") -- that is a known, accepted
# false suggestion, which is exactly why a merge is always human-confirmed.
# ---------------------------------------------------------------------------
_check("fuzzy ratio keeps PADIMAS TRADING and PADIMAS ENTERPRISE apart",
       not sd._fuzzy_pairs(["padimastrading", "padimasenterprise"]))

# ---------------------------------------------------------------------------
# A group found by BOTH passes appears once, not twice
# ---------------------------------------------------------------------------
groups = sd.detect_duplicate_groups(
    ["NORDVIK TRADING", "NORDVIK TRAIDNG", "NORDVIK"]
)
_check("overlapping passes produce exactly one group", len(groups) == 1, f"got {len(groups)}")
_check("the single group holds all three spellings",
       len(groups) == 1 and len(groups[0]["members"]) == 3)

# ---------------------------------------------------------------------------
# Solo names produce no group; every group has a stable key
# ---------------------------------------------------------------------------
_check("no group for unrelated names",
       sd.detect_duplicate_groups(["NORDVIK", "BROOKVALE", "KESTREL"]) == [])
g1 = sd.detect_duplicate_groups(["NORDVIK", "NORDVIK TRADING"])
g2 = sd.detect_duplicate_groups(["NORDVIK TRADING", "NORDVIK"])
_check("group_key does not depend on input order",
       g1[0]["group_key"] == g2[0]["group_key"])

# ---------------------------------------------------------------------------
# Rebuild: names come out of the org's own uploaded files
# ---------------------------------------------------------------------------
db.init_db()

SID_A, SID_B = 8801, 8802   # OrgA, OrgB
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_A, 1, "OrgA", "complete", "all", "{}"))
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_B, 2, "OrgB", "complete", "all", "{}"))

db.execute(f"CREATE TABLE suppliers_{SID_A} (supplier_name TEXT, supplier_type TEXT)")
db.execute(f"INSERT INTO suppliers_{SID_A} VALUES ('KESTREL COLD CHAIN','import')")

db.execute(f"CREATE TABLE purchase_orders_{SID_A} (inventory_desc TEXT, supplier_name TEXT)")
for _item, _sup in [("Brookvale UHT Milk", "NORDVIK TRADING PTE LTD"),
                    ("Padimas Rice 5kg", "NORDVIK"),
                    ("Vanmark Butter", "KESTREL COLD CHAIN")]:
    db.execute(f"INSERT INTO purchase_orders_{SID_A} VALUES (?,?)", (_item, _sup))

# Sales sheet: blocky supplier column (one contiguous run per name, first name
# in the first two rows) -- the plan-008 shape that is safe to read.
db.execute(f"CREATE TABLE sales_{SID_A} (description TEXT, supplier TEXT)")
for _item, _sup in [("Aldermoor Oats", "ALDERMOOR SUPPLY"),
                    ("Aldermoor Oats 1kg", "ALDERMOOR SUPPLY"),
                    ("Brookvale UHT Milk", "NORDVIK TRADING PTE LTD")]:
    db.execute(f"INSERT INTO sales_{SID_A} VALUES (?,?)", (_item, _sup))

# OrgB has its own supplier that OrgA must never see.
db.execute(f"CREATE TABLE purchase_orders_{SID_B} (inventory_desc TEXT, supplier_name TEXT)")
db.execute(f"INSERT INTO purchase_orders_{SID_B} VALUES ('Secret Item','OTHERORG TRADING')")

db.rebuild_supplier_directory("OrgA")
db.rebuild_supplier_directory("OrgB")

rows = db.get_supplier_directory("OrgA")
names = {r["supplier_name"]: r for r in rows}

_check("PO supplier is discovered", "NORDVIK TRADING PTE LTD" in names)
_check("supplier-listing name is discovered", "KESTREL COLD CHAIN" in names)
_check("sales-sheet name is discovered", "ALDERMOOR SUPPLY" in names)
_check("listing beats po as the recorded source",
       names.get("KESTREL COLD CHAIN", {}).get("source") == "listing")
_check("sales-only name is tagged as sales",
       names.get("ALDERMOOR SUPPLY", {}).get("source") == "sales")
_check("a name in both PO and sales is tagged po, not sales",
       names.get("NORDVIK TRADING PTE LTD", {}).get("source") == "po")
_check("OrgA cannot see OrgB's supplier", "OTHERORG TRADING" not in names)
_check("OrgB sees only its own",
       {r["supplier_name"] for r in db.get_supplier_directory("OrgB")} == {"OTHERORG TRADING"})

# Rebuild is idempotent -- running it twice must not duplicate rows.
_before = len(db.get_supplier_directory("OrgA"))
db.rebuild_supplier_directory("OrgA")
_check("rebuild is idempotent", len(db.get_supplier_directory("OrgA")) == _before)

# Pure spelling drift collapses silently -- no suggestion, no confirmation.
# "KESTREL COLD CHAIN" (listing) and "kestrel cold  chain" (PO) are one row.
db.execute(f"INSERT INTO purchase_orders_{SID_A} VALUES (?,?)",
           ("Vanmark Butter 250g", "kestrel cold  chain"))
db.rebuild_supplier_directory("OrgA")
_kestrel = [r for r in db.get_supplier_directory("OrgA")
            if r["supplier_name"].lower().replace(" ", "") == "kestrelcoldchain"]
_check("case and spacing drift collapses to one directory row",
       len(_kestrel) == 1, f"got {[r['supplier_name'] for r in _kestrel]}")
_check("the longer, more complete spelling is the one kept",
       len(_kestrel) == 1 and _kestrel[0]["supplier_name"] == "KESTREL COLD CHAIN")

# Suggestions were computed and stored by the rebuild.
sugg = db.get_merge_suggestions("OrgA")
_nord = [g for g in sugg if any("NORDVIK" in m for m in g["members"])]
_check("rebuild stored a NORDVIK suggestion group", len(_nord) == 1, f"got {len(_nord)}")
_check("suggestion members carry reasons",
       bool(_nord) and all(v for v in _nord[0]["members"].values()))

# A non-blocky sales supplier column must be ignored (plan-008 gate): the same
# name reappears in separate runs, which is a transaction dump, not a merged
# export.
SID_C = 8803
db.execute(
    "INSERT INTO upload_sessions (id, user_id, org_name, status, scope, context_json) "
    "VALUES (?,?,?,?,?,?)", (SID_C, 3, "OrgC", "complete", "all", "{}"))
db.execute(f"CREATE TABLE sales_{SID_C} (description TEXT, supplier TEXT)")
for _item, _sup in [("A", "SCATTER ONE"), ("B", "SCATTER TWO"),
                    ("C", "SCATTER ONE"), ("D", "SCATTER TWO")]:
    db.execute(f"INSERT INTO sales_{SID_C} VALUES (?,?)", (_item, _sup))
db.rebuild_supplier_directory("OrgC")
_check("non-blocky sales column is rejected",
       db.get_supplier_directory("OrgC") == [])

# Dismissed groups stay dismissed across rebuilds.
_key = _nord[0]["group_key"]
db.ignore_merge_group("OrgA", _key)
db.rebuild_supplier_directory("OrgA")
_check("a dismissed group does not come back",
       all(g["group_key"] != _key for g in db.get_merge_suggestions("OrgA")))

# ---------------------------------------------------------------------------
# Merge: the canonical profile must answer for every merged spelling
# ---------------------------------------------------------------------------
db.upsert_supplier_profile("OrgA", "NORDVIK TRADING PTE LTD",
                           avg_lead_time_days=99, delay_probability=0.5,
                           notes="slow on chilled")

_p = db.get_supplier_profile("OrgA", "NORDVIK")
_check("before merge, the short spelling gets defaults",
       _p.get("avg_lead_time_days") != 99)

db.merge_suppliers("OrgA", "NORDVIK TRADING PTE LTD", ["NORDVIK"])

_p = db.get_supplier_profile("OrgA", "NORDVIK")
_check("after merge, the short spelling resolves to the canonical profile",
       _p.get("avg_lead_time_days") == 99, str(_p.get("avg_lead_time_days")))
_check("merged spelling inherits the unreliable delay rate",
       abs(float(_p.get("delay_probability") or 0) - 0.5) < 1e-9)
_check("canonical name still resolves to itself",
       db.get_supplier_profile("OrgA", "NORDVIK TRADING PTE LTD").get("avg_lead_time_days") == 99)
_check("merging does not leak across orgs",
       db.get_supplier_profile("OrgB", "NORDVIK").get("avg_lead_time_days") != 99)

# Merge is reversible.
db.unmerge_supplier("OrgA", "NORDVIK")
_check("unmerge restores the standalone lookup",
       db.get_supplier_profile("OrgA", "NORDVIK").get("avg_lead_time_days") != 99)
db.merge_suppliers("OrgA", "NORDVIK TRADING PTE LTD", ["NORDVIK"])

# Scores fold together instead of splitting across spellings.
import json as _json  # noqa: E402
db.execute(
    "INSERT INTO analysis_results (session_id, inventory_report, recommendations_json) "
    "VALUES (?,?,?)",
    (SID_A, "{}", _json.dumps([
        {"item": "Brookvale UHT Milk", "supplier": "NORDVIK TRADING PTE LTD",
         "approved": True, "order_placed": True, "outcome_status": "stockout_avoided"},
        {"item": "Padimas Rice 5kg", "supplier": "NORDVIK",
         "approved": True, "order_placed": True, "outcome_status": "stockout_avoided"},
    ])))
db.update_supplier_scores("OrgA")
_scores = {r["supplier_name"]: r for r in db.get_supplier_scores("OrgA")}
_check("merged spellings score as one supplier",
       _scores.get("NORDVIK TRADING PTE LTD", {}).get("total_recs") == 2,
       str(_scores.get("NORDVIK TRADING PTE LTD", {}).get("total_recs")))
_check("the merged-away spelling gets no separate score row",
       "NORDVIK" not in _scores)

# A merge must survive the next upload, or it silently undoes itself.
db.rebuild_supplier_directory("OrgA")
_check("merge survives a rebuild",
       db.get_supplier_profile("OrgA", "NORDVIK").get("avg_lead_time_days") == 99)
_check("a merged name is not re-offered as a suggestion",
       all("NORDVIK" not in g["members"] for g in db.get_merge_suggestions("OrgA")))

# Manual merge: two names no pass would ever group, merged by hand from the
# list. This is what makes detection quality a convenience, not a dependency.
db.merge_suppliers("OrgA", "KESTREL COLD CHAIN", ["ALDERMOOR SUPPLY"])
db.upsert_supplier_profile("OrgA", "KESTREL COLD CHAIN", avg_lead_time_days=21)
_check("hand-merged name resolves to the chosen canonical profile",
       db.get_supplier_profile("OrgA", "ALDERMOOR SUPPLY").get("avg_lead_time_days") == 21)
db.rebuild_supplier_directory("OrgA")
_check("hand merge survives a rebuild",
       db.get_supplier_profile("OrgA", "ALDERMOOR SUPPLY").get("avg_lead_time_days") == 21)
db.unmerge_supplier("OrgA", "ALDERMOOR SUPPLY")

# The unreliable flag has to land ABOVE the threshold that selects the
# 2.5-month safety buffer (agents/recommendation.py:356-361) and the high-risk
# flag (agents/shared.py:870). Asserting the boundary, not re-implementing the
# banding: this fails if the flag value is ever changed without checking it
# still clears both.
db.upsert_supplier_profile("OrgA", "BROOKVALE DAIRY", delay_probability=0.5)
_flagged = float(db.get_supplier_profile("OrgA", "BROOKVALE DAIRY")["delay_probability"])
_check("unreliable flag clears the 2.5-month buffer threshold", _flagged > 0.35)
_check("unreliable flag clears the high-risk threshold", _flagged > 0.30)
db.upsert_supplier_profile("OrgA", "BROOKVALE DAIRY", delay_probability=0.2)
_unflagged = float(db.get_supplier_profile("OrgA", "BROOKVALE DAIRY")["delay_probability"])
_check("cleared flag sits in the middle buffer band, not the lowest",
       0.15 < _unflagged <= 0.35)

# ---------------------------------------------------------------------------
# Archive: hide from the list, WITHOUT changing what gets recommended
# ---------------------------------------------------------------------------
db.upsert_supplier_profile("OrgA", "KESTREL COLD CHAIN",
                           avg_lead_time_days=30, delay_probability=0.5)
db.archive_supplier("OrgA", "KESTREL COLD CHAIN", True)

_visible = {r["supplier_name"] for r in db.get_supplier_scores("OrgA")}
_check("archived supplier is hidden from the list",
       "KESTREL COLD CHAIN" not in _visible)
_check("archived supplier is listed when hidden are requested",
       "KESTREL COLD CHAIN" in {r["supplier_name"]
                                for r in db.get_supplier_scores("OrgA", include_archived=True)})

_p = db.get_supplier_profile("OrgA", "KESTREL COLD CHAIN")
_check("archiving does NOT change the profile the pipeline reads",
       _p.get("avg_lead_time_days") == 30
       and abs(float(_p.get("delay_probability") or 0) - 0.5) < 1e-9)

db.archive_supplier("OrgA", "KESTREL COLD CHAIN", False)
_check("un-archiving restores the row",
       "KESTREL COLD CHAIN" in {r["supplier_name"] for r in db.get_supplier_scores("OrgA")})

# Archiving a supplier with no profile row yet must still work.
db.archive_supplier("OrgA", "ALDERMOOR SUPPLY", True)
_check("archiving a profile-less supplier creates the row",
       "ALDERMOOR SUPPLY" in {r["supplier_name"]
                              for r in db.get_supplier_scores("OrgA", include_archived=True)})

# ---------------------------------------------------------------------------
# Routes: every one org-scoped. Org B must never touch Org A's suppliers.
# ---------------------------------------------------------------------------
os.environ.setdefault("SECRET_KEY", "test-secret-not-used-in-prod")
import app as flask_app  # noqa: E402

flask_app.app.config["WTF_CSRF_ENABLED"] = False
flask_app.app.config["TESTING"] = True

db.execute("INSERT OR IGNORE INTO users (id, email, password_hash, org_name, role, "
           "email_verified, tier, session_version) VALUES (?,?,?,?,?,?,?,?)",
           (7001, "a@example.test", "x", "OrgA", "user", 1, "enterprise", 0))
db.execute("INSERT OR IGNORE INTO users (id, email, password_hash, org_name, role, "
           "email_verified, tier, session_version) VALUES (?,?,?,?,?,?,?,?)",
           (7002, "b@example.test", "x", "OrgB", "user", 1, "enterprise", 0))


def _client_for(user_id, org):
    c = flask_app.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = user_id
        s["org_name"] = org
        s["role"] = "user"
        s["sv"] = 0
    return c


_ca = _client_for(7001, "OrgA")
_cb = _client_for(7002, "OrgB")

_r = _ca.get("/suppliers")
_check("owner can load /suppliers", _r.status_code == 200, str(_r.status_code))

# OrgB tries to flag OrgA's supplier as unreliable.
_cb.post("/suppliers/flag", data={"supplier_name": "NORDVIK TRADING PTE LTD",
                                  "unreliable": "1"})
_check("cross-org flag does not touch OrgA's profile",
       abs(float(db.get_supplier_profile(
           "OrgA", "NORDVIK TRADING PTE LTD").get("delay_probability") or 0) - 0.5) < 1e-9)

# OrgB tries to archive OrgA's supplier.
_cb.post("/suppliers/archive", data={"supplier_name": "NORDVIK TRADING PTE LTD",
                                     "archived": "1"})
_check("cross-org archive leaves OrgA's list intact",
       "NORDVIK TRADING PTE LTD" in {r["supplier_name"]
                                     for r in db.get_supplier_scores("OrgA")})

# OrgB tries to merge OrgA's suppliers.
_cb.post("/suppliers/merge", data={"canonical": "NORDVIK TRADING PTE LTD",
                                   "variants": "KESTREL COLD CHAIN"})
_check("cross-org merge does not link OrgA's suppliers",
       not [r for r in db.get_supplier_directory("OrgA")
            if r["supplier_name"] == "KESTREL COLD CHAIN" and r["merged_into"]])

# The owner's own flag works, and lands on the value the buffer logic reads.
_ca.post("/suppliers/flag", data={"supplier_name": "ALDERMOOR SUPPLY", "unreliable": "1"})
_check("owner's unreliable flag writes 0.5",
       abs(float(db.get_supplier_profile(
           "OrgA", "ALDERMOOR SUPPLY").get("delay_probability") or 0) - 0.5) < 1e-9)
_ca.post("/suppliers/flag", data={"supplier_name": "ALDERMOOR SUPPLY"})
_check("clearing the flag writes 0.2",
       abs(float(db.get_supplier_profile(
           "OrgA", "ALDERMOOR SUPPLY").get("delay_probability") or 0) - 0.2) < 1e-9)

# Settings must no longer accept supplier writes — that moved to /suppliers.
_ca.post("/settings", data={"action": "delete_supplier",
                            "supplier_name": "NORDVIK TRADING PTE LTD"})
_check("settings no longer deletes suppliers",
       db.query("SELECT 1 FROM supplier_profiles WHERE org_name=? AND supplier_name=?",
                ("OrgA", "NORDVIK TRADING PTE LTD")) != [])

# A viewer-role account is read-only. Flagging a supplier unreliable changes
# the safety buffer on every future order for the whole org, so it belongs
# behind analyst_required like every other mutating route -- not just behind
# login. Found by the security review; this is its regression guard.
db.execute("INSERT OR IGNORE INTO users (id, email, password_hash, org_name, role, "
           "email_verified, tier, session_version) VALUES (?,?,?,?,?,?,?,?)",
           (7003, "viewer@example.test", "x", "OrgA", "viewer", 1, "enterprise", 0))
_cv = flask_app.app.test_client()
with _cv.session_transaction() as _s:
    _s["user_id"], _s["org_name"], _s["role"], _s["sv"] = 7003, "OrgA", "viewer", 0

db.upsert_supplier_profile("OrgA", "VANMARK DAIRY", delay_probability=0.2)
_cv.post("/suppliers/flag", data={"supplier_name": "VANMARK DAIRY", "unreliable": "1"})
_check("viewer cannot flag a supplier unreliable",
       abs(float(db.get_supplier_profile(
           "OrgA", "VANMARK DAIRY").get("delay_probability") or 0) - 0.2) < 1e-9)

_cv.post("/suppliers/archive", data={"supplier_name": "VANMARK DAIRY", "archived": "1"})
_check("viewer cannot hide a supplier",
       "VANMARK DAIRY" in {r["supplier_name"] for r in db.get_supplier_scores("OrgA")})

_cv.post("/suppliers/merge", data={"canonical": "KESTREL COLD CHAIN",
                                   "variants": "VANMARK DAIRY"})
_check("viewer cannot merge suppliers",
       not [r for r in db.get_supplier_directory("OrgA")
            if r["supplier_name"] == "VANMARK DAIRY" and r["merged_into"]])

_check("viewer can still READ the suppliers page",
       _cv.get("/suppliers").status_code == 200)

# A later upload can spell a supplier more completely under the same normalised
# key, which renames its directory row. Anything merged into the old spelling
# must follow it — otherwise those rows point at a name that no longer exists,
# so they disappear from the page with no row left to undo them from.
# Found by the diff review; this is its regression guard.
db.merge_suppliers("OrgA", "NORDVIK TRADING PTE LTD", ["NORDVIK"])
db.execute(f"INSERT INTO purchase_orders_{SID_A} VALUES (?,?)",
           ("Brookvale UHT Milk 2L", "NORDVIK - TRADING - PTE - LTD"))
db.rebuild_supplier_directory("OrgA")

_dir = {r["supplier_name"]: r for r in db.get_supplier_directory("OrgA")}
_renamed = "NORDVIK - TRADING - PTE - LTD"
_check("the more complete spelling renames the directory row", _renamed in _dir,
       str(sorted(_dir)))
_check("a merged name follows its canonical through a rename",
       _dir.get("NORDVIK", {}).get("merged_into") == _renamed,
       str(_dir.get("NORDVIK", {}).get("merged_into")))
_check("no orphaned merge pointer is left behind",
       all(r["merged_into"] in _dir for r in _dir.values() if r["merged_into"]))

if _FAILED:
    sys.exit(1)
print("\nAll supplier-directory checks passed.")
