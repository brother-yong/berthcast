"""Auth decorators and the session-ownership guard. Extracted verbatim from app.py."""
from functools import wraps
from flask import (
    session, request, redirect, url_for, flash, jsonify, abort, make_response
)

import database as db
from config import ALLOWED_EXTENSIONS


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
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
