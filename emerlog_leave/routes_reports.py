from datetime import date
import calendar
from flask import Blueprint, flash, render_template, request

from .config import LEAVE_TYPES
from .database import get_db
from .services import login_required, role_required, visible_user_ids, log_action

bp = Blueprint("reports", __name__)


@bp.route("/reports")
@login_required
@role_required("admin", "kadry", "menedzer")
def reports_view():
    conn = get_db()
    year = int(request.args.get("year", date.today().year))
    month = int(request.args.get("month", date.today().month))
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    ids = visible_user_ids(conn)
    rows = []
    by_department = []
    by_type = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(f"""
            SELECT lr.*, u.full_name, u.department
            FROM leave_requests lr
            JOIN users u ON u.id = lr.user_id
            WHERE lr.status = 'zaakceptowany'
              AND lr.date_from <= ? AND lr.date_to >= ?
              AND lr.user_id IN ({placeholders})
            ORDER BY u.department, u.full_name
        """, (end.isoformat(), start.isoformat(), *ids)).fetchall()
        by_department = conn.execute(f"""
            SELECT u.department, COUNT(*) AS requests_count, COALESCE(SUM(lr.days_count),0) AS days
            FROM leave_requests lr
            JOIN users u ON u.id = lr.user_id
            WHERE lr.status = 'zaakceptowany'
              AND lr.date_from <= ? AND lr.date_to >= ?
              AND lr.user_id IN ({placeholders})
            GROUP BY u.department
            ORDER BY days DESC
        """, (end.isoformat(), start.isoformat(), *ids)).fetchall()
        by_type = conn.execute(f"""
            SELECT lr.leave_type, COUNT(*) AS requests_count, COALESCE(SUM(lr.days_count),0) AS days
            FROM leave_requests lr
            WHERE lr.status = 'zaakceptowany'
              AND lr.date_from <= ? AND lr.date_to >= ?
              AND lr.user_id IN ({placeholders})
            GROUP BY lr.leave_type
            ORDER BY days DESC
        """, (end.isoformat(), start.isoformat(), *ids)).fetchall()
    conn.close()
    return render_template("reports.html", rows=rows, by_department=by_department, by_type=by_type, selected_year=year, selected_month=month)


@bp.route("/settings", methods=["GET", "POST"])
@login_required
@role_required("admin", "kadry")
def settings_view():
    conn = get_db()
    if request.method == "POST":
        department_name = request.form.get("department_name", "").strip()
        if department_name:
            conn.execute("INSERT OR IGNORE INTO departments (name) VALUES (?)", (department_name,))
            log_action(conn, "dodano dział", "department", None, department_name)
            conn.commit()
            flash("Dział zapisany.")
    departments = conn.execute("SELECT * FROM departments ORDER BY name").fetchall()
    logs = conn.execute("""
        SELECT al.*, u.full_name AS actor_name
        FROM audit_logs al
        LEFT JOIN users u ON al.actor_user_id = u.id
        ORDER BY al.created_at DESC LIMIT 25
    """).fetchall()
    conn.close()
    return render_template("settings.html", departments=departments, logs=logs, leave_types=LEAVE_TYPES)


@bp.route("/audit")
@login_required
@role_required("admin", "kadry")
def audit_view():
    conn = get_db()
    q = request.args.get("q", "").strip()
    actor_id = request.args.get("actor_id", "").strip()
    entity_type = request.args.get("entity_type", "").strip()
    action = request.args.get("action", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    filters = ["1=1"]
    params = []
    if q:
        filters.append("(al.action LIKE ? OR al.details LIKE ? OR actor.full_name LIKE ? OR target_user.full_name LIKE ? OR CAST(al.entity_id AS TEXT) LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])
    if actor_id:
        filters.append("al.actor_user_id = ?")
        params.append(actor_id)
    if entity_type:
        filters.append("al.entity_type = ?")
        params.append(entity_type)
    if action:
        filters.append("al.action = ?")
        params.append(action)
    if date_from:
        filters.append("substr(al.created_at, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        filters.append("substr(al.created_at, 1, 10) <= ?")
        params.append(date_to)

    logs = conn.execute(f"""
        SELECT al.*, actor.full_name AS actor_name,
               target_user.full_name AS target_user_name,
               lr.leave_type AS leave_type,
               lr.date_from AS leave_from,
               lr.date_to AS leave_to,
               CASE
                   WHEN al.entity_type = 'user' THEN COALESCE(target_user.full_name, 'Użytkownik #' || COALESCE(al.entity_id, '—'))
                   WHEN al.entity_type = 'leave_request' THEN 'Wniosek #' || COALESCE(al.entity_id, '—') || COALESCE(' · ' || lr.leave_type || ' · ' || lr.date_from || ' - ' || lr.date_to, '')
                   WHEN al.entity_type = 'department' THEN 'Dział'
                   ELSE al.entity_type || ' #' || COALESCE(al.entity_id, '—')
               END AS object_label
        FROM audit_logs al
        LEFT JOIN users actor ON al.actor_user_id = actor.id
        LEFT JOIN users target_user ON al.entity_type = 'user' AND al.entity_id = target_user.id
        LEFT JOIN leave_requests lr ON al.entity_type = 'leave_request' AND al.entity_id = lr.id
        WHERE {' AND '.join(filters)}
        ORDER BY al.created_at DESC
        LIMIT 500
    """, params).fetchall()

    actors = conn.execute("""
        SELECT DISTINCT u.id, u.full_name
        FROM audit_logs al
        JOIN users u ON u.id = al.actor_user_id
        ORDER BY u.full_name
    """).fetchall()
    entity_types = conn.execute("SELECT DISTINCT entity_type FROM audit_logs ORDER BY entity_type").fetchall()
    actions = conn.execute("SELECT DISTINCT action FROM audit_logs ORDER BY action").fetchall()
    conn.close()
    return render_template("audit.html", logs=logs, actors=actors, entity_types=entity_types, actions=actions)
