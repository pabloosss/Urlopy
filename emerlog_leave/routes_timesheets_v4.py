from collections import OrderedDict
from datetime import date

from flask import flash, jsonify, redirect, render_template, request, session, url_for

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
)
from .routes_timesheets_v3 import (
    _total_hours,
    _total_overtime,
    _upsert_timesheet,
    _validate_payload,
)


def _ensure_submission_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hour_timesheet_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timesheet_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            version_no INTEGER NOT NULL,
            contract_type TEXT NOT NULL,
            fte_percent INTEGER NOT NULL DEFAULT 100,
            target_hours REAL,
            rows_json TEXT NOT NULL,
            submitted_by INTEGER,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            note TEXT,
            UNIQUE (timesheet_id, version_no),
            FOREIGN KEY (timesheet_id) REFERENCES hour_timesheets(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (submitted_by) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hour_timesheet_submissions_user_month "
        "ON hour_timesheet_submissions(user_id, year, month, version_no)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hour_timesheet_submissions_sent "
        "ON hour_timesheet_submissions(submitted_at DESC)"
    )

    # Zachowujemy dotychczasowe przesłane rozliczenia jako pierwszą wersję.
    conn.execute(
        """
        INSERT INTO hour_timesheet_submissions (
            timesheet_id, user_id, year, month, version_no, contract_type,
            fte_percent, target_hours, rows_json, submitted_by, submitted_at, note
        )
        SELECT
            ht.id, ht.user_id, ht.year, ht.month, 1, ht.contract_type,
            ht.fte_percent, ht.target_hours, ht.rows_json, ht.updated_by,
            ht.last_sent_at, NULL
        FROM hour_timesheets ht
        WHERE ht.last_sent_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM hour_timesheet_submissions hs
              WHERE hs.timesheet_id = ht.id
          )
        """
    )


def _submission_info(conn, user_id, year, month):
    row = conn.execute(
        """
        SELECT COUNT(*) AS versions,
               MAX(version_no) AS latest_version,
               MAX(submitted_at) AS latest_submitted_at
        FROM hour_timesheet_submissions
        WHERE user_id = ? AND year = ? AND month = ?
        """,
        (user_id, year, month),
    ).fetchone()
    return {
        "count": int(row["versions"] or 0),
        "latest_version": int(row["latest_version"] or 0),
        "latest_submitted_at": row["latest_submitted_at"],
    }


def _insert_submission(conn, timesheet_id, submitted_by):
    current = conn.execute(
        "SELECT * FROM hour_timesheets WHERE id = ?",
        (timesheet_id,),
    ).fetchone()
    if not current:
        raise ValueError("Nie znaleziono rozliczenia do zapisania.")

    next_version = conn.execute(
        """
        SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version
        FROM hour_timesheet_submissions
        WHERE timesheet_id = ?
        """,
        (timesheet_id,),
    ).fetchone()["next_version"]

    cur = conn.execute(
        """
        INSERT INTO hour_timesheet_submissions (
            timesheet_id, user_id, year, month, version_no, contract_type,
            fte_percent, target_hours, rows_json, submitted_by, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            current["id"],
            current["user_id"],
            current["year"],
            current["month"],
            next_version,
            current["contract_type"],
            current["fte_percent"],
            current["target_hours"],
            current["rows_json"],
            submitted_by,
        ),
    )
    submission = conn.execute(
        "SELECT id, version_no, submitted_at FROM hour_timesheet_submissions WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return submission


def _submission_item(row):
    return {
        "id": row["id"],
        "timesheet_id": row["timesheet_id"],
        "user_id": row["user_id"],
        "full_name": row["full_name"],
        "year": row["year"],
        "month": row["month"],
        "version_no": row["version_no"],
        "contract_type": row["contract_type"],
        "fte_percent": row["fte_percent"],
        "department": row["department"],
        "company_name": row["company_name"],
        "target_hours": row["target_hours"],
        "total_hours": _total_hours(row["rows_json"]),
        "overtime_hours": _total_overtime(row["rows_json"]),
        "submitted_at": row["submitted_at"],
        "rows": _rows_from_json(row["rows_json"]),
        "status": "Przesłane",
        "note": row["note"] or "",
    }


def register_timesheet_routes(bp):
    @bp.route("/kadry/rozliczenia-godzin")
    @bp.route("/rozliczenie-godzin")
    @login_required
    def hours_view():
        selected_month, year, month, month_start, month_end = _selected_month(request.args.get("month"))
        conn = get_db()
        _ensure_submission_table(conn)
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.commit()
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
        info = _submission_info(conn, employee["id"], year, month)
        holidays = sorted(day.isoformat() for day in polish_holidays(year) if day.month == month)
        conn.commit()
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
                "was_submitted": info["count"] > 0,
                "is_submitted": info["count"] > 0,
                "correction_open": False,
                "correction_reason": "",
                "submission_count": info["count"],
                "latest_version": info["latest_version"],
                "latest_submission_at": info["latest_submitted_at"],
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
        _ensure_submission_table(conn)
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.close()
            return jsonify({"ok": False, "error": "Nie znaleziono aktywnego pracownika."}), 404

        timesheet_id = _upsert_timesheet(conn, employee, year, month, rows, target_hours, submitted=False)
        log_action(
            conn,
            "zapisano robocze rozliczenie godzin",
            "hour_timesheet",
            timesheet_id,
            f"{employee['full_name']} | {year}-{month:02d}",
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
        _ensure_submission_table(conn)
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.close()
            return jsonify({"ok": False, "error": "Nie znaleziono aktywnego pracownika."}), 404

        previous = _submission_info(conn, employee["id"], year, month)
        timesheet_id = _upsert_timesheet(conn, employee, year, month, rows, target_hours, submitted=True)
        submission = _insert_submission(conn, timesheet_id, session["user_id"])
        total = round(sum(float(row.get("hours") or 0) + float(row.get("overtime") or 0) for row in rows), 2)
        overtime = round(sum(float(row.get("overtime") or 0) for row in rows), 2)
        log_action(
            conn,
            "przesłano kolejną wersję rozliczenia godzin do Kadr" if previous["count"] else "przesłano rozliczenie godzin do Kadr",
            "hour_timesheet",
            timesheet_id,
            f"{employee['full_name']} | {year}-{month:02d} | wersja {submission['version_no']} | {total} h | nadgodziny {overtime} h",
        )
        conn.commit()
        result = {
            "ok": True,
            "id": timesheet_id,
            "submission_id": submission["id"],
            "version_no": submission["version_no"],
            "last_sent_at": submission["submitted_at"],
        }
        conn.close()
        return jsonify(result)

    @bp.route("/kadry/rozliczenia-pracownikow")
    @login_required
    @role_required("admin", "kadry")
    def hours_inbox():
        year_raw = (request.args.get("year") or "").strip()
        month_raw = (request.args.get("month") or "").strip()
        employee_raw = (request.args.get("employee") or "").strip()
        filters = ["1=1"]
        params = []
        if year_raw.isdigit():
            filters.append("hs.year = ?")
            params.append(int(year_raw))
        if month_raw.isdigit() and 1 <= int(month_raw) <= 12:
            filters.append("hs.month = ?")
            params.append(int(month_raw))
        if employee_raw.isdigit():
            filters.append("hs.user_id = ?")
            params.append(int(employee_raw))

        conn = get_db()
        _ensure_submission_table(conn)
        rows = conn.execute(
            f"""
            SELECT hs.*, u.full_name, u.department, c.name AS company_name
            FROM hour_timesheet_submissions hs
            JOIN users u ON u.id = hs.user_id
            LEFT JOIN companies c ON c.id = u.company_id
            WHERE {' AND '.join(filters)}
            ORDER BY hs.user_id, hs.year DESC, hs.month DESC, hs.version_no DESC
            """,
            params,
        ).fetchall()
        employees = conn.execute(
            """
            SELECT DISTINCT u.id, u.full_name
            FROM hour_timesheet_submissions hs
            JOIN users u ON u.id = hs.user_id
            """
        ).fetchall()
        conn.commit()
        conn.close()

        grouped = OrderedDict()
        for row in rows:
            item = _submission_item(row)
            key = (item["user_id"], item["year"], item["month"])
            if key not in grouped:
                grouped[key] = {
                    "user_id": item["user_id"],
                    "full_name": item["full_name"],
                    "year": item["year"],
                    "month": item["month"],
                    "month_name": MONTH_NAMES[item["month"] - 1],
                    "company_name": item["company_name"],
                    "department": item["department"],
                    "versions": [],
                }
            grouped[key]["versions"].append(item)

        groups = list(grouped.values())
        for group in groups:
            group["versions"].sort(key=lambda item: (int(item["version_no"]), item["submitted_at"] or ""), reverse=True)
            group["latest"] = group["versions"][0]
            group["versions_count"] = len(group["versions"])
        groups.sort(key=lambda group: (
            surname_first(group["full_name"]).casefold(),
            -int(group["year"]),
            -int(group["month"]),
        ))
        employees = sorted(employees, key=lambda row: surname_first(row["full_name"]).casefold())

        return render_template(
            "timesheets_hr_inbox_v3.html",
            groups=groups,
            employees=employees,
            selected_year=year_raw,
            selected_month=month_raw,
            selected_employee=employee_raw,
            current_year=date.today().year,
        )

    @bp.route("/kadry/rozliczenia-pracownikow/wersja/<int:submission_id>")
    @login_required
    @role_required("admin", "kadry")
    def hours_detail(submission_id):
        conn = get_db()
        _ensure_submission_table(conn)
        row = conn.execute(
            """
            SELECT hs.*, u.full_name, u.department, c.name AS company_name
            FROM hour_timesheet_submissions hs
            JOIN users u ON u.id = hs.user_id
            LEFT JOIN companies c ON c.id = u.company_id
            WHERE hs.id = ?
            """,
            (submission_id,),
        ).fetchone()
        conn.commit()
        conn.close()
        if not row:
            return "Nie znaleziono wersji rozliczenia.", 404
        return render_template("timesheet_hr_detail_v3.html", item=_submission_item(row))

    @bp.route("/kadry/rozliczenia-pracownikow/<int:timesheet_id>")
    @login_required
    @role_required("admin", "kadry")
    def hours_latest_detail(timesheet_id):
        conn = get_db()
        _ensure_submission_table(conn)
        latest = conn.execute(
            """
            SELECT id FROM hour_timesheet_submissions
            WHERE timesheet_id = ?
            ORDER BY version_no DESC, id DESC
            LIMIT 1
            """,
            (timesheet_id,),
        ).fetchone()
        conn.commit()
        conn.close()
        if not latest:
            flash("Nie znaleziono przesłanej wersji rozliczenia.")
            return redirect(url_for("hr_tools.hours_inbox"))
        return redirect(url_for("hr_tools.hours_detail", submission_id=latest["id"]))

    @bp.route("/kadry/rozliczenia-pracownikow/<int:timesheet_id>/reopen", methods=["POST"])
    @login_required
    @role_required("admin", "kadry")
    def hours_reopen(timesheet_id):
        flash("Nie trzeba już otwierać korekty. Pracownik może wygenerować i przesłać kolejną wersję, a poprzednie zostają w historii.")
        return redirect(url_for("hr_tools.hours_latest_detail", timesheet_id=timesheet_id))

    @bp.route("/kadry/rozliczenia-pracownikow/pracownik/<int:user_id>.json")
    @login_required
    @role_required("admin", "kadry")
    def hours_employee_history_json(user_id):
        conn = get_db()
        _ensure_submission_table(conn)
        rows = conn.execute(
            """
            SELECT hs.*
            FROM hour_timesheet_submissions hs
            WHERE hs.user_id = ?
            ORDER BY hs.year DESC, hs.month DESC, hs.version_no DESC
            """,
            (user_id,),
        ).fetchall()
        conn.commit()
        conn.close()

        latest_by_month = OrderedDict()
        for row in rows:
            key = (row["year"], row["month"])
            if key not in latest_by_month:
                latest_by_month[key] = {
                    "id": row["id"],
                    "year": row["year"],
                    "month": row["month"],
                    "contract_type": row["contract_type"],
                    "target_hours": row["target_hours"],
                    "total_hours": _total_hours(row["rows_json"]),
                    "overtime_hours": _total_overtime(row["rows_json"]),
                    "submitted_at": row["submitted_at"],
                    "updated_at": row["submitted_at"],
                    "status": "Przesłane",
                    "detail_url": f"/kadry/rozliczenia-pracownikow/wersja/{row['id']}",
                    "versions_count": 1,
                }
            else:
                latest_by_month[key]["versions_count"] += 1
        return jsonify({"ok": True, "items": list(latest_by_month.values())})
