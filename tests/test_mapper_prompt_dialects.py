"""The mapper system prompt teaches the new header dialects + grid quirks."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from agents.ingest_mapper import _SYSTEM  # noqa: E402

F = []


def _check(c, m):
    if not c:
        F.append(m)


low = _SYSTEM.lower()
for token in ("m1", "jan-26", "2026-01", "merged", "first row"):
    _check(token in low, f"prompt should mention {token!r}")
# still layout-only, still the wide_matrix contract
_check("wide_matrix" in _SYSTEM, "wide_matrix contract must remain")
_check("json" in low, "must still ask for JSON")

if F:
    print("FAILED:")
    for m in F:
        print("  -", m)
    sys.exit(1)
print("All mapper-prompt dialect tests passed.")
