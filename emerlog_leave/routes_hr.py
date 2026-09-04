import calendar
import csv
import io
from datetime import date, timedelta

from flask import Blueprint, Response, render_template, request

from .config import uses_vacation_balance
from .database import get_db
from .services import (
    get_app_setting,
    login_required,
    role_required,
    surname_first,
    vacation_summary,
    workdays_in_period,
)
from .routes_timesheets import register_timesheet_routes
from .routes_employee_timesheets import register_employee_timesheet_routes

bp = Blueprint("hr_tools", __name__)
register_timesheet_routes(bp)
register_employee_timesheet_routes(bp)


def _month_range(value):
    try:
        year, month = [int(part) for part in value.split("-", 1)]
        if year < 2000 or year > 2100 or month < 1 or month > 12:
            raise ValueError
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)
    except Exception as error:
        raise ValueError("Niepoprawny miesiąc. Użyj formatu RRRR-MM.") from error


def _safe_year(value):
    try:
        year = int(value) if value else date.today().year
        if year < 2000 or year > 2100:
            raise ValueError
        return year
    except (TypeError, ValueError):
        return date.today().year


def _csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _csv_response(rows, filename):
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";", lineterminator="\n")
    for row in rows:
        writer.writerow([_csv_cell(value) for value in row])
    return Response(
        output.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/kadry")
@login_required
@role_required("admin", "kadry")
def dashboard():
    conn = get_db()
    today = date.today()
    today_iso = today.isoformat()
    try:
        alert_days = max(1, min(365, int(get_app_setting(conn, "contract_alert_days", "45") or 45)))
    except (TypeError, ValueError):
        alert_days = 45
    try:
        carryover_threshold = max(0, min(366, int(get_app_setting(conn, "carryover_alert_threshold", "1") or 0)))
    except (TypeError, ValueError):
        carryover_threshold = 1
    ending_iso = (today + timedelta(days=alert_days)).isoformat()

    employees = conn.execute("""
        SELECT u.*, c.name AS company_name
        FROM users u
        LEFT JOIN companies c ON c.id = u.company_id
        WHERE u.active = 1
    """).fetchall()

    expiring = conn.execute("""
        SELECT u.*, c.name AS company_name
        FROM users u
        LEFT JOIN companies c ON c.id = u.company_id
        WHERE u.active = 1
          AND u.employment_end IS NOT NULL
          AND u.employment_end != ''
          AND u.employment_end >= ?
          AND u.employment_end <= ?
        ORDER BY u.employment_end, u.full_name
    """, (today_iso, ending_iso)).fetchall()

    should_archive = conn.execute("""
        SELECT u.*, c.name AS company_name
        FROM users u
        LEFT JOIN companies c ON c.id = u.company_id
        WHERE u.active = 1
          AND u.employment_end IS NOT NULL
          AND u.employment_end != ''
          AND u.employment_end < ?
        ORDER BY u.employment_end, u.full_name
    """, (today_iso,)).fetchall()

    incomplete = []
    negative_balances = []
    carryover_people = []
    for employee in employees:
        missing = []
        if not employee["employment_start"]:
            missing.append("data zatrudnienia")
        if not employee["department"]:
            missing.append("dział")
        if not employee["company_id"]:
            missing.append("spółka")
        if not employee["contract_type"]:
            missing.append("typ umowy")
        if not employee["fte_percent"]:
            missing.append("etat")
        if missing:
            incomplete.append({"user": employee, "missing": ", ".join(missing)})

        if uses_vacation_balance(employee["contract_type"]):
            summary = vacation_summary(conn, employee)
            if summary["available"] < 0:
                negative_balances.append({"user": employee, "summary": summary})
            if summary["carryover"] >= carryover_threshold and summary["carryover"] > 0:
                carryover_people.append({"user": employee, "summary": summary})

    incomplete.sort(key=lambda item: surname_first(item["user"]["full_name"]).casefold())
    negative_balances.sort(key=lambda item: item["summary"]["available"])
    carryover_people.sort(key=lambda item: (-item["summary"]["carryover"], surname_first(item["user"]["full_name"]).casefold()))

    adjustments = conn.execute("""
        SELECT la.*, u.full_name AS employee_name, a.full_name AS actor_name
        FROM limit_adjustments la
        JOIN users u ON u.id = la.user_id
        LEFT JOIN users a ON a.id = la.changed_by
        ORDER BY la.created_at DESC
        LIMIT 8
    """).fetchall()

    stats = {
        "active": len(employees),
        "expiring": len(expiring),
        "should_archive": len(should_archive),
        "incomplete": len(incomplete),
        "negative": len(negative_balances),
        "carryover": len(carryover_people),
    }
    closed_through = get_app_setting(conn, "hr_closed_through", "") or ""
    conn.close()

    return render_template(
        "hr_dashboard.html",
        stats=stats,
        expiring=expiring,
        should_archive=should_archive,
        incomplete=incomplete[:12],
        negative_balances=negative_balances[:12],
        carryover_people=carryover_people[:12],
        adjustments=adjustments,
        closed_through=closed_through,
        current_month=today.strftime("%Y-%m"),
        contract_alert_days=alert_days,
        carryover_alert_threshold=carryover_threshold,
    )


@bp.route("/kadry/export/salda.csv")
@login_required
@role_required("admin", "kadry")
def export_balances():
    year = _safe_year(request.args.get("year", "").strip())

    conn = get_db()
    include_inactive = get_app_setting(conn, "include_inactive_in_hr_exports", "0") == "1"
    where = "1=1" if include_inactive else "u.active = 1"
    users = conn.execute(f"""
        SELECT u.*, c.name AS company_name
        FROM users u
        LEFT JOIN companies c ON c.id = u.company_id
        WHERE {where}
        ORDER BY u.active DESC, u.full_name
    """).fetchall()
    users = sorted(users, key=lambda user: (not bool(user["active"]), surname_first(user["full_name"]).casefold()))

    rows = [[
        "Nazwisko i imię", "Login", "Status", "Spółka", "Dział", "Typ umowy",
        "Etat %", "Zatrudnienie od", "Zatrudnienie do", "Rok", "Wymiar",
        "Zaległy", "Razem", "Wykorzystany", "Pozostało"
    ]]
    for user in users:
        summary = vacation_summary(conn, user, year)
        rows.append([
            surname_first(user["full_name"]),
            user["login"],
            "aktywny" if user["active"] else "nieaktywny",
            user["company_name"] or "",
            user["department"] or "",
            user["contract_type"] or "",
            user["fte_percent"] or 100,
            user["employment_start"] or "",
            user["employment_end"] or "",
            year,
            summary["base"],
            summary["carryover"],
            summary["total"],
            summary["accepted"],
            summary["available"],
        ])
    conn.close()
    return _csv_response(rows, f"salda_urlopowe_{year}.csv")


@bp.route("/kadry/export/nieobecnosci.csv")
@login_required
@role_required("admin", "kadry")
def export_absences():
    selected_month = request.args.get("month", "").strip() or date.today().strftime("%Y-%m")
    try:
        start, end = _month_range(selected_month)
    except ValueError:
        selected_month = date.today().strftime("%Y-%m")
        start, end = _month_range(selected_month)

    conn = get_db()
    include_inactive = get_app_setting(conn, "include_inactive_in_hr_exports", "0") == "1"
    active_filter = "" if include_inactive else "AND u.active = 1"
    entries = conn.execute(f"""
        SELECT lr.*, u.full_name, u.login, u.department, u.contract_type,
               c.name AS company_name
        FROM leave_requests lr
        JOIN users u ON u.id = lr.user_id
        LEFT JOIN companies c ON c.id = u.company_id
        WHERE lr.status = 'zaakceptowany'
          AND lr.date_from <= ?
          AND lr.date_to >= ?
          {active_filter}
        ORDER BY u.full_name, lr.date_from
    """, (end.isoformat(), start.isoformat())).fetchall()

    rows = [[
        "Nazwisko i imię", "Login", "Spółka", "Dział", "Typ umowy",
        "Rodzaj nieobecności", "Od", "Do", "Dni robocze w miesiącu"
    ]]
    for entry in entries:
        days = workdays_in_period(entry["date_from"], entry["date_to"], start, end)
        rows.append([
            surname_first(entry["full_name"]),
            entry["login"],
            entry["company_name"] or "",
            entry["department"] or "",
            entry["contract_type"] or "",
            entry["leave_type"],
            entry["date_from"],
            entry["date_to"],
            days,
        ])
    conn.close()
    return _csv_response(rows, f"nieobecnosci_{selected_month}.csv")
