import os
import sys
import json
import secrets
import hashlib
import shutil
import threading
import time
from datetime import datetime
from email.mime.text import MIMEText
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, Response, stream_with_context, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
import anthropic as _anthropic

import database as db
import rate_limit
import validators
import quantity
import backup
from logging_setup import logger
from agents import (
    run_pipeline,
)
from agents.shared import sampling_kwargs

from config import UPLOAD_FOLDER, FILE_SLOTS, AVAILABLE_MODELS
from emails import (
    _send_critical_alert, _send_reset_email, _send_analysis_ready_email,
    _send_verification_email, _send_invite_email, _send_contact_email,
    _deliver as _deliver_email,
)
from auth_utils import (
    login_required, admin_required, analyst_required, _allowed, _verify_session_owner,
)
from rec_logic import (
    _normalise_confidence, _effective_qty, _effective_supplier,
    _compute_order_by, _group_recs_by_supplier, _confidence_reasons,
    _quantity_basis, _has_stakes,
)
from chat_logic import _build_chat_context

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

# Per-org caps on the endpoints that call the Claude API. These are wallet
# guards, not product limits: generous enough that no real workflow hits them
# (Cool Link runs 1-2 analyses/day), tight enough that a buggy retry loop or a
# rogue script can't run up hundreds of dollars before anyone notices.
ORG_ANALYSES_PER_DAY   = 10
ORG_CHAT_PER_HOUR      = 60
ORG_DEDUP_RUNS_PER_DAY = 20

# Refuse new uploads/analyses when the data disk is nearly full. A full disk
# mid-analysis dies with a cryptic SQLite I/O error; refusing up front with a
# plain message (and an ERROR in the logs) is the honest failure.
MIN_FREE_DISK_MB = 100

# The two SSE endpoints (/api/chat, /dedup/stream) each hold a gunicorn thread
# for the whole Claude stream — minutes, sometimes. With a finite thread pool,
# enough concurrent (or stuck) streams freeze EVERY route, including login and
# /health, with nothing in the logs: that was the 85-minute outage of 11 June
# 2026. The cap must stay below gunicorn's --threads so plain page loads always
# have free lanes; excess streams get a plain 503 instead of silently queueing
# the whole site to death.
#
# The cap is derived from the LIVE thread count, not assumed: render.yaml said
# 12 threads but the Render dashboard's own start command (which overrides the
# file for manually-created services) still said 4 — discovered 12 June 2026.
# Workers inherit the gunicorn master's argv, so the real --threads value is
# readable at import time. Unknown (local dev, tests) falls back to 8.


def _gunicorn_threads(argv=None):
    """The --threads value from the gunicorn command line, or None."""
    argv = sys.argv if argv is None else argv
    for i, a in enumerate(argv):
        if a == "--threads" and i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
        if a.startswith("--threads=") and a.split("=", 1)[1].isdigit():
            return int(a.split("=", 1)[1])
    return None


def _lanes_for_threads(threads):
    """Streams may hold this many of `threads` lanes: at least 4 threads stay
    free for plain pages, at least 1 stream is always allowed, never more
    than 8 (Claude API + memory headroom)."""
    return max(1, min(8, threads - 4))


_live_threads = _gunicorn_threads()
MAX_CONCURRENT_STREAMS = _lanes_for_threads(_live_threads) if _live_threads else 8
_stream_lanes = threading.BoundedSemaphore(MAX_CONCURRENT_STREAMS)
logger.info("Stream lanes: %d (gunicorn --threads: %s)",
            MAX_CONCURRENT_STREAMS, _live_threads or "not detected")
# How long a streaming Claude call may stall before we give the lane back.
# The SDK default is 10 minutes — far too long for a held thread.
CHAT_STREAM_TIMEOUT_S  = 120
DEDUP_STREAM_TIMEOUT_S = 300

# Session cookie hardening. SECURE is only enforced in production (on Render,
# which is HTTPS-only) so local http dev still works. HTTPONLY keeps JavaScript
# from reading the login cookie; SAMESITE=Lax stops it being sent on cross-site
# requests. Session length is left as a browser-session cookie (clears on close).
app.config.update(
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def _resolve_client_ip(cf_connecting_ip, x_forwarded_for, remote_addr):
    """Pick the client IP to throttle on, from proxy headers. Pure + testable.

    Trust order:
      1. CF-Connecting-IP — Cloudflare (which fronts the site) sets this to the
         true client IP, and a client can't forge it without bypassing Cloudflare
         to hit the Render origin directly. Lock the origin to Cloudflare to close
         that last gap.
      2. The RIGHTMOST X-Forwarded-For entry — the one appended by the closest
         trusted proxy, which the client can't control. The old code trusted the
         LEFTMOST entry, which is fully client-settable: an attacker just sent a
         fake first hop and rotated it to walk straight past every IP throttle.
      3. remote_addr.
    """
    if cf_connecting_ip and cf_connecting_ip.strip():
        return cf_connecting_ip.strip()
    if x_forwarded_for and x_forwarded_for.strip():
        parts = [p.strip() for p in x_forwarded_for.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return remote_addr or "unknown"


def _client_ip():
    """Best-effort client IP for throttling."""
    return _resolve_client_ip(
        request.headers.get("CF-Connecting-IP", ""),
        request.headers.get("X-Forwarded-For", ""),
        request.remote_addr,
    )


def _hash_token(token: str) -> str:
    """One-way hash for reset/verification tokens stored in the DB. The raw
    token only ever exists inside the emailed link — if the database ever
    leaks, the stored hashes can't be replayed as working links."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@app.after_request
def _set_security_headers(resp):
    """Defence-in-depth headers on every response. Chosen to block clickjacking,
    MIME-sniffing, and protocol downgrade without breaking the site: templates
    use inline <style>/<script> (hence 'unsafe-inline'), and the only external
    origin loaded anywhere is Google Fonts."""
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Browser features the app never uses — deny them outright so an injected
    # script can't quietly request camera/mic/location.
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    # Keep our windows out of cross-origin browsing groups (blocks tab-napping
    # style attacks via window.opener).
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'self'"
    )
    # Force HTTPS for a year — production only, so local http dev still works.
    if os.environ.get("RENDER"):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp





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
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@berthcast.com")
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
            (admin_email, generate_password_hash(admin_pass), "berthcast Admin", "claude-sonnet-4-6", 1)
        )

_ensure_admin()

# How long a session can sit in 'analyzing' with no in-memory progress before we
# declare it dead (the worker that was running it restarted/crashed). The progress
# dict is created synchronously before the page is served, so its absence already
# means the run is gone; the window is just a safety margin against timing races.
STUCK_ANALYSIS_SECONDS = 120

# Clean up analyses orphaned by a previous worker dying mid-run, so they don't sit
# in 'analyzing' forever. Runs once at boot.
try:
    _orphaned = db.fail_orphaned_analyses()
    if _orphaned:
        logger.warning("Marked %d interrupted analysis run(s) as failed at startup", _orphaned)
except Exception:
    logger.exception("Startup sweep of orphaned analyses failed")


def _disk_has_room() -> bool:
    """True when the data disk has at least MIN_FREE_DISK_MB free. Logs an
    ERROR when it doesn't (visible in Render logs). If the measurement itself
    fails, let work proceed — never block users on a stats hiccup."""
    try:
        free_mb = shutil.disk_usage(UPLOAD_FOLDER).free / (1024 * 1024)
    except OSError:
        return True
    if free_mb < MIN_FREE_DISK_MB:
        logger.error("Disk nearly full: %.0f MB free (threshold %d MB) — refusing new uploads/analyses",
                     free_mb, MIN_FREE_DISK_MB)
        return False
    return True


def _sweep_stale_chunks(max_age_seconds: int = 86400) -> int:
    """Delete tmp_* upload chunks older than `max_age_seconds`. A chunk only
    means something within its own upload attempt; anything older is litter
    from an interrupted upload, quietly eating the 1GB disk."""
    swept = 0
    now_ts = time.time()
    for f in os.listdir(UPLOAD_FOLDER):
        if not f.startswith("tmp_"):
            continue
        p = os.path.join(UPLOAD_FOLDER, f)
        try:
            if now_ts - os.path.getmtime(p) > max_age_seconds:
                os.remove(p)
                swept += 1
        except OSError:
            pass
    return swept


# Runs once at boot.
try:
    _swept = _sweep_stale_chunks()
    if _swept:
        logger.warning("Removed %d stale upload chunk file(s) older than 24h", _swept)
except Exception:
    logger.exception("Stale upload-chunk sweep failed")


# Backup failures used to be log-only — nobody reads logs, so every snapshot
# could silently be weeks old. Now a failure emails ALERT_EMAIL (if set), at
# most once per day so a stuck disk can't flood the inbox.
_last_backup_alert = {"t": 0.0}


def _backup_failure_alert(error_text: str) -> None:
    alert_to = os.environ.get("ALERT_EMAIL", "")
    sender   = os.environ.get("MAIL_SENDER", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    if not alert_to or not sender or not password:
        return
    if time.time() - _last_backup_alert["t"] < 86400:
        return
    _last_backup_alert["t"] = time.time()
    msg = MIMEText(
        "A berthcast database backup just failed.\n\n"
        f"Error: {error_text}\n\n"
        "Check the Render disk (it may be full) and the logs. Until this is "
        "fixed, the newest on-disk snapshot is getting older every day."
    )
    msg["Subject"] = "berthcast backup FAILED"
    msg["From"]    = sender
    msg["To"]      = alert_to
    _deliver_email(msg, sender, password, alert_to)


# Automatic database backups — production only. Local dev has nothing to protect
# and doesn't stay running. Snapshots land in a backups/ folder on the persistent
# disk, beside the DB; the founder pulls an off-disk copy via the admin panel.
if os.environ.get("RENDER"):
    backup.start_backup_scheduler(
        db.DB_PATH,
        backup.default_backups_dir(db.DB_PATH),
        on_failure=_backup_failure_alert,
        logger=logger.info,
    )


@app.context_processor
def inject_live_stats():
    """Provide live ticker stats to every template. Fails silently if anything's wrong."""
    if "user_id" not in session:
        return {"live_stats": None}
    try:
        sessions = db.query(
            "SELECT id, created_at FROM upload_sessions WHERE org_name=? AND status='complete' "
            "ORDER BY created_at DESC LIMIT 1",
            (session["org_name"],)
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


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/data")
def data_promise():
    return render_template("data.html")


# ── Friendly error pages ─────────────────────────────────────────────────────
# Without these, Flask serves bare default pages. JSON endpoints are unaffected:
# _verify_session_owner aborts with a ready-made JSON response, which bypasses
# these handlers.

@app.errorhandler(403)
def _error_403(e):
    return render_template("error.html", code=403, title="No access to that page",
        message="Your account doesn't have access to this. If you think it should, ask your organisation's admin."), 403


@app.errorhandler(404)
def _error_404(e):
    return render_template("error.html", code=404, title="Page not found",
        message="That page doesn't exist or has moved. Check the address, or head back and try from there."), 404


@app.errorhandler(500)
def _error_500(e):
    logger.error("Unhandled server error on %s %s", request.method, request.path)
    return render_template("error.html", code=500, title="Something went wrong",
        message="The error is on our side and has been logged. Please try again in a moment."), 500


@app.route("/robots.txt")
def robots_txt():
    """Keep crawlers on the public pages and out of the app routes (which all
    redirect to login anyway — this just keeps them out of the index)."""
    return Response(
        "User-agent: *\n"
        "Disallow: /dashboard\n"
        "Disallow: /upload\n"
        "Disallow: /results\n"
        "Disallow: /chat\n"
        "Disallow: /admin\n"
        "Disallow: /settings\n"
        "Disallow: /suppliers\n"
        "Disallow: /analyse\n"
        "Disallow: /api/\n"
        "Allow: /\n",
        mimetype="text/plain",
    )


@app.route("/.well-known/security.txt")
def security_txt():
    """Standard contact point for security researchers (RFC 9116)."""
    return Response(
        "Contact: mailto:admin@berthcast.com\n"
        "Expires: 2027-06-30T00:00:00.000Z\n"
        "Preferred-Languages: en\n"
        "Canonical: https://berthcast.com/.well-known/security.txt\n",
        mimetype="text/plain",
    )


# ── Health check ──────────────────────────────────────────────────────────────
# The probe must read a REAL table, not SELECT 1. On 12 June 2026 the Render
# disk wedged after a plan change: cached pages (SELECT 1, the DB header) read
# fine, so /health kept answering 200 while every login hung forever on an
# uncached page read — Render saw a healthy service and never restarted the
# zombie. A read stuck inside the OS can't be interrupted from Python, so the
# probe runs in a side thread we ABANDON after a bounded wait instead.
HEALTH_DB_PROBE_TIMEOUT_S = 5
_health_lock = threading.Lock()
_health_probe = None  # in-flight probe: (done Event, result dict, started ts)


def _health_db_probe(done, result):
    """Touches the users table — the same page a sign-in needs."""
    global _health_probe
    try:
        db.query("SELECT id FROM users LIMIT 1")
        result["ok"] = True
    except Exception:
        logger.exception("Health probe failed: users table unreadable")
        result["ok"] = False
    finally:
        with _health_lock:
            _health_probe = None
        done.set()


@app.route("/health")
def health():
    """Liveness + real DB readability for Render's monitor. No auth — the
    platform hits it unauthenticated. 200 = data actually serves, 503 = it
    doesn't. Single-flight: concurrent polls share one probe, and while a
    probe is stuck past its deadline we answer 503 instantly — a wedged disk
    leaks at most ONE abandoned thread, never one per poll."""
    global _health_probe
    with _health_lock:
        probe = _health_probe
        if probe is None:
            probe = (threading.Event(), {}, time.time())
            _health_probe = probe
            threading.Thread(target=_health_db_probe,
                             args=(probe[0], probe[1]), daemon=True).start()
    done, result, started = probe
    remaining = HEALTH_DB_PROBE_TIMEOUT_S - (time.time() - started)
    if remaining > 0:
        done.wait(remaining)
    if result.get("ok"):
        return jsonify({"status": "ok"}), 200
    return jsonify({"status": "error"}), 503


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("chat"))
    if request.method == "POST":
        ip       = _client_ip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        # Throttle by client IP AND by the targeted account, so rotating IPs
        # doesn't buy an attacker unlimited guesses at one mailbox.
        acct_key = f"acct:{email}" if email else None
        if rate_limit.is_locked(ip) or (acct_key and rate_limit.is_locked(acct_key)):
            secs = max(rate_limit.seconds_until_unlock(ip),
                       rate_limit.seconds_until_unlock(acct_key) if acct_key else 0)
            mins = max(1, secs // 60)
            flash(f"Too many failed sign-in attempts. Please wait about "
                  f"{mins} minute{'s' if mins != 1 else ''} and try again.", "error")
            return render_template("login.html")
        users = db.query("SELECT * FROM users WHERE email=?", (email,))
        if users and check_password_hash(users[0]["password_hash"], password):
            u = users[0]
            if not u["email_verified"]:
                flash("Please verify your email before signing in. Check your inbox for the verification link.", "error")
                return render_template("login.html")
            rate_limit.clear(ip)
            if acct_key:
                rate_limit.clear(acct_key)
            session["user_id"]  = u["id"]
            session["email"]    = u["email"]
            session["org_name"] = u["org_name"]
            session["model"]    = u["model"]
            session["is_admin"] = bool(u["is_admin"])
            session["tier"]     = u["tier"]
            session["role"]     = u.get("role") or "admin"
            return redirect(url_for("admin_panel") if u["is_admin"] else url_for("chat"))
        rate_limit.record_failure(ip)
        if acct_key:
            rate_limit.record_failure(acct_key)
        flash("Incorrect email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ── Password reset ────────────────────────────────────────────────────────────











@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("chat"))
    if request.method == "POST":
        if rate_limit.hit(f"register:{_client_ip()}", 8, 900):
            return render_template("register.html",
                error="Too many sign-up attempts from your network. Please wait a few minutes and try again.")
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
            else:
                # Org name is the tenant boundary, so a self-registration must
                # never land inside an existing organisation. Colleagues join an
                # existing org through the admin invite flow, not here — so a
                # name that already exists is either a clash or an attempt to
                # piggyback on someone else's data. Refuse it (case/space-insensitive).
                org_taken = db.query(
                    "SELECT id FROM users WHERE LOWER(TRIM(org_name)) = LOWER(TRIM(?)) LIMIT 1",
                    (org_name,)
                )
                if org_taken:
                    error = ("An organisation with that name is already registered. "
                             "If you're joining a colleague's account, ask their admin to invite you. "
                             "Otherwise please use a more specific name — for example, add your "
                             "full company name or city.")

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
            # Issue verification token and email it. Only the HASH is stored —
            # the raw token lives solely inside the emailed link.
            db.execute(
                "DELETE FROM email_verification_tokens WHERE user_id=?",
                (new_user["id"],)
            )
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO email_verification_tokens (user_id, token) VALUES (?,?)",
                (new_user["id"], _hash_token(token))
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
        session["role"]     = "admin"
        flash("Account created. Welcome to berthcast.", "success")
        return redirect(url_for("chat"))

    return render_template("register.html")


@app.route("/verify-email/<token>")
def verify_email(token):
    rows = db.query(
        """SELECT evt.user_id
           FROM email_verification_tokens evt
           WHERE evt.token=?
             AND evt.created_at >= datetime('now', '-24 hours')""",
        (_hash_token(token),)
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
        # Over the limit: show the same neutral page but send nothing. This
        # throttles reset-email bombing without revealing whether an email exists.
        if rate_limit.hit(f"forgot:{_client_ip()}", 5, 900):
            return render_template("forgot_password.html", submitted=True)
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
                # Store only the hash — a DB leak must not yield working links.
                db.execute(
                    "INSERT INTO password_reset_tokens (user_id, token) VALUES (?,?)",
                    (users[0]["id"], _hash_token(token))
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

    # Validate token — must exist (compared by hash) and be less than 1 hour old
    rows = db.query(
        """SELECT prt.id, prt.user_id
           FROM password_reset_tokens prt
           WHERE prt.token=?
             AND prt.created_at >= datetime('now', '-1 hour')""",
        (_hash_token(token),)
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
        db.execute("DELETE FROM password_reset_tokens WHERE token=?", (_hash_token(token),))
        flash("Password updated. Sign in with your new password.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", invalid=False, token=token)


# ── Chat ─────────────────────────────────────────────────────────────────────



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

    if rate_limit.hit(f"chat:{session['org_name']}", ORG_CHAT_PER_HOUR, 3600):
        return jsonify({"error": "Your team has sent a lot of messages in the last hour. "
                                 "Please wait a little while and try again."}), 429

    # Tier limit: free users get 20 chat messages total
    if session.get("tier") == "free":
        cu = db.query("SELECT chat_messages_used FROM users WHERE id=?", (session["user_id"],))
        if cu and cu[0]["chat_messages_used"] >= 20:
            return jsonify({"error": "Free accounts include 20 chat messages. Upgrade to continue chatting."}), 403

    # Verify the conversation exists (read-only; writes happen after the lane
    # is claimed, so a refused request leaves no half-written rows behind).
    if conversation_id:
        rows = db.query(
            "SELECT id FROM chat_conversations WHERE id=? AND (org_name=? OR (org_name IS NULL AND user_id=?))",
            (conversation_id, session["org_name"], session["user_id"])
        )
        if not rows:
            return jsonify({"error": "Conversation not found"}), 404

    # Claim a stream lane. The SSE response holds a gunicorn thread for the
    # whole Claude stream; uncapped, enough concurrent/stuck streams freeze
    # every route (the 11 June 2026 outage). Released via call_on_close below;
    # everything between here and the Response is guarded so the lane can
    # never leak on an exception.
    if not _stream_lanes.acquire(blocking=False):
        return jsonify({"error": "The server is busy with other live requests right now. "
                                 "Please try again in a minute."}), 503
    try:
        is_new_conv = False
        if not conversation_id:
            is_new_conv = True
            conversation_id = db.execute(
                "INSERT INTO chat_conversations (user_id, title, org_name) VALUES (?,?,?)",
                (session["user_id"], "New conversation", session["org_name"])
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
    except Exception:
        _stream_lanes.release()
        raise

    base_system = (
        "You are berthcast, an AI inventory advisor for {org}. "
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
            "how berthcast works and guide them through uploading their data."
        )

    def generate():
        # Explicit timeout: the SDK default (10 min) would pin this gunicorn
        # thread for the full duration if the API stalls mid-stream.
        _client = _anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"),
                                       timeout=CHAT_STREAM_TIMEOUT_S)
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

    resp = Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    # call_on_close fires when the WSGI server closes the response — normal
    # end, client disconnect, or error — so the lane can never leak.
    resp.call_on_close(_stream_lanes.release)
    return resp


@app.route("/api/chat/conversations")
@login_required
def chat_conversations():
    q = request.args.get("q", "").strip()
    org = session["org_name"]
    if q:
        convs = db.query(
            "SELECT id, title, created_at, pinned FROM chat_conversations "
            "WHERE (org_name=? OR (org_name IS NULL AND user_id=?)) AND title LIKE ? "
            "ORDER BY pinned DESC, created_at DESC LIMIT 50",
            (org, session["user_id"], f"%{q}%")
        )
    else:
        convs = db.query(
            "SELECT id, title, created_at, pinned FROM chat_conversations "
            "WHERE (org_name=? OR (org_name IS NULL AND user_id=?)) "
            "ORDER BY pinned DESC, created_at DESC LIMIT 50",
            (org, session["user_id"])
        )
    return jsonify([dict(c) for c in convs])


@app.route("/api/chat/conversation/<int:conv_id>")
@login_required
def chat_conversation(conv_id):
    rows = db.query("SELECT user_id, org_name FROM chat_conversations WHERE id=?", (conv_id,))
    if not rows:
        return jsonify({"error": "Not found"}), 404
    row = rows[0]
    if row["org_name"] and row["org_name"] != session.get("org_name"):
        return jsonify({"error": "Not found"}), 404
    if not row["org_name"] and row["user_id"] != session.get("user_id"):
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
    rows = db.query("SELECT user_id, org_name FROM chat_conversations WHERE id=?", (conv_id,))
    if not rows:
        return jsonify({"error": "Not found"}), 404
    row = rows[0]
    if row["org_name"] and row["org_name"] != session.get("org_name"):
        return jsonify({"error": "Not found"}), 404
    if not row["org_name"] and row["user_id"] != session.get("user_id"):
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
    rows = db.query("SELECT user_id, org_name FROM chat_conversations WHERE id=?", (conv_id,))
    if not rows:
        return jsonify({"error": "Not found"}), 404
    row = rows[0]
    if row["org_name"] and row["org_name"] != session.get("org_name"):
        return jsonify({"error": "Not found"}), 404
    if not row["org_name"] and row["user_id"] != session.get("user_id"):
        return jsonify({"error": "Not found"}), 404
    db.execute("DELETE FROM chat_messages WHERE conversation_id=?", (conv_id,))
    db.execute("DELETE FROM chat_conversations WHERE id=?", (conv_id,))
    return jsonify({"ok": True})




@app.route("/contact", methods=["GET", "POST"])
def contact():
    """Public contact form. Stores submission in DB and emails the berthcast inbox."""
    if request.method == "POST":
        if rate_limit.hit(f"contact:{_client_ip()}", 5, 900):
            flash("You've sent several messages already. Please wait a few minutes before sending another.", "error")
            return render_template("contact.html")
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
        elif action == "change_email":
            # Fix an account's email (e.g. one created with the wrong address).
            # Validation lives in validators.validate_email_change: it rejects
            # blanks/malformed addresses and refuses an email already used by a
            # different account, so this can't be used to hijack another login.
            uid = request.form.get("user_id")
            try:
                target_id = int(uid)
            except (TypeError, ValueError):
                flash("Couldn't identify which account to update.", "error")
            else:
                def _owner(addr):
                    rows = db.query("SELECT id FROM users WHERE email=?", (addr,))
                    return rows[0]["id"] if rows else None
                normalized, err = validators.validate_email_change(
                    request.form.get("new_email", ""), target_id, _owner)
                if err:
                    flash(err, "error")
                else:
                    db.execute("UPDATE users SET email=? WHERE id=?", (normalized, target_id))
                    flash(f"Email updated to {normalized}.", "success")
        elif action == "set_password":
            # Admin sets a new password for an account (e.g. a locked-out user
            # whose reset email can't reach them). Held to the same 8-char
            # minimum as sign-up; admin relays it and asks the user to change it.
            uid = request.form.get("user_id")
            try:
                target_id = int(uid)
            except (TypeError, ValueError):
                flash("Couldn't identify which account to update.", "error")
            else:
                new_password = request.form.get("new_password", "")
                err = validators.password_error(new_password)
                if err:
                    flash(err, "error")
                else:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (generate_password_hash(new_password), target_id))
                    flash("Password updated. Share it with the user and ask them "
                          "to change it after signing in.", "success")
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


@app.route("/admin/backup/download")
@login_required
@admin_required
def admin_backup_download():
    """Stream a fresh, consistent snapshot of the whole database to the admin's
    machine. This is the off-disk copy that survives a total loss of the Render
    disk. Making it also retains the snapshot on disk (then prunes old ones)."""
    backups_dir = backup.default_backups_dir(db.DB_PATH)
    try:
        # A full disk must not stop the founder pulling an off-disk copy —
        # clear old snapshots first if the new one wouldn't fit.
        backup.make_room(backups_dir,
                         backup._db_size(db.DB_PATH) + backup.FREE_SPACE_MARGIN_BYTES)
        path = backup.backup_database(db.DB_PATH, backups_dir)
        backup.prune_backups(backups_dir)
    except Exception as e:
        flash(f"Could not create a backup: {e}", "error")
        return redirect(url_for("admin_panel"))
    return send_file(
        path,
        as_attachment=True,
        download_name=os.path.basename(path),
        mimetype="application/octet-stream",
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

        elif action == "save_company":
            # Industry + description feed the AI prompts (spoilage rules,
            # consequence examples, normalisation context). Without these a
            # new org silently runs on food-distribution-flavoured defaults.
            if session.get("role") != "admin":
                flash("Only admins can change company details.", "error")
            else:
                allowed_industries = ("food_distribution", "beverage", "fmcg",
                                      "general", "other")
                ind  = request.form.get("industry", "").strip()
                desc = request.form.get("company_description", "").strip()[:500]
                if ind not in allowed_industries:
                    flash("Pick an industry from the list.", "error")
                else:
                    db.upsert_company_config(
                        org, industry=ind, company_description=desc or None)
                    flash("Company details saved — the AI will use them from the next analysis.", "success")

        # ── Team management (admin role only) ─────────────────────────────
        elif action == "invite_user":
            if session.get("role") != "admin":
                flash("Only admins can invite team members.", "error")
            else:
                inv_email = request.form.get("invite_email", "").strip().lower()
                inv_role  = request.form.get("invite_role", "reviewer")
                if inv_role not in ("reviewer", "viewer"):
                    inv_role = "reviewer"
                if not inv_email or "@" not in inv_email:
                    flash("Enter a valid email address.", "error")
                else:
                    existing = db.query("SELECT id FROM users WHERE email=?", (inv_email,))
                    if existing:
                        flash("That email is already registered.", "error")
                    else:
                        temp_pw = secrets.token_urlsafe(10)
                        db.execute(
                            """INSERT INTO users
                               (email, password_hash, org_name, model, tier,
                                email_verified, role, analyses_used, chat_messages_used)
                               VALUES (?,?,?,?,?,1,?,0,0)""",
                            (inv_email, generate_password_hash(temp_pw), org,
                             session["model"], session.get("tier", "enterprise"), inv_role)
                        )
                        login_url = url_for("login", _external=True)
                        threading.Thread(
                            target=_send_invite_email,
                            args=(inv_email, org, temp_pw, login_url),
                            daemon=True,
                        ).start()
                        flash(f"Invited {inv_email} as {inv_role}. They'll receive an email with login details.", "success")

        elif action == "remove_user":
            if session.get("role") != "admin":
                flash("Only admins can remove team members.", "error")
            else:
                rm_id = request.form.get("remove_user_id")
                if rm_id and int(rm_id) == session["user_id"]:
                    flash("You can't remove yourself.", "error")
                elif rm_id:
                    # Don't remove the last admin
                    target = db.query("SELECT role, org_name FROM users WHERE id=?", (rm_id,))
                    if not target or target[0]["org_name"] != org:
                        flash("User not found in your org.", "error")
                    else:
                        if target[0]["role"] == "admin":
                            admin_count = db.query(
                                "SELECT COUNT(*) as c FROM users WHERE org_name=? AND role='admin'",
                                (org,)
                            )
                            if admin_count and admin_count[0]["c"] <= 1:
                                flash("Can't remove the last admin.", "error")
                            else:
                                db.execute("DELETE FROM users WHERE id=? AND is_admin=0", (rm_id,))
                                flash("Team member removed.", "success")
                        else:
                            db.execute("DELETE FROM users WHERE id=? AND is_admin=0", (rm_id,))
                            flash("Team member removed.", "success")

        elif action == "change_role":
            if session.get("role") != "admin":
                flash("Only admins can change roles.", "error")
            else:
                cr_id   = request.form.get("change_user_id")
                cr_role = request.form.get("new_role", "")
                if cr_role not in ("admin", "reviewer", "viewer"):
                    flash("Invalid role.", "error")
                elif cr_id and int(cr_id) == session["user_id"]:
                    flash("You can't change your own role.", "error")
                elif cr_id:
                    target = db.query("SELECT role, org_name FROM users WHERE id=?", (cr_id,))
                    if not target or target[0]["org_name"] != org:
                        flash("User not found in your org.", "error")
                    else:
                        # If demoting the last admin, block it
                        if target[0]["role"] == "admin" and cr_role != "admin":
                            admin_count = db.query(
                                "SELECT COUNT(*) as c FROM users WHERE org_name=? AND role='admin'",
                                (org,)
                            )
                            if admin_count and admin_count[0]["c"] <= 1:
                                flash("Can't demote the last admin.", "error")
                            else:
                                db.execute("UPDATE users SET role=? WHERE id=?", (cr_role, cr_id))
                                flash("Role updated.", "success")
                        else:
                            db.execute("UPDATE users SET role=? WHERE id=?", (cr_role, cr_id))
                            flash("Role updated.", "success")

        return redirect(url_for("user_settings"))

    profiles = db.get_supplier_profiles(org)
    company_cfg = db.get_company_config(org)

    # Team members (only loaded for admin role, but harmless otherwise)
    team_members = []
    if session.get("role") == "admin":
        team_members = db.query(
            "SELECT id, email, role, created_at FROM users WHERE org_name=? AND is_admin=0 ORDER BY created_at ASC",
            (org,)
        )

    return render_template("settings.html", profiles=profiles, company_cfg=company_cfg,
                           team_members=team_members)


@app.route("/dashboard")
@login_required
def dashboard():
    # Load all completed sessions for this org (most recent first, cap at 50)
    all_sessions = db.query(
        "SELECT * FROM upload_sessions WHERE org_name=? AND status='complete' ORDER BY created_at DESC LIMIT 50",
        (session["org_name"],)
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

    # Last synced relative time
    last_synced = None
    if last_session:
        try:
            ts = datetime.fromisoformat(str(last_session["created_at"]).replace("Z","").split(".")[0])
            diff = datetime.utcnow() - ts
            mins = max(0, int(diff.total_seconds() / 60))
            if mins < 1:
                last_synced = {"text": "Just now", "status": "fresh"}
            elif mins < 60:
                last_synced = {"text": f"{mins}m ago", "status": "fresh"}
            elif mins < 60 * 24:
                h = mins // 60
                last_synced = {"text": f"{h}h ago", "status": "fresh" if h < 12 else "stale"}
            else:
                d = mins // (60 * 24)
                last_synced = {"text": f"{d}d ago", "status": "stale" if d > 3 else "ok"}
            last_synced["date"] = str(last_session["created_at"])[:16]
        except Exception:
            last_synced = {"text": str(last_session["created_at"])[:10], "status": "ok", "date": ""}

    # Outcome tracking stats for ROI card
    outcome_stats = db.get_outcome_stats(session["org_name"]) if past_sessions else None

    # Supplier scores for dashboard
    supplier_scores = db.get_supplier_scores(session["org_name"]) if past_sessions else []

    return render_template(
        "dashboard.html",
        last_session=last_session,
        stats=stats,
        past_sessions=past_sessions,
        user_usage=user_usage,
        last_synced=last_synced,
        outcome_stats=outcome_stats,
        supplier_scores=supplier_scores,
    )


@app.route("/upload/start")
@login_required
@analyst_required
def upload_start():
    """Entry point for new analysis. Offer choice: reuse last data, or upload fresh."""
    rows = db.query(
        "SELECT * FROM upload_sessions WHERE org_name=? AND status='complete' "
        "ORDER BY created_at DESC LIMIT 1",
        (session["org_name"],)
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
@analyst_required
def upload_use_previous(source_id):
    """Clone tables from a previous complete session into a fresh uploading session."""
    _verify_session_owner(source_id)

    src = db.query("SELECT * FROM upload_sessions WHERE id=?", (source_id,))
    if not src or src[0]["status"] != "complete":
        flash("Cannot use that previous session.", "error")
        return redirect(url_for("upload"))

    # Discard any in-progress upload session for this org — clean slate.
    existing = db.query(
        "SELECT id FROM upload_sessions WHERE org_name=? AND status='uploading'",
        (session["org_name"],)
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
        except Exception:
            logger.exception("Clone of slot %s failed (source %s -> %s)", slot, source_id, new_id)
            failed.append(slot)

    if failed:
        flash("Some files could not be copied: " + ", ".join(failed) +
              ". Please re-upload them.", "error")
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
@analyst_required
def upload():
    upload_sessions = db.query(
        "SELECT * FROM upload_sessions WHERE org_name=? AND status='uploading' ORDER BY created_at DESC LIMIT 1",
        (session["org_name"],)
    )
    if upload_sessions:
        upload_session_id = upload_sessions[0]["id"]
    else:
        upload_session_id = db.execute(
            "INSERT INTO upload_sessions (user_id, org_name, status) VALUES (?,?,?)",
            (session["user_id"], session["org_name"], "uploading")
        )

    if request.method == "POST":
        if not _disk_has_room():
            return jsonify({"ok": False, "error": "Server storage is full — the team has "
                            "been notified. Please try again later."})
        slot = request.form.get("slot")
        if slot not in FILE_SLOTS:
            return jsonify({"ok": False, "error": "Unknown file slot."})

        # ── Chunked upload path ────────────────────────────────────────────────
        chunk_index  = request.form.get("chunk_index")
        total_chunks = request.form.get("total_chunks")
        upload_id    = request.form.get("upload_id", "")
        orig_name    = request.form.get("filename", "file.xlsx")

        if chunk_index is not None:
            # Bound + type-check the chunk counters. A forged total_chunks used to
            # make the server loop billions of times — one request could pin a
            # worker thread for minutes and freeze the whole site; a non-numeric
            # value crashed with an unhandled int() error. Both now fail cleanly.
            meta, meta_err = validators.validate_chunk_meta(chunk_index, total_chunks)
            if meta_err:
                return jsonify({"ok": False, "error": meta_err})
            chunk_index, total_chunks = meta

            # Sanitise the client-supplied upload id before it reaches a filesystem
            # path. Unsanitised, a value like "x/../../.." escaped the upload folder
            # and let an authenticated user write bytes anywhere the worker can
            # reach (path traversal / arbitrary file write).
            safe_upload_id = validators.sanitize_upload_id(upload_id)
            if not safe_upload_id:
                return jsonify({"ok": False, "error": "Invalid upload id."})

            # The chunk path skipped the extension check the single-file path has.
            # Enforce it here too so only .xlsx/.csv can ever be assembled.
            if not _allowed(orig_name):
                return jsonify({"ok": False, "error": "Please upload a .xlsx or .csv file."})

            chunk_data = request.files.get("chunk")
            if not chunk_data:
                return jsonify({"ok": False, "error": "No chunk received."})

            # Save this chunk to a temp file
            chunk_path = os.path.join(UPLOAD_FOLDER, f"tmp_{safe_upload_id}_{chunk_index}")
            chunk_data.save(chunk_path)

            # Count received chunks. total_chunks is now bounded, so this loop is
            # cheap and the exact-index check keeps assembly correct.
            received = sum(
                1 for i in range(total_chunks)
                if os.path.exists(os.path.join(UPLOAD_FOLDER, f"tmp_{safe_upload_id}_{i}"))
            )

            if received < total_chunks:
                # More chunks to come — acknowledge and wait
                return jsonify({"ok": True, "chunk_received": chunk_index})

            # All chunks received — assemble into final file
            safe_name = secure_filename(orig_name)
            filepath  = os.path.join(UPLOAD_FOLDER, f"{upload_session_id}_{slot}_{safe_name}")
            with open(filepath, "wb") as out:
                for i in range(total_chunks):
                    cp = os.path.join(UPLOAD_FOLDER, f"tmp_{safe_upload_id}_{i}")
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
        try:
            result = db.excel_to_sqlite(filepath, table, session_id)
            if result.get("ok"):
                db.set_conversion_status(session_id, slot, "done", rows_count=result.get("rows", 0))
                names_row = db.query("SELECT file_names_json FROM upload_sessions WHERE id=?", (session_id,))
                names = json.loads(names_row[0]["file_names_json"] or "{}") if names_row and names_row[0]["file_names_json"] else {}
                names[slot] = orig_name
                db.execute("UPDATE upload_sessions SET file_names_json=? WHERE id=?", (json.dumps(names), session_id))
            else:
                logger.warning("File conversion failed (session %s, slot %s): %s",
                               session_id, slot, result.get("error"))
                db.set_conversion_status(session_id, slot, "error", error=result.get("error", "Unknown error"))
        except Exception:
            logger.exception("File conversion crashed (session %s, slot %s)", session_id, slot)
            db.set_conversion_status(session_id, slot, "error", error="Processing failed unexpectedly.")
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
@analyst_required
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
@analyst_required
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
    except Exception:
        logger.exception("remove_upload failed (session %s, slot %s)", session_id, slot)
        return jsonify({"ok": False, "error": "Could not remove that file. Please try again."})
    return jsonify({"ok": True})


@app.route("/context/<int:upload_session_id>", methods=["GET", "POST"])
@login_required
@analyst_required
def context_form(upload_session_id):
    _verify_session_owner(upload_session_id)
    if request.method == "POST":
        context = {
            "delayed_suppliers": request.form.get("delayed_suppliers", "").strip(),
            "large_orders":      request.form.get("large_orders", "").strip(),
            "discontinue":       request.form.get("discontinue", "").strip(),
            "other":             request.form.get("other", "").strip(),
        }
        # Column mapping is no longer confirmed here — the inventory agent maps
        # columns itself (LLM proposal validated in Python, keyword fallback)
        # and records its choice in column_map_json. The confirm step confused
        # the people actually running analyses, and a wrong click silently
        # corrupted every number downstream.
        db.execute(
            "UPDATE upload_sessions SET context_json=?, "
            "status='pending', dedup_confirmed=0 WHERE id=?",
            (json.dumps(context), upload_session_id)
        )
        return redirect(url_for("dedup_loading", upload_session_id=upload_session_id))

    return render_template("context_form.html", upload_session_id=upload_session_id)


@app.route("/dedup/loading/<int:upload_session_id>")
@login_required
@analyst_required
def dedup_loading(upload_session_id):
    _verify_session_owner(upload_session_id)
    return render_template("dedup_loading.html", upload_session_id=upload_session_id)


@app.route("/dedup/stream/<int:upload_session_id>")
@login_required
@analyst_required
def dedup_stream(upload_session_id):
    """SSE endpoint — streams Claude tokens for the normalisation agent in real time."""
    _verify_session_owner(upload_session_id)

    # Already scanned this session? Serve the cached result instantly. This is
    # what makes a reconnect (page refresh, phone waking up, EventSource retry)
    # FREE — without it every reconnect re-ran the whole multi-minute Claude
    # call on a fresh thread, which is how a few locked iPhones froze the site.
    # Checked before the rate cap so reconnects don't burn the daily allowance.
    _cached = normalization_cache.get(upload_session_id)
    if _cached is not None:
        _payload = (
            f"data: {json.dumps({'type': 'status', 'count': len(_cached.get('groups', []))})}\n\n"
            f"data: {json.dumps({'type': 'done', 'count': len(_cached.get('groups', []))})}\n\n"
        )
        return Response(_payload, mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    model = session["model"]
    # Resolved here, not inside the generator — session isn't reliably
    # available once the SSE response is streaming. Also resolved BEFORE the
    # lane is claimed: nothing throwable may sit between acquire and Response,
    # or an exception would leak the lane permanently.
    _company_desc = (db.get_company_config(session["org_name"]).get("company_description")
                     or session["org_name"])

    # Lane before cap: a "server busy" rejection must not burn the daily cap.
    if not _stream_lanes.acquire(blocking=False):
        return jsonify({"error": "The server is busy with other live requests right now. "
                                 "Please try again in a minute."}), 503
    if rate_limit.hit(f"dedup:{session['org_name']}", ORG_DEDUP_RUNS_PER_DAY, 86400):
        _stream_lanes.release()
        return jsonify({"error": "Daily limit for re-running item matching reached. "
                                 "Please try again tomorrow."}), 429

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
            f"You are a data normalisation specialist for: {_company_desc}\n"
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
                api_key=os.environ.get("ANTHROPIC_API_KEY"),
                # SDK default is 10 min; a stalled call must not pin this
                # thread that long. 5 min covers the largest real catalogue.
                timeout=DEDUP_STREAM_TIMEOUT_S,
            ).messages.stream(
                model=model,
                max_tokens=8000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                **sampling_kwargs(model),
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

    resp = Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    # Fires when the WSGI server closes the response — normal end, client
    # disconnect, or error — so the lane can never leak.
    resp.call_on_close(_stream_lanes.release)
    return resp


@app.route("/dedup/<int:upload_session_id>", methods=["GET", "POST"])
@login_required
@analyst_required
def dedup_review(upload_session_id):
    _verify_session_owner(upload_session_id)
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
    # Read (don't consume) the result cached by the streaming loading page.
    # This used to POP the cache and, on a miss, run the whole normalisation
    # agent synchronously on this request thread — so one F5 on this page held
    # a gunicorn lane for minutes with no progress UI, and a few refreshes
    # froze the entire site (the 11 June 2026 outage). Now: cache hit renders
    # instantly on every refresh; cache miss goes back to the loading page,
    # which streams the scan with progress and a lane cap.
    cached = normalization_cache.get(upload_session_id)
    if cached is None:
        return redirect(url_for("dedup_loading", upload_session_id=upload_session_id))
    groups  = cached["groups"]
    message = cached.get("message", "")
    return render_template("dedup_review.html", groups=groups, message=message, upload_session_id=upload_session_id)


@app.route("/analyse/<int:upload_session_id>")
@login_required
@analyst_required
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

    if not _disk_has_room():
        flash("Server storage is full — the team has been notified. Please try again later.", "error")
        return redirect(url_for("dashboard"))

    # Counted here, after the in-progress short-circuit, so refreshing the
    # progress page never burns an analysis from the daily allowance.
    if rate_limit.hit(f"analyse:{session['org_name']}", ORG_ANALYSES_PER_DAY, 86400):
        flash("Your team has reached today's analysis limit. Please try again tomorrow.", "error")
        return redirect(url_for("dashboard"))

    # Otherwise kick off a new run in the background.
    model       = session["model"]
    _user_id    = session["user_id"]
    _user_tier  = user_tier
    base_url    = request.host_url.rstrip("/")  # e.g. "https://berthcast.onrender.com"
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

            # ── Run the agent pipeline (inventory -> recommendation) ─────────
            # The orchestrator ("big boss") owns the agent sequence and progress
            # markers; this route keeps the DB/email/notification glue below.
            result = run_pipeline(
                upload_session_id, model, confirmed_groups, context,
                emit=_emit, mark=_mark_agent,
            )
            if "error" in result:
                # A BLOCK is the safety net refusing a file it can't read, not a
                # crash. Log it quietly and show the user the plain reason; a real
                # failure is logged as an error.
                if result.get("blocked"):
                    logger.info("Analysis %s blocked by data safety net: %s",
                                upload_session_id, result["error"])
                else:
                    logger.error("Analysis %s failed: %s", upload_session_id, result["error"])
                db.execute("UPDATE upload_sessions SET status='failed' WHERE id=?", (upload_session_id,))
                with analysis_progress_lock:
                    analysis_progress[upload_session_id]["status"] = "error"
                    analysis_progress[upload_session_id]["error"]  = result["error"]
                    analysis_progress[upload_session_id]["blocked"] = bool(result.get("blocked"))
                return

            inventory_report = result["inventory_report"]
            recommendations  = result["recommendations"]

            db.execute(
                "UPDATE analysis_results SET inventory_report=?, recommendations_json=?, "
                "data_notes=? WHERE session_id=?",
                (json.dumps(inventory_report), json.dumps(recommendations),
                 json.dumps(result.get("data_notes") or []), upload_session_id)
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
                sess_meta = db.query("SELECT user_id, org_name FROM upload_sessions WHERE id=?", (upload_session_id,))
                if sess_meta:
                    uid = sess_meta[0]["user_id"]
                    _org = sess_meta[0]["org_name"]
                    prev = db.query(
                        "SELECT id FROM upload_sessions "
                        "WHERE org_name=? AND status='complete' AND id!=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (_org, upload_session_id)
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
                # Never let alert logic break the analysis — but don't hide it.
                logger.warning("Critical-stock alert step failed for session %s",
                               upload_session_id, exc_info=True)

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
                # Email failure must not block the analysis — but log it.
                logger.warning("Analysis-ready email step failed for session %s",
                               upload_session_id, exc_info=True)

            _emit("Saving results and redirecting...")
            with analysis_progress_lock:
                analysis_progress[upload_session_id]["status"] = "done"
        except Exception as e:
            logger.exception("Analysis %s crashed", upload_session_id)
            try:
                db.execute("UPDATE upload_sessions SET status='failed' WHERE id=?", (upload_session_id,))
            except Exception:
                logger.exception("Could not mark analysis %s failed", upload_session_id)
            with analysis_progress_lock:
                analysis_progress[upload_session_id]["status"] = "error"
                analysis_progress[upload_session_id]["error"]  = str(e)

    # Mark the run as in progress in the DB too, with a start time. If the worker
    # dies mid-run, the in-memory entry vanishes but this row remains — that's how
    # analysis_status (and the boot sweep) detect and report a dead run instead of
    # leaving the user on a spinner forever.
    db.execute(
        "UPDATE upload_sessions SET status='analyzing', analysis_started_at=? WHERE id=?",
        (datetime.utcnow().isoformat(), upload_session_id)
    )

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
    rows = db.query("SELECT status, analysis_started_at FROM upload_sessions WHERE id=?", (upload_session_id,))
    status = rows[0]["status"] if rows else None
    if status == "complete":
        # All three agents must already be done if the session is complete.
        all_done = {
            "normalization":  {"status":"done","summary":"Item names mapped, duplicates merged","started_at":None,"ended_at":None},
            "inventory":      {"status":"done","summary":"Completed","started_at":None,"ended_at":None},
            "recommendation": {"status":"done","summary":"Completed","started_at":None,"ended_at":None},
        }
        return jsonify({"status": "done", "log": [], "elapsed": 0, "agents": all_done, "current_agent": None})

    _interrupted = {
        "status": "error",
        "error": "The analysis stopped unexpectedly — the server may have restarted. Please run it again.",
        "log": [], "elapsed": 0, "agents": {}, "current_agent": None,
    }
    if status == "failed":
        return jsonify(_interrupted)

    if status == "analyzing":
        # DB says it's running but there's no in-memory progress. On a single
        # worker that means the worker running it died. Give a short grace window
        # for timing races, then declare it dead so the page stops spinning.
        started = rows[0]["analysis_started_at"]
        dead = True
        if started:
            try:
                age = (datetime.utcnow() - datetime.fromisoformat(str(started))).total_seconds()
                dead = age > STUCK_ANALYSIS_SECONDS
            except (TypeError, ValueError):
                dead = True
        if dead:
            db.execute("UPDATE upload_sessions SET status='failed' WHERE id=?", (upload_session_id,))
            logger.warning("Analysis %s marked failed: no in-memory progress (worker likely restarted)",
                           upload_session_id)
            return jsonify(_interrupted)
        # Within grace — tell the page to keep waiting.
        return jsonify({"status": "running", "log": [], "elapsed": 0, "agents": {}, "current_agent": None})

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
    try:
        data_notes = json.loads(ar[0].get("data_notes") or "[]")
        if not isinstance(data_notes, list):
            data_notes = []
    except Exception:
        data_notes = []

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
        rec["_quantity_basis"]     = _quantity_basis(rec)
        rec["_has_stakes"]         = _has_stakes(rec)
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

    # Build supplier score lookup for template
    sup_scores_raw = db.get_supplier_scores(session["org_name"])
    supplier_score_map = {}
    for s in sup_scores_raw:
        supplier_score_map[s["supplier_name"]] = {
            "score": s.get("reliability_score", 50),
            "total_recs": s.get("total_recs", 0),
            "orders_placed": s.get("orders_placed", 0),
            "stockouts_avoided": s.get("stockouts_avoided", 0),
        }

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
        user_role=session.get("role", "admin"),
        supplier_score_map=supplier_score_map,
        data_notes=data_notes,
    )


def _stock_on_hand_map(upload_session_id):
    """Map item name -> current stock, read from the saved inventory report.

    Current stock (qty on hand) isn't stored on each recommendation, so the order
    sheets (print / PDF / CSV) join it from the inventory report by item name.
    Returns {} on any problem, in which case the sheets show "—"."""
    try:
        rows = db.query(
            "SELECT inventory_report FROM analysis_results WHERE session_id=?",
            (upload_session_id,)
        )
        if not rows:
            return {}
        inv = json.loads(rows[0]["inventory_report"] or "[]")
        if isinstance(inv, dict):
            inv = inv.get("report") if isinstance(inv.get("report"), list) else []
        if not isinstance(inv, list):
            return {}
        out = {}
        for it in inv:
            if isinstance(it, dict):
                name = str(it.get("item", "")).strip()
                if name and name not in out:
                    out[name] = it.get("stock")
        return out
    except Exception:
        return {}


def _order_by_text(rec):
    """Order-by text shared by every order sheet so they stay consistent: an
    overdue date becomes 'ASAP', a future date shows as-is, unknown shows '—'."""
    ob = _compute_order_by(rec)
    if ob.get("status") == "overdue":
        return "ASAP"
    return ob.get("order_by_date") or "—"


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
    # Current stock (qty on hand) isn't on the rec — join it from the inventory report.
    stock_map = _stock_on_hand_map(upload_session_id)
    # Enrich with effective values + order-by so the print template can stay simple.
    for r in approved:
        _normalise_confidence(r)
        r["_effective_qty"]      = _effective_qty(r)
        r["_effective_supplier"] = _effective_supplier(r)
        r["_order_by"]           = _compute_order_by(r)
        r["_current_stock"]      = stock_map.get(str(r.get("item", "")).strip())
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
    # Current stock (qty on hand) is joined from the inventory report by item name.
    stock_map = _stock_on_hand_map(upload_session_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    # Same columns and labels as the printed sheet and the PDF, so all three match.
    writer.writerow([
        "Item", "On Hand", "Qty To Order", "Supplier",
        "Order By", "Current Stock Lasts (months)", "Notes"
    ])
    # Free-text columns come from uploaded files and the model, so they're run
    # through csv_safe_cell to neutralise spreadsheet formula injection. The
    # numeric/date columns are computed by us and left as-is.
    _safe = validators.csv_safe_cell
    for r in approved:
        dos = r.get("days_of_supply")
        runway = round(dos / 30, 1) if dos else ""
        on_hand = stock_map.get(str(r.get("item", "")).strip())
        # Show the AI's original number inline when a human edited the quantity.
        qty = str(_effective_qty(r) or "")
        sug = str(r.get("suggested_quantity", "") or "")
        if r.get("edited_quantity") and qty.strip() and sug.strip() and qty != sug:
            qty = f"{qty} (AI: {sug})"
        writer.writerow([
            _safe(r.get("item", "")),
            _safe("" if on_hand in (None, "") else on_hand),
            _safe(qty),
            _safe(_effective_supplier(r)),
            _order_by_text(r),
            runway,
            _safe(r.get("note", "")),
        ])

    org_slug = session["org_name"].replace(" ", "_").lower()
    filename = f"berthcast_orders_{org_slug}_{upload_session_id}.csv"
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
    stock_map = _stock_on_hand_map(upload_session_id)

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
        Paragraph("berthcast — Purchase Order Sheet", title_style),
        Paragraph(
            f"{session['org_name']}  ·  Prepared: {today}  ·  "
            f"Analysis date: {generated_at}  ·  {len(approved)} item(s) approved",
            sub_style
        ),
        Spacer(1, 4*mm),
    ]

    if approved:
        from xml.sax.saxutils import escape as _esc  # keep item/supplier names with & or < from breaking the PDF
        # Same columns and labels as the printed sheet and the CSV, so all three match.
        header = ["#", "Item", "On hand", "Qty to order", "Supplier", "Order by", "Current stock lasts", "Note"]
        rows = [header]
        for i, r in enumerate(approved, 1):
            dos = r.get("days_of_supply")
            stock_lasts = f"~{round(dos/30,1)} mo" if dos else "—"
            note = r.get("note", "")
            note_cell = Paragraph(_esc(str(note)), cell_style) if note else Paragraph("—", cell_muted)

            on_hand = stock_map.get(str(r.get("item", "")).strip())
            on_hand_str = "—" if on_hand in (None, "") else _esc(str(on_hand))

            eff_qty = _effective_qty(r) or "—"
            sug_qty = r.get("suggested_quantity", "")
            qty_html = _esc(str(eff_qty))
            if str(eff_qty).strip() and str(sug_qty).strip() and str(eff_qty) != str(sug_qty):
                qty_html = (
                    f"<b>{_esc(str(eff_qty))}</b><br/>"
                    f"<font color='#9ca3af' size='7'>AI: {_esc(str(sug_qty))}</font>"
                )

            ob = _compute_order_by(r)
            if ob.get("status") == "overdue":
                order_by_cell = Paragraph("<b><font color='#b91c1c'>ASAP</font></b>", cell_style)
            else:
                order_by_cell = Paragraph(_esc(ob.get("order_by_date") or "—"), cell_style)

            rows.append([
                str(i),
                Paragraph(f"<b>{_esc(str(r.get('item','')))}</b>", cell_style),
                Paragraph(on_hand_str, cell_style),
                Paragraph(qty_html, cell_style),
                Paragraph(_esc(str(_effective_supplier(r))), cell_style),
                order_by_cell,
                stock_lasts,
                note_cell,
            ])

        col_widths = [7*mm, 30*mm, 18*mm, 22*mm, 30*mm, 20*mm, 16*mm, None]
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
        "Generated by berthcast · For internal purchasing use only",
        ParagraphStyle("Footer", fontName="Helvetica", fontSize=8, textColor=MUTED)
    ))

    doc.build(story)
    buf.seek(0)

    org_slug = session["org_name"].replace(" ", "_").lower()
    filename = f"berthcast_orders_{org_slug}_{upload_session_id}.pdf"
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













# ── Recommendation approve / dismiss routes ──────────────────────────────────

@app.route("/recommend/action", methods=["POST"])
@login_required
@analyst_required
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

    # Ownership check (org-scoped)
    rows = db.query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["org_name"] != session.get("org_name"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    # A user-entered order quantity must never reach the PO sheet unless it's a
    # real, non-negative number. Reject garbage up front.
    if edited_qty is not None and str(edited_qty).strip():
        _q = quantity.parse_quantity(edited_qty)
        if _q is None or _q < 0:
            return jsonify({"ok": False, "error": "Order quantity must be a number (0 or more)."}), 400

    def _mutate(recs):
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
                return {"updated": True}
        return {"updated": False}

    res = db.update_recommendations(session_id, _mutate)
    if not res["ok"]:
        return jsonify({"ok": False, "error": res["error"]}), res["code"]
    if not res["result"]["updated"]:
        return jsonify({"ok": False, "error": "Item not found in recommendations"}), 404
    return jsonify({"ok": True})


@app.route("/recommend/edit", methods=["POST"])
@login_required
@analyst_required
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

    rows = db.query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["org_name"] != session.get("org_name"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    # Same guard as the action route — no non-numeric/negative quantity gets saved.
    if edited_qty is not None and str(edited_qty).strip():
        _q = quantity.parse_quantity(edited_qty)
        if _q is None or _q < 0:
            return jsonify({"ok": False, "error": "Order quantity must be a number (0 or more)."}), 400

    def _mutate(recs):
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
                return {"updated": True}
        return {"updated": False}

    res = db.update_recommendations(session_id, _mutate)
    if not res["ok"]:
        return jsonify({"ok": False, "error": res["error"]}), res["code"]
    if not res["result"]["updated"]:
        return jsonify({"ok": False, "error": "Item not found"}), 404
    return jsonify({"ok": True})


@app.route("/recommend/approve_all", methods=["POST"])
@login_required
@analyst_required
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

    rows = db.query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["org_name"] != session.get("org_name"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    def _mutate(recs):
        newly = []
        for rec in recs:
            if not isinstance(rec, dict) or rec.get("error"):
                continue
            if items_filter and rec.get("item", "") not in items_filter:
                continue
            if not rec.get("dismissed") and not rec.get("approved"):
                rec["approved"] = True
                newly.append(rec.get("item", ""))
        return newly

    res = db.update_recommendations(session_id, _mutate)
    if not res["ok"]:
        return jsonify({"ok": False, "error": res["error"]}), res["code"]
    return jsonify({"ok": True, "newly_approved_items": res["result"]})


@app.route("/recommend/undo_approve_all", methods=["POST"])
@login_required
@analyst_required
def recommend_undo_approve_all():
    """Un-approve a specific list of items (the ones bulk-approved moments ago)."""
    data       = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id")
    items      = data.get("items", [])

    if not session_id:
        return jsonify({"ok": False, "error": "Missing session_id"}), 400

    rows = db.query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["org_name"] != session.get("org_name"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    items_set = set(items)

    def _mutate(recs):
        undone = []
        for rec in recs:
            if not isinstance(rec, dict) or rec.get("error"):
                continue
            if rec.get("item", "") in items_set and rec.get("approved"):
                rec["approved"] = False
                undone.append(rec.get("item", ""))
        return undone

    res = db.update_recommendations(session_id, _mutate)
    if not res["ok"]:
        return jsonify({"ok": False, "error": res["error"]}), res["code"]
    return jsonify({"ok": True, "undone_items": res["result"]})


# ---------------------------------------------------------------------------
# Outcome tracking — staff record whether they placed the order + result
# ---------------------------------------------------------------------------

@app.route("/recommend/outcome", methods=["POST"])
@login_required
@analyst_required
def recommend_outcome():
    """Record outcome for a recommendation: order_placed (bool) and
    outcome_status (stockout_avoided | stockout_happened | '')."""
    data       = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id")
    item       = data.get("item", "").strip()
    field      = data.get("field", "")         # "order_placed" or "outcome_status"
    value      = data.get("value")

    if not session_id or not item or field not in ("order_placed", "outcome_status"):
        return jsonify({"ok": False, "error": "Invalid parameters"}), 400

    rows = db.query("SELECT org_name FROM upload_sessions WHERE id=?", (session_id,))
    if not rows or rows[0]["org_name"] != session.get("org_name"):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    # Reject a bad outcome_status before we take the write lock.
    if field == "outcome_status" and value not in ("stockout_avoided", "stockout_happened", ""):
        return jsonify({"ok": False, "error": "Invalid outcome_status"}), 400

    def _mutate(recs):
        for rec in recs:
            if isinstance(rec, dict) and rec.get("item", "").strip() == item:
                if field == "order_placed":
                    rec["order_placed"] = bool(value)
                    rec["order_placed_at"] = datetime.utcnow().isoformat() if value else None
                elif field == "outcome_status":
                    rec["outcome_status"] = value
                    rec["outcome_recorded_at"] = datetime.utcnow().isoformat() if value else None
                return {"updated": True}
        return {"updated": False}

    res = db.update_recommendations(session_id, _mutate)
    if not res["ok"]:
        return jsonify({"ok": False, "error": res["error"]}), res["code"]
    if not res["result"]["updated"]:
        return jsonify({"ok": False, "error": "Item not found"}), 404

    # Recalculate supplier scores in the background
    org_name = session.get("org_name")
    try:
        db.update_supplier_scores(org_name)
    except Exception:
        logger.warning("Supplier-score recalc failed for org %s", org_name, exc_info=True)  # Non-blocking

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Outcome stats API — for dashboard ROI card
# ---------------------------------------------------------------------------

@app.route("/api/outcome-stats")
@login_required
def outcome_stats_api():
    stats = db.get_outcome_stats(session["org_name"])
    return jsonify(stats)


# ---------------------------------------------------------------------------
# Supplier scores page
# ---------------------------------------------------------------------------

@app.route("/suppliers")
@login_required
def suppliers_page():
    scores = db.get_supplier_scores(session["org_name"])
    outcome_stats = db.get_outcome_stats(session["org_name"])
    return render_template("suppliers.html",
                           suppliers=scores,
                           outcome_stats=outcome_stats,
                           org_name=session["org_name"])


if __name__ == "__main__":
    # Local dev entry point. On Render we use gunicorn (see render.yaml).
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
