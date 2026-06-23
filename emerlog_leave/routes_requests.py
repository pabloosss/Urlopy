from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .config import LEAVE_TYPES, LIMIT_TYPES, STATUSES
from .database import get_db
from .services import login_required, current_user, visible_user_ids, vacation_summary, count_workdays, parse_date, log_action, is_hr, is_manager

bp = Blueprint("requests", __name__)

LEAVE_TYPE_DESCRIPTIONS = {
    "Urlop wypoczynkowy": "Standardowy urlop z limitu rocznego.",
    "Urlop na żądanie": "Pilny urlop z limitu rocznego.",
    "Urlop okolicznościowy": "Ślub, pogrzeb, narodziny dziecka lub inna okoliczność.",
    "L4 / chorobowe": "Nieobecność chorobowa. Wpisz informację w komentarzu.",
    "Urlop bezpłatny": "Nie schodzi z limitu urlopu wypoczynkowego.",
    "Odbiór dnia wolnego": "Odbiór za pracę w innym terminie.",
    "Praca zdalna": "Dzień pracy poza biurem.",
    "Delegacja": "Wyjazd służbowy.",
    "Inne": "Nietypowa nieobecność lub sprawa do opisania w komentarzu.",
}


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
    manager_name = "Kadry / przełożony"
    if user["manager_id"]:
        manager = conn.execute("SELECT full_name FROM users WHERE id=?", (user["manager_id"],)).fetchone()
        if manager:
            manager_name = manager["full_name"]

    form_data = {
        "leave_type": "Urlop wypoczynkowy",
        "date_from": "",
        "date_to": "",
        "replacement_user_id": "",
        "comment": "",
        "attachment_note": "",
    }

    def render_form(status_code=200):
        conn.close()
        return render_template(
            "leave_form.html",
            user=user,
            employees=employees,
            vacation_summary=summary,
            leave_types=LEAVE_TYPES,
            limit_types=sorted(LIMIT_TYPES),
            leave_type_descriptions=LEAVE_TYPE_DESCRIPTIONS,
            manager_name=manager_name,
            today=date.today().isoformat(),
            form_data=form_data,
        ), status_code

    if request.method == "POST":
        form_data = {
            "leave_type": request.form.get("leave_type", "").strip(),
            "date_from": request.form.get("date_from", "").strip(),
            "date_to": request.form.get("date_to", "").strip(),
            "replacement_user_id": request.form.get("replacement_user_id", "").strip(),
            "comment": request.form.get("comment", "").strip(),
            "attachment_note": request.form.get("attachment_note", "").strip(),
        }
        errors = []
        days = 0
        start = None
        end = None
        replacement_id = None

        if form_data["leave_type"] not in LEAVE_TYPES:
            errors.append("Wybierz poprawny typ nieobecności.")

        if not form_data["date_from"] or not form_data["date_to"]:
            errors.append("Uzupełnij datę od i datę do.")
        else:
            try:
                start = parse_date(form_data["date_from"])
                end = parse_date(form_data["date_to"])
                days = count_workdays(start, end)
            except Exception as error:
                errors.append(str(error))

        if days <= 0 and start and end:
            errors.append("Wybrany zakres nie zawiera dni roboczych.")

        if form_data["replacement_user_id"]:
            try:
                replacement_id = int(form_data["replacement_user_id"])
            except ValueError:
                errors.append("Wybierz poprawną osobę na zastępstwo.")
                replacement_id = None
            if replacement_id == user["id"]:
                errors.append("Nie możesz wybrać siebie jako zastępstwa.")
            elif replacement_id:
                replacement = conn.execute("SELECT id FROM users WHERE id=? AND active=1", (replacement_id,)).fetchone()
                if not replacement:
                    errors.append("Wybrana osoba na zastępstwo nie istnieje albo jest nieaktywna.")

        if not errors and form_data["leave_type"] in LIMIT_TYPES and days > summary["available"]:
            errors.append(f"Brak limitu. Dostępne: {summary['available']} dni, wybrano: {days} dni.")

        if not errors:
            overlap = conn.execute(
                """
                SELECT leave_type, date_from, date_to, status
                FROM leave_requests
                WHERE user_id=?
                  AND status IN ('oczekuje', 'zaakceptowany')
                  AND date_from <= ?
                  AND date_to >= ?
                ORDER BY date_from
                LIMIT 1
                """,
                (user["id"], form_data["date_to"], form_data["date_from"]),
            ).fetchone()
            if overlap:
                errors.append(
                    f"Masz już aktywny wniosek w tym terminie: {overlap['leave_type']} "
                    f"({overlap['date_from']} - {overlap['date_to']}, status: {overlap['status']})."
                )

        if len(form_data["comment"]) > 1000:
            errors.append("Komentarz jest za długi. Maksymalnie 1000 znaków.")
        if len(form_data["attachment_note"]) > 255:
            errors.append("Opis załącznika jest za długi. Maksymalnie 255 znaków.")

        if errors:
            for error in errors:
                flash(error)
            return render_form(400)

        cur = conn.execute("""
            INSERT INTO leave_requests (user_id, leave_type, date_from, date_to, days_count, comment, replacement_user_id, attachment_note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (user["id"], form_data["leave_type"], form_data["date_from"], form_data["date_to"], days, form_data["comment"], replacement_id, form_data["attachment_note"]))
        log_action(conn, "złożono wniosek", "leave_request", cur.lastrowid, f"{form_data['leave_type']}: {form_data['date_from']} - {form_data['date_to']}")
        conn.commit()
        conn.close()
        flash(f"Wniosek wysłany. System policzył {days} dni roboczych.")
        return redirect(url_for("requests.requests_view"))

    return render_form()


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
