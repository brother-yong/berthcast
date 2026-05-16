import os
import json
import threading
import tempfile
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import database as db
from agents import (
    run_normalization_agent,
    run_inventory_agent,
    run_recommendation_agent,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "berthai-dev-secret-change-in-prod")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"xlsx", "xls"}

# File slot → table name mapping
FILE_SLOTS = {
    "inventory":       "inventory",
    "purchase_orders": "purchase_orders",
    "sales":           "sales",
    "suppliers":       "suppliers",
    "customers":       "customers",
    "stockouts":       "stockouts",
}

AVAILABLE_MODELS = [
    ("claude-haiku-4-5-20251001",  "Haiku — fast, lower cost (testing)"),
    ("claude-sonnet-4-6",          "Sonnet — balanced (recommended)"),
    ("claude-opus-4-6",            "Opus — most thorough (production reports)"),
]

db.init_db()

# ── Create default admin if none exists ──────────────────────────────────────
def _ensure_admin():
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@berthai.com")
    admin_pass  = os.environ.get("ADMIN_PASSWORD", "changeme123")
    existing = db.query("SELECT id FROM users WHERE is_admin=1")
    if not existing:
        db.execute(
            "INSERT INTO users (email, password_hash, org_name, model, is_admin) VALUES (?,?,?,?,?)",
            (admin_email, generate_password_hash(admin_pass), "BerthAI Admin", "claude-sonnet-4-6", 1)
        )

_ensure_admin()


# ── Decorators ────────────────────────────────────────────────────────────────
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


# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        users = db.query("SELECT * FROM users WHERE email=?", (email,))
        if users and check_password_hash(users[0]["password_hash"], password):
            u = users[0]
            session["user_id"]  = u["id"]
            session["email"]    = u["email"]
            session["org_name"] = u["org_name"]
            session["model"]    = u["model"]
            session["is_admin"] = bool(u["is_admin"])
            return redirect(url_for("admin_panel") if u["is_admin"] else url_for("dashboard"))
        flash("Incorrect email or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Admin ─────────────────────────────────────────────────────────────────────
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

        elif action == "change_model":
            uid   = request.form.get("user_id")
            model = request.form.get("model")
            db.execute("UPDATE users SET model=? WHERE id=?", (model, uid))
            flash("Model updated.", "success")

    users = db.query("SELECT id, email, org_name, model, created_at FROM users WHERE is_admin=0 ORDER BY created_at DESC")
    return render_template("admin.html", users=users, models=AVAILABLE_MODELS)


# ── Main dashboard ────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    # Get user's most recent analysis session
    sessions = db.query(
        "SELECT * FROM upload_sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
        (session["user_id"],)
    )
    last_session = sessions[0] if sessions else None
    return render_template("dashboard.html", last_session=last_session)


# ── Step 1: Upload files ──────────────────────────────────────────────────────
@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    # Create or resume an upload session
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

        file = request.files.get("file")
        if not file or not _allowed(file.filename):
            return jsonify({"ok": False, "error": "Please upload an Excel file (.xlsx or .xls)."})

        original_name = file.filename
        filename = secure_filename(original_name)
        filepath = os.path.join(UPLOAD_FOLDER, f"{upload_session_id}_{slot}_{filename}")

        # Save file to disk immediately — this is fast regardless of file size
        file.save(filepath)

        # Mark as converting in DB so the UI can poll for status
        db.set_conversion_status(upload_session_id, slot, "converting")

        # Process in a background thread — avoids HTTP timeout for large files
        def _process(fp, table, sid, sl, orig_name):
            result = db.excel_to_sqlite(fp, table, sid)
            if result.get("ok"):
                db.set_conversion_status(sid, sl, "done", rows_count=result.get("rows", 0))
                names_row = db.query("SELECT file_names_json FROM upload_sessions WHERE id=?", (sid,))
                names = json.loads(names_row[0]["file_names_json"] or "{}") if names_row and names_row[0]["file_names_json"] else {}
                names[sl] = orig_name
                db.execute("UPDATE upload_sessions SET file_names_json=? WHERE id=?", (json.dumps(names), sid))
            else:
                db.set_conversion_status(sid, sl, "error", error=result.get("error", "Unknown error"))

        t = threading.Thread(
            target=_process,
            args=(filepath, FILE_SLOTS[slot], upload_session_id, slot, original_name),
            daemon=True
        )
        t.start()

        # Return immediately — frontend will poll /upload/status for completion
        return jsonify({"ok": True, "processing": True, "filename": original_name})

    tables = db.get_session_tables(upload_session_id)
    names_row = db.query("SELECT file_names_json FROM upload_sessions WHERE id=?", (upload_session_id,))
    file_names = json.loads(names_row[0]["file_names_json"] or "{}") if names_row and names_row[0]["file_names_json"] else {}
    return render_template("upload.html", tables=tables, session_id=upload_session_id, file_names=file_names)


# ── Upload conversion status (polling) ───────────────────────────────────────
@app.route("/upload/status/<int:upload_session_id>")
@login_required
def upload_status(upload_session_id):
    _verify_session_owner(upload_session_id)
    statuses = db.get_conversion_status(upload_session_id)
    names_row = db.query("SELECT file_names_json FROM upload_sessions WHERE id=?", (upload_session_id,))
    file_names = json.loads(names_row[0]["file_names_json"] or "{}") if names_row and names_row[0]["file_names_json"] else {}
    return jsonify({"statuses": statuses, "file_names": file_names})


# ── Remove uploaded file slot ─────────────────────────────────────────────────
@app.route("/upload/remove", methods=["POST"])
@login_required
def remove_upload():
    data       = request.get_json()
    slot       = data.get("slot")
    session_id = data.get("session_id")

    if slot not in FILE_SLOTS:
        return jsonify({"ok": False, "error": "Unknown slot."})

    _verify_session_owner(session_id)
    table_name = f"{FILE_SLOTS[slot]}_{session_id}"
    try:
        db.execute(f"DROP TABLE IF EXISTS {table_name}")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    return jsonify({"ok": True})


# ── Step 2: Context form ──────────────────────────────────────────────────────
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
            "UPDATE upload_sessions SET context_json=? WHERE id=?",
            (json.dumps(context), upload_session_id)
        )
        return redirect(url_for("dedup_review", upload_session_id=upload_session_id))

    return render_template("context_form.html", upload_session_id=upload_session_id)


# ── Step 3: Deduplication review ──────────────────────────────────────────────
@app.route("/dedup/<int:upload_session_id>", methods=["GET", "POST"])
@login_required
def dedup_review(upload_session_id):
    _verify_session_owner(upload_session_id)
    model = session["model"]

    if request.method == "POST":
        # User submits confirmed groups (may have removed some)
        confirmed_raw = request.form.get("confirmed_groups", "[]")
        try:
            confirmed = json.loads(confirmed_raw)
        except Exception:
            confirmed = []
        db.execute(
            "UPDATE upload_sessions SET dedup_confirmed=1 WHERE id=?",
            (upload_session_id,)
        )
        # Store confirmed groups in analysis_results table placeholder
        db.execute(
            """INSERT OR REPLACE INTO analysis_results (session_id, inventory_report, recommendations_json)
               VALUES (?, ?, ?)""",
            (upload_session_id, json.dumps({"confirmed_groups": confirmed}), "[]")
        )
        return redirect(url_for("run_analysis", upload_session_id=upload_session_id))

    # Run the normalisation agent
    result = run_normalization_agent(upload_session_id, model)
    groups = result.get("groups", [])
    message = result.get("message", "")

    return render_template(
        "dedup_review.html",
        groups=groups,
        message=message,
        upload_session_id=upload_session_id
    )


# ── Step 4: Run analysis ──────────────────────────────────────────────────────
@app.route("/analyse/<int:upload_session_id>")
@login_required
def run_analysis(upload_session_id):
    _verify_session_owner(upload_session_id)
    model = session["model"]

    # Load context
    rows = db.query("SELECT context_json FROM upload_sessions WHERE id=?", (upload_session_id,))
    context = json.loads(rows[0]["context_json"] or "{}") if rows else {}

    # Load confirmed dedup groups
    ar_rows = db.query("SELECT inventory_report FROM analysis_results WHERE session_id=?", (upload_session_id,))
    confirmed_groups = []
    if ar_rows and ar_rows[0]["inventory_report"]:
        try:
            data = json.loads(ar_rows[0]["inventory_report"])
            confirmed_groups = data.get("confirmed_groups", [])
        except Exception:
            pass

    # Run inventory agent
    inv_result = run_inventory_agent(upload_session_id, model, confirmed_groups, context)
    if "error" in inv_result:
        flash(f"Analysis failed: {inv_result['error']}", "error")
        return redirect(url_for("upload"))

    inventory_report = inv_result["report"]

    # Run recommendation agent
    recommendations = run_recommendation_agent(upload_session_id, model, inventory_report, context)

    # Save results
    db.execute(
        """UPDATE analysis_results SET inventory_report=?, recommendations_json=?
           WHERE session_id=?""",
        (json.dumps(inventory_report), json.dumps(recommendations), upload_session_id)
    )
    db.execute(
        "UPDATE upload_sessions SET status='complete' WHERE id=?",
        (upload_session_id,)
    )

    return redirect(url_for("results", upload_session_id=upload_session_id))


# ── Step 5: Results ───────────────────────────────────────────────────────────
@app.route("/results/<int:upload_session_id>")
@login_required
def results(upload_session_id):
    _verify_session_owner(upload_session_id)

    ar = db.query("SELECT * FROM analysis_results WHERE session_id=?", (upload_session_id,))
    if not ar:
        flash("No results found. Please run the analysis first.", "error")
        return redirect(url_for("dashboard"))

    try:
        inventory_report  = json.loads(ar[0]["inventory_report"] or "[]")
        recommendations   = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        inventory_report = []
        recommendations  = []

    # Separate confirmed_groups stash if present
    if isinstance(inventory_report, dict) and "confirmed_groups" in inventory_report:
        inventory_report = []

    return render_template(
        "results.html",
        inventory=inventory_report,
        recommendations=recommendations,
        upload_session_id=upload_session_id,
        org_name=session["org_name"]
    )


# ── PDF export (print-friendly redirect) ─────────────────────────────────────
@app.route("/results/<int:upload_session_id>/print")
@login_required
def print_results(upload_session_id):
    _verify_session_owner(upload_session_id)
    ar = db.query("SELECT * FROM analysis_results WHERE session_id=?", (upload_session_id,))
    if not ar:
        return redirect(url_for("dashboard"))
    try:
        recommendations = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        recommendations = []
    approved = [r for r in recommendations if r.get("approved")]
    return render_template(
        "print_order.html",
        recommendations=approved,
        org_name=session["org_name"]
    )


# ── Approve/dismiss recommendation (AJAX) ────────────────────────────────────
@app.route("/recommend/action", methods=["POST"])
@login_required
def recommend_action():
    data       = request.get_json()
    session_id = data.get("session_id")
    item       = data.get("item")
    action     = data.get("action")   # "approve" or "dismiss"
    note       = data.get("note", "")

    _verify_session_owner(session_id)

    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (session_id,))
    if not ar:
        return jsonify({"ok": False})

    recs = json.loads(ar[0]["recommendations_json"] or "[]")
    for r in recs:
        if r.get("item") == item:
            r["approved"] = (action == "approve")
            r["dismissed"] = (action == "dismiss")
            r["note"] = note

    db.execute(
        "UPDATE analysis_results SET recommendations_json=? WHERE session_id=?",
        (json.dumps(recs), session_id)
    )
    return jsonify({"ok": True})


# ── Helpers ───────────────────────────────────────────────────────────────────
def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _verify_session_owner(upload_session_id):
    rows = db.query("SELECT user_id FROM upload_sessions WHERE id=?", (upload_session_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        from flask import abort
        abort(403)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
