import base64
import binascii
import calendar
import json
from datetime import date

from flask import jsonify, render_template, request, session

from .config import CONTRACT_UOP, normalize_contract_type
from .database import get_db
from .services import login_required, log_action, polish_holidays
from .routes_timesheets import (
    MONTH_NAMES,
    _employee,
    _month_absences,
    _selected_month,
    _send_brevo_pdf,
    _serialize_saved,
    _validate_rows,
)


def _employee_context(employee):
    contract = normalize_contract_type(employee["contract_type"]) if employee else CONTRACT_UOP
    fte = int(employee["fte_percent"] or 100) if employee else 100
    if contract == CONTRACT_UOP and fte == 75:
        contract_pdf_label = "Umowa o pracę (3/4)"
    elif contract == CONTRACT_UOP and fte != 100:
        contract_pdf_label = f"Umowa o pracę ({fte}% etatu)"
    else:
        contract_pdf_label = contract

    return {
        "id": employee["id"],
        "full_name": employee["full_name"],
        "contract_type": contract,
        "contract_pdf_label": contract_pdf_label,
        "fte_percent": fte,
        "company_name": employee["company_name"] or "",
        "department": employee["department"] or "",
    }


def register_employee_timesheet_routes(bp):
    @bp.route("/rozliczenie-godzin")
    @login_required
    def employee_hours_view():
        selected_month, year, month, month_start, month_end = _selected_month(request.args.get("month"))
        conn = get_db()
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.close()
            return render_template(
                "employee_timesheet.html",
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

        context = {
            "employee": _employee_context(employee),
            "year": year,
            "month": month,
            "month_name": MONTH_NAMES[month - 1],
            "holidays": holidays,
            "absences": absences,
            "saved": saved,
        }
        return render_template(
            "employee_timesheet.html",
            selected_month=selected_month,
            timesheet_context=context,
        )

    @bp.route("/rozliczenie-godzin/save", methods=["POST"])
    @login_required
    def employee_hours_save():
        payload = request.get_json(silent=True) or {}
        try:
            year = int(payload.get("year"))
            month = int(payload.get("month"))
            if year < 2000 or year > 2100 or month < 1 or month > 12:
                raise ValueError("Niepoprawny miesiąc rozliczenia.")
            rows = _validate_rows(payload.get("rows"), year, month)
            target_raw = payload.get("target_hours")
            target_hours = None if target_raw in (None, "") else float(target_raw)
            if target_hours is not None and (target_hours < 0 or target_hours > 744):
                raise ValueError("Łączna liczba godzin jest poza dozwolonym zakresem.")
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc) or "Niepoprawne dane rozliczenia."}), 400

        user_id = int(session["user_id"])
        conn = get_db()
        employee = _employee(conn, user_id)
        if not employee or not employee["active"]:
            conn.close()
            return jsonify({"ok": False, "error": "Nie znaleziono aktywnego pracownika."}), 404

        contract = normalize_contract_type(employee["contract_type"])
        fte = int(employee["fte_percent"] or 100)
        rows_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        existing = conn.execute(
            "SELECT id FROM hour_timesheets WHERE user_id = ? AND year = ? AND month = ?",
            (user_id, year, month),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE hour_timesheets
                SET contract_type = ?, fte_percent = ?, target_hours = ?, rows_json = ?,
                    updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (contract, fte, target_hours, rows_json, user_id, existing["id"]),
            )
            timesheet_id = existing["id"]
            action = "zaktualizowano własne rozliczenie godzin"
        else:
            cur = conn.execute(
                """
                INSERT INTO hour_timesheets (
                    user_id, year, month, contract_type, fte_percent, target_hours,
                    rows_json, generated_by, updated_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, year, month, contract, fte, target_hours, rows_json, user_id, user_id),
            )
            timesheet_id = cur.lastrowid
            action = "utworzono własne rozliczenie godzin"

        total = round(sum(float(row["hours"] or 0) for row in rows), 2)
        log_action(
            conn,
            action,
            "hour_timesheet",
            timesheet_id,
            f"{employee['full_name']} | {year}-{month:02d} | {total} h",
        )
        conn.commit()
        saved = conn.execute("SELECT updated_at FROM hour_timesheets WHERE id = ?", (timesheet_id,)).fetchone()
        conn.close()
        return jsonify({"ok": True, "id": timesheet_id, "updated_at": saved["updated_at"] if saved else None})

    @bp.route("/rozliczenie-godzin/send", methods=["POST"])
    @login_required
    def employee_hours_send():
        payload = request.get_json(silent=True) or {}
        try:
            year = int(payload.get("year"))
            month = int(payload.get("month"))
            if year < 2000 or year > 2100 or month < 1 or month > 12:
                raise ValueError("Niepoprawny miesiąc rozliczenia.")
            pdf_data = str(payload.get("pdfData") or "").strip()
            if not pdf_data:
                raise ValueError("Brak PDF do wysłania.")
            if len(pdf_data) > 20_000_000:
                raise ValueError("PDF jest zbyt duży.")
            base64.b64decode(pdf_data, validate=True)
        except (TypeError, ValueError, binascii.Error) as exc:
            return jsonify({"ok": False, "error": str(exc) or "Niepoprawny PDF."}), 400

        user_id = int(session["user_id"])
        conn = get_db()
        employee = _employee(conn, user_id)
        if not employee or not employee["active"]:
            conn.close()
            return jsonify({"ok": False, "error": "Nie znaleziono aktywnego pracownika."}), 404

        timesheet = conn.execute(
            "SELECT id FROM hour_timesheets WHERE user_id = ? AND year = ? AND month = ?",
            (user_id, year, month),
        ).fetchone()
        if not timesheet:
            conn.close()
            return jsonify({"ok": False, "error": "Najpierw zapisz rozliczenie."}), 400

        month_label = f"{MONTH_NAMES[month - 1]} {year}"
        try:
            response_data = _send_brevo_pdf(employee["full_name"], month_label, pdf_data)
        except RuntimeError as exc:
            conn.close()
            return jsonify({"ok": False, "error": str(exc)}), 502

        conn.execute(
            "UPDATE hour_timesheets SET last_sent_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (timesheet["id"],),
        )
        log_action(
            conn,
            "pracownik wysłał rozliczenie godzin",
            "hour_timesheet",
            timesheet["id"],
            f"{employee['full_name']} | {year}-{month:02d}",
        )
        conn.commit()
        sent = conn.execute("SELECT last_sent_at FROM hour_timesheets WHERE id = ?", (timesheet["id"],)).fetchone()
        conn.close()
        return jsonify({
            "ok": True,
            "last_sent_at": sent["last_sent_at"] if sent else None,
            "messageId": response_data.get("messageId"),
        })
