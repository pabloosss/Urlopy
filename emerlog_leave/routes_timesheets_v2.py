import calendar
import json
from datetime import date, timedelta

from flask import jsonify, render_template, request, session

from .config import CONTRACT_UOP, CONTRACT_ZLECENIE, normalize_contract_type
from .database import get_db
from .services import login_required, log_action, parse_date, polish_holidays, role_required, surname_first


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
                    "label": LEAVE_LABELS.get(row["leave_type"], (row["leave_type"] or "Nieobecność")[:12]),
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
    return str(value if value is not None else "").strip()[:max_len]


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
        except (TypeError, ValueError) as error:
            raise ValueError("Niepoprawny numer dnia w rozliczeniu.") from error
        if day < 1 or day > days_in_month or day in seen:
            raise ValueError("Niepoprawna albo powtórzona data w rozliczeniu.")
        seen.add(day)

        expected_iso = date(year, month, day).isoformat()
        iso = _clean_cell(raw.get("iso"), 10)
        if iso != expected_iso:
            raise ValueError("Rozliczenie zawiera datę spoza wybranego miesiąca.")

        try:
            hours = round(float(raw.get("hours") or 0), 2)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Niepoprawna liczba godzin dla dnia {day}.") from error
        if hours < 0 or hours > 24:
            raise ValueError(f"Godziny dla dnia {day} muszą mieścić się w zakresie 0–24.")

        cleaned.append({
            "day": day,
            "iso": iso,
            "weekday": _clean_cell(raw.get("weekday"), 12),
            "start": _clean_cell(raw.get("start"), 12) or "-",
            "end": _clean_cell(raw.get("end"), 12) or "-",
            "hours": hours,
            "sign_employee": _clean_cell(raw.get("sign_employee"), 80),
            "sign_company": _clean_cell(raw.get("sign_company"), 80),
            "leave": _clean_cell(raw.get("leave"), 80) or "-",
            "off": bool(raw.get("off")),
            "off_source": _clean_cell(raw.get("off_source"), 30),
        })

    cleaned.sort(key=lambda item: item["day"])
    return cleaned


def _send_brevo_pdf(employee_name, month_label, pdf_base64):
    """Kompatybilność ze starszą trasą: wysyłka jest teraz wyłącznie wewnętrzna."""
    return {"messageId": "internal"}


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


def _rows_from_json(raw):
    try:
        rows = json.loads(raw or "[]")
        return rows if isinstance(rows, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _total_hours(raw):
    total = 0.0
    for row in _rows_from_json(raw):
        try:
            total += float(row.get("hours") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return round(total, 2)


def _timesheet_status(row):
    if not row["last_sent_at"]:
        return "Robocze"
    if row["updated_at"] and row["updated_at"] > row["last_sent_at"]:
        return "Zmienione po przesłaniu"
    return "Przesłane"


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
                    updated_by = ?, updated_at = CURRENT_TIMESTAMP, last_sent_at = CURRENT_TIMESTAMP
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
            rows_json, generated_by, updated_by, last_sent_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
        """,
        (
            user_id, year, month, contract, fte, target_hours, rows_json,
            session["user_id"], session["user_id"], 1 if submitted else 0,
        ),
    )
    return cur.lastrowid


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
                "timesheets_v2.html",
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
            "timesheets_v2.html",
            selected_month=selected_month,
            timesheet_context={
                "employee": _employee_context(employee),
                "year": year,
                "month": month,
                "month_name": MONTH_NAMES[month - 1],
                "holidays": holidays,
                "absences": absences,
                "saved": saved,
            },
        )

    @bp.route("/kadry/rozliczenia-godzin/save", methods=["POST"])
    @bp.route("/rozliczenie-godzin/save", methods=["POST"])
    @login_required
    def hours_save():
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
        except (TypeError, ValueError) as error:
            return jsonify({"ok": False, "error": str(error) or "Niepoprawne dane rozliczenia."}), 400

        conn = get_db()
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.close()
            return jsonify({"ok": False, "error": "Nie znaleziono aktywnego pracownika."}), 404
        timesheet_id = _upsert_timesheet(conn, employee, year, month, rows, target_hours, submitted=False)
        total = round(sum(float(row["hours"] or 0) for row in rows), 2)
        log_action(conn, "zapisano rozliczenie godzin", "hour_timesheet", timesheet_id, f"{employee['full_name']} | {year}-{month:02d} | {total} h")
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
            year = int(payload.get("year"))
            month = int(payload.get("month"))
            if year < 2000 or year > 2100 or month < 1 or month > 12:
                raise ValueError("Niepoprawny miesiąc rozliczenia.")
            rows = _validate_rows(payload.get("rows"), year, month)
            target_raw = payload.get("target_hours")
            target_hours = None if target_raw in (None, "") else float(target_raw)
            if target_hours is not None and (target_hours < 0 or target_hours > 744):
                raise ValueError("Łączna liczba godzin jest poza dozwolonym zakresem.")
        except (TypeError, ValueError) as error:
            return jsonify({"ok": False, "error": str(error) or "Niepoprawne dane rozliczenia."}), 400

        conn = get_db()
        employee = _employee(conn, session["user_id"])
        if not employee or not employee["active"]:
            conn.close()
            return jsonify({"ok": False, "error": "Nie znaleziono aktywnego pracownika."}), 404

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
            ORDER BY ht.last_sent_at DESC, ht.year DESC, ht.month DESC, u.full_name
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
