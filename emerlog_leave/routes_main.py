from datetime import date, timedelta
import calendar
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .database import get_db
from .services import login_required, current_user, visible_user_ids, vacation_summary, parse_date

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("main.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE login = ? AND active = 1", (request.form.get("login", "").strip(),)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.update({"user_id": user["id"], "login": user["login"], "full_name": user["full_name"], "role": user["role"]})
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
    ids = visible_user_ids(conn)
    today = date.today().isoformat()
    stats = {"absent_today": 0, "pending": 0, "employee_count": 1, "upcoming": [], "latest": []}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        stats["absent_today"] = conn.execute(
            f"SELECT COUNT(DISTINCT user_id) AS c FROM leave_requests WHERE status='zaakceptowany' AND date_from<=? AND date_to>=? AND user_id IN ({placeholders})",
            (today, today, *ids),
        ).fetchone()["c"] or 0
        stats["pending"] = conn.execute(
            f"SELECT COUNT(*) AS c FROM leave_requests WHERE status='oczekuje' AND user_id IN ({placeholders})",
            ids,
        ).fetchone()["c"] or 0
        stats["upcoming"] = conn.execute(
            f"""
            SELECT lr.*, u.full_name, u.department
            FROM leave_requests lr JOIN users u ON u.id = lr.user_id
            WHERE lr.status='zaakceptowany' AND lr.date_from>=? AND lr.user_id IN ({placeholders})
            ORDER BY lr.date_from LIMIT 6
            """,
            (today, *ids),
        ).fetchall()
        stats["employee_count"] = len(ids)
    if session.get("role") in {"admin", "kadry"}:
        stats["employee_count"] = conn.execute("SELECT COUNT(*) AS c FROM users WHERE active=1").fetchone()["c"] or 0
    stats["latest"] = conn.execute("SELECT * FROM leave_requests WHERE user_id=? ORDER BY created_at DESC LIMIT 5", (user["id"],)).fetchall()
    summary = vacation_summary(conn, user)
    conn.close()
    return render_template("dashboard.html", user=user, vacation_summary=summary, stats=stats)


@bp.route("/my-leave")
@login_required
def my_leave():
    conn = get_db()
    user = current_user(conn)
    rows = conn.execute("""
        SELECT lr.*, r.full_name AS replacement_name, d.full_name AS decider_name
        FROM leave_requests lr
        LEFT JOIN users r ON lr.replacement_user_id = r.id
        LEFT JOIN users d ON lr.decided_by = d.id
        WHERE lr.user_id = ? ORDER BY lr.created_at DESC
    """, (user["id"],)).fetchall()
    summary = vacation_summary(conn, user)
    conn.close()
    return render_template("my_leave.html", user=user, vacation_summary=summary, requests_list=rows)


@bp.route("/calendar")
@login_required
def calendar_view():
    year = int(request.args.get("year", date.today().year))
    month = int(request.args.get("month", date.today().month))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    conn = get_db()
    ids = visible_user_ids(conn)
    by_day = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT lr.*, u.full_name, u.department
            FROM leave_requests lr JOIN users u ON u.id=lr.user_id
            WHERE lr.status='zaakceptowany' AND lr.date_from<=? AND lr.date_to>=? AND lr.user_id IN ({placeholders})
            """,
            (last.isoformat(), first.isoformat(), *ids),
        ).fetchall()
        for row in rows:
            current = max(parse_date(row["date_from"]), first)
            end = min(parse_date(row["date_to"]), last)
            while current <= end:
                by_day.setdefault(current.day, []).append(row)
                current += timedelta(days=1)
    conn.close()
    return render_template("calendar.html", selected_year=year, selected_month=month, month_days=calendar.Calendar(firstweekday=0).monthdatescalendar(year, month), requests_by_day=by_day)
