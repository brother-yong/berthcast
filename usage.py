"""Operator visibility: name the *real* outcome of each analysis run, and decide
which failures are worth alerting the operator about.

Why this exists
---------------
A run marked ``complete`` in the database is NOT the same as a run that produced
something useful. The pipeline can finish with:
  - zero items                 -> a blank report (the silent failure)
  - a truncated reply          -> some items quietly missing
  - every recommendation errored -> looks like "nothing to order" but isn't
All three look identical to ``status='complete'``. This module looks at what the
run actually produced and gives the outcome a plain name, so the admin page can
show it and the run path can alert on the bad ones.

Everything here is pure: no Flask, no database, no network. The admin page and
the analysis run path both call the same functions, and it is all unit-testable
with plain dicts (see tests/test_usage_insights.py).
"""
import json


# ── Outcome categories ───────────────────────────────────────────────────────
# Plain strings (not an enum) so they can be stored, compared, and rendered with
# no extra dependency.
HEALTHY     = "healthy"      # items reviewed; recommendations produced, or none needed
BLANK       = "blank"        # complete but ZERO items — the silent failure
INCOMPLETE  = "incomplete"   # complete but the AI reply was cut short; items missing
RECS_FAILED = "recs_failed"  # items exist but every recommendation errored
REFUSED     = "refused"      # safety net declined the file — a coaching nudge, not a fire
FAILED      = "failed"       # crashed, or the worker died mid-run
RUNNING     = "running"      # still analysing
PENDING     = "pending"      # uploaded, not yet analysed

# The note text the pipeline appends when a batch reply is truncated (see
# agents/orchestrator.py — "...reply was cut short..."). Matched here so the page
# can flag an incomplete run. If that wording changes, change this with it.
_TRUNCATED_NOTE_HINT = "cut short"

# Outcomes worth emailing the operator about. A REFUSED run is deliberately
# excluded: that is the product correctly declining unreadable data, a coaching
# moment, not an outage. INCOMPLETE / RECS_FAILED are shown on the page but not
# paged on — they are partial results, not nothing.
ALERTABLE = frozenset({BLANK, FAILED})


def _loads(blob, default):
    """json.loads that never raises and never returns None where a container is
    expected. The stored columns can be NULL, '', or malformed."""
    if not blob:
        return default
    try:
        v = json.loads(blob)
        return default if v is None else v
    except Exception:
        return default


def classify_complete_run(inventory, recommendations, data_notes):
    """Name the real outcome of a run the database calls ``complete``.

    inventory        list of item dicts, or a dict wrapping {"report": [...]}
    recommendations  list of rec dicts; a rec with a truthy ``error`` key failed
    data_notes       list of plain-English caveat strings produced by the run

    Returns {"category", "items", "recs", "error_recs"}.
    """
    if isinstance(inventory, dict):
        inventory = inventory.get("report", [])
    if not isinstance(inventory, list):
        inventory = []
    if not isinstance(recommendations, list):
        recommendations = []
    if not isinstance(data_notes, list):
        data_notes = []

    items      = sum(1 for i in inventory if isinstance(i, dict))
    good_recs  = sum(1 for r in recommendations if isinstance(r, dict) and not r.get("error"))
    error_recs = sum(1 for r in recommendations if isinstance(r, dict) and r.get("error"))
    truncated  = any(_TRUNCATED_NOTE_HINT in str(n).lower() for n in data_notes)

    if items == 0:
        category = BLANK              # produced nothing — worst case, ranked first
    elif truncated:
        category = INCOMPLETE         # has items but is missing some
    elif good_recs == 0 and error_recs > 0:
        category = RECS_FAILED        # items fine, but every recommendation broke
    else:
        category = HEALTHY            # items + recs, OR items + legitimately no recs needed

    return {"category": category, "items": items,
            "recs": good_recs, "error_recs": error_recs}


def classify_session(status, inventory_json, recommendations_json, data_notes_json):
    """Classify one session straight from its stored DB values.

    ``status`` is upload_sessions.status; the ``*_json`` args are the raw TEXT
    columns from analysis_results (any may be None). A ``failed`` row can't be
    split into crash-vs-refused after the fact (that distinction lives only in
    memory at failure time), so historically it is reported as FAILED.
    """
    status = (status or "").lower()
    if status == "complete":
        return classify_complete_run(
            _loads(inventory_json, []),
            _loads(recommendations_json, []),
            _loads(data_notes_json, []),
        )
    if status == "failed":
        return {"category": FAILED, "items": 0, "recs": 0, "error_recs": 0}
    if status == "analyzing":
        return {"category": RUNNING, "items": 0, "recs": 0, "error_recs": 0}
    return {"category": PENDING, "items": 0, "recs": 0, "error_recs": 0}


def should_alert(category):
    """True if this outcome is a real fire the operator should be emailed about."""
    return category in ALERTABLE


def org_run_summary(classifications):
    """Aggregate per-run classification dicts into per-org counts. Order-independent."""
    counts = {}
    for c in classifications:
        cat = c.get("category", PENDING)
        counts[cat] = counts.get(cat, 0) + 1
    return {
        "total":       len(classifications),
        "healthy":     counts.get(HEALTHY, 0),
        "blank":       counts.get(BLANK, 0),
        "incomplete":  counts.get(INCOMPLETE, 0),
        "recs_failed": counts.get(RECS_FAILED, 0),
        "failed":      counts.get(FAILED, 0),
        "running":     counts.get(RUNNING, 0),
    }


# ── Display helpers (used by the admin template) ─────────────────────────────
_LABELS = {
    HEALTHY:     "Healthy",
    BLANK:       "Blank — 0 items",
    INCOMPLETE:  "Incomplete",
    RECS_FAILED: "No recs (all errored)",
    REFUSED:     "Refused bad data",
    FAILED:      "Failed",
    RUNNING:     "Running",
    PENDING:     "Not run yet",
}
# Maps to a CSS tone class on the page: good / warn / bad / muted.
_TONES = {
    HEALTHY: "good",
    BLANK: "bad", FAILED: "bad", RECS_FAILED: "bad",
    INCOMPLETE: "warn", REFUSED: "warn", RUNNING: "warn",
    PENDING: "muted",
}


def label(category):
    return _LABELS.get(category, category)


def tone(category):
    return _TONES.get(category, "muted")
