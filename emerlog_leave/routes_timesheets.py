import base64
import binascii
import calendar
import json
import os
from datetime import date, timedelta
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import jsonify, render_template, request, session

from .config import CONTRACT_UOP, normalize_contract_type
from .database import get_db
from .services import login_required, log_action, parse_date, polish_holidays


MONTH_NAMES = [
    "Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec",
    "Lipiec", "Sierpień", "Wrzesień", "Październik", "Listopad", "Grudzień",
]

LEAVE_LABELS = {
    "Urlop wypoczynkowy": "Urlop",
    "Urlop na żądanie": "Na żądanie",
    "Urlop okolicznościowy": "Okolicz.",
    "L4 / chorobowe": "L4",
    "Urlop bezpłatny": "Bezpłatny",
    "Odbiór dnia wolnego": "Odbiór",
    "Inne": "Inne",
}


def _selected_month(raw):
    value = (raw or "").strip() or date.today().strftime("%Y-%m")
    try:
        year, month = [int(part) for part in value.split("-", 1)]
        if not 2000 <= year <= 2100 or not 1 <= month <= 12:
            raise ValueError
    except Exception:
        year, month = date.today().year, date.today().month
        value = f"{year:04d}-{month:02d}"
    last_day = calendar.monthrange(year, month)[1]
    return value, year, month, date(year, month, 1), date(year, month, last_day)


def _employee(conn, user_id):
    return conn.execute(
        """
        SELECT u.*, c.name AS company_name
        FROM users u
        LEFT JOIN companies c ON c.id = u.company_id
        WHERE u.id = ?
        """,
        (user_id,),
    ).fetchone()


def _month_absences(conn, user_id, start, end):
    rows = conn.execute(
        """
        SELECT id, leave_type, date_from, date_to
        FROM leave_requests
        WHERE user_id = ?
          AND status = 'zaakceptowany'
          AND date_from <= ?
          AND date_to >= ?
        ORDER BY date_from, id
        """,
        (user_id, end.isoformat(), start.isoformat()),
    ).fetchall()

    mapping = {}
    for row in rows:
        try:
            current = max(parse_date(row["date_from"]), start)
            finish = min(parse_date(row["date_to"]), end)
        except Exception:
            continue
        while current <= finish:
            if current.weekday() < 5 and current not in polish_holidays(current.year):
                mapping[current.isoformat()] = {
                    "label": LEAVE_LABELS.get(row["leave_type"], row["leave_type"][:12]),
                    "leave_type": row["leave_type"],
                    "request_id": row["id"],
                }
            current += timedelta(days=1)
    return mapping


def _serialize_saved(row):
    if not row:
        return None
    try:
        rows = json.loads(row["rows_json"] or "[]")
        if not isinstance(rows, list):
            rows = []
    except (TypeError, ValueError, json.JSONDecodeError):
        rows = []
    return {
        "id": row["id"],
        "target_hours": row["target_hours"],
        "rows": rows,
        "updated_at": row["updated_at"],
        "last_sent_at": row["last_sent_at"],
    }


def _clean_cell(value, max_len=80):
    text = str(value if value is not None else "").strip()
    return text[:max_len]


def _validate_rows(rows, year, month):
    days_in_month = calendar.monthrange(year, month)[1]
    if not isinstance(rows, list) or len(rows) != days_in_month:
        raise ValueError("Tabela musi zawierać dokładnie wszystkie dni wybranego miesiąca.")

    cleaned = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("Niepoprawny format wiersza rozliczenia.")
        try:
            day = int(raw.get("day"))
        except (TypeError, ValueError):
            raise ValueError("Niepoprawny numer dnia w rozliczeniu.")
        if day < 1 or day > days_in_month or day in seen:
            raise ValueError("Niepoprawna albo powtórzona data w rozliczeniu.")
        seen.add(day)

        expected_iso = date(year, month, day).isoformat()
        iso = _clean_cell(raw.get("iso"), 10)
        if iso != expected_iso:
            raise ValueError("Rozliczenie zawiera datę spoza wybranego miesiąca.")

        try:
            hours = float(raw.get("hours") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"Niepoprawna liczba godzin dla dnia {day}.")
        if hours < 0 or hours > 24:
            raise ValueError(f"Godziny dla dnia {day} muszą mieścić się w zakresie 0–24.")
        if abs(hours - round(hours, 2)) > 0.0001:
            hours = round(hours, 2)

        cleaned.append({
            "day": day,
            "iso": iso,
            "weekday": _clean_cell(raw.get("weekday"), 12),
            "start": _clean_cell(raw.get("start"), 12) or "-",
            "end": _clean_cell(raw.get("end"), 12) or "-",
            "hours": hours,
            "sign_employee": _clean_cell(raw.get("sign_employee"), 80),
            "sign_company": _clean_cell(raw.get("sign_company"), 80),
            "leave": _clean_cell(raw.get("leave"), 80),
            "off": bool(raw.get("off")),
            "off_source": _clean_cell(raw.get("off_source"), 30),
        })

    cleaned.sort(key=lambda item: item["day"])
    return cleaned


def _parse_sender(value):
    raw = (value or "").strip()
    if "<" in raw and raw.endswith(">"):
        name, email = raw.rsplit("<", 1)
        return name.strip() or "Emerlog", email[:-1].strip()
    return "Emerlog", raw


def _send_brevo_pdf(employee_name, month_label, pdf_base64):
    api_key = (os.environ.get("BREVO_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Brak BREVO_API_KEY na serwerze.")

    mail_to = (os.environ.get("TIMESHEET_MAIL_TO") or os.environ.get("MAIL_TO") or "ewa.dusinska@emerlog.eu").strip()
    sender_raw = (os.environ.get("TIMESHEET_MAIL_FROM") or os.environ.get("MAIL_FROM") or "Emerlog <no-reply@emerlog.eu>").strip()
    sender_name, sender_email = _parse_sender(sender_raw)
    if not sender_email or "@" not in sender_email:
        raise RuntimeError("Niepoprawny TIMESHEET_MAIL_FROM/MAIL_FROM.")
    if not mail_to or "@" not in mail_to:
        raise RuntimeError("Niepoprawny TIMESHEET_MAIL_TO/MAIL_TO.")

    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": mail_to, "name": "Kadry"}],
        "subject": f"Rozliczenie godzin – {employee_name} – {month_label}",
        "htmlContent": (
            f"<p>Dzień dobry,<br>w załączniku rozliczenie godzin dla "
            f"<b>{employee_name}</b> za {month_label}.</p><p>Pozdrawiamy,<br>Emerlog</p>"
        ),
        "attachment": [{"name": "Tabela_Godzinowa.pdf", "content": pdf_base64}],
    }
    req = urllib_request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": api_key,
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Brevo odrzuciło wiadomość ({exc.code}): {detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Nie udało się połączyć z Brevo: {exc.reason}") from exc


def _can_access_user(user_id):
    try:
        return int(user_id) == int(session.get("user_id"))
    except (TypeError, ValueError):
        return False


def register_timesheet_routes(bp):
    @bp.route("/kadry/rozliczenia-godzin")
    @login_required
    def hours_view():
        selected_month, year, month, month_start, month_end = _selected_month(request.args.get("month"))
        conn = get_db()
        selected_user_id = int(session["user_id"])
        employee = _employee(conn, selected_user_id)
        if employee and not employee["active"]:
            employee = None

        saved = None
        absences = {}
        if employee:
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

        contract = normalize_contract_type(employee["contract_type"]) if employee else CONTRACT_UOP
        fte = int(employee["fte_percent"] or 100) if employee else 100
        if contract == CONTRACT_UOP and fte == 75:
            contract_pdf_label = "Umowa o pracę (3/4)"
        elif contract == CONTRACT_UOP and fte != 100:
            contract_pdf_label = f"Umowa o pracę ({fte}% etatu)"
        else:
            contract_pdf_label = contract

        context = {
            "employee": {
                "id": employee["id"],
                "full_name": employee["full_name"],
                "contract_type": contract,
                "contract_pdf_label": contract_pdf_label,
                "fte_percent": fte,
                "company_name": employee["company_name"] or "",
                "department": employee["department"] or "",
            } if employee else None,
            "year": year,
            "month": month,
            "month_name": MONTH_NAMES[month - 1],
            "holidays": holidays,
            "absences": absences,
            "saved": saved,
        }
        return render_template(
            "timesheets.html",
            selected_month=selected_month,
            timesheet_context=context,
        )

    @bp.route("/kadry/rozliczenia-godzin/save", methods=["POST"])
    @login_required
    def hours_save():
        payload = request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id"))
            year = int(payload.get("year"))
            month = int(payload.get("month"))
            if not _can_access_user(user_id):
                return jsonify({"ok": False, "error": "Możesz zapisać tylko własne rozliczenie."}), 403
            if year < 2000 or year > 2100 or month < 1 or month > 12:
                raise ValueError("Niepoprawny miesiąc rozliczenia.")
            rows = _validate_rows(payload.get("rows"), year, month)
            target_raw = payload.get("target_hours")
            target_hours = None if target_raw in (None, "") else float(target_raw)
            if target_hours is not None and (target_hours < 0 or target_hours > 744):
                raise ValueError("Łączna liczba godzin jest poza dozwolonym zakresem.")
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc) or "Niepoprawne dane rozliczenia."}), 400

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
                (contract, fte, target_hours, rows_json, session["user_id"], existing["id"]),
            )
            timesheet_id = existing["id"]
            action = "zaktualizowano rozliczenie godzin"
        else:
            cur = conn.execute(
                """
                INSERT INTO hour_timesheets (
                    user_id, year, month, contract_type, fte_percent, target_hours,
                    rows_json, generated_by, updated_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, year, month, contract, fte, target_hours, rows_json, session["user_id"], session["user_id"]),
            )
            timesheet_id = cur.lastrowid
            action = "utworzono rozliczenie godzin"

        total = round(sum(float(row["hours"] or 0) for row in rows), 2)
        log_action(conn, action, "hour_timesheet", timesheet_id, f"{employee['full_name']} | {year}-{month:02d} | {total} h")
        conn.commit()
        saved = conn.execute("SELECT updated_at FROM hour_timesheets WHERE id = ?", (timesheet_id,)).fetchone()
        conn.close()
        return jsonify({"ok": True, "id": timesheet_id, "updated_at": saved["updated_at"] if saved else None})

    @bp.route("/kadry/rozliczenia-godzin/send", methods=["POST"])
    @login_required
    def hours_send():
        payload = request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id"))
            year = int(payload.get("year"))
            month = int(payload.get("month"))
            if not _can_access_user(user_id):
                return jsonify({"ok": False, "error": "Możesz wysłać tylko własne rozliczenie."}), 403
            pdf_data = str(payload.get("pdfData") or "").strip()
            if not pdf_data:
                raise ValueError("Brak PDF do wysłania.")
            if len(pdf_data) > 20_000_000:
                raise ValueError("PDF jest zbyt duży.")
            base64.b64decode(pdf_data, validate=True)
        except (TypeError, ValueError, binascii.Error) as exc:
            return jsonify({"ok": False, "error": str(exc) or "Niepoprawny PDF."}), 400

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
            "wysłano rozliczenie godzin",
            "hour_timesheet",
            timesheet["id"],
            f"{employee['full_name']} | {year}-{month:02d}",
        )
        conn.commit()
        sent = conn.execute("SELECT last_sent_at FROM hour_timesheets WHERE id = ?", (timesheet["id"],)).fetchone()
        conn.close()
        return jsonify({"ok": True, "last_sent_at": sent["last_sent_at"] if sent else None, "messageId": response_data.get("messageId")})
