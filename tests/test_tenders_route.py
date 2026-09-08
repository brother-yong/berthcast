"""End-to-end test for the /tenders page and its upload.

Drives the real routes through Flask's test client with a CSV in hand, and
proves the things a parser test cannot:
  1. the page renders (a Jinja typo would otherwise ship silently)
  2. a real upload imports rows, drops its scratch table, and keeps no file
  3. a sheet missing a required column imports NOTHING and says which
  4. a sheet carrying its own customer or date columns is refused outright
  5. one org's tender rows never render on another org's page
  6. a viewer-role account cannot upload or delete

Throwaway temp DB, stubbed anthropic client, no API calls. CSRF is disabled for
the test client only. Run: python tests/test_tenders_route.py
"""
import io
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berthcast_tenders_route.db")
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

import database as db                                   # noqa: E402
import app as appmod                                    # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True
flask_app = appmod.app

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _make_user(email, org):
    db.execute("INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
               (email, generate_password_hash("x"), org, "claude-sonnet-5"))
    return db.query("SELECT id FROM users WHERE email=?", (email,))[0]["id"]


def _client(user_id, email, org, role="admin"):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["user_id"]  = user_id
        s["email"]    = email
        s["org_name"] = org
        s["model"]    = "claude-sonnet-5"
        s["is_admin"] = False
        s["role"]     = role
        s["sv"]       = 0
    return c


def _upload(client, csv_text, filename="tenders.csv", **form):
    """Post a sheet plus the four form fields the route now requires.

    The customer, the period and the quantity basis are answered on the form,
    not read off the sheet, so every upload has to carry them.
    """
    data = {"customer": "NORDVIK CATERING", "period_start": "2026-07-01",
            "period_end": "2026-12-31", "qty_basis": "per_month"}
    data.update(form)
    data["file"] = (io.BytesIO(csv_text.encode("utf-8")), filename)
    return client.post("/tenders/upload", data=data,
                       content_type="multipart/form-data", follow_redirects=True)


ALPHA_ID = _make_user("alpha@example.com", "OrgAlpha")
BRAVO_ID = _make_user("bravo@example.com", "OrgBravo")
VIEW_ID  = _make_user("viewer@example.com", "OrgAlpha")

alpha  = _client(ALPHA_ID, "alpha@example.com", "OrgAlpha")
bravo  = _client(BRAVO_ID, "bravo@example.com", "OrgBravo")
viewer = _client(VIEW_ID, "viewer@example.com", "OrgAlpha", role="viewer")


# ── 1. Empty page renders ────────────────────────────────────────────────────

r = alpha.get("/tenders")
_check("empty tenders page renders", r.status_code == 200, detail=str(r.status_code))
_check("empty state is shown",
       b"No tender sheets uploaded yet" in r.data)


# ── 2. A real upload imports ─────────────────────────────────────────────────

GOOD_CSV = (
    "Item Description,Tender Qty\n"
    "BROOKVALE UHT MILK 1L,1200\n"
    "KESTREL ORANGE JUICE 1L,800\n"
    ",50\n"
)
r = _upload(alpha, GOOD_CSV, "alpha_tenders.csv")
_check("upload returns the page", r.status_code == 200, detail=str(r.status_code))
_check("import count reported to the user", b"Imported 2 tender rows" in r.data)
_check("skipped row reported to the user", b"1 row skipped" in r.data)

rows = db.get_tender_commitments("OrgAlpha")
_check("two rows stored", len(rows) == 2, detail=str(len(rows)))
_check("the form's period is stored ISO on every row",
       all(x["period_start"] == "2026-07-01" and x["period_end"] == "2026-12-31"
           for x in rows), detail=str([(x["period_start"], x["period_end"]) for x in rows]))
_check("the form's customer and basis are stored on every row",
       all(x["customer"] == "NORDVIK CATERING" and x["qty_basis"] == "per_month"
           for x in rows), detail=str([(x["customer"], x["qty_basis"]) for x in rows]))
_check("item names render on the page", b"BROOKVALE UHT MILK 1L" in r.data)
_check("the filename is shown", b"alpha_tenders.csv" in r.data)
_check("the reject reason is shown", b"no item name" in r.data)
_check("page states the tender is added and the match is a guess to check",
       b"added on top of your order quantities" in r.data
       and b"automatically" in r.data)

uploads = db.get_tender_uploads("OrgAlpha")
_check("one upload record exists", len(uploads) == 1, detail=str(len(uploads)))
_check("scratch ingest table was dropped",
       not db.table_exists(f"tender_import_{uploads[0]['id']}"))
_leftovers = [f for f in os.listdir(appmod.UPLOAD_FOLDER) if "tender" in f] \
    if os.path.isdir(appmod.UPLOAD_FOLDER) else []
_check("the uploaded file itself is not kept on disk",
       _leftovers == [], detail=str(_leftovers))


# ── 3. Missing column imports nothing ────────────────────────────────────────

NO_QTY_CSV = ("Item,Notes\n"
              "BROOKVALE UHT MILK 1L,as agreed\n")
r = _upload(alpha, NO_QTY_CSV, "no_qty.csv")
_check("missing columns are named back to the user",
       b"Couldn&#39;t find a column for" in r.data or b"Couldn't find a column for" in r.data)
_check("nothing extra was imported",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))
_check("the failed upload left no sheet record",
       len(db.get_tender_uploads("OrgAlpha")) == 1,
       detail=str(len(db.get_tender_uploads("OrgAlpha"))))


# ── 3a. A sheet carrying its own customer and dates is refused ───────────────
# One customer and one period per file. Stamping the form's customer over a
# sheet that names several would mislabel a contract with no warning.

OLD_FIVE_COL_CSV = (
    "Customer,Item Description,Tender Qty,Start Date,End Date\n"
    "NORDVIK CATERING,BROOKVALE UHT MILK 1L,1200,01/01/2026,31/12/2026\n"
)
r = _upload(alpha, OLD_FIVE_COL_CSV, "old_format.csv")
_check("the old five-column sheet is refused",
       b"has its own customer or date column" in r.data)
# The header is quoted back as the ingest layer names it ("Start Date" arrives
# as start_date), and it is autoescaped: it came out of the client's own file.
_check("the refusal quotes the offending header back",
       b"&#34;customer&#34;" in r.data.lower(), detail="header not echoed")
_check("a refused sheet imports nothing",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))
_check("a refused sheet leaves no upload row",
       len(db.get_tender_uploads("OrgAlpha")) == 1,
       detail=str(len(db.get_tender_uploads("OrgAlpha"))))


# ── 3b. A wide sheet is read narrowly, and odd filenames still parse ─────────

# The two real columns buried in a wide export: only those may be materialised.
_wide_cols = [f"spare_col_{i}" for i in range(300)]
WIDE_CSV = (",".join(["Item", "Qty"] + _wide_cols) + "\n"
            + ",".join(["BROOKVALE UHT MILK 1L", "10"] + ["x"] * 300) + "\n")
r = _upload(alpha, WIDE_CSV, "wide.csv")
_check("a 302-column sheet still imports its two real columns",
       b"Imported 1 tender row" in r.data)
_wide_id = db.get_tender_uploads("OrgAlpha")[0]["id"]
db.delete_tender_upload("OrgAlpha", _wide_id)

# secure_filename() strips a fully non-ASCII stem to nothing, which used to
# leave the saved path extensionless and route a CSV to the xlsx parser.
r = _upload(alpha, GOOD_CSV, "訂單.csv")
_check("a non-ASCII filename still parses as CSV",
       b"Imported 2 tender rows" in r.data)
_cjk_id = db.get_tender_uploads("OrgAlpha")[0]["id"]
db.delete_tender_upload("OrgAlpha", _cjk_id)

# The filename is truncated to 120 chars for storage, but the extension has to
# come off the name _allowed() validated, or a long name lands on disk with the
# wrong suffix and gets handed to the wrong parser.
r = _upload(alpha, GOOD_CSV, ("x" * 100) + ".exe.backup.csv")
_check("a >120-character filename still parses as CSV",
       b"Imported 2 tender rows" in r.data)
db.delete_tender_upload("OrgAlpha", db.get_tender_uploads("OrgAlpha")[0]["id"])


# ── 4. Org isolation on the rendered page ────────────────────────────────────

r = bravo.get("/tenders")
_check("the other org's page renders", r.status_code == 200)
_check("org B never sees org A's customers", b"NORDVIK CATERING" not in r.data)
_check("org B never sees org A's filename", b"alpha_tenders.csv" not in r.data)

alpha_upload_id = db.get_tender_uploads("OrgAlpha")[0]["id"]
bravo.post("/tenders/delete", data={"upload_id": str(alpha_upload_id)},
           follow_redirects=True)
_check("org B cannot delete org A's sheet",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))


# ── 5. Viewer role is read-only ──────────────────────────────────────────────

r = viewer.get("/tenders")
_check("a viewer can still read the page", r.status_code == 200)
_check("a viewer is not offered the remove button", b"Remove this sheet" not in r.data)

_upload(viewer, GOOD_CSV, "viewer_tenders.csv")
_check("a viewer cannot upload",
       len(db.get_tender_uploads("OrgAlpha")) == 1,
       detail=str(len(db.get_tender_uploads("OrgAlpha"))))

viewer.post("/tenders/delete", data={"upload_id": str(alpha_upload_id)},
            follow_redirects=True)
_check("a viewer cannot delete",
       len(db.get_tender_commitments("OrgAlpha")) == 2,
       detail=str(len(db.get_tender_commitments("OrgAlpha"))))


# ── 6. Owner delete works ────────────────────────────────────────────────────

alpha.post("/tenders/delete", data={"upload_id": str(alpha_upload_id)},
           follow_redirects=True)
_check("the owning org can delete its own sheet",
       db.get_tender_commitments("OrgAlpha") == [] and db.get_tender_uploads("OrgAlpha") == [])


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll tender route tests passed.")
