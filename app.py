import os
import json
import threading
import time
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, Response, stream_with_context
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import anthropic as _anthropic

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

ALLOWED_EXTENSIONS = {"xlsx", "csv"}

FILE_SLOTS = {
    "inventory":       "inventory",
    "purchase_orders": "purchase_orders",
    "sales":           "sales",
    "suppliers":       "suppliers",
    "customers":       "customers",
    "stockouts":       "stockouts",
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

db.init_db()


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


@app.route("/")
def landing():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        users = db.query("SELECT * FROM users WHERE email=?", (email,))
        if users and check_password_hash(users[0]["password_hash"], password):
            u = users[0]
            session["user_id"]  = u["id"]
            session["email"]    = u["email"]
            session["org_name"] = u["org_name"]
            session["model"]    = u["model"]
            session["is_admin"] = bool(u["is_admin"])
            return redirect(url_for("admin_panel") if u["is_admin"] else url_for("chat"))
        flash("Incorrect email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ── Chat ─────────────────────────────────────────────────────────────────────

@app.route("/chat")
@login_required
def chat():
    return render_template("chat.html")


@app.route("/api/chat", methods=["POST"])
@login_required
def chat_api():
    data = request.get_json() or {}
    conversation_id = data.get("conversation_id")
    user_message = (data.get("message") or "").strip()
    req_features = data.get("features") or []

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

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

    # Build system prompt based on feature toggles
    base_system = (
        "You are BerthAI, an AI assistant specialising in marine supply chain "
        "and inventory management. Help users with inventory questions, demand "
        "forecasting, supplier issues, and procurement planning. Be direct and "
        "practical. When you don't have specific data, say so clearly."
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


@app.route("/contact", methods=["GET", "POST"])
def contact():
    """Public contact form. Stores submission in DB for admin to review.
    No email exposed in HTML. No auth required — prospects can use it."""
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

    users = db.query("SELECT id, email, org_name, model, created_at FROM users WHERE is_admin=0 ORDER BY created_at DESC")
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
        return redirect(url_for("user_settings"))

    profiles = db.get_supplier_profiles(org)
    return render_template("settings.html", profiles=profiles)


@app.route("/dashboard")
@login_required
def dashboard():
    sessions = db.query(
        "SELECT * FROM upload_sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
        (session["user_id"],)
    )
    last_session = sessions[0] if sessions else None

    # Pull summary stats from last completed analysis (if any).
    # Fails silently — dashboard still renders if anything goes wrong.
    stats = None
    if last_session and last_session["status"] == "complete":
        try:
            ar = db.query(
                "SELECT inventory_report, recommendations_json FROM analysis_results WHERE session_id=?",
                (last_session["id"],)
            )
            if ar:
                inv  = json.loads(ar[0]["inventory_report"] or "[]")
                recs = json.loads(ar[0]["recommendations_json"] or "[]")
                if isinstance(inv, dict):
                    inv = []
                stats = {
                    "tracked_skus":   len(inv),
                    "critical_count": sum(1 for i in inv if i.get("status") == "CRITICAL"),
                    "low_count":      sum(1 for i in inv if i.get("status") == "LOW"),
                    "rec_count":      len(recs),
                    "approved_count": sum(1 for r in recs if r.get("approved")),
                }
        except Exception:
            stats = None

    return render_template("dashboard.html", last_session=last_session, stats=stats)


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
        "stockouts":       "Stockout Report",
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
    return render_template("upload.html", tables=tables, session_id=upload_session_id, file_names=file_names)


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
    data       = request.get_json()
    slot       = data.get("slot")
    session_id = data.get("session_id")
    if slot not in FILE_SLOTS:
        return jsonify({"ok": False, "error": "Unknown slot."})
    _verify_session_owner(session_id)
    try:
        db.execute(f'DROP TABLE IF EXISTS "{FILE_SLOTS[slot]}_{session_id}"')
        # Clean up any leftover chunk temp files for this slot
        for f in os.listdir(UPLOAD_FOLDER):
            if f.startswith(f"{session_id}_{slot}_"):
                try:
                    os.remove(os.path.join(UPLOAD_FOLDER, f))
                except Exception:
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
            "UPDATE upload_sessions SET context_json=? WHERE id=?",
            (json.dumps(context), upload_session_id)
        )
        return redirect(url_for("dedup_review", upload_session_id=upload_session_id))
    return render_template("context_form.html", upload_session_id=upload_session_id)


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

    # If a run is in progress already (user refreshed the progress page), show it.
    with analysis_progress_lock:
        existing = analysis_progress.get(upload_session_id)
    if existing and existing["status"] == "running":
        return render_template("analysis_progress.html", upload_session_id=upload_session_id)

    # Otherwise kick off a new run in the background.
    model = session["model"]
    with analysis_progress_lock:
        analysis_progress[upload_session_id] = {
            "started_at": time.time(),
            "log":        [],
            "status":     "running",
            "error":      None,
        }

    def _emit(msg: str):
        with analysis_progress_lock:
            entry = analysis_progress.get(upload_session_id)
            if not entry:
                return
            entry["log"].append({
                "t":   round(time.time() - entry["started_at"], 1),
                "msg": msg,
            })

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

            _emit("Starting inventory health agent")
            inv_result = run_inventory_agent(upload_session_id, model, confirmed_groups, context, progress_emit=_emit)
            if "error" in inv_result:
                with analysis_progress_lock:
                    analysis_progress[upload_session_id]["status"] = "error"
                    analysis_progress[upload_session_id]["error"]  = inv_result["error"]
                return

            inventory_report = inv_result["report"]

            _emit("Starting purchase recommendation agent")
            recommendations = run_recommendation_agent(upload_session_id, model, inventory_report, context, progress_emit=_emit)

            db.execute(
                "UPDATE analysis_results SET inventory_report=?, recommendations_json=? WHERE session_id=?",
                (json.dumps(inventory_report), json.dumps(recommendations), upload_session_id)
            )
            db.execute("UPDATE upload_sessions SET status='complete' WHERE id=?", (upload_session_id,))

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
                "status":  entry["status"],
                "log":     list(entry["log"]),
                "elapsed": round(time.time() - entry["started_at"], 1),
                "error":   entry.get("error"),
            }
        else:
            payload = None

    if payload is not None:
        return jsonify(payload)

    # No in-memory entry — check DB. Worker may have restarted, or analysis
    # completed before this session started polling.
    rows = db.query("SELECT status FROM upload_sessions WHERE id=?", (upload_session_id,))
    if rows and rows[0]["status"] == "complete":
        return jsonify({"status": "done", "log": [], "elapsed": 0})
    return jsonify({"status": "not_found", "log": [], "elapsed": 0})


@app.route("/results/<int:upload_session_id>")
@login_required
def results(upload_session_id):
    _verify_session_owner(upload_session_id)
    ar = db.query("SELECT * FROM analysis_results WHERE session_id=?", (upload_session_id,))
    if not ar:
        flash("No results found. Please run the analysis first.", "error")
        return redirect(url_for("dashboard"))
    try:
        inventory_report = json.loads(ar[0]["inventory_report"] or "[]")
        recommendations  = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        inventory_report = []
        recommendations  = []
    if isinstance(inventory_report, dict) and "confirmed_groups" in inventory_report:
        inventory_report = []

    # Build item → status lookup so recommendation cards can show/filter by inventory status
    status_by_item = {}
    for item in inventory_report:
        if isinstance(item, dict) and item.get("item"):
            status_by_item[item["item"]] = item.get("status") or ""

    # Pull session created_at so we can show "Generated 2 hours ago" byline on results
    sess_row = db.query("SELECT created_at FROM upload_sessions WHERE id=?", (upload_session_id,))
    generated_at = sess_row[0]["created_at"] if sess_row else None

    return render_template(
        "results.html",
        inventory=inventory_report,
        recommendations=recommendations,
        upload_session_id=upload_session_id,
        org_name=session["org_name"],
        status_by_item=status_by_item,
        generated_at=generated_at,
    )


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
    return render_template("print_order.html", recommendations=approved, org_name=session["org_name"])


@app.route("/recommend/approve_all", methods=["POST"])
@login_required
def recommend_approve_all():
    data       = request.get_json() or {}
    session_id = data.get("session_id")
    _verify_session_owner(session_id)
    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (session_id,))
    if not ar:
        return jsonify({"ok": False, "error": "No recommendations found."})
    try:
        recs = json.loads(ar[0]["recommendations_json"] or "[]")
    except Exception:
        return jsonify({"ok": False, "error": "Could not read recommendations."})

    approved_count = 0
    for r in recs:
        if r.get("error"):
            continue
        if r.get("dismissed"):
            # User explicitly dismissed this — leave it alone
            continue
        if not r.get("approved"):
            approved_count += 1
        r["approved"] = True

    db.execute(
        "UPDATE analysis_results SET recommendations_json=? WHERE session_id=?",
        (json.dumps(recs), session_id)
    )
    return jsonify({"ok": True, "newly_approved": approved_count, "total": len(recs)})


@app.route("/recommend/action", methods=["POST"])
@login_required
def recommend_action():
    data       = request.get_json()
    session_id = data.get("session_id")
    item       = data.get("item")
    action     = data.get("action")
    note       = data.get("note", "")
    _verify_session_owner(session_id)
    ar = db.query("SELECT recommendations_json FROM analysis_results WHERE session_id=?", (session_id,))
    if not ar:
        return jsonify({"ok": False})
    recs = json.loads(ar[0]["recommendations_json"] or "[]")
    for r in recs:
        if r.get("item") == item:
            r["approved"]  = (action == "approve")
            r["dismissed"] = (action == "dismiss")
            r["note"]      = note
    db.execute(
        "UPDATE analysis_results SET recommendations_json=? WHERE session_id=?",
        (json.dumps(recs), session_id)
    )
    return jsonify({"ok": True})


def _allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _verify_session_owner(upload_session_id):
    rows = db.query("SELECT user_id FROM upload_sessions WHERE id=?", (upload_session_id,))
    if not rows or rows[0]["user_id"] != session.get("user_id"):
        from flask import abort
        abort(403)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
