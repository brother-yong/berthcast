"""A failed or cut-off duplicate scan must never look like a clean "no duplicates".

Two gaps on /dedup/stream, found in review on 8 Oct 2026:
  1. A reconnect (refresh, EventSource retry) is served from normalization_cache.
     That branch always sent "done", so a scan that FAILED came back as
     "No duplicates detected" on the second load.
  2. A reply cut off at max_tokens was parsed and shown as complete. Either the
     repaired groups were shown with no warning, or (cut before the first group
     closed) nothing parsed and staff saw "No duplicates detected".

Invented brands only. Run: python tests/test_dedup_cutoff_and_cache_break.py
"""
import json
import os
import sys
import tempfile
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_dedup_cutoff_break_")
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
    raise_exc = None

    def __init__(self, **kw):
        pass

    @property
    def messages(self):
        class _M:
            def stream(self, **kw):
                _FakeAnthropic.calls.append(kw)
                if _FakeAnthropic.raise_exc:
                    raise _FakeAnthropic.raise_exc
                return _FakeStream(_FakeAnthropic.chunks)
        return _M()


appmod._anthropic.Anthropic = _FakeAnthropic

ORG = "NORDVIK Trading"
uid = db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
                 ("buyer@nordvik.test", generate_password_hash("x"), ORG, "claude-haiku-5-5", "enterprise"))


def _session(names=("BROOKVALE OAT MILK 1L", "BRKVL OAT MLK 1L",
                    "NORDVIK RYE FLOUR 25KG", "NORDVIK RYE FLR 25 KG")):
    sid = db.execute("INSERT INTO upload_sessions (user_id, org_name, status, scope, context_json) "
                     "VALUES (?,?,?,?,?)", (uid, ORG, "uploading", "all", "{}"))
    db.execute(f'CREATE TABLE inventory_{sid} ("description" TEXT, "qty" TEXT)')
    for n in names:
        db.execute(f"INSERT INTO inventory_{sid} VALUES (?,?)", (n, "5"))
    return sid


client = appmod.app.test_client()
with client.session_transaction() as s:
    s.update(user_id=uid, email="buyer@nordvik.test", org_name=ORG, model="claude-haiku-5-5",
             is_admin=False, tier="enterprise", role="admin")


def _get_stream(sid):
    """One GET of the stream. Does NOT clear the cache, so a second call is a reconnect."""
    rate_limit._hits.clear()
    r = client.get(f"/dedup/stream/{sid}")
    body = r.get_data(as_text=True)
    r.close()
    return [json.loads(x[6:]) for x in body.splitlines() if x.startswith("data: ")]


def _scan(sid, chunks=(), raise_exc=None):
    appmod.normalization_cache.pop(sid, None)
    _FakeAnthropic.chunks = list(chunks)
    _FakeAnthropic.raise_exc = raise_exc
    _FakeAnthropic.calls = []
    try:
        return _get_stream(sid)
    finally:
        _FakeAnthropic.raise_exc = None


def _types(events):
    return [e.get("type") for e in events]


# ── 1. Reconnect after a FAILED scan ─────────────────────────────────────────
# 1a. Empty reply: first load says error; a reconnect must say error too.
SID = _session()
first = _scan(SID, [])
_check("setup: empty reply errors on the first load", "error" in _types(first), detail=str(first))
_FakeAnthropic.calls = []
again = _get_stream(SID)
_check("reconnect after an empty reply is served from cache (no second Claude call)",
       _FakeAnthropic.calls == [], detail=str(len(_FakeAnthropic.calls)))
_check("reconnect after an empty reply reports the error, not 'done'",
       "error" in _types(again) and "done" not in _types(again), detail=str(again))
_check("reconnect error carries the original message",
       any(e.get("type") == "error" and "no answer" in (e.get("msg") or "") for e in again),
       detail=str(again))

# 1b. API exception: same promise.
SID_EXC = _session()
first = _scan(SID_EXC, raise_exc=RuntimeError("PADIMAS overloaded"))
_check("setup: API exception errors on the first load", "error" in _types(first), detail=str(first))
again = _get_stream(SID_EXC)
_check("reconnect after an API exception reports the error, not 'done'",
       any(e.get("type") == "error" and "PADIMAS overloaded" in (e.get("msg") or "") for e in again)
       and "done" not in _types(again), detail=str(again))

# 1c'. The review page the loading page redirects to must say it failed.
page = client.get(f"/dedup/{SID}").get_data(as_text=True)
_check("review page after a failed scan shows the failure", "no answer" in page,
       detail="failure message missing from the page")
_check("review page after a failed scan never says no duplicates were detected",
       "No duplicate item names were detected" not in page, detail="page claims a clean scan")

# 1c''. An exception with no text still leaves staff a reason.
SID_BLANK = _session()
first = _scan(SID_BLANK, raise_exc=RuntimeError(""))
again = _get_stream(SID_BLANK)
_check("exception with no text still sends a non-blank error message",
       "error" in _types(again)
       and all((e.get("msg") or "").strip() for e in first + again if e.get("type") == "error"),
       detail=str(first + again))

# 1c. Control: a good scan still reconnects as done with its group count.
SID_OK = _session()
_scan(SID_OK, ['[{"canonical": "BROOKVALE OAT MILK 1L", "variants": ["BRKVL OAT MLK 1L"]}]'])
again = _get_stream(SID_OK)
_check("control: reconnect after a good scan is 'done' with 1 group",
       any(e.get("type") == "done" and e.get("count") == 1 for e in again)
       and "error" not in _types(again), detail=str(again))

# 1d. Control: a session with no item names is not a failure.
SID_NONE = _session(names=())
_scan(SID_NONE)
again = _get_stream(SID_NONE)
_check("control: reconnect when there were no item names is still 'done'",
       "done" in _types(again) and "error" not in _types(again), detail=str(again))
page = client.get(f"/dedup/{SID_NONE}").get_data(as_text=True)
_check("control: no item names keeps the plain 'no duplicates' page",
       "No duplicate item names were detected" in page, detail="plain page changed")

# ── 2. Reply cut off at max_tokens ───────────────────────────────────────────
# 2a. Cut after one complete group: keep it, but warn staff on the review page.
SID_CUT = _session()
ev = _scan(SID_CUT, ['[{"canonical": "BROOKVALE OAT MILK 1L", "variants": ["BRKVL OAT MLK 1L"]}, ',
                     '{"canonical": "NORDVIK RYE FLOUR 25KG", "variants": ["NORDVIK RYE FL'])
cached = appmod.normalization_cache.get(SID_CUT) or {}
_check("cut-off reply keeps the complete group", len(cached.get("groups") or []) == 1, detail=str(cached))
_check("cut-off reply records a warning message", bool(str(cached.get("message", "")).strip()),
       detail=str(cached))
page = client.get(f"/dedup/{SID_CUT}").get_data(as_text=True)
_check("review page shows the cut-off warning next to the groups",
       bool(cached.get("message")) and cached["message"] in page and "BROOKVALE OAT MILK 1L" in page,
       detail="warning missing from the page")

# 2b. Cut before any group closed: nothing parses. Must not be 'done, 0 groups'.
SID_CUT0 = _session()
ev = _scan(SID_CUT0, ['[{"canonical": "BROOKVALE OAT MILK 1L", "variants": ["BRKVL OAT'])
_check("reply cut before the first group closes is an error, not 'done, 0 groups'",
       "error" in _types(ev) and "done" not in _types(ev), detail=str(ev))
again = _get_stream(SID_CUT0)
_check("...and a reconnect says the same", "error" in _types(again) and "done" not in _types(again),
       detail=str(again))

# 2c. Control: a complete reply carries no warning.
cached = appmod.normalization_cache.get(SID_OK) or {}
_check("control: complete reply has no warning message", cached.get("message") == "", detail=str(cached))
SID_EMPTY_OK = _session()
ev = _scan(SID_EMPTY_OK, ["[]"])
_check("control: model says '[]' -> clean 'done, 0 groups'",
       any(e.get("type") == "done" and e.get("count") == 0 for e in ev), detail=str(ev))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll dedup cut-off and cache break tests passed.")
