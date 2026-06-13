"""Proof for the upload-hardening + client-IP fixes.

Three things are covered:

  1. validators.validate_chunk_meta — bounds the chunk counters so a forged
     total_chunks can't make the upload route iterate billions of times (a
     single request used to be able to pin a worker thread for minutes and
     freeze the whole site). Non-numeric / negative / out-of-range is rejected.

  2. validators.sanitize_upload_id — strips a client-supplied upload id down to
     a filesystem-safe token before it is concatenated into a temp filename, so
     no slashes/dots reach a path. (The leading "tmp_" prefix already blocked a
     real escape, but input flowing into a path must be sanitised regardless.)

  3. app._resolve_client_ip — picks the IP to throttle on from proxy headers.
     The old code trusted the LEFTMOST X-Forwarded-For entry, which the client
     sets, so spoofing it walked past every IP throttle. Now CF-Connecting-IP
     wins, else the RIGHTMOST (proxy-appended) XFF entry.

End-to-end, it drives the real /upload route with a logged-in analyst and proves
the chunk path now rejects forged metadata, a bad upload id, and a non-spreadsheet,
while a normal chunk still lands inside the uploads folder.

Run:  python tests/test_upload_safety.py
"""
import os
import sys
import io
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Throwaway DB + uploads dir, not on Render, BEFORE importing config/app (both
# read these at import time and run guards).
_TMP = tempfile.mkdtemp(prefix="berth_uploadsafety_")
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["UPLOAD_FOLDER"] = os.path.join(_TMP, "uploads")
os.environ.pop("RENDER", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-not-used")

# Stub anthropic — constructed at import, never called here.
if "anthropic" not in sys.modules:
    _stub = types.ModuleType("anthropic")
    class _AnthropicStub:  # noqa: N801
        def __init__(self, *a, **k):
            pass
    _stub.Anthropic = _AnthropicStub
    _stub.AnthropicError = Exception
    sys.modules["anthropic"] = _stub

import validators                                       # noqa: E402
import database as db                                   # noqa: E402
import app as appmod                                    # noqa: E402
from werkzeug.security import generate_password_hash    # noqa: E402

appmod.app.config["WTF_CSRF_ENABLED"] = False
appmod.app.config["TESTING"] = True

UPLOAD_FOLDER = os.environ["UPLOAD_FOLDER"]
_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    print(("ok: " if cond else "FAIL: ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        _FAILED = True


# ── 1. validate_chunk_meta (the DoS bound) ───────────────────────────────────
_check("good counters parse to ints",
       validators.validate_chunk_meta("0", "3") == ((0, 3), None))
_check("forged huge total_chunks is rejected",
       validators.validate_chunk_meta("0", "999999999")[0] is None)
_check("total_chunks at the ceiling is allowed",
       validators.validate_chunk_meta("0", str(validators.MAX_UPLOAD_CHUNKS))[0] is not None)
_check("total_chunks just over the ceiling is rejected",
       validators.validate_chunk_meta("0", str(validators.MAX_UPLOAD_CHUNKS + 1))[0] is None)
_check("non-numeric chunk_index is rejected (no unhandled int() crash)",
       validators.validate_chunk_meta("abc", "3")[0] is None)
_check("None counters are rejected",
       validators.validate_chunk_meta(None, None)[0] is None)
_check("chunk_index >= total_chunks is rejected",
       validators.validate_chunk_meta("3", "3")[0] is None)
_check("negative chunk_index is rejected",
       validators.validate_chunk_meta("-1", "3")[0] is None)
_check("zero total_chunks is rejected",
       validators.validate_chunk_meta("0", "0")[0] is None)


# ── 2. sanitize_upload_id (path-safety) ──────────────────────────────────────
_check("plain id is kept", validators.sanitize_upload_id("abc123") == "abc123")
_check("hyphen/underscore kept", validators.sanitize_upload_id("a-b_c") == "a-b_c")
_check("slashes and dots are stripped",
       validators.sanitize_upload_id("x/../../etc/passwd") == "xetcpasswd")
_check("a pure-traversal id collapses to empty",
       validators.sanitize_upload_id("../..") == "")
_check("backslashes stripped (windows-style)",
       validators.sanitize_upload_id("..\\..\\evil") == "evil")
_check("result is length-capped at 64",
       len(validators.sanitize_upload_id("a" * 500)) == 64)
_check("None/empty is empty string",
       validators.sanitize_upload_id(None) == "" and validators.sanitize_upload_id("") == "")


# ── 3. _resolve_client_ip (throttle-spoof fix) ───────────────────────────────
_check("CF-Connecting-IP wins when present",
       appmod._resolve_client_ip("9.9.9.9", "1.1.1.1, 2.2.2.2", "10.0.0.1") == "9.9.9.9")
_check("rightmost XFF (proxy-appended) is used, not the spoofable leftmost",
       appmod._resolve_client_ip("", "1.2.3.4, 10.0.0.5", "10.0.0.5") == "10.0.0.5")
_check("single XFF entry is returned as-is",
       appmod._resolve_client_ip("", "9.9.9.9", "10.0.0.1") == "9.9.9.9")
_check("falls back to remote_addr with no proxy headers",
       appmod._resolve_client_ip("", "", "10.0.0.1") == "10.0.0.1")
_check("never returns empty",
       appmod._resolve_client_ip("", "", None) == "unknown")


# ── 4. End-to-end through the real /upload route ─────────────────────────────
db.execute("INSERT INTO users (email, password_hash, org_name, model, role) VALUES (?,?,?,?,?)",
           ("analyst@example.com", generate_password_hash("x"), "a regional food distributor",
            "claude-sonnet-4-6", "admin"))
uid = db.query("SELECT id FROM users WHERE email=?", ("analyst@example.com",))[0]["id"]

client = appmod.app.test_client()
with client.session_transaction() as s:
    s["user_id"]  = uid
    s["email"]    = "analyst@example.com"
    s["org_name"] = "a regional food distributor"
    s["model"]    = "claude-sonnet-4-6"
    s["is_admin"] = False
    s["tier"]     = "enterprise"
    s["role"]     = "admin"


def _post_chunk(upload_id, total_chunks, chunk_index="0", filename="data.csv",
                slot="inventory", body=b"col\n1\n"):
    return client.post("/upload", content_type="multipart/form-data", data={
        "slot": slot,
        "chunk_index": str(chunk_index),
        "total_chunks": str(total_chunks),
        "upload_id": upload_id,
        "filename": filename,
        "chunk": (io.BytesIO(body), "chunk.bin"),
    })


# Forged total_chunks is refused at the route (the DoS that motivated this).
r = _post_chunk("abc123", 999999999)
_check("route rejects forged total_chunks", r.get_json().get("error") == "Bad chunk metadata.",
       detail=str(r.get_json()))

# A pure-traversal upload id (sanitises to empty) is refused.
r = _post_chunk("../..", 2)
_check("route rejects an unusable upload id", r.get_json().get("error") == "Invalid upload id.",
       detail=str(r.get_json()))

# Non-spreadsheet on the chunk path is refused (the missing extension check).
r = _post_chunk("okid", 2, filename="evil.exe")
_check("route rejects a non-spreadsheet on the chunk path",
       "xlsx" in (r.get_json().get("error") or ""), detail=str(r.get_json()))

# A normal chunk is accepted and lands INSIDE the uploads folder under a
# sanitised name; a traversal-flavoured id can't escape it.
r = _post_chunk("a/../../evil", 2, chunk_index="0", filename="data.csv")
j = r.get_json()
_check("normal chunk accepted (waiting for more)", j.get("ok") is True and j.get("chunk_received") == 0,
       detail=str(j))
_check("chunk written inside uploads under the sanitised id",
       os.path.exists(os.path.join(UPLOAD_FOLDER, "tmp_aevil_0")))
_check("nothing escaped to the uploads folder's parent",
       "evil_0" not in os.listdir(os.path.dirname(UPLOAD_FOLDER)))


if _FAILED:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll upload-safety tests passed.")
