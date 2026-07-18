"""Auth decorators and the session-ownership guard. Extracted verbatim from app.py."""
from functools import wraps
from datetime import datetime
from flask import (
    session, request, redirect, url_for, flash, jsonify, abort, make_response
)

import database as db
from config import ALLOWED_EXTENSIONS


def trial_expired() -> bool:
    """True when the logged-in account is on a trial whose end date has passed.

    `trial_ends_at` is stamped into the session at login. NULL/blank = a permanent
    account (never expires). Date-only comparison, so the whole end day stays
    active — "ends on 24/07" means usable through the 24th."""
    ends = session.get("trial_ends_at")
    if not ends:
        return False
    try:
        end_date = datetime.fromisoformat(str(ends)).date()
    except (TypeError, ValueError):
        return False  # unparseable → treat as permanent, never lock someone out by accident
    return datetime.utcnow().date() > end_date


def trial_active_required(f):
    """Block the money/value actions once a trial has ended (soft lock). The
    account can still log in and read past results — only new analyses, uploads,
    chat and exports are gated. JSON endpoints get a JSON 403; pages flash + redirect."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if trial_expired():
            msg = "Your trial has ended. Contact berthcast to keep running analyses."
            wants_json = (
                request.is_json
                or request.path.startswith(("/api/", "/recommend/", "/dedup/", "/upload"))
                or "application/json" in (request.headers.get("Accept", "") or "")
            )
            if wants_json:
                return jsonify({"ok": False, "error": msg}), 403
            flash(msg, "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        # Revalidate the cookie against the live account each request. A signed
        # cookie is valid for its full 30-day life, so without this a removed,
        # demoted, or password-reset user would keep access until it expired.
        # One cheap indexed primary-key read; a missing row (deleted user) or a
        # version bump both invalidate the session. Cookies minted before this
        # column existed carry no "sv" and default to 0, matching the DB default.
        rows = db.query("SELECT session_version FROM users WHERE id=?", (session["user_id"],))
        if not rows or (rows[0]["session_version"] or 0) != session.get("sv", 0):
            session.clear()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def analyst_required(f):
    """Only admin and reviewer roles can run analyses and approve/dismiss.
    Viewers get a flash message and redirect."""
    @wraps(f)
    def decorated(*args, **kwargs):
        role = session.get("role", "admin")
        if role == "viewer":
            # JSON endpoints get a JSON error
            if request.is_json or request.path.startswith(("/api/", "/recommend/")):
                return jsonify({"ok": False, "error": "You have view-only access."}), 403
            flash("You have view-only access. Ask your admin to run a new analysis.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def _allowed(filename: str) -> bool:
    """True if filename has an accepted extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _verify_session_owner(session_id):
    """Guard for any session-scoped route. Aborts the request if the current
    user's org does not own the session. All users in the same org can see
    the same upload sessions — we check org_name, not user_id.

    Returns nothing on success. On failure raises a Flask abort:
      - 404 if session_id is missing or not numeric
      - 404 if no session with that id exists
      - 403 if the session belongs to a different org

    For JSON endpoints (anything that returned a body the frontend tried to
    `.json()` parse), we return JSON instead of Flask's default HTML error
    page so the browser console shows a clear message rather than a vague
    "Network error".
    """
    from flask import abort, jsonify, make_response

    wants_json = (
        request.is_json
        or request.path.startswith(("/api/", "/upload/", "/recommend/", "/analysis_status"))
        or "application/json" in (request.headers.get("Accept", "") or "")
    )

    def _fail(code: int, msg: str):
        # JSON endpoints need a JSON body so the frontend's `.json()` call
        # succeeds and surfaces a real message instead of "Network error".
        if wants_json:
            abort(make_response(jsonify({"ok": False, "error": msg}), code))
        abort(code)

    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        _fail(404, "Session not found.")

    rows = db.query("SELECT org_name FROM upload_sessions WHERE id=?", (sid,))
    if not rows:
        _fail(404, "Session not found.")
    if rows[0]["org_name"] != session.get("org_name"):
        _fail(403, "You don't have access to this session.")
