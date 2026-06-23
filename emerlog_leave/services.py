from datetime import date, datetime, timedelta
from functools import wraps
from flask import flash, redirect, session, url_for

from .config import HR_ROLES


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("main.login"))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get("role") not in roles:
                flash("Brak uprawnień do tej sekcji.")
                return redirect(url_for("main.dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def is_hr():
    return session.get("role") in HR_ROLES


def is_manager():
    return session.get("role") == "menedzer"


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def format_pl_date(value):
    if not value:
        return "—"
    try:
        return parse_date(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def calculate_easter(year):
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31; day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def polish_holidays(year):
    e = calculate_easter(year)
    return {date(year,1,1), date(year,1,6), e + timedelta(days=1), date(year,5,1), date(year,5,3), e + timedelta(days=60), date(year,8,15), date(year,11,1), date(year,11,11), date(year,12,25), date(year,12,26)}


def count_workdays(start, end):
    if end < start:
        raise ValueError("Data do nie może być wcześniejsza niż data od.")
    holidays = set()
    for year in range(start.year, end.year + 1):
        holidays.update(polish_holidays(year))
    current = start
    days = 0
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            days += 1
        current += timedelta(days=1)
    return days


def current_user(conn):
    return conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def visible_user_ids(conn):
    if is_hr():
        return [row["id"] for row in conn.execute("SELECT id FROM users")]
    if is_manager():
        return [row["id"] for row in conn.execute("SELECT id FROM users WHERE manager_id = ?", (session["user_id"],))]
    return [session["user_id"]]


def vacation_summary(conn, user):
    rows = conn.execute("""
        SELECT status, COALESCE(SUM(days_count), 0) AS days
        FROM leave_requests
        WHERE user_id = ? AND leave_type IN (?, ?) AND status IN ('oczekuje', 'zaakceptowany')
        GROUP BY status
    """, (user["id"], "Urlop wypoczynkowy", "Urlop na żądanie")).fetchall()
    accepted = sum(row["days"] for row in rows if row["status"] == "zaakceptowany")
    pending = sum(row["days"] for row in rows if row["status"] == "oczekuje")
    total = (user["vacation_days"] or 0) + (user["carryover_days"] or 0)
    return {"total": total, "base": user["vacation_days"] or 0, "carryover": user["carryover_days"] or 0, "accepted": accepted, "pending": pending, "available": total - accepted - pending}


def log_action(conn, action, entity_type, entity_id=None, details=""):
    conn.execute("INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?)", (session.get("user_id"), action, entity_type, entity_id, details))
