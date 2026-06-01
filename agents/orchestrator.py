"""The "big boss" — runs the agents in order and reports progress.

This is the single place in charge of the inventory -> recommendation sequence.
It was previously inlined inside app.py's analysis route; pulling it here keeps
the agent flow in the agents package and leaves only web/DB/email glue in app.py.

It talks to the outside world through two optional callbacks so it stays free of
any Flask/database/email concerns:
  emit(msg)                      -> append a human-readable progress line
  mark(name, status, summary)    -> update one agent's lifecycle card

Returns either {"error": <message>} or
{"inventory_report": [...], "recommendations": [...]}.
"""

from rec_logic import _normalise_confidence
from .inventory import run_inventory_agent
from .recommendation import run_recommendation_agent


def _summarise_inventory(report):
    """One-line summary for the inventory agent's collapsed card."""
    if not isinstance(report, list):
        return "Inventory classified"
    total    = len(report)
    critical = sum(1 for r in report if isinstance(r, dict) and r.get("status") == "CRITICAL")
    low      = sum(1 for r in report if isinstance(r, dict) and r.get("status") == "LOW")
    dead     = sum(1 for r in report if isinstance(r, dict) and r.get("status") == "DEAD")
    parts = [f"{total} items reviewed"]
    if critical: parts.append(f"{critical} critical")
    if low:      parts.append(f"{low} low")
    if dead:     parts.append(f"{dead} dead")
    return " · ".join(parts)


def _summarise_recommendations(recs):
    """One-line summary for the recommendation agent's collapsed card."""
    if not isinstance(recs, list):
        return "Recommendations generated"
    valid    = [r for r in recs if isinstance(r, dict) and not r.get("error")]
    total    = len(valid)
    flagged  = sum(1 for r in valid if r.get("supplier_risk") == "HIGH" or r.get("flags"))
    if total == 0:
        return "No reorder recommendations needed"
    s = f"{total} reorder recommendation" + ("s" if total != 1 else "")
    if flagged:
        s += f" · {flagged} flagged"
    return s


def run_pipeline(session_id, model, confirmed_groups, context, *, emit=None, mark=None):
    """Run the inventory health agent, then the recommendation agent.

    emit/mark are optional callbacks (see module docstring). Behaviour mirrors the
    original inline sequence exactly: same order, same progress markers, same
    confidence normalisation, same shape of saved output.
    """
    if emit is None:
        emit = lambda *a, **k: None
    if mark is None:
        mark = lambda *a, **k: None

    # ── Agent 2: Inventory health ────────────────────────────────────────────
    mark("inventory", "running")
    emit("Starting inventory health agent")
    inv_result = run_inventory_agent(session_id, model, confirmed_groups, context, progress_emit=emit)
    if "error" in inv_result:
        mark("inventory", "error", summary="Failed — see error below")
        return {"error": inv_result["error"]}

    inventory_report = inv_result["report"]
    mark("inventory", "done", summary=_summarise_inventory(inventory_report))

    # ── Agent 3: Purchase recommendations ────────────────────────────────────
    mark("recommendation", "running")
    emit("Starting purchase recommendation agent")
    recommendations = run_recommendation_agent(session_id, model, inventory_report, context, progress_emit=emit)

    # Defensive: normalise confidence values before persisting so the UI doesn't
    # have to guess what "MED" or "high" means later.
    for _rec in recommendations:
        _normalise_confidence(_rec)

    mark("recommendation", "done", summary=_summarise_recommendations(recommendations))

    return {"inventory_report": inventory_report, "recommendations": recommendations}
