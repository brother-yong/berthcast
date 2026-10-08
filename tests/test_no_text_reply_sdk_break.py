"""_call_claude's "no text came back" path against the REAL anthropic SDK.

tests/test_call_claude_retry.py fakes the SDK's RuntimeError by hand. The 8 Oct
2026 update depends on the real SDK raising exactly RuntimeError from
get_final_text() when a reply has no text block (a refusal, or Haiku 5.5's
default thinking used the whole cap). If a future SDK pin raises something else,
every such reply turns back into a run-killing exception, and only a test on the
real SDK notices.

No network: the real SDK client is built on an httpx MockTransport that replays
canned server-sent events and records the request body. Also checks what the
real SDK actually puts on the wire for each model (thinking type, no temperature).

Run: python tests/test_no_text_reply_sdk_break.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="berth_notext_sdk_break_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

try:
    import anthropic  # the real SDK, pinned in requirements.txt
    import httpx
    if not hasattr(anthropic, "Anthropic") or not hasattr(httpx, "MockTransport"):
        raise ImportError("stubbed")
except ImportError:
    print("ok: real anthropic SDK not installed here - skipped")
    sys.exit(0)

import agents.shared as shared  # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


def _ev(name, data):
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _start(model):
    return _ev("message_start", {"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 120, "output_tokens": 1}}})


def _end(stop, out_tokens):
    return (_ev("message_delta", {"type": "message_delta",
                                  "delta": {"stop_reason": stop, "stop_sequence": None},
                                  "usage": {"output_tokens": out_tokens}})
            + _ev("message_stop", {"type": "message_stop"}))


def _refusal(model):
    return _start(model) + _end("refusal", 1)


def _thinking_only(model):
    return (_start(model)
            + _ev("content_block_start", {"type": "content_block_start", "index": 0,
                                          "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            + _ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "thinking_delta",
                                                    "thinking": "BROOKVALE oat milk cover vs lead time..."}})
            + _ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "signature_delta", "signature": "sig"}})
            + _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            + _end("max_tokens", 4000))


def _text(model, text):
    return (_start(model)
            + _ev("content_block_start", {"type": "content_block_start", "index": 0,
                                          "content_block": {"type": "text", "text": ""}})
            + _ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": text}})
            + _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            + _end("end_turn", 12))


def _client(sse_for_model, seen):
    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse_for_model(body["model"]).encode())
    return anthropic.Anthropic(api_key="dummy-key-not-used", max_retries=0,
                               http_client=httpx.Client(transport=httpx.MockTransport(handler)))


_warned, _logged = [], []
_orig = (shared.client, shared.logger.warning, shared.logger.info, shared._RETRY_SLEEP)
shared.logger.warning = lambda msg, *a: _warned.append(msg % a)
shared.logger.info = lambda msg, *a: _logged.append(msg % a)
shared._RETRY_SLEEP = lambda s: None
try:
    # 1. refusal, no content block at all
    seen = []
    shared.client = _client(_refusal, seen)
    try:
        out = shared._call_claude("claude-sonnet-5-5", "sys", "BROOKVALE")
        _check("real SDK refusal with no content -> '' (not an exception)", out == "", detail=repr(out))
    except Exception as e:  # noqa: BLE001
        _check("real SDK refusal with no content -> '' (not an exception)", False, detail=repr(e))
    _check("refusal is not retried (one request)", len(seen) == 1, detail=str(len(seen)))
    _check("sonnet-5-5 on the wire: thinking between_tools, no temperature",
           seen and seen[0].get("thinking") == {"type": "between_tools"} and "temperature" not in seen[0],
           detail=str({k: seen[0].get(k) for k in ("thinking", "temperature")} if seen else None))

    # 2. Haiku 5.5: thinking block only, cap hit
    seen, _warned[:], _logged[:] = [], [], []
    shared.client = _client(_thinking_only, seen)
    try:
        out = shared._call_claude("claude-haiku-5-5", "sys", "NORDVIK", max_tokens=4000)
        _check("real SDK thinking-only reply -> ''", out == "", detail=repr(out))
    except Exception as e:  # noqa: BLE001
        _check("real SDK thinking-only reply -> ''", False, detail=repr(e))
    _check("thinking-only reply is logged as no text", any("no text" in w for w in _warned), detail=str(_warned))
    _check("thinking-only reply is still billed in the usage log (4000 output tokens)",
           any("out 4000" in m for m in _logged), detail=str(_logged))
    _check("haiku-5-5 on the wire: no thinking key, no temperature",
           seen and "thinking" not in seen[0] and "temperature" not in seen[0],
           detail=str({k: seen[0].get(k) for k in ("thinking", "temperature")} if seen else None))

    # 3. a normal text reply is untouched
    seen = []
    shared.client = _client(lambda m: _text(m, '[{"item": "PADIMAS CHILLI 1L"}]'), seen)
    out = shared._call_claude("claude-sonnet-5", "sys", "PADIMAS")
    _check("real SDK text reply comes back verbatim", out == '[{"item": "PADIMAS CHILLI 1L"}]', detail=repr(out))
    _check("sonnet-5 on the wire: thinking disabled, no temperature",
           seen and seen[0].get("thinking") == {"type": "disabled"} and "temperature" not in seen[0],
           detail=str({k: seen[0].get(k) for k in ("thinking", "temperature")} if seen else None))
finally:
    shared.client, shared.logger.warning, shared.logger.info, shared._RETRY_SLEEP = _orig

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll real-SDK no-text tests passed.")
