"""An empty model reply must never be reported as "no duplicates".

The 8 Oct 2026 model update made _call_claude return "" for a reply with no
text (a refusal, or Haiku 5.5's default thinking used the whole cap), and added
a guard in agents/normalization.py so that "" is an error, not "No duplicates
found.". But run_normalization_agent is only called by smoke_live.py. The page
staff actually use is /dedup/stream in app.py, which builds its own request and
parses its own reply. This file drives that route with an empty reply, and also
pushes a whitespace-only reply through the agent.

Expected (what the agent guard already promises): an empty reply is surfaced as
an error and the review page does not tell staff every product is unique.

Invented brands only. Run: python tests/test_dedup_empty_reply_break.py
"""
import json
import os
import sys
import tempfile
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_dedup_empty_break_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
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
import rate_limit                                       # noqa: E402
import app as appmod                                    # noqa: E402
import agents.normalization as norm                     # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self._chunks)

    def get_final_message(self):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=900, output_tokens=8000,
                                                     cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0))


class _FakeAnthropic:
    chunks = []
    calls = []

    def __init__(self, **kw):
        pass

    @property
    def messages(self):
        outer = self

        class _M:
            def stream(self, **kw):
                _FakeAnthropic.calls.append(kw)
                return _FakeStream(_FakeAnthropic.chunks)
        return _M()


appmod._anthropic.Anthropic = _FakeAnthropic

ORG = "BROOKVALE Foods"
uid = db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
                 ("buyer@brookvale.test", generate_password_hash("x"), ORG, "claude-haiku-5-5", "enterprise"))


def _session_with_names():
    sid = db.execute("INSERT INTO upload_sessions (user_id, org_name, status, scope, context_json) "
                     "VALUES (?,?,?,?,?)", (uid, ORG, "uploading", "all", "{}"))
    db.execute(f'CREATE TABLE inventory_{sid} ("description" TEXT, "qty" TEXT)')
    for n in ("BROOKVALE OAT MILK 1L", "BRKVL OAT MLK 1L", "NORDVIK RYE FLOUR 25KG", "NORDVIK RYE FLR 25 KG"):
        db.execute(f"INSERT INTO inventory_{sid} VALUES (?,?)", (n, "5"))
    return sid


client = appmod.app.test_client()
with client.session_transaction() as s:
    s.update(user_id=uid, email="buyer@brookvale.test", org_name=ORG, model="claude-haiku-5-5",
             is_admin=False, tier="enterprise", role="admin")


def _run_stream(sid, chunks):
    rate_limit._hits.clear()
    appmod.normalization_cache.pop(sid, None)
    _FakeAnthropic.chunks = list(chunks)
    _FakeAnthropic.calls = []
    r = client.get(f"/dedup/stream/{sid}")
    body = r.get_data(as_text=True)
    r.close()
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except ValueError:
                pass
    return r, events


# ── Control: a real reply still works through the live route ─────────────────
SID_OK = _session_with_names()
r, ev = _run_stream(SID_OK, ['[{"canonical": "BROOKVALE OAT MILK 1L", ',
                             '"variants": ["BRKVL OAT MLK 1L"]}]'])
_check("control: real reply -> done with 1 group",
       r.status_code == 200 and any(e.get("type") == "done" and e.get("count") == 1 for e in ev),
       detail=f"{r.status_code} {ev}")

# ── 1. Empty reply on /dedup/stream (the page staff use) ─────────────────────
SID = _session_with_names()
r, ev = _run_stream(SID, [])
_check("route reached the model (setup sanity)", len(_FakeAnthropic.calls) == 1, detail=str(len(_FakeAnthropic.calls)))
cached = appmod.normalization_cache.get(SID) or {}
_check("empty reply on /dedup/stream is surfaced as an error, not a clean 'done, 0 groups'",
       any(e.get("type") == "error" for e in ev) or bool(str(cached.get("message", "")).strip()),
       detail=f"events={ev} cached={cached}")

page = client.get(f"/dedup/{SID}").get_data(as_text=True)
_check("review page after an empty reply does not tell staff every product is unique with no warning",
       not (bool(cached) and not cached.get("message")
            and "No duplicate item names were detected" in page),
       detail="page shows the plain 'No duplicate item names were detected' line")

# ── 1b. Reply cut off by the 8000-token cap (Haiku 5.5 thinks inside that cap) ─
#    The agent path repairs this with _extract_json_array; the live route's own
#    regex + json.loads throws the found groups away and reports a clean zero.
SID_CUT = _session_with_names()
r, ev = _run_stream(SID_CUT, ['[{"canonical": "BROOKVALE OAT MILK 1L", "variants": ["BRKVL OAT MLK 1L"]}, ',
                              '{"canonical": "NORDVIK RYE FLOUR 25KG", "variants": ["NORDVIK RYE FL'])
cached = appmod.normalization_cache.get(SID_CUT) or {}
_check("cut-off reply keeps the complete group or reports a problem, never a clean 'done, 0 groups'",
       len(cached.get("groups") or []) >= 1 or bool(str(cached.get("message", "")).strip())
       or any(e.get("type") == "error" for e in ev),
       detail=f"events={ev} cached={cached}")

# ── 2. Whitespace-only reply through the agent (same promise, one char wider) ─
_orig = norm._call_claude
try:
    norm._call_claude = lambda *a, **k: "\n\n"
    out = norm.run_normalization_agent(SID, "claude-haiku-5-5")
    _check("whitespace-only reply -> error branch, not 'No duplicates found.'",
           str(out.get("message", "")).startswith("Normalisation agent error"), detail=str(out))
finally:
    norm._call_claude = _orig

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll dedup empty-reply break tests passed.")
