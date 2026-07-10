from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from leadops.db import init_db
from leadops.importers import import_ai_messages, import_contacts, import_suppression
from leadops.services import (
    OUTCOME_CHOICES,
    admin_outcome_rows,
    approve_message,
    assign_next,
    dashboard_context,
    edit_contact,
    export_approved_csv,
    get_contact,
    get_contact_rows,
    get_student_queue,
    get_students,
    leaderboard_rows,
    skip_message,
    update_outcome,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
EXPORT_DIR = BASE_DIR / "exports"
ALLOWED_UPLOAD_EXTENSIONS = {".csv", ".xlsx", ".txt"}
CONTACTS_PAGE_SIZE = 50


def create_app() -> Flask:
    app = Flask(__name__)
    # Use a stable secret in production via env; the default keeps local dev simple.
    app.config["SECRET_KEY"] = os.environ.get("LEADOPS_SECRET_KEY", "local-mvp-change-before-hosting")
    # Optional admin PIN. If set, Abe/Admin must enter it to sign in.
    app.config["ADMIN_PIN"] = os.environ.get("ADMIN_PIN", "").strip()
    UPLOAD_DIR.mkdir(exist_ok=True)
    EXPORT_DIR.mkdir(exist_ok=True)
    init_db()

    @app.context_processor
    def inject_globals():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return {
            "students": get_students(active_only=True),
            "session_role": session.get("role"),
            "session_student_id": session.get("student_id"),
            "csrf_token": session["_csrf"],
            "admin_pin_required": bool(app.config["ADMIN_PIN"]),
        }

    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            expected = session.get("_csrf")
            provided = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(str(expected), str(provided)):
                abort(400, "CSRF token missing or invalid. Reload the page and try again.")

    def require_admin():
        if session.get("role") != "admin":
            flash("Choose Abe/Admin to continue.", "warning")
            return redirect(url_for("login"))
        return None

    def require_student():
        if session.get("role") != "student" or not session.get("student_id"):
            flash("Choose a student identity to continue.", "warning")
            return redirect(url_for("login"))
        return None

    @app.route("/")
    def index():
        if session.get("role") == "admin":
            return redirect(url_for("admin_dashboard"))
        if session.get("role") == "student":
            return redirect(url_for("student_queue"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            role = request.form.get("role", "").strip()
            if role == "admin":
                if app.config["ADMIN_PIN"]:
                    pin = request.form.get("pin", "").strip()
                    if not hmac.compare_digest(pin, app.config["ADMIN_PIN"]):
                        flash("Incorrect admin PIN.", "error")
                        return redirect(url_for("login"))
                session.clear()
                session["role"] = "admin"
                flash("Signed in as Abe/Admin.", "success")
                return redirect(url_for("admin_dashboard"))
            if role == "student":
                student_id = request.form.get("student_id", type=int)
                if not student_id:
                    flash("Pick a student.", "error")
                    return redirect(url_for("login"))
                session.clear()
                session["role"] = "student"
                session["student_id"] = student_id
                flash("Signed in as student.", "success")
                return redirect(url_for("student_queue"))
            flash("Pick a role.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("Signed out.", "success")
        return redirect(url_for("login"))

    @app.route("/admin")
    def admin_dashboard():
        guard = require_admin()
        if guard:
            return guard
        return render_template("admin_dashboard.html", **dashboard_context())

    @app.route("/admin/import", methods=["GET", "POST"])
    def admin_import():
        guard = require_admin()
        if guard:
            return guard
        result = None
        if request.method == "POST":
            kind = request.form.get("kind", "")
            upload = request.files.get("file")
            if not upload or upload.filename == "":
                flash("Choose a file to import.", "error")
                return redirect(url_for("admin_import"))
            filename = secure_filename(upload.filename)
            if Path(filename).suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
                flash("Unsupported file type. Use .csv, .xlsx, or .txt.", "error")
                return redirect(url_for("admin_import"))
            path = UPLOAD_DIR / filename
            upload.save(path)
            try:
                if kind == "contacts":
                    result = import_contacts(path, actor_role="admin")
                elif kind == "ai":
                    result = import_ai_messages(path, actor_role="admin")
                elif kind == "suppression":
                    result = import_suppression(path, actor_role="admin")
                else:
                    flash("Choose an import type.", "error")
                    return redirect(url_for("admin_import"))
                flash(f"Imported {kind}: {result['summary']}", "success")
            except Exception as exc:  # pragma: no cover - surfaced in UI
                flash(f"Import failed: {exc}", "error")
        return render_template("admin_import.html", result=result)

    @app.route("/admin/contacts")
    def admin_contacts():
        guard = require_admin()
        if guard:
            return guard
        filters = {
            "q": request.args.get("q", ""),
            "status": request.args.get("status", ""),
            "student_id": request.args.get("student_id", ""),
            "block_reason": request.args.get("block_reason", ""),
        }
        page = request.args.get("page", default=1, type=int) or 1
        result = get_contact_rows(filters, page=page, page_size=CONTACTS_PAGE_SIZE)
        return render_template(
            "admin_contacts.html",
            rows=result["rows"],
            filters=filters,
            page=result["page"],
            pages=result["pages"],
            total=result["total"],
            has_prev=result["has_prev"],
            has_next=result["has_next"],
        )

    @app.route("/admin/contacts/<int:contact_id>/edit", methods=["GET", "POST"])
    def admin_contact_edit(contact_id):
        guard = require_admin()
        if guard:
            return guard
        contact = get_contact(contact_id)
        if not contact:
            flash("Contact not found.", "error")
            return redirect(url_for("admin_contacts"))
        if request.method == "POST":
            try:
                new_status = edit_contact(contact_id, request.form.to_dict(), actor_role="admin")
                flash(f"Contact saved. New status: {new_status}.", "success")
                return redirect(url_for("admin_contacts"))
            except ValueError as exc:
                flash(str(exc), "error")
                contact = get_contact(contact_id) or contact
        return render_template("admin_contact_edit.html", contact=contact)

    @app.route("/admin/assign", methods=["GET", "POST"])
    def admin_assign():
        guard = require_admin()
        if guard:
            return guard
        assigned = []
        if request.method == "POST":
            student_id = request.form.get("student_id", type=int)
            count = request.form.get("count", type=int) or 0
            if not student_id or count <= 0:
                flash("Choose a student and a positive count.", "error")
            else:
                try:
                    assigned = assign_next(student_id, count, actor_role="admin")
                    flash(f"Assigned {len(assigned)} lead(s).", "success")
                except ValueError as exc:
                    flash(str(exc), "error")
        context = dashboard_context()
        return render_template("admin_assign.html", assigned=assigned, **context)

    @app.route("/admin/export", methods=["GET", "POST"])
    def admin_export():
        guard = require_admin()
        if guard:
            return guard
        context = dashboard_context()
        if request.method == "POST":
            path, count = export_approved_csv(EXPORT_DIR, actor_role="admin")
            if count == 0:
                flash("No approved rows to export right now.", "warning")
                return redirect(url_for("admin_export"))
            flash(f"Exported {count} approved row(s).", "success")
            return send_file(path, as_attachment=True, download_name=Path(path).name)
        return render_template("admin_export.html", **context)

    @app.route("/admin/outcomes", methods=["GET", "POST"])
    def admin_outcomes():
        guard = require_admin()
        if guard:
            return guard
        if request.method == "POST":
            try:
                update_outcome(
                    contact_id=request.form.get("contact_id", type=int),
                    message_id=request.form.get("message_id", type=int),
                    student_id=request.form.get("student_id", type=int),
                    reply_status=request.form.get("reply_status", ""),
                    meeting_status=request.form.get("meeting_status", ""),
                    hot=bool(request.form.get("hot")),
                    notes=request.form.get("notes", ""),
                    actor_role="admin",
                )
                flash("Outcome updated.", "success")
            except ValueError as exc:
                flash(str(exc), "error")
            return redirect(url_for("admin_outcomes"))
        return render_template(
            "admin_outcomes.html",
            rows=admin_outcome_rows(),
            outcome_choices=OUTCOME_CHOICES,
        )

    @app.route("/admin/leaderboard")
    def admin_leaderboard():
        guard = require_admin()
        if guard:
            return guard
        return render_template("admin_leaderboard.html", rows=leaderboard_rows())

    @app.route("/student", methods=["GET", "POST"])
    def student_queue():
        guard = require_student()
        if guard:
            return guard
        student_id = int(session["student_id"])
        if request.method == "POST":
            action = request.form.get("action")
            message_id = request.form.get("message_id", type=int)
            contact_id = request.form.get("contact_id", type=int)
            try:
                if action == "approve":
                    approve_message(
                        message_id,
                        student_id,
                        request.form.get("current_message", ""),
                        actor_role="student",
                    )
                    flash("Message approved.", "success")
                elif action == "skip":
                    skip_message(
                        message_id,
                        student_id,
                        request.form.get("skip_reason", ""),
                        actor_role="student",
                    )
                    flash("Lead skipped.", "success")
                elif action == "outcome":
                    update_outcome(
                        contact_id=contact_id,
                        message_id=message_id,
                        student_id=student_id,
                        reply_status=request.form.get("reply_status", ""),
                        meeting_status=request.form.get("meeting_status", ""),
                        hot=bool(request.form.get("hot")),
                        notes=request.form.get("notes", ""),
                        actor_role="student",
                        actor_id=student_id,
                    )
                    flash("Outcome saved.", "success")
                else:
                    flash("Unknown action.", "error")
            except ValueError as exc:
                flash(str(exc), "error")
            return redirect(url_for("student_queue"))
        return render_template(
            "student_queue.html",
            rows=get_student_queue(student_id),
            outcome_choices=OUTCOME_CHOICES,
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        threaded=True,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
