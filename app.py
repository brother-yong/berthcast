import os
import json
import secrets
import threading
import time
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, Response, stream_with_context
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
import anthropic as _anthropic

import database as db
from agents import (
    run_normalization_agent,
    run_inventory_agent,
    run_recommendation_agent,
)

app = Flask(__name__)

# SECRET_KEY must be set in production (Render env vars). In local dev,
# a random key is generated per run — sessions won't survive restarts,
# which is fine for testing.
_secret = os.environ.get("SECRET_KEY")
if not _secret and os.environ.get("RENDER"):
    raise RuntimeError("SECRET_KEY environment variable is required in production. Set it in Render → Environment.")
app.secret_key = _secret or secrets.token_hex(32)

# CSRF protection — all POST/PUT/DELETE requests must include a valid token.
# HTML forms get it via {{ csrf_token() }}. AJAX calls read it from the
# <meta name="csrf-token"> tag in base.html and send it as X-CSRFToken header.
csrf = CSRFProtect(app)

app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB

UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"xlsx", "csv"}

FILE_SLOTS = {
    "inventory":       "inventory",
    "purchase_orders": "purchase_orders",
    "sales":           "sales",
    "suppliers":       "suppliers",
    "customers":       "customers",
}

AVAILABLE_MODELS = [
    ("claude-haiku-4-5-20251001", "Haiku — fast, lower cost (testing)"),
    ("claude-sonnet-4-6",         "Sonnet — balanced (recommended)"),
    ("claude-opus-4-6",           "Opus — most thorough (production reports)"),
]

# In-memory progress store for analysis runs. Keyed by upload_session_id.
# Render uses a single gunicorn worker with threads → shared across requests.
# Schema: { session_id: { started_at, log: [{t, msg}], status, error } }
analysis_progress = {}
analysis_progress_lock = threading.Lock()

# Cache for normalization results produced by the streaming dedup page.
# Keyed by upload_session_id. dedup_review GET pops from here so the agent
# doesn't run twice. Single-worker Render instance — safe to share in-process.
normalization_cache = {}

db.init_db()


def _ensure_admin():
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@berthai.com")
    admin_pass  = os.environ.get("ADMIN_PASSWORD")
    if not admin_pass:
        # In production, refuse to create an admin with a guessable password.
        if os.environ.get("RENDER"):
            print("WARNING: ADMIN_PASSWORD not set. Skipping default admin creation.")
            return
        admin_pass = "changeme123"  # Local dev only
    existing = db.query("SELECT id FROM users WHERE is_admin=1")
    if not existing:
        db.execute(
            "INSERT INTO users (email, password_hash, org_name, model, is_admin) VALUES (?,?,?,?,?)",
            (admin_email, generate_password_hash(admin_pass), "BerthAI Admin", "claude-sonnet-4-6", 1)
        )

_ensure_admin()


@app.context_processor
def inject_live_stats():
    """Provide live ticker stats to every template. Fails silently if anything's wrong."""
    if "user_id" not in session:
        return {"live_stats": None}
    try:
        sessions = db.query(
            "SELECT id, created_at FROM upload_sessions WHERE user_id=? AND status='complete' "
            "ORDER BY created_at DESC LIMIT 1",
            (session["user_id"],)
        )
        if not sessions:
            return {"live_stats": None}
        s = sessions[0]
        ar = db.query(
            "SELECT inventory_report, recommendations_json FROM analysis_results WHERE session_id=?",
            (s["id"],)
        )
        if not ar:
            return {"live_stats": None}

        inv  = json.loads(ar[0]["inventory_report"] or "[]")
        recs = json.loads(ar[0]["recommendations_json"] or "[]")
        if isinstance(inv, dict):
            inv = []

        from datetime import datetime
        try:
            ts = datetime.fromisoformat(str(s["created_at"]).replace("Z", "").split(".")[0])
            mins = max(0, int((datetime.utcnow() - ts).total_seconds() / 60))
            if   mins < 1:        age = "just now"
            elif mins < 60:       age = f"{mins}m ago"
            elif mins < 60 * 24:  age = f"{mins // 60}h ago"
            else:                 age = f"{mins // (60 * 24)}d ago"
        except Exception:
            age = str(s["created_at"])[:10]

        return {"live_stats": {
            "skus":     len(inv),
            "critical": sum(1 for i in inv if isinstance(i, dict) and i.get("status") == "CRITICAL"),
            "recs":     sum(1 for r in recs if isinstance(r, dict) and not r.get("error")),
            "approved": sum(1 for r in recs if isinstance(r, dict) and r.get("approved")),
            "last_age": age,
        }}
    except Exception:
        return {"live_stats": None}


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


def _allowed(filename: str) -> bool:
    """True if filename has an accepted extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _verify_session_owner(session_id):
    """Guard for any session-scoped route. Aborts the request if the current
    user is not the owner. Used at the top of every upload/analysis route.

    Returns nothing on success. On failure raises a Flask abort:
      - 404 if session_id is missing or not numeric
      - 404 if no session with that id exists
      - 403 if the session belongs to a different user

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

    rows = db.query("SELECT user_id FROM upload_sessions WHERE id=?", (sid,))
    if not rows:
        _fail(404, "Session not found.")
    if rows[0]["user_id"] != session.get("user_id"):
        _fail(403, "You don't have access to this session.")


@app.route("/")
def landing():
    if "user_id" in session:
        return redirect(url_for("chat"))
    return render_template("landing.html")


@app.route("/guide")
@login_required
def guide():
    return render_template("guide.html")


@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("chat"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        users = db.query("SELECT * FROM users WHERE email=?", (email,))
        if users and check_password_hash(users[0]["password_hash"], password):
            u = users[0]
            if not u["email_verified"]:
                flash("Please verify your email before signing in. Check your inbox for the verification link.", "error")
                return render_template("login.html")
            session["user_id"]  = u["id"]
            session["email"]    = u["email"]
            session["org_name"] = u["org_name"]
            session["model"]    = u["model"]
            session["is_admin"] = bool(u["is_admin"])
            session["tier"]     = u["tier"]
            return redirect(url_for("admin_panel") if u["is_admin"] else url_for("chat"))
        flash("Incorrect email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ── Password reset ────────────────────────────────────────────────────────────

def _send_critical_alert(user_id: int, upload_session_id: int, new_critical: list,
                          base_url: str = "") -> None:
    """Email the user when items newly enter CRITICAL status versus the previous run."""
    users = db.query("SELECT email FROM users WHERE id=?", (user_id,))
    if not users:
        return
    to_email = users[0]["email"]

    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    count   = len(new_critical)
    subject = f"⚠ {count} item{'s' if count != 1 else ''} hit critical stock — BerthAI"

    results_path = f"{base_url}/results/{upload_session_id}"

    rows_text = "\n".join(
        f"  • {i.get('item','')}  ({i.get('days_of_supply','?')} days of supply remaining)"
        for i in new_critical
    )
    rows_html = "".join(
        f"""<tr>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:500;">{i.get('item','')}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#c0392b;">{i.get('days_of_supply','—')} days</td>
              <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:13px;">{i.get('observation','')}</td>
            </tr>"""
        for i in new_critical
    )

    text = (
        f"BerthAI stock alert\n\n"
        f"{count} item{'s' if count != 1 else ''} moved to CRITICAL stock level since your last analysis:\n\n"
        f"{rows_text}\n\n"
        f"View the full report: {results_path}\n\n"
        f"— BerthAI"
    )
    html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:560px;margin:0 auto;color:#1a2a3a;">
      <div style="background:#fef2f2;border-left:4px solid #c0392b;
                  padding:16px 20px;border-radius:6px;margin-bottom:24px;">
        <div style="font-size:14px;font-weight:700;color:#c0392b;text-transform:uppercase;
                    letter-spacing:0.05em;margin-bottom:4px;">Stock alert</div>
        <div style="font-size:17px;font-weight:600;color:#1a2a3a;">
          {count} item{'s' if count != 1 else ''} moved to critical since your last run
        </div>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:24px;">
        <thead>
          <tr style="background:#f9fafb;">
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       letter-spacing:0.06em;color:#6b7280;border-bottom:2px solid #e5e7eb;">Item</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       letter-spacing:0.06em;color:#6b7280;border-bottom:2px solid #e5e7eb;">Stock runway</th>
            <th style="padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;
                       letter-spacing:0.06em;color:#6b7280;border-bottom:2px solid #e5e7eb;">Note</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
      <a href="{results_path}"
         style="display:inline-block;padding:11px 24px;background:#c8924c;color:#fff;
                text-decoration:none;border-radius:8px;font-weight:600;font-size:14px;">
        View full report →
      </a>
      <p style="font-size:12px;color:#9ca3af;margin-top:24px;">— BerthAI</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to_email
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, to_email, msg.as_string())
    except Exception:
        pass


def _send_reset_email(to_email: str, reset_url: str) -> None:
    """Send a password reset link via Gmail SMTP. Fails silently if not configured."""
    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Reset your BerthAI password"
    msg["From"]    = sender
    msg["To"]      = to_email

    text = (
        f"Hi,\n\n"
        f"Someone requested a password reset for your BerthAI account.\n\n"
        f"Click the link below to set a new password. It expires in 1 hour.\n\n"
        f"{reset_url}\n\n"
        f"If you didn't request this, you can ignore this email — your password won't change.\n\n"
        f"— BerthAI"
    )
    html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:480px;margin:0 auto;color:#1a2a3a;">
      <p style="font-size:15px;line-height:1.6;">
        Someone requested a password reset for your BerthAI account.
      </p>
      <a href="{reset_url}"
         style="display:inline-block;margin:20px 0;padding:12px 28px;
                background:#c8924c;color:#fff;text-decoration:none;
                border-radius:8px;font-weight:600;font-size:14px;">
        Reset password
      </a>
      <p style="font-size:13px;color:#6b7280;line-height:1.5;">
        This link expires in 1 hour. If you didn't request a reset, ignore this email.
      </p>
      <p style="font-size:13px;color:#6b7280;">— BerthAI</p>
    </div>
    """
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, to_email, msg.as_string())
    except Exception:
        pass


def _send_analysis_ready_email(user_id: int, upload_session_id: int,
                                summary: dict, base_url: str = "") -> None:
    """Email the user when their analysis has finished. summary is a small dict
    with: total_items, critical, low, rec_count, flagged."""
    users = db.query("SELECT email FROM users WHERE id=?", (user_id,))
    if not users:
        return
    to_email = users[0]["email"]

    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    results_path = f"{base_url}/results/{upload_session_id}"
    total    = summary.get("total_items", 0)
    critical = summary.get("critical", 0)
    low      = summary.get("low", 0)
    recs     = summary.get("rec_count", 0)
    flagged  = summary.get("flagged", 0)

    subject = "Your BerthAI analysis is ready"

    text = (
        f"Your BerthAI analysis is ready.\n\n"
        f"{total} items reviewed.\n"
        f"{critical} flagged as critical, {low} low.\n"
        f"{recs} reorder recommendations ({flagged} flagged for attention).\n\n"
        f"Open it here: {results_path}\n\n"
        f"— BerthAI"
    )
    html = f"""
    <div style="font-family:'Inter','Segoe UI',sans-serif;max-width:560px;margin:0 auto;color:#0F1B2D;">
      <div style="font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;
                  color:#8B6B3D;margin-bottom:8px;">Analysis ready</div>
      <div style="font-family:'Inter Tight','Inter',sans-serif;font-size:24px;font-weight:600;
                  letter-spacing:-0.01em;color:#0B1424;margin-bottom:16px;">
        Your BerthAI analysis is ready
      </div>
      <p style="font-size:14.5px;line-height:1.6;color:#0F1B2D;margin:0 0 14px;">
        {total} items reviewed · <strong style="color:#8B2C2C;">{critical} critical</strong> · {low} low ·
        {recs} reorder recommendation{'s' if recs != 1 else ''}{' · ' + str(flagged) + ' flagged' if flagged else ''}.
      </p>
      <a href="{results_path}"
         style="display:inline-block;margin:18px 0;padding:12px 28px;
                background:#0F1B2D;color:#fff;text-decoration:none;
                border-radius:10px;font-weight:600;font-size:14px;">
        Open the analysis →
      </a>
      <p style="font-size:13px;color:#6B7280;line-height:1.5;margin-top:24px;">
        Tip: edit any recommendation before approving — quantity, supplier, and notes all save automatically.
      </p>
      <p style="font-size:12px;color:#9ca3af;margin-top:18px;">— BerthAI</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to_email
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, to_email, msg.as_string())
    except Exception:
        pass


def _send_verification_email(to_email: str, verify_url: str) -> None:
    """Send an email verification link via Gmail SMTP. Fails silently if not configured."""
    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not sender or not password:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Verify your BerthAI account"
    msg["From"]    = sender
    msg["To"]      = to_email

    text = (
        f"Hi,\n\n"
        f"Thanks for signing up for BerthAI.\n\n"
        f"Click the link below to verify your email and activate your account:\n\n"
        f"{verify_url}\n\n"
        f"This link expires in 24 hours.\n\n"
        f"— BerthAI"
    )
    html = f"""
    <div style="font-family:'Segoe UI',sans-serif;max-width:480px;margin:0 auto;color:#1a2a3a;">
      <p style="font-size:15px;line-height:1.6;margin-bottom:8px;">
        Thanks for signing up for BerthAI.
      </p>
      <p style="font-size:15px;line-height:1.6;margin-top:0;">
        Click below to verify your email and activate your account.
      </p>
      <a href="{verify_url}"
         style="display:inline-block;margin:20px 0;padding:12px 28px;
                background:#c8924c;color:#fff;text-decoration:none;
                border-radius:8px;font-weight:600;font-size:14px;">
        Verify my email
      </a>
      <p style="font-size:13px;color:#6b7280;line-height:1.5;">
        This link expires in 24 hours. If you didn't sign up, you can ignore this email.
      </p>
      <p style="font-size:13px;color:#6b7280;">— BerthAI</p>
    </div>
    """
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, to_email, msg.as_string())
    except Exception:
        pass


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("chat"))
    if request.method == "POST":
        org_name     = request.form.get("org_name", "").strip()
        email        = request.form.get("email", "").strip().lower()
        password     = request.form.get("password", "")
        password2    = request.form.get("password2", "")
        # Browsers only send checkbox values when checked. Anything other than
        # the exact "on" / "1" / "true" we consider unchecked — don't trust
        # the front-end to enforce the agreement.
        accept_terms = request.form.get("accept_terms", "").strip().lower() in ("on", "1", "true", "yes")

        # Validation
        error = None
        if not org_name:
            error = "Organisation name is required."
        elif not email or "@" not in email:
            error = "A valid email address is required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != password2:
            error = "Passwords don't match."
        elif not accept_terms:
            error = "Please tick the box to agree to the Terms of Service and Privacy Policy."
        else:
            existing = db.query("SELECT id FROM users WHERE email=?", (email,))
            if existing:
                error = "An account with that email already exists."

        if error:
            return render_template("register.html", error=error,
                                   org_name=org_name, email=email,
                                   accept_terms=accept_terms)

        # If we can't actually send mail (MAIL_SENDER / MAIL_APP_PASSWORD
        # unset on Render), auto-verify the account. Without this, the user
        # gets stuck: they sign up, no email arrives, and login refuses them
        # forever with "Please verify your email".
        mail_configured = bool(os.environ.get("MAIL_SENDER")) and bool(os.environ.get("MAIL_APP_PASSWORD"))
        verified_on_create = 0 if mail_configured else 1

        db.execute(
            """INSERT INTO users
               (email, password_hash, org_name, model, tier, email_verified,
                analyses_used, chat_messages_used)
               VALUES (?,?,?,?,?,?,?,?)""",
            (email, generate_password_hash(password), org_name,
             "claude-haiku-4-5-20251001", "free", verified_on_create, 0, 0)
        )
        new_user = db.query("SELECT id FROM users WHERE email=?", (email,))[0]

        if mail_configured:
            # Issue verification token and email it
            db.execute(
                "DELETE FROM email_verification_tokens WHERE user_id=?",
                (new_user["id"],)
            )
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO email_verification_tokens (user_id, token) VALUES (?,?)",
                (new_user["id"], token)
            )
            verify_url = url_for("verify_email", token=token, _external=True)
            threading.Thread(
                target=_send_verification_email,
                args=(email, verify_url),
                daemon=True,
            ).start()
            return render_template("register.html", submitted=True, email=email)

        # Mail not configured — log them in immediately so they don't get stuck.
        session["user_id"]  = new_user["id"]
        session["email"]    = email
        session["org_name"] = org_name
        session["model"]    = "claude-haiku-4-5-20251001"
        session["is_admin"] = False
        session["tier"]     = "free"
        flash("Account created. Welcome to BerthAI.", "success")
        return redirect(url_for("chat"))

    return render_template("register.html")


@app.route("/verify-email/<token>")
def verify_email(token):
    rows = db.query(
        """SELECT evt.user_id
           FROM email_verification_tokens evt
           WHERE evt.token=?
             AND evt.created_at >= datetime('now', '-24 hours')""",
        (token,)
    )
    if not rows:
        return render_template("verify_email.html", expired=True)

    user_id = rows[0]["user_id"]
    db.execute("UPDATE users SET email_verified=1 WHERE id=?", (user_id,))
    db.execute("DELETE FROM email_verification_tokens WHERE user_id=?", (user_id,))
    return render_template("verify_email.html", expired=False)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if "user_id" in session:
        return redirect(url_for("chat"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if email:
            users = db.query("SELECT id FROM users WHERE email=?", (email,))
            if users:
                # Delete any existing tokens for this user first
                db.execute(
                    "DELETE FROM password_reset_tokens WHERE user_id=?",
                    (users[0]["id"],)
                )
                token = secrets.token_urlsafe(32)
                db.execute(
                    "INSERT INTO password_reset_tokens (user_id, token) VALUES (?,?)",
                    (users[0]["id"], token)
                )
                reset_url = url_for("reset_password", token=token, _external=True)
                threading.Thread(
                    target=_send_reset_email,
                    args=(email, reset_url),
                    daemon=True,
                ).start()
        # Always show the same message — don't reveal whether email exists
        return render_template("forgot_password.html", submitted=True)
    return render_template("forgot_password.html", submitted=False)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if "user_id" in session:
        return redirect(url_for("chat"))

    # Validate token — must exist and be less than 1 hour old
    rows = db.query(
        """SELECT prt.id, prt.user_id
           FROM password_reset_tokens prt
           WHERE prt.token=?
             AND prt.created_at >= datetime('now', '-1 hour')""",
        (token,)
    )
    if not rows:
        return render_template("reset_password.html", invalid=True, token=token)

    if request.method == "POST":
        password  = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if len(password) < 8:
            return render_template("reset_password.html", invalid=False, token=token,
                                   error="Password must be at least 8 characters.")
        if password != password2:
            return render_template("reset_password.html", invalid=False, token=token,
                                   error="Passwords don't match.")

        user_id = rows[0]["user_id"]
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(password), user_id)
        )
        db.execute("DELETE FROM password_reset_tokens WHERE token=?", (token,))
        flash("Password updated. Sign in with your new password.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", invalid=False, token=token)


# ── Chat ─────────────────────────────────────────────────────────────────────

def _build_chat_context(user_id: int, org_name: str, detailed: bool = False) -> dict:
    """Load the user's latest analysis data for the chat system prompt.

    Returns a dict with:
      summary_text:  always-included context block (stats, critical items, pending recs)
      detailed_text: extra detail when the 'Analysis context' toggle is on
      starters:      list of 4 data-aware starter question strings
      has_data:      bool — whether the user has any completed analysis
    """
    result = {"summary_text": "", "detailed_text": "", "starters": [], "has_data": False}

    # Latest completed session
    sessions = db.query(
        "SELECT id, created_at FROM upload_sessions WHERE user_id=? AND status='complete' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,)
    )
    if not sessions:
        result["starters"] = [
            "What does BerthAI do?",
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


@app.route("/chat")
@login_required
def chat():
    ctx = _build_chat_context(session["user_id"], session["org_name"])
    return render_template("chat.html", chat_starters=ctx["starters"], has_data=ctx["has_data"])


@app.route("/api/chat", methods=["POST"])
@login_required
def chat_api():
    data = request.get_json() or {}
    conversation_id = data.get("conversation_id")
    user_message = (data.get("message") or "").strip()
    req_features = data.get("features") or []

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    # Tier limit: free users get 20 chat messages total
    if session.get("tier") == "free":
        cu = db.query("SELECT chat_messages_used FROM users WHERE id=?", (session["user_id"],))
        if cu and cu[0]["chat_messages_used"] >= 20:
            return jsonify({"error": "Free accounts include 20 chat messages. Upgrade to continue chatting."}), 403

    # Verify or create conversation
    is_new_conv = False
    if conversation_id:
        rows = db.query(
            "SELECT id FROM chat_conversations WHERE id=? AND user_id=?",
            (conversation_id, session["user_id"])
        )
        if not rows:
            return jsonify({"error": "Conversation not found"}), 404
    else:
        is_new_conv = True
        conversation_id = db.execute(
            "INSERT INTO chat_conversations (user_id, title) VALUES (?,?)",
            (session["user_id"], "New conversation")
        )

    # Save user message first
    db.execute(
        "INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
        (conversation_id, "user", user_message)
    )

    # Increment chat_messages_used for free users
    if session.get("tier") == "free":
        db.execute(
            "UPDATE users SET chat_messages_used = chat_messages_used + 1 WHERE id=?",
            (session["user_id"],)
        )

    # Build message history for Claude
    history_rows = db.query(
        "SELECT role, content FROM chat_messages WHERE conversation_id=? ORDER BY created_at ASC",
        (conversation_id,)
    )
    messages = [{"role": r["role"], "content": r["content"]} for r in history_rows]
    model = session["model"]
    conv_id_snapshot = conversation_id
    is_new_snapshot = is_new_conv
    user_msg_snapshot = user_message
    features_snapshot = req_features

    # Build system prompt with live data
    use_detailed = "use_analysis_context" in features_snapshot
    chat_ctx = _build_chat_context(session["user_id"], session["org_name"], detailed=use_detailed)

    base_system = (
        "You are BerthAI, an AI inventory advisor for {org}. "
        "You have access to this company's real inventory data, analysis results, "
        "and supplier information. Use it to give specific, actionable answers. "
        "Cite actual item names, quantities, and supplier names from the data. "
        "Be direct and practical. If the data doesn't cover what they're asking, "
        "say so and suggest what data they'd need.\n\n"
        "RULES:\n"
        "- Always reference the real data below — never make up item names or numbers.\n"
        "- When asked what to order, prioritise by: days of supply (lowest first), "
        "then confidence level, then supplier risk.\n"
        "- When discussing suppliers, mention their delay rate and lead time if known.\n"
        "- Keep answers concise. Use bullet points only when listing multiple items."
    ).format(org=session["org_name"])

    if chat_ctx["summary_text"]:
        base_system += "\n\n" + chat_ctx["summary_text"]
    if chat_ctx["detailed_text"]:
        base_system += "\n\n" + chat_ctx["detailed_text"]
    if not chat_ctx["has_data"]:
        base_system += (
            "\n\nThis user has not run an analysis yet. Help them understand "
            "how BerthAI works and guide them through uploading their data."
        )

    def generate():
        _client = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        full_response = []
        feature_addons = []
        if "show_reasoning" in features_snapshot:
            feature_addons.append(
                "Before your answer, wrap your step-by-step reasoning in <thinking>...</thinking> tags. "
                "Write it in first-person exploratory prose — think out loud, consider the problem, then give your answer."
            )
        if "detailed" in features_snapshot:
            feature_addons.append("Provide a thorough, detailed response with examples where relevant.")
        system_prompt = base_system + ("\n\n" + " ".join(feature_addons) if feature_addons else "")
        try:
            yield f"data: {json.dumps({'conversation_id': conv_id_snapshot})}\n\n"
            with _client.messages.stream(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    full_response.append(text)
                    yield f"data: {json.dumps({'text': text})}\n\n"
            assistant_text = "".join(full_response)
            db.execute(
                "INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
                (conv_id_snapshot, "assistant", assistant_text)
            )
            # Auto-generate a smart title on first exchange
            if is_new_snapshot:
                try:
                    title_resp = _client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=30,
                        system="Generate a short 4-7 word conversation title based on the user's question. Return ONLY the title, no punctuation, no quotes.",
                        messages=[{"role": "user", "content": user_msg_snapshot}],
                    )
                    auto_title = title_resp.content[0].text.strip().strip('"').strip("'")
                    if auto_title:
                        db.execute(
                            "UPDATE chat_conversations SET title=? WHERE id=?",
                            (auto_title, conv_id_snapshot)
                        )
                        yield f"data: {json.dumps({'title_updated': auto_title})}\n\n"
                except Exception:
                    pass
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/chat/conversations")
@login_required
def chat_conversations():
    q = request.args.get("q", "").strip()
    if q:
        convs = db.query(
            "SELECT id, title, created_at, pinned FROM chat_conversations "
            "WHERE user_id=? AND title LIKE ? "
            "ORDER BY pinned DESC, created_at DESC LIMIT 50",
            (session["user_id"], f"%{q}%")
        )
    else:
        convs = db.query(
            "SELECT id, title, created_at, pinned FROM chat_conversations "
            "WHERE user_id=? ORDER BY pinned DESC, created_at DESC LIMIT 50",
            (session["user_id"],)
        )
    return jsonify([dict(c) for c in convs])


@app.route("/api/chat/conversation/<int:conv_id>")
@login_required
def chat_conversation(conv_id):
    rows = db.query("SELECT user_id FROM chat_conversations WHERE id=?", (conv_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        return jsonify({"error": "Not found"}), 404
    messages = db.query(
        "SELECT role, content, created_at FROM chat_messages "
        "WHERE conversation_id=? ORDER BY created_at ASC",
        (conv_id,)
    )
    title_row = db.query("SELECT title FROM chat_conversations WHERE id=?", (conv_id,))
    return jsonify({
        "title": title_row[0]["title"] if title_row else "",
        "messages": [dict(m) for m in messages],
    })


@app.route("/api/chat/conversation/<int:conv_id>", methods=["PATCH"])
@login_required
def patch_conversation(conv_id):
    rows = db.query("SELECT user_id FROM chat_conversations WHERE id=?", (conv_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    if "title" in data:
        new_title = str(data["title"]).strip()[:120] or "Untitled"
        db.execute("UPDATE chat_conversations SET title=? WHERE id=?", (new_title, conv_id))
    if "pinned" in data:
        db.execute("UPDATE chat_conversations SET pinned=? WHERE id=?", (1 if data["pinned"] else 0, conv_id))
    return jsonify({"ok": True})


@app.route("/api/chat/conversation/<int:conv_id>", methods=["DELETE"])
@login_required
def delete_conversation(conv_id):
    rows = db.query("SELECT user_id FROM chat_conversations WHERE id=?", (conv_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        return jsonify({"error": "Not found"}), 404
    db.execute("DELETE FROM chat_messages WHERE conversation_id=?", (conv_id,))
    db.execute("DELETE FROM chat_conversations WHERE id=?", (conv_id,))
    return jsonify({"ok": True})


def _send_contact_email(name: str, email: str, company: str, message: str) -> None:
    """Send a contact form submission to the BerthAI inbox via Gmail SMTP.
    Requires MAIL_SENDER and MAIL_APP_PASSWORD env vars. Fails silently if not set."""
    sender    = os.environ.get("MAIL_SENDER", "")
    password  = os.environ.get("MAIL_APP_PASSWORD", "")
    recipient = os.environ.get("MAIL_RECIPIENT", "tanyonghan41@gmail.com")
    if not sender or not password:
        return  # Not configured — DB record is the fallback

    subject = f"BerthAI contact: {name}" + (f" ({company})" if company else "")
    body = (
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Company: {company or '—'}\n\n"
        f"Message:\n{message}\n\n"
        f"---\nReply directly to this email to respond to {name}."
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Reply-To"] = email
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, recipient, msg.as_string())
    except Exception:
        pass  # Never surface email errors to the user


@app.route("/contact", methods=["GET", "POST"])
def contact():
    """Public contact form. Stores submission in DB and emails the BerthAI inbox."""
    if request.method == "POST":
        name    = request.form.get("name", "").strip()
        email   = request.form.get("email", "").strip().lower()
        company = request.form.get("company", "").strip()
        message = request.form.get("message", "").strip()

        if not name or not email or not message:
            flash("Please fill in your name, email, and message.", "error")
            return render_template(
                "contact.html",
                name=name, email=email, company=company, message=message
            )

        # Basic length cap, prevent spam dumps
        if len(message) > 5000 or len(name) > 200 or len(email) > 200:
            flash("One of your fields is too long. Please trim and retry.", "error")
            return render_template(
                "contact.html",
                name=name[:200], email=email[:200], company=company[:200], message=message[:5000]
            )

        db.execute(
            "INSERT INTO contact_requests (name, email, company, message) VALUES (?,?,?,?)",
            (name, email, company, message)
        )
        # Fire email in background so the page responds instantly
        threading.Thread(
            target=_send_contact_email,
            args=(name, email, company, message),
            daemon=True,
        ).start()
        return render_template("contact.html", submitted=True)

    return render_template("contact.html")


@app.route("/admin", methods=["GET", "POST"])
@login_required
@admin_required
def admin_panel():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create_user":
            email    = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            org      = request.form.get("org_name", "").strip()
            model    = request.form.get("model", "claude-sonnet-4-6")
            if not email or not password or not org:
                flash("All fields are required.", "error")
            else:
                try:
                    db.execute(
                        "INSERT INTO users (email, password_hash, org_name, model) VALUES (?,?,?,?)",
                        (email, generate_password_hash(password), org, model)
                    )
                    flash(f"Account created for {email}.", "success")
                except Exception:
                    flash("That email is already registered.", "error")
        elif action == "delete_user":
            uid = request.form.get("user_id")
            db.execute("DELETE FROM users WHERE id=? AND is_admin=0", (uid,))
            flash("Account removed.", "success")
        elif action == "verify_user":
            # Safety net: manually mark a user as email-verified so they can
            # log in even when mail isn't configured or the link expired.
            uid = request.form.get("user_id")
            db.execute("UPDATE users SET email_verified=1 WHERE id=?", (uid,))
            db.execute("DELETE FROM email_verification_tokens WHERE user_id=?", (uid,))
            flash("Account verified — user can now log in.", "success")
        elif action == "change_model":
            uid   = request.form.get("user_id")
            model = request.form.get("model")
            db.execute("UPDATE users SET model=? WHERE id=?", (model, uid))
            flash("Model updated.", "success")
        elif action == "mark_contact_read":
            cid = request.form.get("contact_id")
            db.execute("UPDATE contact_requests SET status='read' WHERE id=?", (cid,))
            flash("Contact request marked as read.", "success")
        elif action == "delete_contact":
            cid = request.form.get("contact_id")
            db.execute("DELETE FROM contact_requests WHERE id=?", (cid,))
            flash("Contact request deleted.", "success")
        elif action == "save_company_config":
            org = request.form.get("org_name", "").strip()
            if org:
                try:
                    db.upsert_company_config(
                        org,
                        stockout_cost_per_unit     = float(request.form.get("stockout_cost_per_unit", 50)),
                        holding_cost_per_unit_per_day = float(request.form.get("holding_cost_per_unit_per_day", 0.5)),
                        service_level_target       = float(request.form.get("service_level_target", 0.95)),
                        default_lead_time_days     = int(request.form.get("default_lead_time_days", 56)),
                        lead_time_variance_days    = int(request.form.get("lead_time_variance_days", 14)),
                    )
                    flash(f"Config saved for {org}.", "success")
                except Exception as e:
                    flash(f"Error saving config: {e}", "error")
        elif action == "save_supplier_profile":
            org      = request.form.get("org_name", "").strip()
            sup_name = request.form.get("supplier_name", "").strip()
            if org and sup_name:
                try:
                    db.upsert_supplier_profile(
                        org, sup_name,
                        delay_probability  = float(request.form.get("delay_probability", 0.2)),
                        avg_lead_time_days = int(request.form.get("avg_lead_time_days", 56)),
                        data_quality_score = float(request.form.get("data_quality_score", 0.5)),
                        notes              = request.form.get("notes", ""),
                    )
                    flash(f"Supplier profile saved: {sup_name} ({org}).", "success")
                except Exception as e:
                    flash(f"Error saving supplier profile: {e}", "error")

    users = db.query("SELECT id, email, org_name, model, email_verified, created_at FROM users WHERE is_admin=0 ORDER BY created_at DESC")
    contact_requests = db.query(
        "SELECT id, name, email, company, message, status, created_at "
        "FROM contact_requests ORDER BY (status='new') DESC, created_at DESC"
    )

    # Org config data for consequence engine panel
    orgs = sorted({u["org_name"] for u in users})
    org_configs = {org: db.get_company_config(org) for org in orgs}
    supplier_profiles_map = {org: db.get_supplier_profiles(org) for org in orgs}

    return render_template(
        "admin.html",
        users=users,
        models=AVAILABLE_MODELS,
        contact_requests=contact_requests,
        orgs=orgs,
        org_configs=org_configs,
        supplier_profiles=supplier_profiles_map,
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def user_settings():
    org = session.get("org_name", "")
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save_supplier":
            sup_name = request.form.get("supplier_name", "").strip()
            if sup_name:
                try:
                    db.upsert_supplier_profile(
                        org, sup_name,
                        supplier_type      = request.form.get("supplier_type", "other"),
                        avg_lead_time_days = int(request.form.get("avg_lead_time_days", 56)),
                        delay_probability  = float(request.form.get("delay_probability", 0.2)),
                        data_quality_score = 0.8,   # user-entered = high confidence
                        notes              = request.form.get("notes", ""),
                    )
                    flash(f"Supplier '{sup_name}' saved.", "success")
                except Exception as e:
                    flash(f"Could not save supplier: {e}", "error")
        elif action == "delete_supplier":
            sup_name = request.form.get("supplier_name", "").strip()
            if sup_name:
                db.execute(
                    "DELETE FROM supplier_profiles WHERE org_name=? AND supplier_name=?",
                    (org, sup_name)
                )
                flash(f"Supplier '{sup_name}' deleted.", "success")
        elif action == "change_password":
            current_pw  = request.form.get("current_password", "")
            new_pw      = request.form.get("new_password", "")
            confirm_pw  = request.form.get("confirm_password", "")
            user_row    = db.query("SELECT password_hash FROM users WHERE id=?", (session["user_id"],))
            if not user_row or not check_password_hash(user_row[0]["password_hash"], current_pw):
                flash("Current password is incorrect.", "error")
            elif len(new_pw) < 8:
                flash("New password must be at least 8 characters.", "error")
            elif new_pw != confirm_pw:
                flash("New passwords do not match.", "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(new_pw), session["user_id"])
                )
                flash("Password updated.", "success")
        elif action == "save_defaults":
            # Default lead times — used by the AI when an item has no supplier
            # profile on file. The user (not just admin) gets to set these now.
            try:
                lead_days = int(request.form.get("default_lead_time_days", 56))
                lead_var  = int(request.form.get("lead_time_variance_days", 14))
                if lead_days < 1 or lead_days > 365:
                    raise ValueError("Lead time must be between 1 and 365 days.")
                if lead_var < 0 or lead_var > 90:
                    raise ValueError("Lead time variance must be between 0 and 90 days.")
                db.upsert_company_config(
                    org,
                    default_lead_time_days  = lead_days,
                    lead_time_variance_days = lead_var,
                )
                flash("Default lead times saved.", "success")
            except (ValueError, TypeError) as e:
                flash(f"Could not save defaults: {e}", "error")
        return redirect(url_for("user_settings"))

    profiles = db.get_supplier_profiles(org)
    company_cfg = db.get_company_config(org)
    return render_template("settings.html", profiles=profiles, company_cfg=company_cfg)


@app.route("/dashboard")
@login_required
def dashboard():
    # Load all completed sessions (most recent first, cap at 50)
    all_sessions = db.query(
        "SELECT * FROM upload_sessions WHERE user_id=? AND status='complete' ORDER BY created_at DESC LIMIT 50",
        (session["user_id"],)
    )
    last_session = all_sessions[0] if all_sessions else None

    # Build per-session stats; fail silently per session
    def _session_stats(sess_id):
        try:
            ar = db.query(
                "SELECT inventory_report, recommendations_json FROM analysis_results WHERE session_id=?",
                (sess_id,)
            )
            if not ar:
                return None
            inv  = json.loads(ar[0]["inventory_report"] or "[]")
            recs = json.loads(ar[0]["recommendations_json"] or "[]")
            if isinstance(inv, dict):
                inv = []
            return {
                "tracked_skus":   len(inv),
                "critical_count": sum(1 for i in inv if i.get("status") == "CRITICAL"),
                "low_count":      sum(1 for i in inv if i.get("status") == "LOW"),
                "rec_count":      len(recs),
                "approved_count": sum(1 for r in recs if r.get("approved")),
            }
        except Exception:
            return None

    stats = _session_stats(last_session["id"]) if last_session else None

    # Build history list for all sessions (dict so template can iterate)
    past_sessions = []
    for s in all_sessions:
        st = _session_stats(s["id"])
        past_sessions.append({
            "id":         s["id"],
            "date":       (s["created_at"] or "")[:10],
            "rec_count":  st["rec_count"]      if st else 0,
            "approved":   st["approved_count"] if st else 0,
            "critical":   st["critical_count"] if st else 0,
            "skus":       st["tracked_skus"]   if st else 0,
        })

    # Free tier usage info
    user_usage = None
    if session.get("tier") == "free":
        urow = db.query(
            "SELECT analyses_used, chat_messages_used FROM users WHERE id=?",
            (session["user_id"],)
        )
        if urow:
            user_usage = {
                "analyses_used":  urow[0]["analyses_used"],
                "analyses_limit": 1,
                "chat_used":      urow[0]["chat_messages_used"],
                "chat_limit":     20,
            }

    return render_template(
        "dashboard.html",
        last_session=last_session,
        stats=stats,
        past_sessions=past_sessions,
        user_usage=user_usage,
    )


@app.route("/upload/start")
@login_required
def upload_start():
    """Entry point for new analysis. Offer choice: reuse last data, or upload fresh."""
    rows = db.query(
        "SELECT * FROM upload_sessions WHERE user_id=? AND status='complete' "
        "ORDER BY created_at DESC LIMIT 1",
        (session["user_id"],)
    )
    last_complete = rows[0] if rows else None

    # No previous data → skip the choice, go straight to fresh upload.
    if not last_complete:
        return redirect(url_for("upload"))

    file_names = {}
    if last_complete["file_names_json"]:
        try:
            file_names = json.loads(last_complete["file_names_json"])
        except Exception:
            pass

    conv_status = db.get_conversion_status(last_complete["id"])

    slot_labels = {
        "inventory":       "Inventory Report",
        "purchase_orders": "Purchase Order Record",
        "sales":           "Sales Report",
        "suppliers":       "Supplier Listing",
        "customers":       "Customer Listing",
    }
    preserved = []
    for slot, label in slot_labels.items():
        if db.table_exists(f"{FILE_SLOTS[slot]}_{last_complete['id']}"):
            rows_count = conv_status.get(slot, {}).get("rows", 0)
            preserved.append({
                "slot":  slot,
                "label": label,
                "rows":  rows_count,
                "name":  file_names.get(slot, ""),
            })

    # If somehow no tables were preserved (data wiped), fall through to fresh upload.
    if not preserved:
        return redirect(url_for("upload"))

    return render_template(
        "upload_choice.html",
        last_complete=last_complete,
        preserved=preserved,
    )


@app.route("/upload/use_previous/<int:source_id>", methods=["POST"])
@login_required
def upload_use_previous(source_id):
    """Clone tables from a previous complete session into a fresh uploading session."""
    _verify_session_owner(source_id)

    src = db.query("SELECT * FROM upload_sessions WHERE id=?", (source_id,))
    if not src or src[0]["status"] != "complete":
        flash("Cannot use that previous session.", "error")
        return redirect(url_for("upload"))

    # Discard any in-progress upload session this user already has — clean slate.
    existing = db.query(
        "SELECT id FROM upload_sessions WHERE user_id=? AND status='uploading'",
        (session["user_id"],)
    )
    for row in existing:
        _purge_uploading_session(row["id"])

    # New uploading session, inherit file_names_json so the upload page shows the right names.
    new_id = db.execute(
        "INSERT INTO upload_sessions (user_id, org_name, status, file_names_json) VALUES (?,?,?,?)",
        (session["user_id"], session["org_name"], "uploading", src[0]["file_names_json"] or "{}")
    )

    cloned = 0
    failed = []
    for slot, table in FILE_SLOTS.items():
        old_table = f"{table}_{source_id}"
        new_table = f"{table}_{new_id}"
        if not db.table_exists(old_table):
            continue
        try:
            db.execute(f'DROP TABLE IF EXISTS "{new_table}"')
            db.execute(f'CREATE TABLE "{new_table}" AS SELECT * FROM "{old_table}"')
            cnt = db.query(f'SELECT COUNT(*) as n FROM "{new_table}"')
            rows_count = cnt[0]["n"] if cnt else 0
            db.set_conversion_status(new_id, slot, "done", rows_count=rows_count)
            cloned += 1
        except Exception as e:
            failed.append(f"{slot}: {e}")

    if failed:
        flash(f"Some files could not be copied: {'; '.join(failed)}", "error")
    if cloned:
        flash(
            f"Loaded {cloned} file{'s' if cloned != 1 else ''} from your previous analysis. "
            "Replace any one of them on the next page if needed.",
            "success"
        )
    return redirect(url_for("upload"))


def _purge_uploading_session(session_id):
    """Drop all tables for an uploading session and remove its row. Used before reuse."""
    for slot, table in FILE_SLOTS.items():
        try:
            db.execute(f'DROP TABLE IF EXISTS "{table}_{session_id}"')
        except Exception:
            pass
    try:
        db.execute("DELETE FROM upload_sessions WHERE id=?", (session_id,))
    except Exception:
        pass


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    upload_sessions = db.query(
        "SELECT * FROM upload_sessions WHERE user_id=? AND status='uploading' ORDER BY created_at DESC LIMIT 1",
        (session["user_id"],)
    )
    if upload_sessions:
        upload_session_id = upload_sessions[0]["id"]
    else:
        upload_session_id = db.execute(
            "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
            (session["user_id"], session["org_name"], "uploading")
        )

    if request.method == "POST":
        slot = request.form.get("slot")
        if slot not in FILE_SLOTS:
            return jsonify({"ok": False, "error": "Unknown file slot."})

        # ── Chunked upload path ────────────────────────────────────────────────
        chunk_index  = request.form.get("chunk_index")
        total_chunks = request.form.get("total_chunks")
        upload_id    = request.form.get("upload_id", "")
        orig_name    = request.form.get("filename", "file.xlsx")

        if chunk_index is not None:
            chunk_index  = int(chunk_index)
            total_chunks = int(total_chunks)
            chunk_data   = request.files.get("chunk")
            if not chunk_data:
                return jsonify({"ok": False, "error": "No chunk received."})

            # Save this chunk to a temp file
            chunk_path = os.path.join(UPLOAD_FOLDER, f"tmp_{upload_id}_{chunk_index}")
            chunk_data.save(chunk_path)

            # Check how many chunks we have so far
            received = sum(
                1 for i in range(total_chunks)
                if os.path.exists(os.path.join(UPLOAD_FOLDER, f"tmp_{upload_id}_{i}"))
            )

            if received < total_chunks:
                # More chunks to come — acknowledge and wait
                return jsonify({"ok": True, "chunk_received": chunk_index})

            # All chunks received — assemble into final file
            safe_name = secure_filename(orig_name)
            filepath  = os.path.join(UPLOAD_FOLDER, f"{upload_session_id}_{slot}_{safe_name}")
            with open(filepath, "wb") as out:
                for i in range(total_chunks):
                    cp = os.path.join(UPLOAD_FOLDER, f"tmp_{upload_id}_{i}")
                    with open(cp, "rb") as inf:
                        out.write(inf.read())
                    os.remove(cp)

            # Kick off background processing
            db.set_conversion_status(upload_session_id, slot, "converting")
            _start_processing(filepath, FILE_SLOTS[slot], upload_session_id, slot, orig_name)
            return jsonify({"ok": True, "processing": True, "filename": orig_name})

        # ── Single-file upload path (fallback for small files) ─────────────────
        file = request.files.get("file")
        if not file or not _allowed(file.filename):
            return jsonify({"ok": False, "error": "Please upload a .xlsx or .csv file."})

        original_name = file.filename
        filepath = os.path.join(UPLOAD_FOLDER, f"{upload_session_id}_{slot}_{secure_filename(original_name)}")
        file.save(filepath)
        db.set_conversion_status(upload_session_id, slot, "converting")
        _start_processing(filepath, FILE_SLOTS[slot], upload_session_id, slot, original_name)
        return jsonify({"ok": True, "processing": True, "filename": original_name})

    tables    = db.get_session_tables(upload_session_id)
    names_row = db.query("SELECT file_names_json FROM upload_sessions WHERE id=?", (upload_session_id,))
    file_names = json.loads(names_row[0]["file_names_json"] or "{}") if names_row and names_row[0]["file_names_json"] else {}
    # Conversion status — needed so the template can distinguish "fully done"
    # from "still processing". Without it, a mid-conversion refresh would show
    # the slot as uploaded just because the (partial) table exists.
    conversion_status = db.get_conversion_status(upload_session_id)
    return render_template(
        "upload.html",
        tables=tables,
        session_id=upload_session_id,
        file_names=file_names,
        conversion_status=conversion_status,
    )


def _start_processing(filepath, table, session_id, slot, orig_name):
    def _process():
        result = db.excel_to_sqlite(filepath, table, session_id)
        if result.get("ok"):
            db.set_conversion_status(session_id, slot, "done", rows_count=result.get("rows", 0))
            names_row = db.query("SELECT file_names_json FROM upload_sessions WHERE id=?", (session_id,))
            names = json.loads(names_row[0]["file_names_json"] or "{}") if names_row and names_row[0]["file_names_json"] else {}
            names[slot] = orig_name
            db.execute("UPDATE upload_sessions SET file_names_json=? WHERE id=?", (json.dumps(names), session_id))
        else:
            db.set_conversion_status(session_id, slot, "error", error=result.get("error", "Unknown error"))
    t = threading.Thread(target=_process, daemon=True)
    t.start()


@app.route("/upload/status/<int:upload_session_id>")
@login_required
def upload_status(upload_session_id):
    _verify_session_owner(upload_session_id)
    statuses  = db.get_conversion_status(upload_session_id)
    names_row = db.query("SELECT file_names_json FROM upload_sessions WHERE id=?", (upload_session_id,))
    file_names = json.loads(names_row[0]["file_names_json"] or "{}") if names_row and names_row[0]["file_names_json"] else {}
    return jsonify({"statuses": statuses, "file_names": file_names})


@app.route("/upload/scope/<int:upload_session_id>", methods=["POST"])
@login_required
def upload_set_scope(upload_session_id):
    _verify_session_owner(upload_session_id)
    data  = request.get_json() or {}
    scope = str(data.get("scope", "all")).strip()
    if scope != "all":
        try:
            n = int(scope)
            if n < 1:
                return jsonify({"ok": False, "error": "Scope must be a positive number."})
            scope = str(n)
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid scope value."})
    db.execute("UPDATE upload_sessions SET scope=? WHERE id=?", (scope, upload_session_id))
    return jsonify({"ok": True})


@app.route("/upload/remove", methods=["POST"])
@login_required
def remove_upload():
    data       = request.get_json() or {}
    slot       = data.get("slot")
    session_id = data.get("session_id")
    if slot not in FILE_SLOTS:
        return jsonify({"ok": False, "error": "Unknown slot."})
    _verify_session_owner(session_id)
    try:
        # 1. Drop the per-session table (partial or complete).
        db.execute(f'DROP TABLE IF EXISTS "{FILE_SLOTS[slot]}_{session_id}"')

        # 2. Forget this slot in conversion_status_json so a page refresh
        #    no longer shows it as "done" or "converting".
        conv = db.get_conversion_status(session_id)
        if slot in conv:
            del conv[slot]
            db.execute(
                "UPDATE upload_sessions SET conversion_status_json=? WHERE id=?",
                (json.dumps(conv), session_id)
            )

        # 3. Forget the filename so the slot label resets.
        names_row = db.query("SELECT file_names_json FROM upload_sessions WHERE id=?", (session_id,))
        if names_row and names_row[0]["file_names_json"]:
            try:
                names = json.loads(names_row[0]["file_names_json"])
                if slot in names:
                    del names[slot]
                    db.execute(
                        "UPDATE upload_sessions SET file_names_json=? WHERE id=?",
                        (json.dumps(names), session_id)
                    )
            except Exception:
                pass

        # 4. Remove any final files and stray chunk temp files for this slot.
        try:
            for f in os.listdir(UPLOAD_FOLDER):
                if f.startswith(f"{session_id}_{slot}_") or f.startswith(f"tmp_{session_id}_{slot}_"):
                    try:
                        os.remove(os.path.join(UPLOAD_FOLDER, f))
                    except Exception:
                        pass
        except FileNotFoundError:
            pass
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True})


@app.route("/context/<int:upload_session_id>", methods=["GET", "POST"])
@login_required
def context_form(upload_session_id):
    _verify_session_owner(upload_session_id)
    if request.method == "POST":
        context = {
            "delayed_suppliers": request.form.get("delayed_suppliers", "").strip(),
            "large_orders":      request.form.get("large_orders", "").strip(),
            "discontinue":       request.form.get("discontinue", "").strip(),
            "other":             request.form.get("other", "").strip(),
        }
        db.execute(
            "UPDATE upload_sessions SET context_json=?, status='pending', dedup_confirmed=0 WHERE id=?",
            (json.dumps(context), upload_session_id)
        )
        return redirect(url_for("dedup_loading", upload_session_id=upload_session_id))
    return render_template("context_form.html", upload_session_id=upload_session_id)


@app.route("/dedup/loading/<int:upload_session_id>")
@login_required
def dedup_loading(upload_session_id):
    _verify_session_owner(upload_session_id)
    return render_template("dedup_loading.html", upload_session_id=upload_session_id)


@app.route("/dedup/stream/<int:upload_session_id>")
@login_required
def dedup_stream(upload_session_id):
    """SSE endpoint — streams Claude tokens for the normalisation agent in real time."""
    _verify_session_owner(upload_session_id)
    model = session["model"]

    def generate():
        import re as _re

        # ── Collect unique item names (mirrors run_normalization_agent logic) ──
        item_names = set()

        def _col_candidates(table, candidates):
            try:
                row = db.query(f"SELECT * FROM {table} LIMIT 1")
                if not row:
                    return []
                cols = list(row[0].keys())
                for c in candidates:
                    if c in cols:
                        rows = db.query(
                            f'SELECT DISTINCT "{c}" FROM {table} '
                            f'WHERE "{c}" IS NOT NULL LIMIT 2000'
                        )
                        return [r[c] for r in rows if r[c]]
            except Exception:
                pass
            return []

        cand = ["description", "item_description", "inventory_desc",
                "item_name", "product_description", "product_name"]
        item_names.update(_col_candidates(f"inventory_{upload_session_id}",       cand))
        item_names.update(_col_candidates(f"purchase_orders_{upload_session_id}", cand))
        item_names.update(_col_candidates(f"sales_{upload_session_id}",           cand))

        if not item_names:
            normalization_cache[upload_session_id] = {"groups": [], "message": "No item names found."}
            yield f"data: {json.dumps({'type': 'done', 'count': 0})}\n\n"
            return

        # ── Load scope for Top-N filtering ────────────────────────────────────
        # NOTE: Dead SKU exclusion is NOT done here via exact-name matching —
        # that approach zeroes out the list because inventory and sales names
        # differ in spelling (which is exactly what dedup is for). Dead SKUs
        # are detected by the inventory agent AFTER dedup and separated into
        # their own tab on the results page.
        sess_rows = db.query("SELECT scope FROM upload_sessions WHERE id=?", (upload_session_id,))
        scope_val = (sess_rows[0]["scope"] if sess_rows else None) or "all"

        # ── Apply Top-N scope by sales transaction frequency ──────────────────
        if scope_val != "all":
            try:
                top_n = int(scope_val)
                # Count occurrences of each name in the sales table (proxy for revenue)
                sales_names_raw = _col_candidates(f"sales_{upload_session_id}", cand)
                sales_freq: dict = {}
                for n in sales_names_raw:
                    key = n.strip().lower() if n else ""
                    if key:
                        sales_freq[key] = sales_freq.get(key, 0) + 1

                def _sales_score(name: str) -> int:
                    return sales_freq.get(name.strip().lower(), 0)

                sorted_items = sorted(item_names, key=_sales_score, reverse=True)
                item_names   = set(sorted_items[:top_n])
            except (ValueError, TypeError):
                pass  # Bad scope value — fall through with all items

        items_list = sorted(list(item_names))[:1500]
        yield f"data: {json.dumps({'type': 'status', 'count': len(items_list)})}\n\n"

        system_prompt = (
            "You are a data normalisation specialist for a food distribution company.\n"
            "Identify item names that clearly refer to the same product but are written differently.\n\n"
            "Rules:\n"
            "- Only group items you are confident are the same product (same product, same size/weight)\n"
            "- Do NOT merge items if uncertain - leave them separate\n"
            "- Return ONLY a JSON array of groups\n"
            '- Each group has: "canonical" (clearest name) and "variants" (list of other names)\n'
            "- Only include groups with 2+ variants - skip solo items\n\n"
            "Example:\n"
            '[{"canonical": "White Bread 400g", "variants": ["WHT BRD 400G", "Bread White 400g"]}]'
        )
        user_prompt = (
            f"Here are {len(items_list)} unique item names. Group the duplicates.\n\nItem names:\n"
            + "\n".join(items_list)
        )

        # ── Stream tokens from Claude ──────────────────────────────────────────
        full_text = ""
        try:
            with _anthropic.Anthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY")
            ).messages.stream(
                model=model,
                max_tokens=8000,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                for text in stream.text_stream:
                    full_text += text
                    yield f"data: {json.dumps({'type': 'token', 'text': text})}\n\n"
        except Exception as e:
            normalization_cache[upload_session_id] = {"groups": [], "message": str(e)}
            yield f"data: {json.dumps({'type': 'error', 'msg': str(e)})}\n\n"
            return

        # ── Parse and cache result ─────────────────────────────────────────────
        groups = []
        try:
            m = _re.search(r'\[.*\]', full_text, _re.DOTALL)
            if m:
                groups = json.loads(m.group())
        except Exception:
            pass

        normalization_cache[upload_session_id] = {"groups": groups, "message": ""}
        yield f"data: {json.dumps({'type': 'done', 'count': len(groups)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/dedup/<int:upload_session_id>", methods=["GET", "POST"])
@login_required
def dedup_review(upload_session_id):
    _verify_session_owner(upload_session_id)
    model = session["model"]
    if request.method == "POST":
        confirmed_raw = request.form.get("confirmed_groups", "[]")
        try:
            confirmed = json.loads(confirmed_raw)
        except Exception:
            confirmed = []
        db.execute("UPDATE upload_sessions SET dedup_confirmed=1 WHERE id=?", (upload_session_id,))
        db.execute(
            "INSERT OR REPLACE INTO analysis_results (session_id, inventory_report, recommendations_json) VALUES (?,?,?)",
            (upload_session_id, json.dumps({"confirmed_groups": confirmed}), "[]")
        )
        return redirect(url_for("run_analysis", upload_session_id=upload_session_id))
    # Use result cached by the streaming loading page if available; otherwise block
    cached = normalization_cache.pop(upload_session_id, None)
    if cached is not None:
        groups  = cached["groups"]
        message = cached.get("message", "")
    else:
        result  = run_normalization_agent(upload_session_id, model)
        groups  = result.get("groups", [])
        message = result.get("message", "")
    return render_template("dedup_review.html", groups=groups, message=message, upload_session_id=upload_session_id)


@app.route("/analyse/<int:upload_session_id>")
@login_required
def run_analysis(upload_session_id):
    _verify_session_owner(upload_session_id)

    # If analysis already completed for this session, skip straight to results.
    s = db.query("SELECT status FROM upload_sessions WHERE id=?", (upload_session_id,))
    if s and s[0]["status"] == "complete":
        return redirect(url_for("results", upload_session_id=upload_session_id))

    # Tier limit: free users get 1 analysis total
    user_tier = session.get("tier", "enterprise")
    current_user = db.query(
        "SELECT analyses_used FROM users WHERE id=?", (session["user_id"],)
    )
    if user_tier == "free" and current_user and current_user[0]["analyses_used"] >= 1:
        flash("Free accounts include 1 analysis. Upgrade to Professional or Enterprise for unlimited analyses.", "error")
        return redirect(url_for("dashboard"))

    # If a run is in progress already (user refreshed the progress page), show it.
    with analysis_progress_lock:
        existing = analysis_progress.get(upload_session_id)
    if existing and existing["status"] == "running":
        return render_template("analysis_progress.html", upload_session_id=upload_session_id)

    # Otherwise kick off a new run in the background.
    model       = session["model"]
    _user_id    = session["user_id"]
    _user_tier  = user_tier
    base_url    = request.host_url.rstrip("/")  # e.g. "https://berthai.onrender.com"
    with analysis_progress_lock:
        analysis_progress[upload_session_id] = {
            "started_at":    time.time(),
            "log":           [],
            "status":        "running",
            "error":         None,
            "current_agent": None,
            "agents": {
                # Normalisation runs earlier in the dedup step. Mark it done
                # so the progress page can show all three stages truthfully.
                "normalization": {
                    "status":  "done",
                    "summary": "Item names mapped, duplicates merged",
                    "started_at": None,
                    "ended_at":   None,
                },
                "inventory": {
                    "status":  "pending",
                    "summary": "",
                    "started_at": None,
                    "ended_at":   None,
                },
                "recommendation": {
                    "status":  "pending",
                    "summary": "",
                    "started_at": None,
                    "ended_at":   None,
                },
            },
        }

    def _emit(msg: str, agent: str = None):
        with analysis_progress_lock:
            entry = analysis_progress.get(upload_session_id)
            if not entry:
                return
            a = agent or entry.get("current_agent")
            entry["log"].append({
                "t":     round(time.time() - entry["started_at"], 1),
                "msg":   msg,
                "agent": a,
            })

    def _mark_agent(name: str, status: str, summary: str = None):
        """Update one agent's lifecycle state. Called when an agent starts,
        finishes, or errors."""
        with analysis_progress_lock:
            entry = analysis_progress.get(upload_session_id)
            if not entry:
                return
            now_rel = round(time.time() - entry["started_at"], 1)
            ag = entry["agents"].get(name)
            if not ag:
                return
            ag["status"] = status
            if status == "running":
                ag["started_at"] = now_rel
                entry["current_agent"] = name
            elif status in ("done", "error"):
                ag["ended_at"] = now_rel
                if entry.get("current_agent") == name:
                    entry["current_agent"] = None
            if summary is not None:
                ag["summary"] = summary

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

    def _run():
        try:
            rows    = db.query("SELECT context_json FROM upload_sessions WHERE id=?", (upload_session_id,))
            context = json.loads(rows[0]["context_json"] or "{}") if rows else {}
            ar_rows = db.query("SELECT inventory_report FROM analysis_results WHERE session_id=?", (upload_session_id,))
            confirmed_groups = []
            if ar_rows and ar_rows[0]["inventory_report"]:
                try:
                    data = json.loads(ar_rows[0]["inventory_report"])
                    confirmed_groups = data.get("confirmed_groups", [])
                except Exception:
                    pass

            # ── Agent 2: Inventory health ────────────────────────────────────
            _mark_agent("inventory", "running")
            _emit("Starting inventory health agent")
            inv_result = run_inventory_agent(upload_session_id, model, confirmed_groups, context, progress_emit=_emit)
            if "error" in inv_result:
                _mark_agent("inventory", "error", summary="Failed — see error below")
                with analysis_progress_lock:
                    analysis_progress[upload_session_id]["status"] = "error"
                    analysis_progress[upload_session_id]["error"]  = inv_result["error"]
                return

            inventory_report = inv_result["report"]
            _mark_agent("inventory", "done", summary=_summarise_inventory(inventory_report))

            # ── Agent 3: Purchase recommendations ────────────────────────────
            _mark_agent("recommendation", "running")
            _emit("Starting purchase recommendation agent")
            recommendations = run_recommendation_agent(upload_session_id, model, inventory_report, context, progress_emit=_emit)

            # Defensive: normalise confidence values before persisting so the
            # UI doesn't have to guess what "MED" or "high" means later.
            for _rec in recommendations:
                _normalise_confidence(_rec)

            _mark_agent("recommendation", "done", summary=_summarise_recommendations(recommendations))

            db.execute(
                "UPDATE analysis_results SET inventory_report=?, recommendations_json=? WHERE session_id=?",
                (json.dumps(inventory_report), json.dumps(recommendations), upload_session_id)
            )
            db.execute("UPDATE upload_sessions SET status='complete' WHERE id=?", (upload_session_id,))

            # Increment analyses_used for free users
            if _user_tier == "free":
                db.execute(
                    "UPDATE users SET analyses_used = analyses_used + 1 WHERE id=?",
                    (_user_id,)
                )

            # ── Critical stock alert ─────────────────────────────────────────
            # Compare against the previous completed session for this user.
            # If any items are newly CRITICAL (weren't critical before), email the user.
            try:
                sess_meta = db.query("SELECT user_id FROM upload_sessions WHERE id=?", (upload_session_id,))
                if sess_meta:
                    uid = sess_meta[0]["user_id"]
                    prev = db.query(
                        "SELECT id FROM upload_sessions "
                        "WHERE user_id=? AND status='complete' AND id!=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (uid, upload_session_id)
                    )
                    if prev:
                        prev_ar = db.query(
                            "SELECT inventory_report FROM analysis_results WHERE session_id=?",
                            (prev[0]["id"],)
                        )
                        if prev_ar and prev_ar[0]["inventory_report"]:
                            prev_inv = json.loads(prev_ar[0]["inventory_report"] or "[]")
                            if isinstance(prev_inv, dict):
                                prev_inv = []
                            prev_critical = {
                                str(i.get("item", "")).strip()
                                for i in prev_inv
                                if isinstance(i, dict) and i.get("status") == "CRITICAL"
                            }
                            newly_critical = [
                                i for i in inventory_report
                                if isinstance(i, dict)
                                and i.get("status") == "CRITICAL"
                                and str(i.get("item", "")).strip() not in prev_critical
                            ]
                            if newly_critical:
                                threading.Thread(
                                    target=_send_critical_alert,
                                    args=(uid, upload_session_id, newly_critical, base_url),
                                    daemon=True,
                                ).start()
            except Exception:
                pass  # Never let alert logic break the analysis

            # ── Analysis-ready notification ─────────────────────────────────
            # Always email when an analysis completes so users can close the tab.
            try:
                summary_dict = {
                    "total_items": len([i for i in inventory_report if isinstance(i, dict)]),
                    "critical":    sum(1 for i in inventory_report if isinstance(i, dict) and i.get("status") == "CRITICAL"),
                    "low":         sum(1 for i in inventory_report if isinstance(i, dict) and i.get("status") == "LOW"),
                    "rec_count":   len([r for r in recommendations if isinstance(r, dict) and not r.get("error")]),
                    "flagged":     sum(1 for r in recommendations if isinstance(r, dict) and (r.get("supplier_risk") == "HIGH" or r.get("flags"))),
                }
                threading.Thread(
                    target=_send_analysis_ready_email,
                    args=(_user_id, upload_session_id, summary_dict, base_url),
                    daemon=True,
                ).start()
            except Exception:
                pass  # Email failure must not block the analysis

            _emit("Saving results and redirecting...")
            with analysis_progress_lock:
                analysis_progress[upload_session_id]["status"] = "done"
        except Exception as e:
            with analysis_progress_lock:
                analysis_progress[upload_session_id]["status"] = "error"
                analysis_progress[upload_session_id]["error"]  = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return render_template("analysis_progress.html", upload_session_id=upload_session_id)


@app.route("/analysis_status/<int:upload_session_id>")
@login_required
def analysis_status(upload_session_id):
    _verify_session_owner(upload_session_id)

    with analysis_progress_lock:
        entry = analysis_progress.get(upload_session_id)
        if entry is not None:
            # Snapshot what we need so we don't hold the lock during jsonify
            payload = {
                "status":        entry["status"],
                "log":           list(entry["log"]),
                "elapsed":       round(time.time() - entry["started_at"], 1),
                "error":         entry.get("error"),
                "current_agent": entry.get("current_agent"),
                "agents":        {k: dict(v) for k, v in entry.get("agents", {}).items()},
            }
        else:
            payload = None

    if payload is not None:
        return jsonify(payload)

    # No in-memory entry — check DB. Worker may have restarted, or analysis
    # completed before this session started polling.
    rows = db.query("SELECT status FROM upload_sessions WHERE id=?", (upload_session_id,))
    if rows and rows[0]["status"] == "complete":
        # All three agents must already be done if the session is complete.
        all_done = {
            "normalization":  {"status":"done","summary":"Item names mapped, duplicates merged","started_at":None,"ended_at":None},
            "inventory":      {"status":"done","summary":"Completed","started_at":None,"ended_at":None},
            "recommendation": {"status":"done","summary":"Completed","started_at":None,"ended_at":None},
        }
        return jsonify({"status": "done", "log": [], "elapsed": 0, "agents": all_done, "current_agent": None})
    return jsonify({"status": "not_found", "log": [], "elapsed": 0, "agents": {}, "current_agent": None})


@app.route("/results/<int:upload_session_id>")
@login_required
def results(upload_session_id):
    _verify_session_owner(upload_session_id)
    ar = db.query("SELECT * FROM analysis_results WHERE session_id=?", (upload_session_id,))
    if not ar:
        flash("No results found. Please run the analysis first.", "error")
        return redirect(url_for("dashboard"))
    try:
        inventory_raw   = json.loads(ar[0]["inventory_report"] or "[]")
        recommendations = json.loads(ar[0]["recommendations_json"] or "[]")
        generated_at    = ar[0].get("created_at", "")
    except Exception:
        inventory_raw   = []
        recommendations = []
        generated_at    = ""

    # inventory_report may be a list (after analysis) or a placeholder dict
    if isinstance(inventory_raw, list):
        inventory = inventory_raw
    elif isinstance(inventory_raw, dict):
        inventory = inventory_raw.get("report", [])
    else:
        inventory = []

    status_by_item = {
        str(item.get("item", "")): item.get("status", "")
        for item in inventory
    }

    # Enrich each recommendation with order-by date and confidence reasons
    # so the template can render them without computing inline. Confidence
    # is normalised first so old rows (with "MED") match the template's
    # MEDIUM check and the ring renders the correct colour.
    # Stable global index per rec, used as DOM id so the grouped template
    # keeps unique ids regardless of nesting.
    _idx = 0
    for rec in recommendations:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        _normalise_confidence(rec)
        rec["_order_by"]      = _compute_order_by(rec)
        rec["_conf_reasons"]  = _confidence_reasons(rec)
        rec["_effective_qty"]      = _effective_qty(rec)
        rec["_effective_supplier"] = _effective_supplier(rec)
        rec["_card_idx"]      = _idx
        _idx += 1

    # Group recs by supplier so the template can render one section per
    # supplier with bulk-approve. Most urgent supplier first.
    rec_groups = _group_recs_by_supplier(recommendations, status_by_item)

    # Walk the groups in render order and stamp a sequential display number on
    # each rec. Same-supplier items naturally get consecutive numbers (e.g.
    # 1–4 from Supplier A, 5–6 from Supplier B), making the grouping obvious
    # at a glance — also helpful when the user prints or shares the page.
    _n = 0
    for _grp in rec_groups:
        _grp["start_num"] = _n + 1
        for _rec in _grp["recs"]:
            _n += 1
            _rec["_display_num"] = _n
        _grp["end_num"] = _n

    return render_template(
        "results.html",
        recommendations=recommendations,
        rec_groups=rec_groups,
        inventory=inventory,
        upload_session_id=upload_session_id,
        org_name=session["org_name"],
        generated_at=generated_at,
        status_by_item=status_by_item,
        user_tier=session.get("tier", "enterprise"),
    )


@app.route("/results/<int:upload_session_id>/print")
@login_required
def print_results(upload_session_id):
    """Render a print-ready sheet containing only approved recommendations."""
    _verify_session_owner(upload_session_id)
    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (upload_session_id,))
    if not ar:
        flash("No results found.", "error")
        return redirect(url_for("dashboard"))
    try:
        recommendations = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        recommendations = []
    approved = [r for r in recommendations if r.get("approved") and not r.get("error")]
    # Enrich with effective values + order-by so the print template can stay simple.
    for r in approved:
        _normalise_confidence(r)
        r["_effective_qty"]      = _effective_qty(r)
        r["_effective_supplier"] = _effective_supplier(r)
        r["_order_by"]           = _compute_order_by(r)
    return render_template("print_order.html", recommendations=approved, org_name=session["org_name"])


@app.route("/results/<int:upload_session_id>/export.csv")
@login_required
def export_csv(upload_session_id):
    """Download approved recommendations as a CSV file."""
    if session.get("tier") == "free":
        flash("CSV export is available on Professional and Enterprise plans.", "error")
        return redirect(url_for("results", upload_session_id=upload_session_id))
    import csv, io
    _verify_session_owner(upload_session_id)
    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (upload_session_id,))
    if not ar:
        flash("No results found.", "error")
        return redirect(url_for("dashboard"))
    try:
        recommendations = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        recommendations = []
    approved = [r for r in recommendations if r.get("approved") and not r.get("error")]
    for r in approved:
        _normalise_confidence(r)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Item", "Supplier", "Supplier Type", "Order Quantity", "AI Suggested Qty",
        "Order By", "Days of Supply", "Stock Runway (months)", "Confidence", "Reason", "Note"
    ])
    for r in approved:
        dos = r.get("days_of_supply")
        runway = round(dos / 30, 1) if dos else ""
        order_by = _compute_order_by(r).get("order_by_date") or ""
        writer.writerow([
            r.get("item", ""),
            _effective_supplier(r),
            r.get("supplier_type", ""),
            _effective_qty(r),
            r.get("suggested_quantity", ""),
            order_by,
            dos or "",
            runway,
            ("N/A" if r.get("confidence") == "INSUFFICIENT_DATA" else r.get("confidence", "")),
            r.get("reason", ""),
            r.get("note", ""),
        ])

    org_slug = session["org_name"].replace(" ", "_").lower()
    filename = f"berthai_orders_{org_slug}_{upload_session_id}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/results/<int:upload_session_id>/export.pdf")
@login_required
def export_pdf(upload_session_id):
    """Download approved recommendations as a formatted PDF."""
    if session.get("tier") == "free":
        flash("PDF export is available on Professional and Enterprise plans.", "error")
        return redirect(url_for("results", upload_session_id=upload_session_id))
    import io
    from datetime import datetime
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER

    _verify_session_owner(upload_session_id)
    ar = db.query(
        "SELECT recommendations_json, created_at FROM analysis_results WHERE session_id=?",
        (upload_session_id,)
    )
    if not ar:
        flash("No results found.", "error")
        return redirect(url_for("dashboard"))
    try:
        recommendations = json.loads(ar[0]["recommendations_json"] or "[]")
        generated_at = (ar[0].get("created_at") or "")[:10]
    except Exception:
        recommendations = []
        generated_at = ""
    approved = [r for r in recommendations if r.get("approved") and not r.get("error")]
    for r in approved:
        _normalise_confidence(r)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=18*mm, bottomMargin=18*mm,
    )

    styles = getSampleStyleSheet()
    BRASS  = colors.HexColor("#c8924c")
    NAVY   = colors.HexColor("#0f1b2d")
    LIGHT  = colors.HexColor("#f9fafb")
    BORDER = colors.HexColor("#e5e7eb")
    MUTED  = colors.HexColor("#6b7280")

    title_style = ParagraphStyle(
        "Title", fontName="Helvetica-Bold", fontSize=18,
        textColor=NAVY, spaceAfter=2
    )
    sub_style = ParagraphStyle(
        "Sub", fontName="Helvetica", fontSize=10,
        textColor=MUTED, spaceAfter=12
    )
    cell_style = ParagraphStyle(
        "Cell", fontName="Helvetica", fontSize=9,
        textColor=NAVY, leading=12
    )
    cell_muted = ParagraphStyle(
        "CellMuted", fontName="Helvetica", fontSize=8,
        textColor=MUTED, leading=11
    )

    today = datetime.utcnow().strftime("%d %b %Y")
    story = [
        Paragraph("BerthAI — Purchase Order Sheet", title_style),
        Paragraph(
            f"{session['org_name']}  ·  Prepared: {today}  ·  "
            f"Analysis date: {generated_at}  ·  {len(approved)} item(s) approved",
            sub_style
        ),
        Spacer(1, 4*mm),
    ]

    if approved:
        header = ["#", "Item", "Supplier", "Qty", "Order by", "Runway", "Confidence", "Reason / Note"]
        rows = [header]
        for i, r in enumerate(approved, 1):
            dos = r.get("days_of_supply")
            runway = f"{round(dos/30,1)} mo" if dos else "—"
            reason = r.get("reason", "")
            note   = r.get("note", "")
            reason_cell = Paragraph(
                reason + (f'<br/><font color="#9ca3af"><i>Note: {note}</i></font>' if note else ""),
                cell_style
            )

            eff_qty = _effective_qty(r) or "—"
            sug_qty = r.get("suggested_quantity", "")
            qty_html = str(eff_qty)
            if str(eff_qty).strip() and str(sug_qty).strip() and str(eff_qty) != str(sug_qty):
                qty_html = (
                    f"<b>{eff_qty}</b><br/>"
                    f"<font color='#9ca3af' size='7'>AI: {sug_qty}</font>"
                )

            order_by_str = _compute_order_by(r).get("order_by_date") or "—"

            rows.append([
                str(i),
                Paragraph(f"<b>{r.get('item','')}</b>", cell_style),
                Paragraph(
                    f"{_effective_supplier(r)}<br/>"
                    f"<font color='#6b7280'>{r.get('supplier_type','')}</font>",
                    cell_style
                ),
                Paragraph(qty_html, cell_style),
                Paragraph(order_by_str, cell_style),
                runway,
                ("N/A" if r.get("confidence") == "INSUFFICIENT_DATA" else r.get("confidence", "—")),
                reason_cell,
            ])

        col_widths = [7*mm, 34*mm, 28*mm, 18*mm, 20*mm, 14*mm, 18*mm, None]
        tbl = Table(rows, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            # Header row
            ("BACKGROUND",   (0,0), (-1,0), BRASS),
            ("TEXTCOLOR",    (0,0), (-1,0), colors.white),
            ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",     (0,0), (-1,0), 8),
            ("TOPPADDING",   (0,0), (-1,0), 6),
            ("BOTTOMPADDING",(0,0), (-1,0), 6),
            # Body rows
            ("FONTNAME",     (0,1), (-1,-1), "Helvetica"),
            ("FONTSIZE",     (0,1), (-1,-1), 9),
            ("TOPPADDING",   (0,1), (-1,-1), 6),
            ("BOTTOMPADDING",(0,1), (-1,-1), 6),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, LIGHT]),
            ("GRID",         (0,0), (-1,-1), 0.5, BORDER),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("ALIGN",        (0,0), (0,-1), "CENTER"),
        ]))
        story.append(tbl)
    else:
        story.append(Paragraph(
            "No approved orders to export. Go back and approve recommendations first.",
            sub_style
        ))

    story.append(Spacer(1, 8*mm))
    story.append(Paragraph(
        "Generated by BerthAI · For internal purchasing use only",
        ParagraphStyle("Footer", fontName="Helvetica", fontSize=8, textColor=MUTED)
    ))

    doc.build(story)
    buf.seek(0)

    org_slug = session["org_name"].replace(" ", "_").lower()
    filename = f"berthai_orders_{org_slug}_{upload_session_id}.pdf"
    return Response(
        buf.read(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/diff/<int:session_a_id>/<int:session_b_id>")
@login_required
def diff_view(session_a_id, session_b_id):
    """Compare two analysis sessions — show what changed between them."""
    _verify_session_owner(session_a_id)
    _verify_session_owner(session_b_id)

    def _load(sid):
        sess = db.query("SELECT * FROM upload_sessions WHERE id=?", (sid,))
        ar   = db.query(
            "SELECT inventory_report, recommendations_json FROM analysis_results WHERE session_id=?",
            (sid,)
        )
        if not sess or not ar:
            return None, [], []
        inv  = json.loads(ar[0]["inventory_report"]    or "[]")
        recs = json.loads(ar[0]["recommendations_json"] or "[]")
        if isinstance(inv, dict):
            inv = []
        return sess[0], inv, recs

    sess_a, inv_a, _  = _load(session_a_id)
    sess_b, inv_b, _  = _load(session_b_id)

    if not sess_a or not sess_b:
        flash("Could not load one or both sessions.", "error")
        return redirect(url_for("dashboard"))

    # Guarantee A is the newer session
    if str(sess_a["created_at"]) < str(sess_b["created_at"]):
        sess_a, inv_a, session_a_id, sess_b, inv_b, session_b_id = \
            sess_b, inv_b, session_b_id, sess_a, inv_a, session_a_id

    map_a = {str(i.get("item", "")).strip(): i for i in inv_a if isinstance(i, dict)}
    map_b = {str(i.get("item", "")).strip(): i for i in inv_b if isinstance(i, dict)}
    all_items = sorted(set(map_a) | set(map_b))

    STATUS_ORDER = {"CRITICAL": 0, "LOW": 1, "NORMAL": 2, "DEAD": 3}

    escalated      = []   # became CRITICAL
    improved       = []   # left CRITICAL
    status_changed = []   # other status shift
    new_items      = []   # only in A
    resolved       = []   # only in B
    unchanged      = []   # same status

    for item in all_items:
        a = map_a.get(item)
        b = map_b.get(item)
        if a and not b:
            new_items.append(a)
        elif b and not a:
            resolved.append(b)
        else:
            sa = (a.get("status") or "").upper()
            sb = (b.get("status") or "").upper()
            entry = {"item": item, "from_status": sb, "to_status": sa, "a": a, "b": b}
            if sa == sb:
                unchanged.append(entry)
            elif sa == "CRITICAL" and sb != "CRITICAL":
                escalated.append(entry)
            elif sb == "CRITICAL" and sa != "CRITICAL":
                improved.append(entry)
            else:
                status_changed.append(entry)

    return render_template(
        "diff.html",
        sess_a=sess_a,
        sess_b=sess_b,
        session_a_id=session_a_id,
        session_b_id=session_b_id,
        escalated=escalated,
        improved=improved,
        status_changed=status_changed,
        new_items=new_items,
        resolved=resolved,
        unchanged=unchanged,
    )



# ── Recommendation helpers ───────────────────────────────────────────────────

# Confidence values the rest of the codebase (templates, CSV, PDF) expects.
# Normalise on both save and read so old DB rows render correctly.
_CONFIDENCE_ALIASES = {
    "MED":          "MEDIUM",
    "MID":          "MEDIUM",
    "M":            "MEDIUM",
    "H":            "HIGH",
    "L":            "LOW",
    "INSUFFICIENT": "INSUFFICIENT_DATA",
    "UNKNOWN":      "INSUFFICIENT_DATA",
    "N/A":          "INSUFFICIENT_DATA",
}

def _normalise_confidence(rec):
    """Coerce rec['confidence'] to one of HIGH / MEDIUM / LOW / INSUFFICIENT_DATA."""
    if not isinstance(rec, dict):
        return
    raw = (rec.get("confidence") or "").strip().upper()
    if not raw:
        return
    if raw in ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT_DATA"):
        rec["confidence"] = raw
        return
    rec["confidence"] = _CONFIDENCE_ALIASES.get(raw, "INSUFFICIENT_DATA")


def _effective_qty(rec):
    """Return the quantity to display/export: edited value if user adjusted it,
    otherwise the AI's suggested quantity."""
    if not isinstance(rec, dict):
        return ""
    edited = rec.get("edited_quantity")
    if edited not in (None, "", "null"):
        return edited
    return rec.get("suggested_quantity", "")


def _effective_supplier(rec):
    """Return the supplier to display/export: edited value if user adjusted it,
    otherwise the AI's suggested supplier."""
    if not isinstance(rec, dict):
        return ""
    edited = rec.get("edited_supplier")
    if edited not in (None, "", "null"):
        return edited
    return rec.get("supplier", "")


def _compute_order_by(rec):
    """Compute when the user must place this order to avoid a stockout.

    Returns a dict with:
      order_by_date: human-readable string like "12 Jun 2026", or None
      buffer_days:   int (negative = already overdue), or None
      status:        'overdue' | 'urgent' | 'ok' | 'unknown'
    """
    if not isinstance(rec, dict):
        return {"order_by_date": None, "buffer_days": None, "status": "unknown"}
    dos = rec.get("days_of_supply")
    lt  = rec.get("lead_time_days")
    try:
        dos = float(dos) if dos not in (None, "", "null") else None
        lt  = float(lt)  if lt  not in (None, "", "null") else None
    except (TypeError, ValueError):
        return {"order_by_date": None, "buffer_days": None, "status": "unknown"}

    if dos is None or lt is None:
        return {"order_by_date": None, "buffer_days": None, "status": "unknown"}

    buffer_days = int(round(dos - lt))
    order_by = datetime.utcnow() + timedelta(days=buffer_days)
    order_by_date = order_by.strftime("%d %b %Y")

    if buffer_days < 0:
        status = "overdue"
    elif buffer_days <= 7:
        status = "urgent"
    else:
        status = "ok"

    return {
        "order_by_date": order_by_date,
        "buffer_days":   buffer_days,
        "status":        status,
    }


def _group_recs_by_supplier(recommendations, status_by_item):
    """Group recommendations by their effective supplier (user-edited if present,
    otherwise the AI's suggestion). Returns a list of dicts, ordered with the
    most-urgent supplier first.

    Each group dict:
      - name:      supplier display name (or 'Unknown supplier' if blank)
      - key:       slug used as DOM id
      - count:     total recs in this group
      - critical:  number of CRITICAL items
      - low:       number of LOW items
      - supplier_type: 'import' / 'local' / 'other' (taken from first rec)
      - items:     list of item names (for the bulk-approve POST)
      - recs:      list of the actual rec dicts

    Groups are sorted: most critical first, then most items first, then name.
    """
    groups = {}
    for rec in recommendations:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        supplier = _effective_supplier(rec) or "Unknown supplier"
        key = supplier.strip() or "Unknown supplier"
        g = groups.get(key)
        if g is None:
            g = {
                "name":          key,
                "key":           "sg-" + "".join(c if c.isalnum() else "-" for c in key.lower())[:60],
                "count":         0,
                "critical":      0,
                "low":           0,
                "supplier_type": rec.get("supplier_type") or "other",
                "item_names":    [],
                "recs":          [],
            }
            groups[key] = g
        g["count"] += 1
        g["recs"].append(rec)
        g["item_names"].append(rec.get("item", ""))
        item_status = status_by_item.get(str(rec.get("item", "")), "")
        if item_status == "CRITICAL":
            g["critical"] += 1
        elif item_status == "LOW":
            g["low"] += 1

    ordered = sorted(
        groups.values(),
        key=lambda g: (-g["critical"], -g["count"], g["name"].lower())
    )
    return ordered


def _confidence_reasons(rec):
    """Build a short, plain-English list of reasons explaining why the AI
    settled on this confidence level. Used in the confidence-ring popover."""
    if not isinstance(rec, dict):
        return []
    reasons = []
    conf = (rec.get("confidence") or "").upper()
    if conf == "INSUFFICIENT_DATA":
        reasons.append("Supplier or sales history not in system.")

    if not rec.get("supplier") or rec.get("supplier") in ("Unknown", "unknown", "—"):
        reasons.append("Supplier not on file.")

    lt = rec.get("lead_time_days")
    if lt in (None, "", "null"):
        reasons.append("Lead time unknown — buffer based on supplier type.")
    else:
        try:
            lt_f = float(lt)
            reasons.append(f"Lead time on file: {int(lt_f)} days.")
        except (TypeError, ValueError):
            pass

    dos = rec.get("days_of_supply")
    if dos in (None, "", "null"):
        reasons.append("Stock runway unknown — limited sales history.")

    risk = rec.get("supplier_risk")
    if risk == "HIGH":
        reasons.append("Supplier flagged as high-risk (delays or unreliable).")

    flags = rec.get("flags") or []
    if isinstance(flags, list) and flags:
        for f in flags[:3]:
            if f:
                reasons.append(str(f))

    if not reasons:
        if conf == "HIGH":
            reasons.append("Strong sales history, known supplier, lead time on file.")
        elif conf in ("MED", "MEDIUM"):
            reasons.append("Some data missing — recommendation is solid but not certain.")
        elif conf == "LOW":
            reasons.append("Limited data — review before approving.")
    return reasons


# ── Recommendation approve / dismiss routes ──────────────────────────────────

@app.route("/recommend/action", methods=["POST"])
@login_required
def recommend_action():
    """Approve or dismiss a single recommendation. Optionally accepts
    edited_quantity and edited_supplier so the user's adjustments are saved
    in the same call."""
    data            = request.get_json(force=True, silent=True) or {}
    session_id      = data.get("session_id")
    item            = data.get("item", "").strip()
    action          = data.get("action", "")   # "approve" | "dismiss"
    note            = data.get("note", "").strip()
    edited_qty      = data.get("edited_quantity", None)
    edited_supplier = data.get("edited_supplier", None)

    if not session_id or not item or action not in ("approve", "dismiss"):
        return jsonify({"ok": False, "error": "Invalid parameters"}), 400

    # Ownership check
    rows = db.query("SELECT user_id FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (session_id,))
    if not ar:
        return jsonify({"ok": False, "error": "No results for this session"}), 404

    try:
        recs = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        return jsonify({"ok": False, "error": "Corrupt data"}), 500

    updated = False
    for rec in recs:
        if isinstance(rec, dict) and rec.get("item", "").strip() == item:
            rec["approved"]  = (action == "approve")
            rec["dismissed"] = (action == "dismiss")
            if note:
                rec["note"] = note
            # Save edits if they differ from the original suggestion.
            if edited_qty is not None:
                eq = str(edited_qty).strip()
                if eq and eq != str(rec.get("suggested_quantity", "")).strip():
                    rec["edited_quantity"] = eq
                elif not eq:
                    rec.pop("edited_quantity", None)
            if edited_supplier is not None:
                es = str(edited_supplier).strip()
                if es and es != str(rec.get("supplier", "")).strip():
                    rec["edited_supplier"] = es
                elif not es:
                    rec.pop("edited_supplier", None)
            updated = True
            break

    if not updated:
        return jsonify({"ok": False, "error": "Item not found in recommendations"}), 404

    db.execute(
        "UPDATE analysis_results SET recommendations_json=? WHERE session_id=?",
        (json.dumps(recs), session_id)
    )
    return jsonify({"ok": True})


@app.route("/recommend/edit", methods=["POST"])
@login_required
def recommend_edit():
    """Save the user's edited quantity and/or supplier without changing the
    approve/dismiss state. Called on blur from the inline edit inputs so the
    latest values are persisted before any approve_all is fired."""
    data            = request.get_json(force=True, silent=True) or {}
    session_id      = data.get("session_id")
    item            = data.get("item", "").strip()
    edited_qty      = data.get("edited_quantity", None)
    edited_supplier = data.get("edited_supplier", None)

    if not session_id or not item:
        return jsonify({"ok": False, "error": "Invalid parameters"}), 400

    rows = db.query("SELECT user_id FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (session_id,))
    if not ar:
        return jsonify({"ok": False, "error": "No results for this session"}), 404

    try:
        recs = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        return jsonify({"ok": False, "error": "Corrupt data"}), 500

    updated = False
    for rec in recs:
        if isinstance(rec, dict) and rec.get("item", "").strip() == item:
            if edited_qty is not None:
                eq = str(edited_qty).strip()
                if eq and eq != str(rec.get("suggested_quantity", "")).strip():
                    rec["edited_quantity"] = eq
                else:
                    rec.pop("edited_quantity", None)
            if edited_supplier is not None:
                es = str(edited_supplier).strip()
                if es and es != str(rec.get("supplier", "")).strip():
                    rec["edited_supplier"] = es
                else:
                    rec.pop("edited_supplier", None)
            updated = True
            break

    if not updated:
        return jsonify({"ok": False, "error": "Item not found"}), 404

    db.execute(
        "UPDATE analysis_results SET recommendations_json=? WHERE session_id=?",
        (json.dumps(recs), session_id)
    )
    return jsonify({"ok": True})


@app.route("/recommend/approve_all", methods=["POST"])
@login_required
def recommend_approve_all():
    """Approve non-dismissed recommendations for a session.

    If `items` is provided, only approve those specific item names (filtered subset).
    If omitted, approve all non-dismissed recommendations.
    """
    data       = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id")
    items_filter = set(data.get("items") or [])   # optional subset

    if not session_id:
        return jsonify({"ok": False, "error": "Missing session_id"}), 400

    rows = db.query("SELECT user_id FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (session_id,))
    if not ar:
        return jsonify({"ok": False, "error": "No results for this session"}), 404

    try:
        recs = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        return jsonify({"ok": False, "error": "Corrupt data"}), 500

    newly_approved = []
    for rec in recs:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        if items_filter and rec.get("item", "") not in items_filter:
            continue
        if not rec.get("dismissed") and not rec.get("approved"):
            rec["approved"] = True
            newly_approved.append(rec.get("item", ""))

    db.execute(
        "UPDATE analysis_results SET recommendations_json=? WHERE session_id=?",
        (json.dumps(recs), session_id)
    )
    return jsonify({"ok": True, "newly_approved_items": newly_approved})


@app.route("/recommend/undo_approve_all", methods=["POST"])
@login_required
def recommend_undo_approve_all():
    """Un-approve a specific list of items (the ones bulk-approved moments ago)."""
    data       = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id")
    items      = data.get("items", [])

    if not session_id:
        return jsonify({"ok": False, "error": "Missing session_id"}), 400

    rows = db.query("SELECT user_id FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (session_id,))
    if not ar:
        return jsonify({"ok": False, "error": "No results for this session"}), 404

    try:
        recs = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        return jsonify({"ok": False, "error": "Corrupt data"}), 500

    items_set = set(items)
    undone = []
    for rec in recs:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        if rec.get("item", "") in items_set and rec.get("approved"):
            rec["approved"] = False
            undone.append(rec.get("item", ""))

    db.execute(
        "UPDATE analysis_results SET recommendations_json=? WHERE session_id=?",
        (json.dumps(recs), session_id)
    )
    return jsonify({"ok": True, "undone_items": undone})


if __name__ == "__main__":
    # Local dev entry point. On Render we use gunicorn (see render.yaml).
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
