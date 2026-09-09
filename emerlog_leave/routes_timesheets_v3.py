import json
import math
import re
from datetime import date

from flask import flash, jsonify, redirect, render_template, request, session, url_for

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
    _validate_rows,
)


HR_ROLES = {"admin", "kadry"}
MAX_OVERTIME_PER_DAY = 8
MAX_TOTAL_HOURS_PER_DAY = 16


def _is_hr_session():
    return session.get("role") in HR_ROLES


def _validate_rows_with_overtime(rows, year, month):
    cleaned = _validate_rows(rows, year, month)
    raw_by_day = {}
    if isinstance(rows, list):
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            try:
                raw_by_day[int(raw.get("day"))] = raw
            except (TypeError, ValueError):
                continue

    for row in cleaned:
        raw = raw_by_day.get(row["day"], {})
        try:
            overtime = round(float(raw.get("overtime") or 0), 2)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Niepoprawne nadgodziny dla dnia {row['day']}.") from error
        if not math.isfinite(overtime) or overtime < 0 or overtime > MAX_OVERTIME_PER_DAY:
            raise ValueError(
                f"Nadgodziny dla dnia {row['day']} muszą mieścić się w zakresie 0–{MAX_OVERTIME_PER_DAY} h."
            )
        if row["off"] and overtime:
            raise ValueError(f"Nie można wpisać nadgodzin w dniu wolnym: {row['day']}.")
        if float(row["hours"] or 0) + overtime > MAX_TOTAL_HOURS_PER_DAY:
            raise ValueError(
                f"Łączny czas dla dnia {row['day']} nie może przekraczać {MAX_TOTAL_HOURS_PER_DAY} h."
            )
        row["overtime"] = overtime
        if row["off"]:
            if row["hours"] or row["start"] != "-" or row["end"] != "-":
                raise ValueError(f"Dzień wolny {row['day']} nie może zawierać godzin pracy.")
            continue
        start = _time_minutes(row["start"])
        end = _time_minutes(row["end"], allow_midnight=True)
        if end <= start or not math.isclose(
            end - start, round((row["hours"] + overtime) * 60), abs_tol=1
        ):
            raise ValueError(f"Godziny rozpoczęcia i zakończenia dnia {row['day']} nie zgadzają się z sumą godzin i nadgodzin.")
    return cleaned


def _time_minutes(value, *, allow_midnight=False):
    if allow_midnight and value == "24:00":
        return 1440
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Podaj poprawne godziny w formacie HH:MM.")
    hours, minutes = map(int, value.split(":"))
    return hours * 60 + minutes


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Niepoprawny format danych rozliczenia.")
    year = int(payload.get("year"))
    month = int(payload.get("month"))
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise ValueError("Niepoprawny miesiąc rozliczenia.")
    rows = _validate_rows_with_overtime(payload.get("rows"), year, month)
    target_raw = payload.get("target_hours")
    target_hours = None if target_raw in (None, "") else float(target_raw)
    if target_hours is not None and (not math.isfinite(target_hours) or target_hours < 0 or target_hours > 744):
        raise ValueError("Łączna liczba godzin jest poza dozwolonym zakresem.")
    return year, month, rows, target_hours


def _row_total(row):
    try:
        regular = float(row.get("hours") or 0)
    except (TypeError, ValueError, AttributeError):
        regular = 0
    try:
        overtime = float(row.get("overtime") or 0)
    except (TypeError, ValueError, AttributeError):
        overtime = 0
    return regular + overtime


def _total_hours(raw):
    return round(sum(_row_total(row) for row in _rows_from_json(raw)), 2)


def _total_overtime(raw):
    total = 0.0
    for row in _rows_from_json(raw):
        try:
            total += float(row.get("overtime") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return round(total, 2)


def _timesheet_status(row):
    if not row["last_sent_at"]:
        return "Robocze"
    if "correction_open" in row.keys() and row["correction_open"]:
        return "Do korekty"
    if row["updated_at"] and row["updated_at"] > row["last_sent_at"]:
        return "Zmienione po przesłaniu"
    return "Przesłane"


def _submitted_row(conn, user_id, year, month):
    return conn.execute(
        """
        SELECT id, last_sent_at, correction_open, correction_reason
        FROM hour_timesheets
        WHERE user_id = ? AND year = ? AND month = ?
        """,
        (user_id, year, month),
    ).fetchone()


def _closed_response(month, sent_at=None):
    month_name = MONTH_NAMES[month - 1].lower()
    return jsonify({
        "ok": False,
        "closed": True,
        "last_sent_at": sent_at,
        "error": f"Rozliczenie za {month_name} zostało już przesłane do Kadr.",
    }), 409


def _upsert_timesheet(conn, employee, year, month, rows, target_hours, *, submitted=False):
    user_id = int(employee["id"])
    contract = normalize_contract_type(employee["contract_type"])
    fte = int(employee["fte_percent"] or 100)
    rows_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    existing = conn.execute(
        "SELECT id FROM hour_timesheets WHERE user_id = ? AND year = ? AND month = ?",
        (user_id, year, month),
    ).fetchone()

    if existing:
        if submitted:
            conn.execute(
                """
                UPDATE hour_timesheets
                SET contract_type = ?, fte_percent = ?, target_hours = ?, rows_json = ?,
                    updated_by = ?, updated_at = CURRENT_TIMESTAMP,
                    last_sent_at = CURRENT_TIMESTAMP, correction_open = 0
                WHERE id = ?
                """,
                (contract, fte, target_hours, rows_json, session["user_id"], existing["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE hour_timesheets
                SET contract_type = ?, fte_percent = ?, target_hours = ?, rows_json = ?,
                    updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (contract, fte, target_hours, rows_json, session["user_id"], existing["id"]),
            )
        return existing["id"]

    cur = conn.execute(
        """
        INSERT INTO hour_timesheets (
            user_id, year, month, contract_type, fte_percent, target_hours,
            rows_json, generated_by, updated_by, last_sent_at, correction_open
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END, 0)
        """,
        (
            user_id, year, month, contract, fte, target_hours, rows_json,
            session["user_id"], session["user_id"], 1 if submitted else 0,
        ),
    )
    return cur.lastrowid


def _serialize_saved_with_correction(row):
    saved = _serialize_saved(row)
    if not saved or not row:
        return saved
    saved["correction_open"] = bool(row["correction_open"])
    saved["correction_reason"] = row["correction_reason"] or ""
    saved["correction_opened_at"] = row["correction_opened_at"]
    return saved


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
        saved = _serialize_saved_with_correction(saved_row)
        correction_open = bool(saved_row and saved_row["correction_open"])
        was_submitted = bool(saved and saved.get("last_sent_at"))
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
                "was_submitted": was_submitted,
                "correction_open": correction_open,
                "correction_reason": saved.get("correction_reason", "") if saved else "",
                "is_submitted": was_submitted and not correction_open,
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
        if existing and existing["last_sent_at"] and not existing["correction_open"] and not _is_hr_session():
            sent_at = existing["last_sent_at"]
            conn.close()
            return _closed_response(month, sent_at)

        timesheet_id = _upsert_timesheet(conn, employee, year, month, rows, target_hours, submitted=False)
        total = round(sum(_row_total(row) for row in rows), 2)
        overtime = round(sum(float(row.get("overtime") or 0) for row in rows), 2)
        log_action(
            conn,
            "zapisano rozliczenie godzin",
            "hour_timesheet",
            timesheet_id,
            f"{employee['full_name']} | {year}-{month:02d} | {total} h | nadgodziny {overtime} h",
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
        if existing and existing["last_sent_at"] and not existing["correction_open"] and not _is_hr_session():
            sent_at = existing["last_sent_at"]
            conn.close()
            return _closed_response(month, sent_at)

        resubmission = bool(existing and existing["last_sent_at"])
        timesheet_id = _upsert_timesheet(conn, employee, year, month, rows, target_hours, submitted=True)
        total = round(sum(_row_total(row) for row in rows), 2)
        overtime = round(sum(float(row.get("overtime") or 0) for row in rows), 2)
        log_action(
            conn,
            "ponownie przesłano rozliczenie godzin do Kadr" if resubmission else "przesłano rozliczenie godzin do Kadr",
            "hour_timesheet",
            timesheet_id,
            f"{employee['full_name']} | {year}-{month:02d} | {total} h | nadgodziny {overtime} h",
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
            "overtime_hours": _total_overtime(row["rows_json"]),
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
            SELECT ht.*, u.full_name, u.department, c.name AS company_name,
                   opener.full_name AS correction_opened_by_name
            FROM hour_timesheets ht
            JOIN users u ON u.id = ht.user_id
            LEFT JOIN companies c ON c.id = u.company_id
            LEFT JOIN users opener ON opener.id = ht.correction_opened_by
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
            "overtime_hours": _total_overtime(row["rows_json"]),
            "submitted_at": row["last_sent_at"],
            "updated_at": row["updated_at"],
            "status": _timesheet_status(row),
            "rows": _rows_from_json(row["rows_json"]),
            "correction_open": bool(row["correction_open"]),
            "correction_reason": row["correction_reason"] or "",
            "correction_opened_at": row["correction_opened_at"],
            "correction_opened_by_name": row["correction_opened_by_name"],
        }
        return render_template("timesheet_hr_detail_v2.html", item=item)

    @bp.route("/kadry/rozliczenia-pracownikow/<int:timesheet_id>/reopen", methods=["POST"])
    @login_required
    @role_required("admin", "kadry")
    def hours_reopen(timesheet_id):
        reason = (request.form.get("reason") or "").strip()
        if len(reason) < 5:
            flash("Podaj krótki powód otwarcia rozliczenia do korekty.")
            return redirect(url_for("hr_tools.hours_detail", timesheet_id=timesheet_id))
        if len(reason) > 500:
            reason = reason[:500]

        conn = get_db()
        row = conn.execute(
            """
            SELECT ht.*, u.full_name
            FROM hour_timesheets ht
            JOIN users u ON u.id = ht.user_id
            WHERE ht.id = ?
            """,
            (timesheet_id,),
        ).fetchone()
        if not row:
            conn.close()
            flash("Nie znaleziono rozliczenia.")
            return redirect(url_for("hr_tools.hours_inbox"))
        if not row["last_sent_at"]:
            conn.close()
            flash("To rozliczenie nie zostało jeszcze przesłane.")
            return redirect(url_for("hr_tools.hours_detail", timesheet_id=timesheet_id))

        conn.execute(
            """
            UPDATE hour_timesheets
            SET correction_open = 1, correction_reason = ?, correction_opened_by = ?,
                correction_opened_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (reason, session["user_id"], timesheet_id),
        )
        log_action(
            conn,
            "otwarto rozliczenie godzin do korekty",
            "hour_timesheet",
            timesheet_id,
            f"{row['full_name']} | {row['year']}-{row['month']:02d} | {reason}",
        )
        conn.commit()
        conn.close()
        flash("Rozliczenie zostało otwarte pracownikowi do korekty.")
        return redirect(url_for("hr_tools.hours_detail", timesheet_id=timesheet_id))

    @bp.route("/kadry/rozliczenia-pracownikow/pracownik/<int:user_id>.json")
    @login_required
    @role_required("admin", "kadry")
    def hours_employee_history_json(user_id):
        conn = get_db()
        rows = conn.execute(
            """
            SELECT id, year, month, contract_type, target_hours, rows_json,
                   last_sent_at, updated_at, created_at, correction_open
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
            "overtime_hours": _total_overtime(row["rows_json"]),
            "submitted_at": row["last_sent_at"],
            "updated_at": row["updated_at"],
            "status": _timesheet_status(row),
            "detail_url": f"/kadry/rozliczenia-pracownikow/{row['id']}",
        } for row in rows]
        return jsonify({"ok": True, "items": items})
