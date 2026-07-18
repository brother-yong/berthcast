"""Builds the chat assistant's data context from the latest analysis.
Extracted verbatim from app.py."""
import json

import database as db
from agents.shared import wrap_untrusted, UNTRUSTED_GUARD


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


# ── Static product guide (staff daily-use scope only — NO admin/operator
# content, per spec 2026-07-09). Trusted text: stays OUTSIDE the fence.
# When a staff-facing flow or button label changes, update this in the same
# commit — stale instructions are worse than none.
PRODUCT_GUIDE = """PRODUCT GUIDE (how the berthcast app is used — answer how-to questions from these facts, never guess):

What berthcast is: AI inventory forecasting for distributors. Staff upload stock and sales exports; berthcast says what to reorder, how much, and what's at risk. It recommends — it never places orders.

Files to upload: inventory (stock on hand) and sales history are required. A supplier list and purchase-order history are optional but improve supplier detection and lead-time awareness. Excel (.xlsx) or CSV, exported straight from the company's system — no reformatting needed; berthcast detects the right columns itself.

Running an analysis: click "New Analysis" in the top navigation → upload files → fill the short context form (anything unusual this period) → review possible duplicate items → run. Takes a minute or two; a live progress screen shows findings as they appear.

Results page: recommendations grouped by supplier, most urgent first. Red left edge = critical, amber = low. Each row shows item, quantity, order-by date (red if overdue). Click a row to expand full detail — the reasoning, confidence, and what happens if you don't act. The ✓ button approves, ✕ dismisses. In the expanded panel you can edit quantity or supplier before approving. The dashed note field on each row saves automatically.

Logging outcomes (important): after approving, the row asks "Did you place this order?" — later, mark whether the stockout was avoided or happened. This is what builds supplier reliability scores and the ROI numbers; if nobody logs outcomes, those stay empty.

Getting the order sheet out: the "Print / PDF" button prints approved items (choose "Save as PDF" in the print dialog for a PDF file). The "CSV" button downloads a spreadsheet for Excel.

Dashboard: past analyses, most recent first. Open any old run, or compare two runs to see what changed between them.

Suppliers page: every detected supplier with a 0–100 reliability score. Everyone starts at 50; scores move as outcomes are logged. The search box filters the table.

Chat (you): you see the latest completed analysis only — not older runs. The "Analysis context" toggle gives you more detail (low-stock lists, dead SKUs, supplier profiles). You cannot place orders or change data.

Settings: edit supplier profiles — lead time in days and delay likelihood. Filling these in sharpens reorder timing, especially for slow import suppliers."""


def build_chat_system_prompt(org_name: str, chat_ctx: dict, features=None) -> str:
    """Assemble the chat system prompt: persona + rules + untrusted-data guard,
    then the trusted PRODUCT_GUIDE, then the spreadsheet-derived context blocks
    fenced with wrap_untrusted (July 2026 review: chat was the last unfenced
    prompt site), then feature add-ons. Pure function — testable offline."""
    features = features or []

    parts = [(
        "You are berthcast, an AI inventory advisor for {org}. "
        "You have access to this company's real inventory data, analysis results, "
        "and supplier information. Use it to give specific, actionable answers. "
        "Cite actual item names, quantities, and supplier names from the data. "
        "Be direct and practical. If the data doesn't cover what they're asking, "
        "say so and suggest what data they'd need.\n\n"
        "RULES:\n"
        "- Lead with the answer. Do not narrate your process, think out loud, or open "
        "with preamble like \"Let me look\" or \"Sure\" — give the direct answer first.\n"
        "- Always reference the real data below — never make up item names or numbers.\n"
        "- When asked what to order, prioritise by: days of supply (lowest first), "
        "then confidence level, then supplier risk.\n"
        "- When discussing suppliers, mention their delay rate and lead time if known.\n"
        "- Keep answers concise. Use bullet points only when listing multiple items.\n\n"
    ).format(org=org_name) + UNTRUSTED_GUARD]

    parts.append(PRODUCT_GUIDE)

    if chat_ctx.get("summary_text"):
        parts.append(wrap_untrusted(chat_ctx["summary_text"]))
    if chat_ctx.get("detailed_text"):
        parts.append(wrap_untrusted(chat_ctx["detailed_text"]))
    if not chat_ctx.get("has_data"):
        parts.append(
            "This user has not run an analysis yet. Help them understand "
            "how berthcast works and guide them through uploading their data."
        )

    addons = []
    if "show_reasoning" in features:
        addons.append(
            "Before your answer, wrap your step-by-step reasoning in <thinking>...</thinking> tags. "
            "Write it in first-person exploratory prose — think out loud, consider the problem, then give your answer."
        )
    if "detailed" in features:
        addons.append("Provide a thorough, detailed response with examples where relevant.")
    if addons:
        parts.append(" ".join(addons))

    return "\n\n".join(parts)
