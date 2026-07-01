"""Sonnet 5 request-parameter guards.

Sonnet 5 (unlike Sonnet 4.6) rejects `temperature` with a hard 400, and turns
"adaptive" thinking on by default when the thinking parameter is omitted. This
app has always run every model with no thinking, so both quirks are handled the
same way the existing temperature quirk already was: per-model kwargs helpers in
agents/shared.py. This test pins that behaviour down so a future model-list edit
can't silently re-break Sonnet 5 (temperature 400) or start spending thinking
tokens that truncate the small JSON calls.

Dependency-free: run with `python tests/test_sonnet5_model_params.py`.
Exits non-zero on the first failed assertion.
"""
import os
import sys
import tempfile

# database.py reads DB_PATH at import; shared.py builds the Anthropic client at
# import. Set both BEFORE importing so the module loads without a real key.
_TMPDIR = tempfile.mkdtemp(prefix="berth_sonnet5_test_")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.shared import sampling_kwargs, thinking_kwargs

_FAILED = False


def _check(name, cond, detail=""):
    global _FAILED
    if cond:
        print(f"ok: {name}")
    else:
        print(f"FAIL: {name}  {detail}")
        _FAILED = True


# ── temperature: Sonnet 5 must send NONE (else 400); older Sonnet keeps 0 ──────
_check("sonnet-5 omits temperature",
       sampling_kwargs("claude-sonnet-5") == {},
       detail=str(sampling_kwargs("claude-sonnet-5")))
_check("sonnet-4-6 still sends temperature=0",
       sampling_kwargs("claude-sonnet-4-6") == {"temperature": 0},
       detail=str(sampling_kwargs("claude-sonnet-4-6")))
_check("opus-4-8 omits temperature (unchanged)",
       sampling_kwargs("claude-opus-4-8") == {})
_check("haiku-4-5 still sends temperature=0 (unchanged)",
       sampling_kwargs("claude-haiku-4-5-20251001") == {"temperature": 0})

# ── thinking: Sonnet 5 must be pinned off; every other model omits it ─────────
_check("sonnet-5 pins thinking disabled",
       thinking_kwargs("claude-sonnet-5") == {"thinking": {"type": "disabled"}},
       detail=str(thinking_kwargs("claude-sonnet-5")))
_check("sonnet-4-6 leaves thinking untouched",
       thinking_kwargs("claude-sonnet-4-6") == {})
_check("opus-4-8 leaves thinking untouched",
       thinking_kwargs("claude-opus-4-8") == {})
_check("haiku-4-5 leaves thinking untouched",
       thinking_kwargs("claude-haiku-4-5-20251001") == {})

# ── the model shipped in the dropdown is the exact API id (no date suffix) ────
from config import AVAILABLE_MODELS

_ids = [mid for mid, _label in AVAILABLE_MODELS]
_check("sonnet-5 is offered in the model list", "claude-sonnet-5" in _ids,
       detail=str(_ids))
_check("stale sonnet-4-6 no longer offered", "claude-sonnet-4-6" not in _ids,
       detail=str(_ids))

sys.exit(1 if _FAILED else 0)
