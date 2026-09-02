from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit

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


def is_safe_local_path(value):
    """Pozwala na przekierowania wyłącznie wewnątrz tej aplikacji."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return False
    if "\\" in value:
        return False
    parsed = urlsplit(value)
    return not parsed.scheme and not parsed.netloc


def calculate_easter(year):
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31; day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def polish_holidays(year):
    e = calculate_easter(year)
    holidays = {
        date(year, 1, 1),
        date(year, 1, 6),
        e + timedelta(days=1),
        date(year, 5, 1),
        date(year, 5, 3),
        e + timedelta(days=60),
        date(year, 8, 15),
        date(year, 11, 1),
        date(year, 11, 11),
        date(year, 12, 25),
        date(year, 12, 26),
    }
    # Wigilia jest dniem ustawowo wolnym od pracy w Polsce od 2025 r.
    if year >= 2025:
        holidays.add(date(year, 12, 24))
    return holidays


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


def workdays_in_period(date_from, date_to, period_start, period_end):
    """Liczba dni roboczych z wniosku przypadających faktycznie na wskazany okres."""
    start = max(parse_date(date_from), period_start)
    end = min(parse_date(date_to), period_end)
    if start > end:
        return 0
    return count_workdays(start, end)


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
        used += workdays_in_period(row["date_from"], row["date_to"], year_start, year_end)
    return used


def _effective_used_days(conn, user_id, year, balance=None):
    """Zwraca wykorzystanie z uwzględnieniem snapshotu z importu i późniejszych zmian wniosków."""
    if balance is None:
        balance = conn.execute(
            "SELECT * FROM vacation_year_balances WHERE user_id = ? AND year = ?",
            (user_id, year),
        ).fetchone()

    request_used = vacation_days_used_in_year(conn, user_id, year)
    if not balance:
        return request_used, request_used

    source_used = balance["source_used_days"]
    if source_used is not None:
        baseline = int(balance["request_used_baseline"] or 0)
        effective = max(0, int(source_used or 0) + request_used - baseline)
        return effective, request_used

    # Rekordy utworzone przed mechanizmem snapshotów zachowują starą logikę.
    opening_used = int(balance["opening_used_days"] or 0)
    return opening_used + request_used, request_used


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
            used, _ = _effective_used_days(conn, user["id"], latest_year, previous)
            adjustment = previous["availability_adjustment"] or 0
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

    if balance:
        base = balance["base_days"] or 0
        carryover = balance["opening_carryover"] or 0
        adjustment = balance["availability_adjustment"] or 0
    elif year == current_year:
        base = user["vacation_days"] or 0
        carryover = user["carryover_days"] or 0
        adjustment = 0
    elif year > current_year:
        # Przyszły rok może mieć już zaplanowane wnioski przed wykonaniem rolloveru.
        # Używamy wtedy nowego limitu dla typu umowy, bez zgadywania przyszłych zaległości.
        base = _default_days_for_contract(conn, user["contract_type"])
        carryover = 0
        adjustment = 0
    else:
        # Brak historycznego rekordu oznacza, że nie znamy dawnego limitu.
        base = 0
        carryover = 0
        adjustment = 0

    accepted, request_used = _effective_used_days(conn, user["id"], year, balance)
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
        "opening_used": (balance["opening_used_days"] or 0) if balance else 0,
        "request_used": request_used,
        "source_used": balance["source_used_days"] if balance else None,
        "request_used_baseline": (balance["request_used_baseline"] or 0) if balance else 0,
        "availability_adjustment": adjustment,
    }


def repair_leave_request_day_counts(conn):
    """Ujednolica zapisane dni z aktualnym kalendarzem dni roboczych."""
    changed = 0
    rows = conn.execute("SELECT id, date_from, date_to, days_count FROM leave_requests").fetchall()
    for row in rows:
        try:
            days = count_workdays(parse_date(row["date_from"]), parse_date(row["date_to"]))
        except Exception:
            continue
        if int(row["days_count"] or 0) != days:
            conn.execute("UPDATE leave_requests SET days_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (days, row["id"]))
            changed += 1
    return changed


def log_action(conn, action, entity_type, entity_id=None, details=""):
    conn.execute("INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, details) VALUES (?, ?, ?, ?, ?)", (session.get("user_id"), action, entity_type, entity_id, details))
