"""Builds the chat assistant's data context from the latest analysis.
Extracted verbatim from app.py."""
import json

import database as db


def _build_chat_context(user_id: int, org_name: str, detailed: bool = False) -> dict:
    """Load the user's latest analysis data for the chat system prompt.

    Returns a dict with:
      summary_text:  always-included context block (stats, critical items, pending recs)
      detailed_text: extra detail when the 'Analysis context' toggle is on
      starters:      list of 4 data-aware starter question strings
      has_data:      bool — whether the user has any completed analysis
    """
    result = {"summary_text": "", "detailed_text": "", "starters": [], "has_data": False}

    # Latest completed session (org-scoped — all users in the org see the same data)
    sessions = db.query(
        "SELECT id, created_at FROM upload_sessions WHERE org_name=? AND status='complete' "
        "ORDER BY created_at DESC LIMIT 1",
        (org_name,)
    )
    if not sessions:
        result["starters"] = [
            "What does berthcast do?",
            "How do I run my first analysis?",
            "What files do I need to upload?",
            "What kind of recommendations will I get?",
        ]
        return result

    sid = sessions[0]["id"]
    analysis_date = str(sessions[0]["created_at"])[:10]
    result["has_data"] = True

    ar = db.query(
        "SELECT inventory_report, recommendations_json FROM analysis_results WHERE session_id=?",
        (sid,)
    )
    if not ar:
        return result

    try:
        inventory = json.loads(ar[0]["inventory_report"] or "[]")
        recs = json.loads(ar[0]["recommendations_json"] or "[]")
        if isinstance(inventory, dict):
            inventory = []
    except Exception:
        inventory, recs = [], []

    # ── Summary stats ────────────────────────────────────────────────────
    total      = len(inventory)
    critical   = [i for i in inventory if isinstance(i, dict) and i.get("status") == "CRITICAL"]
    low        = [i for i in inventory if isinstance(i, dict) and i.get("status") == "LOW"]
    dead       = [i for i in inventory if isinstance(i, dict) and i.get("status") == "DEAD"]
    healthy    = [i for i in inventory if isinstance(i, dict) and i.get("status") == "HEALTHY"]
    valid_recs = [r for r in recs if isinstance(r, dict) and not r.get("error")]
    pending    = [r for r in valid_recs if not r.get("approved") and not r.get("dismissed")]
    approved   = [r for r in valid_recs if r.get("approved")]
    high_risk  = [r for r in valid_recs if r.get("supplier_risk") == "HIGH"]

    lines = [
        f"=== {org_name} — LIVE INVENTORY DATA (analysis date: {analysis_date}) ===",
        f"Total items tracked: {total}",
        f"CRITICAL: {len(critical)} | LOW: {len(low)} | HEALTHY: {len(healthy)} | DEAD: {len(dead)}",
        f"Recommendations: {len(valid_recs)} total, {len(pending)} pending review, {len(approved)} approved",
    ]
    if high_risk:
        lines.append(f"High-risk supplier items: {len(high_risk)}")

    # ── Critical items (always included) ─────────────────────────────────
    if critical:
        lines.append("")
        lines.append("CRITICAL ITEMS (need immediate attention):")
        for item in critical[:30]:
            dos = item.get("days_of_supply", "?")
            obs = item.get("observation", "")
            lines.append(f"  • {item.get('item', '?')} — {dos} days of supply. {obs}")

    # ── Pending recommendations (always included) ────────────────────────
    if pending:
        lines.append("")
        lines.append(f"PENDING RECOMMENDATIONS ({len(pending)} awaiting review):")
        for rec in pending[:20]:
            supplier = rec.get("supplier", "Unknown")
            qty = rec.get("suggested_quantity", "?")
            conf = rec.get("confidence", "?")
            reason = rec.get("reason", "")
            lines.append(f"  • {rec.get('item', '?')} — order {qty} from {supplier} (confidence: {conf}). {reason}")

    result["summary_text"] = "\n".join(lines)

    # ── Detailed text (only when toggle is on) ───────────────────────────
    if detailed:
        detail_lines = []
        if low:
            detail_lines.append(f"\nLOW STOCK ITEMS ({len(low)}):")
            for item in low[:40]:
                dos = item.get("days_of_supply", "?")
                detail_lines.append(f"  • {item.get('item', '?')} — {dos} days of supply. {item.get('observation', '')}")
        if dead:
            detail_lines.append(f"\nDEAD SKUs ({len(dead)}):")
            for item in dead[:20]:
                detail_lines.append(f"  • {item.get('item', '?')} — {item.get('observation', '')}")
        if approved:
            detail_lines.append(f"\nAPPROVED ORDERS ({len(approved)}):")
            for rec in approved[:20]:
                detail_lines.append(f"  • {rec.get('item', '?')} — qty {rec.get('suggested_quantity', '?')} from {rec.get('supplier', '?')}")

        # Supplier profiles
        profiles = db.get_supplier_profiles(org_name)
        if profiles:
            detail_lines.append(f"\nSUPPLIER PROFILES ({len(profiles)}):")
            for p in profiles:
                delay = int((p.get("delay_probability") or 0) * 100)
                lt = p.get("avg_lead_time_days", "?")
                detail_lines.append(f"  • {p['supplier_name']} — lead time {lt}d, delay rate {delay}%, notes: {p.get('notes', '')}")

        result["detailed_text"] = "\n".join(detail_lines)

    # ── Starter questions based on actual data ───────────────────────────
    starters = []
    if critical:
        starters.append(f"I have {len(critical)} critical items. What should I order first?")
    if pending:
        starters.append(f"Walk me through the {len(pending)} pending recommendations.")
    if high_risk:
        names = list({r.get("supplier", "?") for r in high_risk})[:3]
        starters.append(f"What's the risk with {', '.join(names)}?")
    if dead:
        starters.append(f"Should I discontinue any of these {len(dead)} dead SKUs?")
    if len(starters) < 4 and low:
        starters.append(f"Which of my {len(low)} low-stock items need action soonest?")
    if len(starters) < 4:
        starters.append("Give me a quick summary of my inventory health.")
    result["starters"] = starters[:4]

    return result
