from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .config import LEAVE_TYPES, LIMIT_TYPES, STATUSES
from .database import get_db
from .services import login_required, current_user, visible_user_ids, vacation_summary, count_workdays, parse_date, log_action, is_hr, is_manager

bp = Blueprint("requests", __name__)


def _query_requests(conn):
    ids = visible_user_ids(conn)
    if not ids:
        return []
    filters = [f"lr.user_id IN ({','.join('?' for _ in ids)})"]
    params = list(ids)
    for field, column in [("department", "u.department"), ("status", "lr.status"), ("leave_type", "lr.leave_type"), ("manager_id", "u.manager_id")]:
        value = request.args.get(field, "").strip()
        if value:
            filters.append(f"{column}=?")
            params.append(value)
    employee = request.args.get("employee", "").strip()
    if employee:
        filters.append("u.full_name LIKE ?")
        params.append(f"%{employee}%")
    if request.args.get("date_from"):
        filters.append("lr.date_to >= ?")
        params.append(request.args.get("date_from"))
    if request.args.get("date_to"):
        filters.append("lr.date_from <= ?")
        params.append(request.args.get("date_to"))
    return conn.execute(f"""
        SELECT lr.*, u.full_name, u.department, m.full_name AS manager_name, d.full_name AS decider_name, r.full_name AS replacement_name
        FROM leave_requests lr
        JOIN users u ON u.id=lr.user_id
        LEFT JOIN users m ON u.manager_id=m.id
        LEFT JOIN users d ON lr.decided_by=d.id
        LEFT JOIN users r ON lr.replacement_user_id=r.id
        WHERE {' AND '.join(filters)} ORDER BY lr.created_at DESC
    """, params).fetchall()


@bp.route("/leave/new", methods=["GET", "POST"])
@login_required
def new_leave_request():
    conn = get_db()
    user = current_user(conn)
    summary = vacation_summary(conn, user)
    employees = conn.execute("SELECT id, full_name FROM users WHERE active=1 AND id!=? ORDER BY full_name", (user["id"],)).fetchall()
    if request.method == "POST":
        leave_type = request.form.get("leave_type")
        date_from = request.form.get("date_from")
        date_to = request.form.get("date_to")
        try:
            days = count_workdays(parse_date(date_from), parse_date(date_to))
        except Exception as error:
            flash(str(error))
            conn.close()
            return redirect(url_for("requests.new_leave_request"))
        if days <= 0:
            flash("Wybrany zakres nie zawiera dni roboczych.")
            conn.close()
            return redirect(url_for("requests.new_leave_request"))
        if leave_type in LIMIT_TYPES and days > summary["available"]:
            flash(f"Brak limitu. Dostępne: {summary['available']} dni, wybrano: {days} dni.")
            conn.close()
            return redirect(url_for("requests.new_leave_request"))
        cur = conn.execute("""
            INSERT INTO leave_requests (user_id, leave_type, date_from, date_to, days_count, comment, replacement_user_id, attachment_note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (user["id"], leave_type, date_from, date_to, days, request.form.get("comment"), request.form.get("replacement_user_id") or None, request.form.get("attachment_note")))
        log_action(conn, "złożono wniosek", "leave_request", cur.lastrowid, f"{leave_type}: {date_from} - {date_to}")
        conn.commit()
        conn.close()
        flash(f"Wniosek wysłany. System policzył {days} dni roboczych.")
        return redirect(url_for("requests.requests_view"))
    conn.close()
    return render_template("leave_form.html", user=user, employees=employees, vacation_summary=summary, leave_types=LEAVE_TYPES)


@bp.route("/new-request", methods=["POST"])
@login_required
def new_request():
    return new_leave_request()


@bp.route("/requests")
@login_required
def requests_view():
    conn = get_db()
    rows = _query_requests(conn)
    departments = conn.execute("SELECT name FROM departments ORDER BY name").fetchall()
    managers = conn.execute("SELECT id, full_name FROM users WHERE role IN ('menedzer','admin','kadry') ORDER BY full_name").fetchall()
    conn.close()
    return render_template("requests.html", requests_list=rows, departments=departments, managers=managers, statuses=STATUSES, leave_types=LEAVE_TYPES)


@bp.route("/request/<int:request_id>/<action>", methods=["POST"])
@login_required
def change_request_status(request_id, action):
    status_map = {"accept": "zaakceptowany", "reject": "odrzucony", "cancel": "anulowany", "return": "cofniety"}
    if action not in status_map:
        flash("Nieznana akcja.")
        return redirect(url_for("requests.requests_view"))
    conn = get_db()
    leave_request = conn.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone()
    if not leave_request:
        conn.close(); flash("Nie znaleziono wniosku."); return redirect(url_for("requests.requests_view"))
    owner = conn.execute("SELECT * FROM users WHERE id=?", (leave_request["user_id"],)).fetchone()
    can_decide = is_hr() or (is_manager() and owner and owner["manager_id"] == session["user_id"])
    can_cancel = leave_request["user_id"] == session["user_id"] and leave_request["status"] == "oczekuje"
    if action in {"accept", "reject", "return"} and not can_decide:
        conn.close(); flash("Brak uprawnień do decyzji."); return redirect(url_for("requests.requests_view"))
    if action == "cancel" and not (can_cancel or can_decide):
        conn.close(); flash("Nie można anulować tego wniosku."); return redirect(url_for("requests.requests_view"))
    new_status = status_map[action]
    comment = request.form.get("decision_comment", "")
    conn.execute("UPDATE leave_requests SET status=?, decision_comment=?, decided_by=?, decided_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status, comment, session["user_id"], request_id))
    log_action(conn, f"zmieniono status na {new_status}", "leave_request", request_id, comment)
    conn.commit(); conn.close()
    flash(f"Status wniosku zmieniony na: {new_status}.")
    return redirect(url_for("requests.requests_view"))


@bp.route("/reports/export.csv")
@login_required
def export_report_csv():
    import csv, io
    from flask import Response
    conn = get_db(); rows = _query_requests(conn); conn.close()
    output = io.StringIO(); writer = csv.writer(output, delimiter=";")
    writer.writerow(["Pracownik", "Dział", "Typ", "Od", "Do", "Dni", "Status", "Menedżer", "Data złożenia"])
    for row in rows:
        writer.writerow([row["full_name"], row["department"], row["leave_type"], row["date_from"], row["date_to"], row["days_count"], row["status"], row["manager_name"] or "", row["created_at"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=emerlog_urlopy.csv"})
