from datetime import date, timedelta
import calendar

from flask import Blueprint, flash, redirect, render_template, request, session, url_for, send_from_directory
from werkzeug.security import check_password_hash

from .database import get_db
from .services import (
    login_required,
    role_required,
    current_user,
    visible_user_ids,
    vacation_summary,
    parse_date,
    surname_first,
    is_hr,
    is_manager,
)

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("main.login"))


@bp.route("/graphics/<path:filename>")
def graphics_file(filename):
    return send_from_directory("grafiki", filename)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE login = ? AND active = 1",
            (request.form.get("login", "").strip(),),
        ).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.update(
                {
                    "user_id": user["id"],
                    "login": user["login"],
                    "full_name": user["full_name"],
                    "role": user["role"],
                }
            )
            return redirect(url_for("main.dashboard"))
        flash("Błędny login albo hasło.")
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@bp.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    user = current_user(conn)
    today = date.today().isoformat()
    role = session.get("role")

    stats = {
        "absent_today": 0,
        "employee_count": 0,
        "upcoming": [],
        "latest": [],
        "new_requests": 0,
    }

    scope_ids = visible_user_ids(conn)
    if scope_ids:
        placeholders = ",".join("?" for _ in scope_ids)
        stats["absent_today"] = conn.execute(
            f"""
            SELECT COUNT(DISTINCT lr.user_id) AS c
            FROM leave_requests lr
            JOIN users u ON u.id = lr.user_id
            WHERE lr.status = 'zaakceptowany'
              AND lr.date_from <= ?
              AND lr.date_to >= ?
              AND u.active = 1
              AND lr.user_id IN ({placeholders})
            """,
            (today, today, *scope_ids),
        ).fetchone()["c"] or 0

        stats["employee_count"] = conn.execute(
            f"SELECT COUNT(*) AS c FROM users WHERE active = 1 AND id IN ({placeholders})",
            scope_ids,
        ).fetchone()["c"] or 0

        if role in {"admin", "kadry", "menedzer"}:
            stats["upcoming"] = conn.execute(
                f"""
                SELECT lr.*, u.full_name, u.department
                FROM leave_requests lr
                JOIN users u ON u.id = lr.user_id
                WHERE lr.status = 'zaakceptowany'
                  AND lr.date_to >= ?
                  AND u.active = 1
                  AND lr.user_id IN ({placeholders})
                ORDER BY CASE WHEN lr.date_from < ? THEN ? ELSE lr.date_from END, u.full_name
                LIMIT 6
                """,
                (today, *scope_ids, today, today),
            ).fetchall()

    if role in {"admin", "kadry"}:
        stats["employee_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE active = 1"
        ).fetchone()["c"] or 0

    if role == "admin":
        stats["new_requests"] = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM request_notifications
            WHERE recipient_user_id = ? AND seen_at IS NULL
            """,
            (session["user_id"],),
        ).fetchone()["c"] or 0

    stats["latest"] = conn.execute(
        """
        SELECT * FROM leave_requests
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 4
        """,
        (user["id"],),
    ).fetchall()

    summary = vacation_summary(conn, user)
    conn.close()
    return render_template(
        "dashboard.html",
        user=user,
        vacation_summary=summary,
        stats=stats,
        new_requests_count=stats["new_requests"],
    )


@bp.route("/my-leave")
@login_required
def my_leave():
    conn = get_db()
    user = current_user(conn)
    rows = conn.execute(
        """
        SELECT lr.*, r.full_name AS replacement_name, d.full_name AS decider_name
        FROM leave_requests lr
        LEFT JOIN users r ON lr.replacement_user_id = r.id
        LEFT JOIN users d ON lr.decided_by = d.id
        WHERE lr.user_id = ?
        ORDER BY lr.created_at DESC
        """,
        (user["id"],),
    ).fetchall()
    summary = vacation_summary(conn, user)
    conn.close()
    return render_template(
        "my_leave.html",
        user=user,
        vacation_summary=summary,
        requests_list=rows,
    )


@bp.route("/presence")
@login_required
@role_required("admin", "kadry", "menedzer")
def presence_view():
    selected_date = request.args.get("date") or date.today().isoformat()
    department = request.args.get("department", "").strip()
    employee = request.args.get("employee", "").strip()
    leave_type = request.args.get("leave_type", "").strip()
    day_status = request.args.get("day_status", "").strip()

    conn = get_db()
    ids = visible_user_ids(conn)
    employees = []
    stats = {"all": 0, "present": 0, "absent": 0}

    if ids:
        placeholders = ",".join("?" for _ in ids)
        filters = [f"u.id IN ({placeholders})", "u.active = 1"]
        params = list(ids)
        if department:
            filters.append("u.department = ?")
            params.append(department)
        if employee:
            parts = employee.split()
            reversed_employee = " ".join(parts[1:] + parts[:1]) if len(parts) > 1 else employee
            filters.append("(u.full_name LIKE ? OR u.full_name LIKE ?)")
            params.extend([f"%{employee}%", f"%{reversed_employee}%"])

        people = conn.execute(
            f"""
            SELECT u.*, m.full_name AS manager_name
            FROM users u
            LEFT JOIN users m ON u.manager_id = m.id
            WHERE {' AND '.join(filters)}
            """,
            params,
        ).fetchall()
        people = sorted(
            people,
            key=lambda person: ((person["department"] or "").casefold(), surname_first(person["full_name"]).casefold()),
        )

        for person in people:
            absence = conn.execute(
                """
                SELECT * FROM leave_requests
                WHERE user_id = ?
                  AND status = 'zaakceptowany'
                  AND date_from <= ?
                  AND date_to >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (person["id"], selected_date, selected_date),
            ).fetchone()

            current_status = "nieobecny" if absence else "obecny"
            current_type = absence["leave_type"] if absence else "—"

            if leave_type and current_type != leave_type:
                continue
            if day_status and current_status != day_status:
                continue

            employees.append(
                {
                    "user": person,
                    "absence": absence,
                    "day_status": current_status,
                    "type": current_type,
                }
            )
            if current_status == "obecny":
                stats["present"] += 1
            else:
                stats["absent"] += 1

    stats["all"] = len(employees)
    departments = conn.execute("SELECT name FROM departments ORDER BY name").fetchall()
    conn.close()
    return render_template(
        "presence.html",
        selected_date=selected_date,
        employees=employees,
        stats=stats,
        departments=departments,
    )


@bp.route("/calendar")
@login_required
@role_required("admin", "kadry", "menedzer")
def calendar_view():
    try:
        year = int(request.args.get("year", date.today().year))
        month = int(request.args.get("month", date.today().month))
        if month < 1 or month > 12:
            raise ValueError
    except (TypeError, ValueError):
        year = date.today().year
        month = date.today().month

    department = request.args.get("department", "").strip()
    employee = request.args.get("employee", "").strip()
    leave_type = request.args.get("leave_type", "").strip()

    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    conn = get_db()
    ids = visible_user_ids(conn)
    by_day = {}
    rows = []
    team_stats = {"employees": 0, "requests": 0, "days": 0}

    if ids:
        placeholders = ",".join("?" for _ in ids)
        team_stats["employees"] = conn.execute(
            f"SELECT COUNT(*) AS c FROM users WHERE active = 1 AND id IN ({placeholders})",
            ids,
        ).fetchone()["c"] or 0

        filters = [
            "lr.status = 'zaakceptowany'",
            "lr.date_from <= ?",
            "lr.date_to >= ?",
            f"lr.user_id IN ({placeholders})",
        ]
        params = [last.isoformat(), first.isoformat(), *ids]
        if department:
            filters.append("u.department = ?")
            params.append(department)
        if employee:
            parts = employee.split()
            reversed_employee = " ".join(parts[1:] + parts[:1]) if len(parts) > 1 else employee
            filters.append("(u.full_name LIKE ? OR u.full_name LIKE ?)")
            params.extend([f"%{employee}%", f"%{reversed_employee}%"])
        if leave_type:
            filters.append("lr.leave_type = ?")
            params.append(leave_type)

        rows = conn.execute(
            f"""
            SELECT lr.*, u.full_name, u.department
            FROM leave_requests lr
            JOIN users u ON u.id = lr.user_id
            WHERE {' AND '.join(filters)}
            """,
            params,
        ).fetchall()
        rows = sorted(rows, key=lambda row: (row["date_from"], surname_first(row["full_name"]).casefold()))

        team_stats["requests"] = len(rows)
        team_stats["days"] = sum((row["days_count"] or 0) for row in rows)

        for row in rows:
            current = max(parse_date(row["date_from"]), first)
            end = min(parse_date(row["date_to"]), last)
            while current <= end:
                by_day.setdefault(current.day, []).append(row)
                current += timedelta(days=1)

    if is_manager():
        departments = conn.execute(
            """
            SELECT DISTINCT department AS name
            FROM users
            WHERE manager_id = ? AND active = 1 AND department IS NOT NULL
            ORDER BY department
            """,
            (session["user_id"],),
        ).fetchall()
        calendar_title = "Kalendarz zespołu"
        list_title = "Nieobecności zespołu"
    else:
        departments = conn.execute("SELECT name FROM departments ORDER BY name").fetchall()
        calendar_title = "Kalendarz firmowy"
        list_title = "Nieobecności w miesiącu"

    conn.close()
    return render_template(
        "calendar.html",
        selected_year=year,
        selected_month=month,
        month_days=calendar.Calendar(firstweekday=0).monthdatescalendar(year, month),
        requests_by_day=by_day,
        requests_list=rows,
        departments=departments,
        calendar_title=calendar_title,
        list_title=list_title,
        team_stats=team_stats,
    )
