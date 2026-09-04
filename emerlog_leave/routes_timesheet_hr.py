import json
from datetime import date

from flask import Blueprint, jsonify, render_template, request

from .database import get_db
from .services import login_required, role_required, surname_first
from . import routes_timesheets as timesheet_routes
from . import routes_employee_timesheets as employee_timesheet_routes


bp = Blueprint("timesheet_hr", __name__)


def _internal_submission(employee_name, month_label, pdf_base64):
    """Rozliczenia są przekazywane wewnątrz Urlopów, bez wysyłki e-mail."""
    return {"messageId": "internal"}


# Stare endpointy zapisu/przesłania zostają kompatybilne, ale zamiast Brevo
# oznaczają rozliczenie jako przekazane do Kadr przez last_sent_at.
timesheet_routes._send_brevo_pdf = _internal_submission
employee_timesheet_routes._send_brevo_pdf = _internal_submission


def _decode_rows(raw):
    try:
        rows = json.loads(raw or "[]")
        return rows if isinstance(rows, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _total_hours(raw):
    total = 0.0
    for row in _decode_rows(raw):
        try:
            total += float(row.get("hours") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return round(total, 2)


def _status(row):
    if not row["last_sent_at"]:
        return "Robocze"
    if row["updated_at"] and row["updated_at"] > row["last_sent_at"]:
        return "Zmienione po przesłaniu"
    return "Przesłane"


@bp.route("/kadry/rozliczenia-pracownikow")
@login_required
@role_required("admin", "kadry")
def inbox():
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
        SELECT ht.*, u.full_name, u.contract_type, u.department,
               c.name AS company_name
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

    items = []
    for row in rows:
        items.append({
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
            "status": _status(row),
        })

    employees = sorted(employees, key=lambda row: surname_first(row["full_name"]).casefold())
    return render_template(
        "timesheets_hr_inbox.html",
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
def detail(timesheet_id):
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

    rows = _decode_rows(row["rows_json"])
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
        "status": _status(row),
        "rows": rows,
    }
    return render_template("timesheet_hr_detail.html", item=item)


@bp.route("/kadry/rozliczenia-pracownikow/pracownik/<int:user_id>.json")
@login_required
@role_required("admin", "kadry")
def employee_timesheets_json(user_id):
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
        "status": _status(row),
        "detail_url": f"/kadry/rozliczenia-pracownikow/{row['id']}",
    } for row in rows]
    return jsonify({"ok": True, "items": items})
