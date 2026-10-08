"""Adversarial pins for the 8 Oct 2026 model update (Sonnet 5.5 / Haiku 5.5).

What the update changed, and what this file locks so it can't silently regress:
  1. /api/chat: a reply with no text (a refusal, or Haiku 5.5's thinking used the
     whole cap) saves NO assistant row; a real reply is still saved. A rival
     org's conversation id is refused before anything is written.
     The chat title reads the first TEXT block, not content[0] (a thinking block
     can come first).
  2. /admin: a user whose stored model is no longer offered still shows it,
     selected, marked "(old)"; offered models never get "(old)"; a hostile
     stored model value is HTML-escaped.
  3. run_normalization_agent: an empty reply is the error branch, never
     "No duplicates found.", and a good reply still parses.
  4. record_usage pricing: 5.5 rows win over their 5 prefix, Haiku 5.5's cache
     tokens count toward its 100K long-prompt threshold.
  5. thinking/sampling kwargs: dated snapshot ids resolve to the same row as
     their alias; a dated Sonnet 5 id never picks up Sonnet 5.5's switch; the
     helpers actually reach messages.stream through _call_claude.

Invented brands only (BROOKVALE, NORDVIK, PADIMAS).
Run: python tests/test_model_update_break.py
"""
import json
import os
import re
import sys
import tempfile
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_modelupd_break_")
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
import agents.shared as shared                          # noqa: E402
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


def _usage(i=10, o=5, cw=0, cr=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o,
                           cache_creation_input_tokens=cw, cache_read_input_tokens=cr)


# ── Fake Anthropic client for app.py's two stream routes ─────────────────────
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
        return SimpleNamespace(usage=_usage())


class _FakeMessages:
    def stream(self, **kw):
        _FakeAnthropic.stream_calls.append(kw)
        return _FakeStream(_FakeAnthropic.chunks)

    def create(self, **kw):
        _FakeAnthropic.create_calls.append(kw)
        return SimpleNamespace(model=kw.get("model"), usage=_usage(),
                               content=_FakeAnthropic.title_content)


class _FakeAnthropic:
    chunks = []
    stream_calls = []
    create_calls = []
    title_content = []

    def __init__(self, **kw):
        pass

    @property
    def messages(self):
        return _FakeMessages()


appmod._anthropic.Anthropic = _FakeAnthropic


def _sse(body):
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def _chat(client, message, conversation_id=None, chunks=(), title_content=None):
    _FakeAnthropic.chunks = list(chunks)
    _FakeAnthropic.stream_calls = []
    _FakeAnthropic.create_calls = []
    _FakeAnthropic.title_content = title_content if title_content is not None else [
        SimpleNamespace(type="text", text="NORDVIK reorder planning")]
    payload = {"message": message}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    r = client.post("/api/chat", json=payload)
    body = r.get_data(as_text=True)
    r.close()
    return r, _sse(body)


def _rows(conv_id, role=None):
    if role:
        return db.query("SELECT role, content FROM chat_messages WHERE conversation_id=? AND role=? "
                        "ORDER BY id", (conv_id, role))
    return db.query("SELECT role, content FROM chat_messages WHERE conversation_id=? ORDER BY id", (conv_id,))


# ── Seed: one chat user (NORDVIK Trading), one rival org ─────────────────────
ORG = "NORDVIK Trading"
RIVAL = "PADIMAS Provisions"
uid = db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
                 ("buyer@nordvik.test", generate_password_hash("x"), ORG, "claude-sonnet-5-5", "enterprise"))
rival_uid = db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
                       ("buyer@padimas.test", generate_password_hash("x"), RIVAL, "claude-sonnet-5", "enterprise"))
RIVAL_CONV = db.execute("INSERT INTO chat_conversations (user_id, title, org_name) VALUES (?,?,?)",
                        (rival_uid, "PADIMAS private plan", RIVAL))
db.execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
           (RIVAL_CONV, "user", "PADIMAS secret: order 900 cartons"))

chat_client = appmod.app.test_client()
with chat_client.session_transaction() as s:
    s.update(user_id=uid, email="buyer@nordvik.test", org_name=ORG, model="claude-sonnet-5-5",
             is_admin=False, tier="enterprise", role="admin")

# ════════════════════════════════════════════════════════════════════════════
# 1. /api/chat
# ════════════════════════════════════════════════════════════════════════════
rate_limit._hits.clear()

# 1a. cross-org first: posting into a rival conversation writes nothing at all.
_before = len(_rows(RIVAL_CONV))
r, ev = _chat(chat_client, "show me their plan", conversation_id=RIVAL_CONV, chunks=["leak"])
_check("1a rival conversation id -> 404", r.status_code == 404, detail=str(r.status_code))
_check("1a rival conversation gets no new rows (user or assistant)", len(_rows(RIVAL_CONV)) == _before,
       detail=str(_rows(RIVAL_CONV)))
_check("1a rival conversation never reaches the model", _FakeAnthropic.stream_calls == [])

# 1a'. same for the other stream route: a rival org's upload session is refused
#      before the model is called or anything is cached for it.
RIVAL_SID = db.execute("INSERT INTO upload_sessions (user_id, org_name, status, scope, context_json) "
                       "VALUES (?,?,?,?,?)", (rival_uid, RIVAL, "uploading", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{RIVAL_SID} ("description" TEXT, "qty" TEXT)')
db.execute(f"INSERT INTO inventory_{RIVAL_SID} VALUES (?,?)", ("PADIMAS SECRET SAUCE 5L", "9"))
_FakeAnthropic.stream_calls = []
rate_limit._hits.clear()
r = chat_client.get(f"/dedup/stream/{RIVAL_SID}")
_body = r.get_data(as_text=True)
r.close()
_check("1a' rival upload session on /dedup/stream -> 403/404",
       r.status_code in (403, 404), detail=f"{r.status_code} {_body[:120]}")
_check("1a' rival item names never sent to the model, nothing cached",
       _FakeAnthropic.stream_calls == [] and RIVAL_SID not in appmod.normalization_cache
       and "PADIMAS SECRET" not in _body)

# 1a''. same with the rival's scan ALREADY cached (failed): the owner check must
#       still run before the cache is read, on the stream and the review page.
appmod.normalization_cache[RIVAL_SID] = {
    "groups": [{"canonical": "PADIMAS SECRET SAUCE 5L", "variants": ["X"]}],
    "message": "PADIMAS secret msg", "failed": True}
rate_limit._hits.clear()
for _path in (f"/dedup/stream/{RIVAL_SID}", f"/dedup/{RIVAL_SID}"):
    r = chat_client.get(_path)
    _body = r.get_data(as_text=True)
    r.close()
    _check(f"1a'' rival cached scan on {_path.rsplit('/', 1)[0]} -> 403/404, nothing leaked",
           r.status_code in (403, 404) and "PADIMAS" not in _body,
           detail=f"{r.status_code} {_body[:120]}")
appmod.normalization_cache.pop(RIVAL_SID, None)

# 1b. empty reply (text_stream yields nothing) on a NEW conversation.
r, ev = _chat(chat_client, "how many BROOKVALE oat milk cases should I order?", chunks=[])
conv_empty = next((e["conversation_id"] for e in ev if "conversation_id" in e), None)
_check("1b empty reply: stream answered 200 with a conversation id",
       r.status_code == 200 and conv_empty is not None, detail=f"{r.status_code} {ev}")
_check("1b empty reply: NO assistant row saved", conv_empty is not None and _rows(conv_empty, "assistant") == [],
       detail=str(_rows(conv_empty) if conv_empty else None))
_check("1b empty reply: the user's question is still saved",
       conv_empty is not None and [x["content"] for x in _rows(conv_empty, "user")]
       == ["how many BROOKVALE oat milk cases should I order?"])
# An error, not "done": a bare "done" left the chat page with an empty bubble.
_check("1b empty reply: stream ends with an error the page can show, not a bare done",
       any(e.get("error") for e in ev) and not any(e.get("done") for e in ev), detail=str(ev))
_check("1b sonnet-5-5 chat sends between_tools thinking, never 'disabled'",
       _FakeAnthropic.stream_calls and _FakeAnthropic.stream_calls[0].get("thinking") == {"type": "between_tools"},
       detail=str(_FakeAnthropic.stream_calls[:1]))

# 1c. a stream of empty-string chunks is still an empty reply.
r, ev = _chat(chat_client, "BROOKVALE stock check", chunks=["", "", ""])
conv_blank = next((e["conversation_id"] for e in ev if "conversation_id" in e), None)
_check("1c empty-string chunks: NO assistant row saved",
       conv_blank is not None and _rows(conv_blank, "assistant") == [],
       detail=str(_rows(conv_blank) if conv_blank else None))

# 1d. a real reply is still saved, joined exactly.
r, ev = _chat(chat_client, "NORDVIK rye flour cover?", chunks=["Order 40 ", "bags of ", "NORDVIK rye flour."])
conv_ok = next((e["conversation_id"] for e in ev if "conversation_id" in e), None)
_saved = _rows(conv_ok, "assistant") if conv_ok else []
_check("1d non-empty reply: exactly one assistant row with the joined text",
       [x["content"] for x in _saved] == ["Order 40 bags of NORDVIK rye flour."], detail=str(_saved))
_check("1d streamed chunks reached the browser",
       [e["text"] for e in ev if "text" in e] == ["Order 40 ", "bags of ", "NORDVIK rye flour."], detail=str(ev))

# 1e. continuing the empty-reply conversation: history has no empty turn to poison it,
#     and the new reply lands.
r, ev = _chat(chat_client, "try again please", conversation_id=conv_empty, chunks=["About 12 cases."])
sent = _FakeAnthropic.stream_calls[0]["messages"] if _FakeAnthropic.stream_calls else []
_check("1e follow-up sends no empty-content turn",
       sent and all(str(m.get("content", "")).strip() for m in sent), detail=str(sent))
_check("1e follow-up reply is saved to the same conversation",
       [x["content"] for x in _rows(conv_empty, "assistant")] == ["About 12 cases."],
       detail=str(_rows(conv_empty)))

# 1f. the title call: Haiku 5.5, thinking off, reads the TEXT block even when a
#     thinking block comes first (content[0].text would AttributeError and lose the title).
r, ev = _chat(chat_client, "PADIMAS vs BROOKVALE lead times", chunks=["Lead times differ."],
              title_content=[SimpleNamespace(type="thinking", thinking="hmm"),
                             SimpleNamespace(type="text", text='"Supplier lead time comparison"')])
conv_t = next((e["conversation_id"] for e in ev if "conversation_id" in e), None)
_title = db.query("SELECT title FROM chat_conversations WHERE id=?", (conv_t,))[0]["title"] if conv_t else None
_check("1f title taken from the text block after a thinking block, quotes stripped",
       _title == "Supplier lead time comparison", detail=f"{_title!r} {ev}")
_tc = _FakeAnthropic.create_calls[0] if _FakeAnthropic.create_calls else {}
_check("1f title call is Haiku 5.5 with thinking disabled",
       _tc.get("model") == "claude-haiku-5-5" and _tc.get("thinking") == {"type": "disabled"}, detail=str(_tc))

# 1g. a title reply with no text block at all keeps the default title, no crash.
r, ev = _chat(chat_client, "NORDVIK cover", chunks=["Fine."],
              title_content=[SimpleNamespace(type="thinking", thinking="only thinking")])
conv_nt = next((e["conversation_id"] for e in ev if "conversation_id" in e), None)
_title = db.query("SELECT title FROM chat_conversations WHERE id=?", (conv_nt,))[0]["title"] if conv_nt else None
_check("1g no-text title reply keeps 'New conversation' and the stream still finishes",
       _title == "New conversation" and any(e.get("done") for e in ev), detail=f"{_title!r} {ev}")

# ════════════════════════════════════════════════════════════════════════════
# 2. /admin per-user model dropdown
# ════════════════════════════════════════════════════════════════════════════
_admin_id = db.query("SELECT id FROM users WHERE is_admin=1")[0]["id"]
EVIL = 'claude-x"><script>alert(1)</script>'
EVIL2 = "claude-<b>bold</b>"
_seed = {
    "old-sonnet@nordvik.test": "claude-sonnet-5",
    "old-opus@nordvik.test": "claude-opus-5",
    "old-haiku@padimas.test": "claude-haiku-4-5-20251001",
    "cur-sonnet@brookvale.test": "claude-sonnet-5-5",
    "cur-haiku@brookvale.test": "claude-haiku-5-5",
    "evil@padimas.test": EVIL,
    "evil2@padimas.test": EVIL2,
}
_ids = {}
for email, model in _seed.items():
    _ids[email] = db.execute(
        "INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
        (email, generate_password_hash("x"), "BROOKVALE Foods", model, "enterprise"))

admin_client = appmod.app.test_client()
with admin_client.session_transaction() as s:
    s.update(user_id=_admin_id, email="admin@berthcast.com", org_name="berthcast Admin",
             model="claude-sonnet-5-5", is_admin=True, tier="enterprise", role="admin")
r = admin_client.get("/admin")
page = r.get_data(as_text=True)
_check("2 /admin renders for the admin", r.status_code == 200, detail=str(r.status_code))

_blocks = dict(re.findall(
    r'<input type="hidden" name="action" value="change_model">\s*'
    r'<input type="hidden" name="user_id" value="(\d+)">\s*'
    r'<select name="model"[^>]*>(.*?)</select>', page, re.S))


def _block(email):
    return _blocks.get(str(_ids[email]), "")


for email, model in (("old-sonnet@nordvik.test", "claude-sonnet-5"),
                     ("old-opus@nordvik.test", "claude-opus-5"),
                     ("old-haiku@padimas.test", "claude-haiku-4-5-20251001")):
    b = _block(email)
    _check(f"2 {model}: shown selected as '(old)'",
           f'<option value="{model}" selected>{model} (old)</option>' in b, detail=b.strip()[:300])
    _check(f"2 {model}: exactly one option selected (no offered model pre-selected)",
           b.count("selected") == 1, detail=b.strip()[:300])

for email, model in (("cur-sonnet@brookvale.test", "claude-sonnet-5-5"),
                     ("cur-haiku@brookvale.test", "claude-haiku-5-5")):
    b = _block(email)
    _check(f"2 {model}: no '(old)' option", b and "(old)" not in b, detail=b.strip()[:300])
    _check(f"2 {model}: its offered option is the one selected",
           re.search(rf'<option value="{re.escape(model)}"\s+selected>', b) is not None
           and b.count("selected") == 1, detail=b.strip()[:300])
    _check(f"2 {model}: only the two offered options are listed",
           re.findall(r'<option value="([^"]*)"', b) == ["claude-sonnet-5-5", "claude-haiku-5-5"],
           detail=str(re.findall(r'<option value="([^"]*)"', b)))

b = _block("evil@padimas.test")
_check("2 hostile model value: quote and angle brackets escaped inside value=",
       'value="claude-x&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" selected' in b, detail=b.strip()[:300])
_check("2 hostile model value: escaped in the visible '(old)' text too",
       "claude-x&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt; (old)" in b, detail=b.strip()[:300])
_check("2 hostile model value: raw <script> never reaches the page", "<script>alert(1)</script>" not in page)
b2 = _block("evil2@padimas.test")
_check("2 '<b>' model value escaped, not rendered as markup",
       "claude-&lt;b&gt;bold&lt;/b&gt; (old)" in b2 and "<b>bold</b>" not in page, detail=b2.strip()[:300])
_check("2 create-user form pre-selects Sonnet 5.5",
       re.search(r'<option value="claude-sonnet-5-5"\s+selected>Sonnet: most accurate', page) is not None)

# ════════════════════════════════════════════════════════════════════════════
# 3. run_normalization_agent on an empty reply
# ════════════════════════════════════════════════════════════════════════════
NSID = db.execute("INSERT INTO upload_sessions (user_id, org_name, status, scope, context_json) "
                  "VALUES (?,?,?,?,?)", (uid, ORG, "uploading", "all", "{}"))
db.execute(f'CREATE TABLE inventory_{NSID} ("description" TEXT, "qty" TEXT)')
for n in ("BROOKVALE OAT MILK 1L", "BRKVL OAT MLK 1L", "NORDVIK RYE FLOUR 25KG"):
    db.execute(f"INSERT INTO inventory_{NSID} VALUES (?,?)", (n, "5"))

_orig_norm_call = norm._call_claude
try:
    for label, reply in (("empty string", ""), ("None", None)):
        norm._call_claude = lambda *a, _r=reply, **k: _r
        out = norm.run_normalization_agent(NSID, "claude-haiku-5-5")
        _check(f"3 {label} reply -> error branch, not 'No duplicates found.'",
               str(out.get("message", "")).startswith("Normalisation agent error")
               and out.get("groups") == [], detail=str(out))

    norm._call_claude = lambda *a, **k: (
        '[{"canonical": "BROOKVALE OAT MILK 1L", "variants": ["BRKVL OAT MLK 1L"]}]')
    out = norm.run_normalization_agent(NSID, "claude-haiku-5-5")
    _check("3 a real reply still parses into groups (guard does not eat good replies)",
           len(out.get("groups") or []) == 1 and out["groups"][0]["canonical"] == "BROOKVALE OAT MILK 1L",
           detail=str(out))

    norm._call_claude = lambda *a, **k: "[]"
    out = norm.run_normalization_agent(NSID, "claude-haiku-5-5")
    _check("3 an explicit '[]' reply is 'no groups', not an error",
           out.get("groups") == [] and "error" not in str(out.get("message", "")).lower(), detail=str(out))
finally:
    norm._call_claude = _orig_norm_call

# ════════════════════════════════════════════════════════════════════════════
# 4. record_usage pricing: prefix traps
# ════════════════════════════════════════════════════════════════════════════
_orig_info = shared.logger.info
shared.logger.info = lambda *a, **k: None


def _usd(model, i=0, o=0, cw=0, cr=0):
    before = shared.USAGE["usd"]
    shared.record_usage(model, lambda: _usage(i, o, cw, cr), "test")
    return shared.USAGE["usd"] - before


def _near(a, b):
    return abs(a - b) < 1e-9


try:
    # Cache reads isolate the 5.5 vs 5 difference (every other Sonnet rate is equal).
    for model, want in (("claude-sonnet-5-5", 0.10), ("claude-sonnet-5-5-20261001", 0.10),
                        ("claude-sonnet-5", 0.20), ("claude-sonnet-5-20260514", 0.20)):
        got = _usd(model, cr=1_000_000)
        _check(f"4 {model}: 1M cache-read tokens cost ${want:.2f}", _near(got, want), detail=str(got))

    # Haiku 5.5 card, each rate alone, under the 100K long-prompt line.
    for field, kw, want in (("input", {"i": 10_000}, 0.001), ("output", {"o": 10_000}, 0.005),
                            ("cache write", {"cw": 10_000}, 0.00125), ("cache read", {"cr": 10_000}, 0.0001)):
        got = _usd("claude-haiku-5-5", **kw)
        _check(f"4 haiku-5-5 {field}: 10K tokens = ${want}", _near(got, want), detail=str(got))
    got = _usd("claude-haiku-5-5-20261001", i=10_000)
    _check("4 dated haiku-5-5 id uses the Haiku 5.5 card", _near(got, 0.001), detail=str(got))

    # Haiku 4.5 must never be priced (or long-prompt multiplied) as Haiku 5.5.
    got = _usd("claude-haiku-4-5-20251001", i=200_000)
    _check("4 haiku-4-5: 200K input = $0.20, no 5.5 card, no 5x", _near(got, 0.20), detail=str(got))

    # The 100K line counts cached prompt tokens too: 40K input + 70K cache read = 110K.
    got = _usd("claude-haiku-5-5", i=40_000, cr=70_000)
    _check("4 haiku-5-5: cache reads count toward the 100K line (5x applies)",
           _near(got, 5 * (40_000 * 0.10 + 70_000 * 0.01) / 1e6), detail=str(got))
    got = _usd("claude-haiku-5-5", i=40_000, cw=60_000)
    _check("4 haiku-5-5: exactly 100K incl. cache writes stays on the base card",
           _near(got, (40_000 * 0.10 + 60_000 * 0.125) / 1e6), detail=str(got))
    # Output tokens are not prompt: 90K in + 50K out is still the base card.
    got = _usd("claude-haiku-5-5", i=90_000, o=50_000)
    _check("4 haiku-5-5: output tokens do not push a prompt over the 100K line",
           _near(got, (90_000 * 0.10 + 50_000 * 0.50) / 1e6), detail=str(got))
finally:
    shared.logger.info = _orig_info

# ════════════════════════════════════════════════════════════════════════════
# 5. thinking / sampling kwargs: dated ids and the Sonnet 5 / 5.5 collision
# ════════════════════════════════════════════════════════════════════════════
from agents.shared import sampling_kwargs, thinking_kwargs  # noqa: E402

_check("5 dated sonnet-5-5 id -> between_tools",
       thinking_kwargs("claude-sonnet-5-5-20261001") == {"thinking": {"type": "between_tools"}},
       detail=str(thinking_kwargs("claude-sonnet-5-5-20261001")))
_check("5 dated sonnet-5 id stays 'disabled' (must not catch 5.5's switch)",
       thinking_kwargs("claude-sonnet-5-20260514") == {"thinking": {"type": "disabled"}},
       detail=str(thinking_kwargs("claude-sonnet-5-20260514")))
_check("5 dated haiku-5-5 id: no temperature, default thinking",
       sampling_kwargs("claude-haiku-5-5-20261001") == {} and thinking_kwargs("claude-haiku-5-5-20261001") == {})
_check("5 haiku-4-5 dated id keeps temperature=0 (not swept into the haiku-5 no-temp prefix)",
       sampling_kwargs("claude-haiku-4-5-20251001") == {"temperature": 0})


class _CapStream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_text(self):
        return "[]"

    def get_final_message(self):
        return SimpleNamespace(usage=_usage())


class _CapMessages:
    def __init__(self):
        self.calls = []

    def stream(self, **kw):
        self.calls.append(kw)
        return _CapStream()


class _CapClient:
    def __init__(self):
        self.messages = _CapMessages()

    def with_options(self, **kw):
        return self


_orig_client = shared.client
shared.logger.info = lambda *a, **k: None
try:
    for model, want_temp, want_think in (
            ("claude-sonnet-5-5", None, {"type": "between_tools"}),
            ("claude-haiku-5-5", None, None),
            ("claude-sonnet-5", None, {"type": "disabled"}),
            ("claude-haiku-4-5-20251001", 0, None)):
        shared.client = _CapClient()
        shared._call_claude(model, "sys", "BROOKVALE", max_tokens=100)
        kw = shared.client.messages.calls[0]
        _check(f"5 _call_claude({model}) sends temperature={want_temp!r}, thinking={want_think!r}",
               kw.get("temperature") == want_temp and kw.get("thinking") == want_think
               and (("temperature" in kw) == (want_temp is not None))
               and (("thinking" in kw) == (want_think is not None)),
               detail=str({k: kw.get(k) for k in ("temperature", "thinking")}))
finally:
    shared.client = _orig_client
    shared.logger.info = _orig_info

# ════════════════════════════════════════════════════════════════════════════
# 6. schema default change: init_db() again is a no-op, stored models untouched,
#    a row inserted without a model gets the new default on a fresh DB.
# ════════════════════════════════════════════════════════════════════════════
_models_before = db.query("SELECT id, model FROM users ORDER BY id")
try:
    db.init_db()
    _ok = True
except Exception as e:  # noqa: BLE001
    _ok = repr(e)
_check("6 init_db() a second time does not raise", _ok is True, detail=str(_ok))
_check("6 second init_db() leaves every stored model as it was (no silent migration)",
       db.query("SELECT id, model FROM users ORDER BY id") == _models_before)
_nid = db.execute("INSERT INTO users (email, password_hash, org_name) VALUES (?,?,?)",
                  ("default@padimas.test", "x", "PADIMAS Provisions"))
_check("6 fresh-DB default model is claude-sonnet-5-5",
       db.query("SELECT model FROM users WHERE id=?", (_nid,))[0]["model"] == "claude-sonnet-5-5")

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll model-update break tests passed.")
