from datetime import date, datetime, timedelta
from functools import wraps
from flask import flash, redirect, session, url_for

from .config import CONTRACT_ZLECENIE, HR_ROLES, LIMIT_TYPES


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


def surname_first(value):
    """Wyświetla nazwisko przed imieniem bez zmiany danych zapisanych w bazie."""
    text = " ".join(str(value or "").split())
    parts = text.split(" ") if text else []
    if len(parts) < 2:
        return text
    return f"{parts[-1]} {' '.join(parts[:-1])}"


def surname_first_to_storage(value):
    """Zamienia formularz 'Nazwisko Imię' na wewnętrzny format 'Imię Nazwisko'."""
    text = " ".join(str(value or "").split())
    parts = text.split(" ") if text else []
    if len(parts) < 2:
        return text
    return f"{' '.join(parts[1:])} {parts[0]}"


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
    return conn.execute("""
        SELECT u.*, c.name AS company_name
        FROM users u
        LEFT JOIN companies c ON u.company_id = c.id
        WHERE u.id = ?
    """, (session["user_id"],)).fetchone()


def visible_user_ids(conn):
    if is_hr():
        return [row["id"] for row in conn.execute("SELECT id FROM users")]
    if is_manager():
        return [row["id"] for row in conn.execute("SELECT id FROM users WHERE manager_id = ?", (session["user_id"],))]
    return [session["user_id"]]


def get_app_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_app_setting(conn, key, value):
    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """,
        (key, str(value)),
    )


def vacation_days_used_in_year(conn, user_id, year):
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    limit_types = sorted(LIMIT_TYPES)
    placeholders = ",".join("?" for _ in limit_types)
    rows = conn.execute(
        f"""
        SELECT date_from, date_to
        FROM leave_requests
        WHERE user_id = ?
          AND status = 'zaakceptowany'
          AND leave_type IN ({placeholders})
          AND date_from <= ?
          AND date_to >= ?
        """,
        (user_id, *limit_types, year_end.isoformat(), year_start.isoformat()),
    ).fetchall()

    used = 0
    for row in rows:
        start = max(parse_date(row["date_from"]), year_start)
        end = min(parse_date(row["date_to"]), year_end)
        if start <= end:
            used += count_workdays(start, end)
    return used


def sync_user_year_balance(conn, user_id, base_days, carryover_days, year=None):
    year = year or date.today().year
    conn.execute(
        """
        INSERT INTO vacation_year_balances (user_id, year, base_days, opening_carryover)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, year) DO UPDATE SET
            base_days=excluded.base_days,
            opening_carryover=excluded.opening_carryover
        """,
        (user_id, year, int(base_days or 0), int(carryover_days or 0)),
    )


def _default_days_for_contract(conn, contract_type):
    legacy = int(get_app_setting(conn, "default_vacation_days", "26") or 26)
    if contract_type == CONTRACT_ZLECENIE:
        return int(get_app_setting(conn, "default_vacation_days_zlecenie", str(legacy)) or legacy)
    return int(get_app_setting(conn, "default_vacation_days_uop", str(legacy)) or legacy)


def ensure_vacation_years(conn, current_year=None):
    """Tworzy bieżący rok i wykonuje rollover tylko raz przy zmianie roku."""
    current_year = current_year or date.today().year
    carryover_enabled = get_app_setting(conn, "carryover_enabled", "1") == "1"
    max_carryover = int(get_app_setting(conn, "max_carryover_days", "0") or 0)
    users = conn.execute(
        "SELECT id, vacation_days, carryover_days, contract_type FROM users ORDER BY id"
    ).fetchall()

    for user in users:
        default_days = _default_days_for_contract(conn, user["contract_type"])
        balances = conn.execute(
            "SELECT * FROM vacation_year_balances WHERE user_id = ? ORDER BY year",
            (user["id"],),
        ).fetchall()

        if not balances:
            sync_user_year_balance(
                conn,
                user["id"],
                user["vacation_days"] if user["vacation_days"] is not None else default_days,
                user["carryover_days"] or 0,
                current_year,
            )
            continue

        latest_year = balances[-1]["year"]
        while latest_year < current_year:
            previous = conn.execute(
                "SELECT * FROM vacation_year_balances WHERE user_id = ? AND year = ?",
                (user["id"], latest_year),
            ).fetchone()
            request_used = vacation_days_used_in_year(conn, user["id"], latest_year)
            opening_used = previous["opening_used_days"] or 0
            adjustment = previous["availability_adjustment"] or 0
            used = opening_used + request_used
            available = (
                (previous["base_days"] or 0)
                + (previous["opening_carryover"] or 0)
                - used
                + adjustment
            )
            carried = max(0, available) if carryover_enabled else 0
            if max_carryover > 0:
                carried = min(carried, max_carryover)

            conn.execute(
                """
                UPDATE vacation_year_balances
                SET used_days = ?, carried_to_next = ?, processed_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND year = ?
                """,
                (used, carried, user["id"], latest_year),
            )

            next_year = latest_year + 1
            next_default_days = _default_days_for_contract(conn, user["contract_type"])
            conn.execute(
                """
                INSERT OR IGNORE INTO vacation_year_balances
                    (user_id, year, base_days, opening_carryover)
                VALUES (?, ?, ?, ?)
                """,
                (user["id"], next_year, next_default_days, carried),
            )
            latest_year = next_year

        current_balance = conn.execute(
            "SELECT base_days, opening_carryover FROM vacation_year_balances WHERE user_id = ? AND year = ?",
            (user["id"], current_year),
        ).fetchone()
        if current_balance:
            conn.execute(
                "UPDATE users SET vacation_days = ?, carryover_days = ? WHERE id = ?",
                (
                    current_balance["base_days"] or 0,
                    current_balance["opening_carryover"] or 0,
                    user["id"],
                ),
            )


def vacation_summary(conn, user, year=None):
    year = year or date.today().year
    current_year = date.today().year
    balance = conn.execute(
        "SELECT * FROM vacation_year_balances WHERE user_id = ? AND year = ?",
        (user["id"], year),
    ).fetchone()

    base = (balance["base_days"] if balance else user["vacation_days"]) or 0
    if balance:
        carryover = balance["opening_carryover"] or 0
        opening_used = balance["opening_used_days"] or 0
        adjustment = balance["availability_adjustment"] or 0
    elif year == current_year:
        carryover = user["carryover_days"] or 0
        opening_used = 0
        adjustment = 0
    else:
        carryover = 0
        opening_used = 0
        adjustment = 0

    request_used = vacation_days_used_in_year(conn, user["id"], year)
    accepted = opening_used + request_used
    total = base + carryover
    available = total - accepted + adjustment

    return {
        "year": year,
        "total": total,
        "base": base,
        "carryover": carryover,
        "accepted": accepted,
        "pending": 0,
        "available": available,
        "unused": max(0, available),
        "opening_used": opening_used,
        "request_used": request_used,
        "availability_adjustment": adjustment,
    }


def log_action(conn, action, entity_type, entity_id=None, details=""):
    conn.execute("INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?)", (session.get("user_id"), action, entity_type, entity_id, details))
