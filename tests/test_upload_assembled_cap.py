"""Chunked uploads: the ASSEMBLED file must respect the size cap server-side.

Flask's MAX_CONTENT_LENGTH bounds each chunk REQUEST; the client-side 100MB
check is advisory JavaScript. Without a post-assembly check, 4000 chunks of
up to ~100MB each could assemble a file far past the advertised ceiling.

Dependency-free:  python tests/test_upload_assembled_cap.py
"""
import io
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp_db = os.path.join(tempfile.gettempdir(), "berth_asmcap.db")
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

import app as appmod  # noqa: E402

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


appmod.app.config["TESTING"] = True
appmod.app.config["WTF_CSRF_ENABLED"] = False
client = appmod.app.test_client()
with client.session_transaction() as sess:
    sess["user_id"] = 1
    sess["org_name"] = "Test Org"
    sess["model"] = "claude-sonnet-4-6"
    sess["is_admin"] = False
    sess["tier"] = "enterprise"
    sess["role"] = "admin"

# Two 3000-byte chunks against a temporarily lowered 5000-byte cap: each chunk
# REQUEST stays under the per-request limit (multipart envelope included),
# but the assembled 6000 bytes must be rejected server-side.
_orig_cap = appmod.app.config.get("MAX_CONTENT_LENGTH")
appmod.app.config["MAX_CONTENT_LENGTH"] = 5000

def _send_chunk(idx):
    return client.post("/upload", data={
        "slot": "sales",
        "chunk_index": str(idx),
        "total_chunks": "2",
        "upload_id": "asmcaptest01",
        "filename": "big.csv",
        "chunk": (io.BytesIO(b"x" * 3000), "blob"),
    }, content_type="multipart/form-data")

r1 = _send_chunk(0).get_json()
_check("first chunk accepted", r1.get("ok") is True, r1)
r2 = _send_chunk(1).get_json()
_check("assembled file over cap rejected", r2.get("ok") is False, r2)
_check("rejection names the size problem", "too large" in (r2.get("error") or "").lower(), r2)

appmod.app.config["MAX_CONTENT_LENGTH"] = _orig_cap

if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll assembled-cap tests passed.")
