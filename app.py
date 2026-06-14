from datetime import date, datetime, timedelta
from functools import wraps
import sqlite3

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = "dev-secret-change-before-production"

DATABASE = "database.db"
VACATION_LIMIT_TYPES = {"Wypoczynkowy", "Na żądanie"}


# -------------------------
# Baza danych
# -------------------------
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT,
            role TEXT NOT NULL,
            vacation_days INTEGER DEFAULT 20,
            active INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            leave_type TEXT NOT NULL,
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            days_count INTEGER NOT NULL,
            comment TEXT,
            status TEXT DEFAULT 'oczekuje',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.commit()

    demo_users = [
        ("admin", "admin123", "Administrator", "admin@firma.pl", "admin", 26),
        ("kadry", "kadry123", "Kadrowa", "kadry@firma.pl", "kadry", 26),
        ("jan", "jan123", "Jan Kowalski", "jan.kowalski@firma.pl", "pracownik", 20),
    ]

    for login, password, full_name, email, role, vacation_days in demo_users:
        exists = cur.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone()
        if not exists:
            cur.execute(
                """
                INSERT INTO users (login, password_hash, full_name, email, role, vacation_days)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    login,
                    generate_password_hash(password),
                    full_name,
                    email,
                    role,
                    vacation_days,
                ),
            )

    conn.commit()
    conn.close()


# -------------------------
# Pomocnicze
# -------------------------
def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def hr_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get("role") not in ["admin", "kadry"]:
            flash("Brak uprawnień.")
            return redirect(url_for("dashboard"))
        return func(*args, **kwargs)

    return wrapper


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def calculate_easter(year):
    """Zwraca datę Wielkanocy dla podanego roku."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def polish_holidays(year):
    easter = calculate_easter(year)

    return {
        date(year, 1, 1),   # Nowy Rok
        date(year, 1, 6),   # Trzech Króli
        easter + timedelta(days=1),   # Poniedziałek Wielkanocny
        date(year, 5, 1),   # Święto Pracy
        date(year, 5, 3),   # Konstytucja 3 Maja
        easter + timedelta(days=60),  # Boże Ciało
        date(year, 8, 15),  # Wniebowzięcie NMP
        date(year, 11, 1),  # Wszystkich Świętych
        date(year, 11, 11), # Święto Niepodległości
        date(year, 12, 25), # Boże Narodzenie
        date(year, 12, 26), # Drugi dzień Bożego Narodzenia
    }


def count_workdays(start_date, end_date):
    """Liczy dni robocze od start_date do end_date włącznie."""
    if end_date < start_date:
        raise ValueError("Data do nie może być wcześniejsza niż data od.")

    holidays = set()
    for year in range(start_date.year, end_date.year + 1):
        holidays.update(polish_holidays(year))

    days = 0
    current = start_date

    while current <= end_date:
        is_weekend = current.weekday() >= 5
        is_holiday = current in holidays

        if not is_weekend and not is_holiday:
            days += 1

        current += timedelta(days=1)

    return days


def get_vacation_summary(conn, user_id, total_days):
    """Podsumowanie limitu urlopu dla typów odejmujących pulę."""
    rows = conn.execute(
        """
        SELECT status, COALESCE(SUM(days_count), 0) AS days
        FROM leave_requests
        WHERE user_id = ?
          AND leave_type IN (?, ?)
          AND status IN ('oczekuje', 'zaakceptowany')
        GROUP BY status
        """,
        (user_id, "Wypoczynkowy", "Na żądanie"),
    ).fetchall()

    accepted = 0
    pending = 0

    for row in rows:
        if row["status"] == "zaakceptowany":
            accepted = row["days"] or 0
        elif row["status"] == "oczekuje":
            pending = row["days"] or 0

    remaining_without_pending = total_days - accepted
    available = total_days - accepted - pending

    return {
        "total": total_days,
        "accepted": accepted,
        "pending": pending,
        "remaining_without_pending": remaining_without_pending,
        "available": available,
    }


def get_hr_stats(conn):
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'oczekuje' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'zaakceptowany' THEN 1 ELSE 0 END) AS accepted,
            SUM(CASE WHEN status = 'odrzucony' THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN status = 'anulowany' THEN 1 ELSE 0 END) AS cancelled
        FROM leave_requests
        """
    ).fetchone()

    return {
        "total": row["total"] or 0,
        "pending": row["pending"] or 0,
        "accepted": row["accepted"] or 0,
        "rejected": row["rejected"] or 0,
        "cancelled": row["cancelled"] or 0,
    }


# -------------------------
# Widoki
# -------------------------
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE login = ? AND active = 1",
            (login_value,),
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["login"] = user["login"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))

        flash("Błędny login albo hasło.")

    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()

    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()

    vacation_summary = get_vacation_summary(conn, user["id"], user["vacation_days"])
    hr_stats = None

    if session["role"] in ["admin", "kadry"]:
        requests_list = conn.execute(
            """
            SELECT lr.*, u.full_name
            FROM leave_requests lr
            JOIN users u ON lr.user_id = u.id
            ORDER BY lr.created_at DESC
            """
        ).fetchall()
        hr_stats = get_hr_stats(conn)
    else:
        requests_list = conn.execute(
            """
            SELECT lr.*, u.full_name
            FROM leave_requests lr
            JOIN users u ON lr.user_id = u.id
            WHERE lr.user_id = ?
            ORDER BY lr.created_at DESC
            """,
            (session["user_id"],),
        ).fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        user=user,
        requests_list=requests_list,
        vacation_summary=vacation_summary,
        hr_stats=hr_stats,
        vacation_limit_types=VACATION_LIMIT_TYPES,
    )


@app.route("/new-request", methods=["POST"])
@login_required
def new_request():
    leave_type = request.form.get("leave_type")
    date_from = request.form.get("date_from")
    date_to = request.form.get("date_to")
    comment = request.form.get("comment")

    if not leave_type or not date_from or not date_to:
        flash("Uzupełnij wymagane pola.")
        return redirect(url_for("dashboard"))

    try:
        start_date = parse_date(date_from)
        end_date = parse_date(date_to)
        days_count = count_workdays(start_date, end_date)
    except ValueError as error:
        flash(str(error))
        return redirect(url_for("dashboard"))

    if days_count <= 0:
        flash("Wybrany zakres nie zawiera dni roboczych.")
        return redirect(url_for("dashboard"))

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()

    if leave_type in VACATION_LIMIT_TYPES:
        vacation_summary = get_vacation_summary(conn, user["id"], user["vacation_days"])
        if days_count > vacation_summary["available"]:
            conn.close()
            flash(
                f"Nie można złożyć wniosku. Dostępne po oczekujących wnioskach: "
                f"{vacation_summary['available']} dni, a wybrano {days_count} dni."
            )
            return redirect(url_for("dashboard"))

    conn.execute(
        """
        INSERT INTO leave_requests
        (user_id, leave_type, date_from, date_to, days_count, comment)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            session["user_id"],
            leave_type,
            date_from,
            date_to,
            days_count,
            comment,
        ),
    )
    conn.commit()
    conn.close()

    flash(f"Wniosek został wysłany. System policzył: {days_count} dni roboczych.")
    return redirect(url_for("dashboard"))


@app.route("/request/<int:request_id>/accept", methods=["POST"])
@login_required
@hr_required
def accept_request(request_id):
    conn = get_db()
    conn.execute(
        """
        UPDATE leave_requests
        SET status = 'zaakceptowany'
        WHERE id = ? AND status = 'oczekuje'
        """,
        (request_id,),
    )
    conn.commit()
    conn.close()

    flash("Wniosek został zaakceptowany.")
    return redirect(url_for("dashboard"))


@app.route("/request/<int:request_id>/reject", methods=["POST"])
@login_required
@hr_required
def reject_request(request_id):
    conn = get_db()
    conn.execute(
        """
        UPDATE leave_requests
        SET status = 'odrzucony'
        WHERE id = ? AND status = 'oczekuje'
        """,
        (request_id,),
    )
    conn.commit()
    conn.close()

    flash("Wniosek został odrzucony.")
    return redirect(url_for("dashboard"))


@app.route("/request/<int:request_id>/cancel", methods=["POST"])
@login_required
def cancel_request(request_id):
    conn = get_db()
    leave_request = conn.execute(
        "SELECT * FROM leave_requests WHERE id = ?",
        (request_id,),
    ).fetchone()

    if not leave_request:
        conn.close()
        flash("Nie znaleziono wniosku.")
        return redirect(url_for("dashboard"))

    can_cancel = leave_request["user_id"] == session["user_id"] and leave_request["status"] == "oczekuje"
    can_cancel_as_hr = session.get("role") in ["admin", "kadry"] and leave_request["status"] == "oczekuje"

    if not can_cancel and not can_cancel_as_hr:
        conn.close()
        flash("Nie można anulować tego wniosku.")
        return redirect(url_for("dashboard"))

    conn.execute(
        """
        UPDATE leave_requests
        SET status = 'anulowany'
        WHERE id = ? AND status = 'oczekuje'
        """,
        (request_id,),
    )
    conn.commit()
    conn.close()

    flash("Wniosek został anulowany.")
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
