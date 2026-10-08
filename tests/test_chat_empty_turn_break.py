"""A blank assistant turn must never reach the model in a chat history.

The 8 Oct 2026 update stopped /api/chat from SAVING an empty reply, because "an
empty assistant turn makes every later call in this conversation 400". Two ways
that promise still breaks:
  1. A whitespace-only reply ("\\n\\n") is truthy, so it is saved. The API rejects
     whitespace-only text the same way it rejects empty text.
  2. The history builder sends whatever is in chat_messages. A conversation that
     already holds an empty assistant row (saved before this fix shipped) keeps
     sending it, so that conversation stays broken for good.

Invented brands only. Run: python tests/test_chat_empty_turn_break.py
"""
import json
import os
import sys
import tempfile
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_chat_turn_break_")
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
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=10, output_tokens=4096,
                                                     cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0))


class _FakeAnthropic:
    chunks = []
    calls = []

    def __init__(self, **kw):
        pass

    @property
    def messages(self):
        class _M:
            def stream(self, **kw):
                _FakeAnthropic.calls.append(kw)
                return _FakeStream(_FakeAnthropic.chunks)

            def create(self, **kw):
                return SimpleNamespace(model=kw.get("model"), usage=None,
                                       content=[SimpleNamespace(type="text", text="PADIMAS chat")])
        return _M()


appmod._anthropic.Anthropic = _FakeAnthropic

ORG = "PADIMAS Provisions"
uid = db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
                 ("buyer@padimas.test", generate_password_hash("x"), ORG, "claude-haiku-5-5", "enterprise"))
client = appmod.app.test_client()
with client.session_transaction() as s:
    s.update(user_id=uid, email="buyer@padimas.test", org_name=ORG, model="claude-haiku-5-5",
             is_admin=False, tier="enterprise", role="admin")


def _chat(message, conversation_id=None, chunks=()):
    rate_limit._hits.clear()
    _FakeAnthropic.chunks = list(chunks)
    _FakeAnthropic.calls = []
    payload = {"message": message}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    r = client.post("/api/chat", json=payload)
    body = r.get_data(as_text=True)
    r.close()
    ev = [json.loads(x[6:]) for x in body.splitlines() if x.startswith("data: ")]
    return r, ev


def _assistant_rows(conv):
    return [x["content"] for x in db.query(
        "SELECT content FROM chat_messages WHERE conversation_id=? AND role='assistant' ORDER BY id", (conv,))]


# ── 1. Whitespace-only reply (Haiku 5.5 thinking ate the cap after a newline) ─
r, ev = _chat("PADIMAS chilli sauce cover?", chunks=["\n", "\n"])
conv = next((e["conversation_id"] for e in ev if "conversation_id" in e), None)
_check("setup: stream answered", r.status_code == 200 and conv is not None, detail=f"{r.status_code} {ev}")
_check("whitespace-only reply is not saved as an assistant turn",
       conv is not None and _assistant_rows(conv) == [], detail=repr(_assistant_rows(conv) if conv else None))

r, ev = _chat("and the NORDVIK rye?", conversation_id=conv, chunks=["About 8 bags."])
sent = _FakeAnthropic.calls[0]["messages"] if _FakeAnthropic.calls else []
_check("follow-up after a whitespace-only reply sends no blank turn",
       sent and all(str(m.get("content", "")).strip() for m in sent), detail=repr(sent))

# ── 2. A conversation already holding an empty assistant row (pre-fix data) ──
OLD = db.execute("INSERT INTO chat_conversations (user_id, title, org_name) VALUES (?,?,?)",
                 (uid, "BROOKVALE oat milk", ORG))
db.execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
           (OLD, "user", "how much BROOKVALE oat milk?"))
db.execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
           (OLD, "assistant", ""))
r, ev = _chat("please answer", conversation_id=OLD, chunks=["Order 30 cases."])
sent = _FakeAnthropic.calls[0]["messages"] if _FakeAnthropic.calls else []
_check("setup: old conversation reached the model", bool(sent), detail=f"{r.status_code} {ev}")
_check("a stored empty assistant row is not replayed to the model",
       sent and all(str(m.get("content", "")).strip() for m in sent), detail=repr(sent))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll chat empty-turn break tests passed.")
