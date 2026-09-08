from datetime import date

from flask import jsonify, render_template, request, session

from .config import normalize_contract_type
from .database import get_db
from .services import login_required, log_action, polish_holidays, role_required, surname_first
from .routes_timesheets_v2 import (
    MONTH_NAMES,
    _employee,
    _employee_context,
    _month_absences,
    _rows_from_json,
    _selected_month,
    _serialize_saved,
    _timesheet_status,
    _total_hours,
    _upsert_timesheet,
    _validate_rows,
)


HR_ROLES = {"admin", "kadry"}


def _is_hr_session():
    return session.get("role") in HR_ROLES


def _validate_payload(payload):
    year = int(payload.get("year"))
    month = int(payload.get("month"))
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise ValueError("Niepoprawny miesiąc rozliczenia.")
    rows = _validate_rows(payload.get("rows"), year, month)
    target_raw = payload.get("target_hours")
    target_hours = None if target_raw in (None, "") else float(target_raw)
    if target_hours is not None and (target_hours < 0 or target_hours > 744):
        raise ValueError("Łączna liczba godzin jest poza dozwolonym zakresem.")
    return year, month, rows, target_hours


def _submitted_row(conn, user_id, year, month):
    return conn.execute(
        """
        SELECT id, last_sent_at
        FROM hour_timesheets
        WHERE user_id = ? AND year = ? AND month = ?
        """,
        (user_id, year, month),
    ).fetchone()


def _closed_response(month):
    month_name = MONTH_NAMES[month - 1].lower()
    return jsonify({
        "ok": False,
        "closed": True,
        "error": f"Rozliczenie za {month_name} zostało już przesłane do Kadr.",
    }), 409


def register_timesheet_routes(bp):
    @bp.route("/kadry/rozliczenia-godzin")
    @bp.route("/rozliczenie-godzin")
    @login_required
    def hours_view():
        selected_month, year, month, month_start, month_end = _selected_month(request.args.get("month"))
        conn = get_db()
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.close()
            return render_template(
                "timesheets_v3.html",
                selected_month=selected_month,
                timesheet_context={"employee": None},
            )

        absences = _month_absences(conn, employee["id"], month_start, month_end)
        saved_row = conn.execute(
            """
            SELECT * FROM hour_timesheets
            WHERE user_id = ? AND year = ? AND month = ?
            """,
            (employee["id"], year, month),
        ).fetchone()
        saved = _serialize_saved(saved_row)
        holidays = sorted(day.isoformat() for day in polish_holidays(year) if day.month == month)
        conn.close()

        return render_template(
            "timesheets_v3.html",
            selected_month=selected_month,
            timesheet_context={
                "employee": _employee_context(employee),
                "year": year,
                "month": month,
                "month_name": MONTH_NAMES[month - 1],
                "holidays": holidays,
                "absences": absences,
                "saved": saved,
                "is_submitted": bool(saved and saved.get("last_sent_at")),
            },
        )

    @bp.route("/kadry/rozliczenia-godzin/save", methods=["POST"])
    @bp.route("/rozliczenie-godzin/save", methods=["POST"])
    @login_required
    def hours_save():
        payload = request.get_json(silent=True) or {}
        try:
            year, month, rows, target_hours = _validate_payload(payload)
        except (TypeError, ValueError) as error:
            return jsonify({"ok": False, "error": str(error) or "Niepoprawne dane rozliczenia."}), 400

        conn = get_db()
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.close()
            return jsonify({"ok": False, "error": "Nie znaleziono aktywnego pracownika."}), 404

        existing = _submitted_row(conn, employee["id"], year, month)
        if existing and existing["last_sent_at"] and not _is_hr_session():
            conn.close()
            return _closed_response(month)

        timesheet_id = _upsert_timesheet(conn, employee, year, month, rows, target_hours, submitted=False)
        total = round(sum(float(row["hours"] or 0) for row in rows), 2)
        log_action(
            conn,
            "zapisano rozliczenie godzin",
            "hour_timesheet",
            timesheet_id,
            f"{employee['full_name']} | {year}-{month:02d} | {total} h",
        )
        conn.commit()
        saved = conn.execute("SELECT updated_at FROM hour_timesheets WHERE id = ?", (timesheet_id,)).fetchone()
        conn.close()
        return jsonify({"ok": True, "id": timesheet_id, "updated_at": saved["updated_at"] if saved else None})

    @bp.route("/kadry/rozliczenia-godzin/send", methods=["POST"])
    @bp.route("/rozliczenie-godzin/submit", methods=["POST"])
    @login_required
    def hours_send():
        payload = request.get_json(silent=True) or {}
        try:
            year, month, rows, target_hours = _validate_payload(payload)
        except (TypeError, ValueError) as error:
            return jsonify({"ok": False, "error": str(error) or "Niepoprawne dane rozliczenia."}), 400

        conn = get_db()
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.close()
            return jsonify({"ok": False, "error": "Nie znaleziono aktywnego pracownika."}), 404

        existing = _submitted_row(conn, employee["id"], year, month)
        if existing and existing["last_sent_at"] and not _is_hr_session():
            conn.close()
            return _closed_response(month)

        timesheet_id = _upsert_timesheet(conn, employee, year, month, rows, target_hours, submitted=True)
        total = round(sum(float(row["hours"] or 0) for row in rows), 2)
        log_action(
            conn,
            "przesłano rozliczenie godzin do Kadr",
            "hour_timesheet",
            timesheet_id,
            f"{employee['full_name']} | {year}-{month:02d} | {total} h",
        )
        conn.commit()
        sent = conn.execute("SELECT last_sent_at FROM hour_timesheets WHERE id = ?", (timesheet_id,)).fetchone()
        conn.close()
        return jsonify({"ok": True, "id": timesheet_id, "last_sent_at": sent["last_sent_at"] if sent else None})

    @bp.route("/kadry/rozliczenia-pracownikow")
    @login_required
    @role_required("admin", "kadry")
    def hours_inbox():
        year_raw = (request.args.get("year") or "").strip()
        month_raw = (request.args.get("month") or "").strip()
        employee_raw = (request.args.get("employee") or "").strip()
        filters = ["ht.last_sent_at IS NOT NULL"]
        params = []
        if year_raw.isdigit():
            filters.append("ht.year = ?")
            params.append(int(year_raw))
        if month_raw.isdigit() and 1 <= int(month_raw) <= 12:
            filters.append("ht.month = ?")
            params.append(int(month_raw))
        if employee_raw.isdigit():
            filters.append("ht.user_id = ?")
            params.append(int(employee_raw))

        conn = get_db()
        rows = conn.execute(
            f"""
            SELECT ht.*, u.full_name, u.department, c.name AS company_name
            FROM hour_timesheets ht
            JOIN users u ON u.id = ht.user_id
            LEFT JOIN companies c ON c.id = u.company_id
            WHERE {' AND '.join(filters)}
            """,
            params,
        ).fetchall()
        employees = conn.execute(
            """
            SELECT DISTINCT u.id, u.full_name
            FROM hour_timesheets ht
            JOIN users u ON u.id = ht.user_id
            WHERE ht.last_sent_at IS NOT NULL
            """
        ).fetchall()
        conn.close()

        items = [{
            "id": row["id"],
            "user_id": row["user_id"],
            "full_name": row["full_name"],
            "year": row["year"],
            "month": row["month"],
            "contract_type": row["contract_type"],
            "department": row["department"],
            "company_name": row["company_name"],
            "total_hours": _total_hours(row["rows_json"]),
            "submitted_at": row["last_sent_at"],
            "updated_at": row["updated_at"],
            "status": _timesheet_status(row),
        } for row in rows]
        items.sort(key=lambda item: (
            surname_first(item["full_name"]).casefold(),
            -int(item["year"]),
            -int(item["month"]),
        ))
        employees = sorted(employees, key=lambda row: surname_first(row["full_name"]).casefold())
        return render_template(
            "timesheets_hr_inbox_v2.html",
            items=items,
            employees=employees,
            selected_year=year_raw,
            selected_month=month_raw,
            selected_employee=employee_raw,
            current_year=date.today().year,
        )

    @bp.route("/kadry/rozliczenia-pracownikow/<int:timesheet_id>")
    @login_required
    @role_required("admin", "kadry")
    def hours_detail(timesheet_id):
        conn = get_db()
        row = conn.execute(
            """
            SELECT ht.*, u.full_name, u.department, c.name AS company_name
            FROM hour_timesheets ht
            JOIN users u ON u.id = ht.user_id
            LEFT JOIN companies c ON c.id = u.company_id
            WHERE ht.id = ?
            """,
            (timesheet_id,),
        ).fetchone()
        conn.close()
        if not row:
            return "Nie znaleziono rozliczenia.", 404
        item = {
            "id": row["id"],
            "user_id": row["user_id"],
            "full_name": row["full_name"],
            "year": row["year"],
            "month": row["month"],
            "contract_type": row["contract_type"],
            "fte_percent": row["fte_percent"],
            "department": row["department"],
            "company_name": row["company_name"],
            "target_hours": row["target_hours"],
            "total_hours": _total_hours(row["rows_json"]),
            "submitted_at": row["last_sent_at"],
            "updated_at": row["updated_at"],
            "status": _timesheet_status(row),
            "rows": _rows_from_json(row["rows_json"]),
        }
        return render_template("timesheet_hr_detail_v2.html", item=item)

    @bp.route("/kadry/rozliczenia-pracownikow/pracownik/<int:user_id>.json")
    @login_required
    @role_required("admin", "kadry")
    def hours_employee_history_json(user_id):
        conn = get_db()
        rows = conn.execute(
            """
            SELECT id, year, month, contract_type, target_hours, rows_json,
                   last_sent_at, updated_at, created_at
            FROM hour_timesheets
            WHERE user_id = ?
            ORDER BY year DESC, month DESC
            """,
            (user_id,),
        ).fetchall()
        conn.close()
        items = [{
            "id": row["id"],
            "year": row["year"],
            "month": row["month"],
            "contract_type": row["contract_type"],
            "target_hours": row["target_hours"],
            "total_hours": _total_hours(row["rows_json"]),
            "submitted_at": row["last_sent_at"],
            "updated_at": row["updated_at"],
            "status": _timesheet_status(row),
            "detail_url": f"/kadry/rozliczenia-pracownikow/{row['id']}",
        } for row in rows]
        return jsonify({"ok": True, "items": items})
