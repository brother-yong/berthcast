"""A blank or cut-off chat reply must be visible to the user, not silent.

Two gaps on /api/chat, found in review on 8 Oct 2026:
  1. A blank reply saved nothing (correct) but the stream still ended with plain
     "done", so the chat page showed an empty bubble with no explanation.
  2. The stream never checked stop_reason. A reply cut off by the 4096-token cap
     (Haiku 5.5 thinks inside that cap) was saved and shown as a complete answer.

Invented brands only. Run: python tests/test_chat_cutoff_break.py
"""
import json
import os
import sys
import tempfile
import types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="berth_chat_cutoff_break_")
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
    def __init__(self, chunks, stop_reason):
        self._chunks = list(chunks)
        self._stop = stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        return iter(self._chunks)

    def get_final_message(self):
        return SimpleNamespace(stop_reason=self._stop,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=4096,
                                                     cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0))


class _FakeAnthropic:
    chunks = []
    stop_reason = "end_turn"

    def __init__(self, **kw):
        pass

    @property
    def messages(self):
        class _M:
            def stream(self, **kw):
                return _FakeStream(_FakeAnthropic.chunks, _FakeAnthropic.stop_reason)

            def create(self, **kw):
                return SimpleNamespace(model=kw.get("model"), usage=None,
                                       content=[SimpleNamespace(type="text", text="NORDVIK chat")])
        return _M()


appmod._anthropic.Anthropic = _FakeAnthropic

ORG = "BROOKVALE Provisions"
uid = db.execute("INSERT INTO users (email, password_hash, org_name, model, tier) VALUES (?,?,?,?,?)",
                 ("buyer@brookvale.test", generate_password_hash("x"), ORG, "claude-haiku-5-5", "enterprise"))
client = appmod.app.test_client()
with client.session_transaction() as s:
    s.update(user_id=uid, email="buyer@brookvale.test", org_name=ORG, model="claude-haiku-5-5",
             is_admin=False, tier="enterprise", role="admin")


def _chat(message, chunks=(), stop_reason="end_turn"):
    rate_limit._hits.clear()
    _FakeAnthropic.chunks = list(chunks)
    _FakeAnthropic.stop_reason = stop_reason
    r = client.post("/api/chat", json={"message": message})
    body = r.get_data(as_text=True)
    r.close()
    ev = [json.loads(x[6:]) for x in body.splitlines() if x.startswith("data: ")]
    conv = next((e["conversation_id"] for e in ev if "conversation_id" in e), None)
    return ev, conv


def _assistant_rows(conv):
    return [x["content"] for x in db.query(
        "SELECT content FROM chat_messages WHERE conversation_id=? AND role='assistant' ORDER BY id", (conv,))]


def _streamed(ev):
    return "".join(e.get("text", "") for e in ev)


# ── 1. Blank reply: tell the user, don't end on a bare 'done' ─────────────────
# The "show reasoning" toggle asks for <thinking> tags in the text; the page hides
# them, so a reply that is only reasoning is blank to the user too.
for label, chunks, stop in (("empty reply", [], "end_turn"),
                            ("whitespace-only reply", ["\n", "\n"], "end_turn"),
                            ("thinking used the whole cap", [], "max_tokens"),
                            ("reasoning-only reply", ["<thinking>checking PADIMAS stock</thinking>\n"], "end_turn"),
                            ("cut off inside the reasoning", ["<thinking>checking PADIMAS st"], "max_tokens")):
    ev, conv = _chat("PADIMAS chilli sauce cover?", chunks, stop)
    _check(f"setup ({label}): stream opened a conversation", conv is not None, detail=str(ev))
    _check(f"{label} ends with an error event the chat page can show",
           any(e.get("error") for e in ev), detail=str(ev))
    _check(f"{label} does not also send 'done'", not any(e.get("done") for e in ev), detail=str(ev))
    _check(f"{label} still saves no assistant turn", conv is not None and _assistant_rows(conv) == [],
           detail=repr(_assistant_rows(conv) if conv else None))
    _check(f"{label} still titles the new conversation (later turns never retry it)",
           any(e.get("title_updated") for e in ev), detail=str(ev))

# ── 2. Reply cut off at max_tokens: tell the user, don't save it as complete ──
CUT = "Order 30 cases of BROOKVALE oat milk because cover is"
ev, conv = _chat("How much BROOKVALE oat milk?", [CUT], "max_tokens")
rows = _assistant_rows(conv) if conv else []
_check("cut-off reply is still saved (the partial answer is useful)", len(rows) == 1, detail=repr(rows))
_check("saved cut-off reply is marked as cut off, not saved as complete",
       len(rows) == 1 and rows[0].startswith(CUT) and rows[0] != CUT and "cut off" in rows[0].lower(),
       detail=repr(rows))
_check("the user is told the reply was cut off, in the stream",
       "cut off" in _streamed(ev).lower(), detail=repr(_streamed(ev)))
_check("cut-off reply still finishes with 'done' (the answer renders)",
       any(e.get("done") for e in ev) and not any(e.get("error") for e in ev), detail=str(ev))

# ── 2b. Cut off inside a reasoning block that came AFTER visible text ─────────
# chat.html files everything after an unclosed <thinking> into the collapsed
# reasoning box, so the note must close the block first or it is hidden there.
def _visible(raw):
    """Same split as chat.html's live stream: first block only."""
    o = raw.find("<thinking>")
    if o == -1:
        return raw
    c = raw.find("</thinking>", o)
    return raw[:o] + raw[c + 11:] if c != -1 else raw[:o]


ev, conv = _chat("How much PADIMAS sauce?", ["Order 30 cases.\n<thinking>I should check PADIMAS"], "max_tokens")
rows = _assistant_rows(conv) if conv else []
_check("2b cut-off note is visible, not hidden in the reasoning box",
       "cut off" in _visible(_streamed(ev)).lower(), detail=repr(_visible(_streamed(ev))))
_check("2b saved reply has no unclosed reasoning block",
       len(rows) == 1 and rows[0].count("<thinking>") == rows[0].count("</thinking>"), detail=repr(rows))

# ── 3. Control: a normal reply is saved exactly, no note ─────────────────────
ev, conv = _chat("And NORDVIK rye?", ["About 8 bags."], "end_turn")
_check("control: normal reply saved exactly as streamed",
       conv is not None and _assistant_rows(conv) == ["About 8 bags."], detail=repr(_assistant_rows(conv)))
_check("control: normal reply ends with 'done', no error, no cut-off note",
       any(e.get("done") for e in ev) and not any(e.get("error") for e in ev)
       and "cut off" not in _streamed(ev).lower(), detail=str(ev))

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll chat cut-off break tests passed.")
