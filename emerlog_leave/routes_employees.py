from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from .config import CONTRACT_TYPES, CONTRACT_ZLECENIE
from .database import get_db
from .services import (
    get_app_setting,
    is_safe_local_path,
    login_required,
    role_required,
    log_action,
    surname_first,
    surname_first_to_storage,
    sync_user_year_balance,
    vacation_summary,
)

bp = Blueprint("employees", __name__)

ROLES = [
    ("pracownik", "Pracownik"),
    ("menedzer", "Menedżer"),
    ("kadry", "Kadry"),
    ("admin", "Admin"),
]
ROLE_VALUES = {value for value, _ in ROLES}


def _to_int(value, default=0):
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _clean_date(value):
    value = (value or "").strip()
    if not value:
        return None
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _fte_percent(value):
    result = _to_int(value, 100)
    if result < 1 or result > 100:
        raise ValueError("Wymiar etatu musi mieścić się w zakresie 1–100%.")
    return result


def _days_value(value, default, label):
    result = _to_int(value, default)
    if result < 0 or result > 366:
        raise ValueError(f"{label} musi mieścić się w zakresie 0–366 dni.")
    return result


def _next(default="employees.employees_view"):
    target = request.form.get("next", "")
    if is_safe_local_path(target):
        return redirect(target)
    return redirect(url_for(default))


def _employee_lists(conn):
    departments = conn.execute("SELECT name FROM departments ORDER BY name").fetchall()
    managers = conn.execute("""
        SELECT id, full_name, department
        FROM users
        WHERE role = 'menedzer' AND active = 1
    """).fetchall()
    managers = sorted(managers, key=lambda row: surname_first(row["full_name"]).casefold())
    companies = conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    return departments, managers, companies


def _employee_or_404(conn, user_id):
    return conn.execute("""
        SELECT u.*, m.full_name AS manager_name, c.name AS company_name
        FROM users u
        LEFT JOIN users m ON u.manager_id = m.id
        LEFT JOIN companies c ON u.company_id = c.id
        WHERE u.id = ?
    """, (user_id,)).fetchone()


def _available_roles():
    if session.get("role") == "admin":
        return ROLES
    return [item for item in ROLES if item[0] != "admin"]


def _validate_role(value):
    value = (value or "pracownik").strip()
    if value not in ROLE_VALUES:
        raise ValueError("Niepoprawna rola użytkownika.")
    if value == "admin" and session.get("role") != "admin":
        raise PermissionError("Tylko administrator może nadawać rolę Admin.")
    return value


def _validate_contract(value):
    value = (value or "Umowa o pracę").strip()
    if value not in CONTRACT_TYPES:
        raise ValueError("Niepoprawny typ umowy.")
    return value


def _validate_department(conn, value):
    value = (value or "").strip()
    if not value:
        raise ValueError("Wybierz dział.")
    row = conn.execute("SELECT 1 FROM departments WHERE name = ?", (value,)).fetchone()
    if not row:
        raise ValueError("Wybrany dział nie istnieje.")
    return value


def _company_id_from_form(conn):
    raw = (request.form.get("company_id") or "").strip()
    if not raw:
        raise ValueError("Wybierz spółkę.")
    try:
        company_id = int(raw)
    except ValueError as error:
        raise ValueError("Niepoprawna spółka.") from error
    if not conn.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone():
        raise ValueError("Wybrana spółka nie istnieje.")
    return company_id


def _manager_id_from_form(conn, user_id=None):
    raw = (request.form.get("manager_id") or "").strip()
    if not raw:
        return None
    try:
        manager_id = int(raw)
    except ValueError as error:
        raise ValueError("Niepoprawny menedżer.") from error
    if user_id and manager_id == user_id:
        raise ValueError("Pracownik nie może być swoim własnym menedżerem.")
    manager = conn.execute(
        "SELECT id FROM users WHERE id = ? AND role = 'menedzer' AND active = 1",
        (manager_id,),
    ).fetchone()
    if not manager:
        raise ValueError("Wybrany menedżer nie istnieje albo jest nieaktywny.")
    return manager_id


def _default_vacation_days(conn, contract_type):
    legacy = _to_int(get_app_setting(conn, "default_vacation_days", "26"), 26)
    if contract_type == CONTRACT_ZLECENIE:
        return _to_int(get_app_setting(conn, "default_vacation_days_zlecenie", "20"), 20)
    return _to_int(get_app_setting(conn, "default_vacation_days_uop", str(legacy)), legacy)


def _ensure_login_available(conn, login_value, exclude_user_id=None):
    if not login_value or len(login_value) > 120:
        raise ValueError("Login jest wymagany i może mieć maksymalnie 120 znaków.")
    if any(ch.isspace() for ch in login_value):
        raise ValueError("Login nie może zawierać spacji.")
    if exclude_user_id:
        conflict = conn.execute(
            "SELECT id FROM users WHERE lower(login) = lower(?) AND id <> ?",
            (login_value, exclude_user_id),
        ).fetchone()
    else:
        conflict = conn.execute(
            "SELECT id FROM users WHERE lower(login) = lower(?)",
            (login_value,),
        ).fetchone()
    if conflict:
        raise ValueError("Taki login jest już używany.")


def _ensure_can_manage_target(user):
    if user and user["role"] == "admin" and session.get("role") != "admin":
        raise PermissionError("Tylko administrator może zarządzać kontem innego administratora.")


def _ensure_not_last_admin(conn, user, new_role=None, new_active=None):
    if not user or user["role"] != "admin" or not user["active"]:
        return
    removes_admin = (new_role is not None and new_role != "admin") or (new_active is not None and not new_active)
    if not removes_admin:
        return
    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND active = 1 AND id <> ?",
        (user["id"],),
    ).fetchone()["c"] or 0
    if remaining == 0:
        raise ValueError("Nie można wyłączyć ani zdegradować ostatniego aktywnego administratora.")


@bp.route("/employees", methods=["GET", "POST"])
@login_required
@role_required("admin", "kadry")
def employees_view():
    conn = get_db()
    default_uop = _default_vacation_days(conn, "Umowa o pracę")
    default_zlecenie = _default_vacation_days(conn, CONTRACT_ZLECENIE)

    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        full_name = surname_first_to_storage(request.form.get("full_name", ""))
        password = request.form.get("password", "")
        try:
            if not full_name or len(full_name) > 160:
                raise ValueError("Nazwisko i imię są wymagane i mogą mieć maksymalnie 160 znaków.")
            _ensure_login_available(conn, login_value)
            if len(password) < 8:
                raise ValueError("Hasło startowe musi mieć minimum 8 znaków.")

            role = _validate_role(request.form.get("role"))
            contract_type = _validate_contract(request.form.get("contract_type"))
            department = _validate_department(conn, request.form.get("department"))
            company_id = _company_id_from_form(conn)
            manager_id = _manager_id_from_form(conn)
            employment_start = _clean_date(request.form.get("employment_start"))
            employment_end = _clean_date(request.form.get("employment_end"))
            if employment_start and employment_end and employment_end < employment_start:
                raise ValueError("Data zakończenia nie może być wcześniejsza niż data zatrudnienia.")
            fte_percent = _fte_percent(request.form.get("fte_percent"))
            hr_note = request.form.get("hr_note", "").strip()[:2000]
            default_days = _default_vacation_days(conn, contract_type)
            vacation_days = _days_value(request.form.get("vacation_days"), default_days, "Limit urlopu")
            carryover_days = _days_value(request.form.get("carryover_days"), 0, "Zaległe dni")

            cur = conn.execute("""
                INSERT INTO users (
                    login, password_hash, full_name, email, role, vacation_days,
                    active, department, job_title, manager_id, contract_type, carryover_days, company_id,
                    employment_start, employment_end, fte_percent, hr_note
                ) VALUES (?, ?, ?, '', ?, ?, 1, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                login_value,
                generate_password_hash(password),
                full_name,
                role,
                vacation_days,
                department,
                manager_id,
                contract_type,
                carryover_days,
                company_id,
                employment_start,
                employment_end,
                fte_percent,
                hr_note,
            ))
            sync_user_year_balance(conn, cur.lastrowid, vacation_days, carryover_days)
            log_action(conn, "dodano pracownika", "user", cur.lastrowid, full_name)
            conn.commit()
            flash("Pracownik dodany.")
        except (ValueError, PermissionError) as error:
            conn.rollback()
            flash(str(error))
        except Exception as error:
            conn.rollback()
            flash(f"Nie udało się dodać pracownika. Błąd: {error}")

    q = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip()
    department = request.args.get("department", "").strip()
    status = request.args.get("status", "").strip()
    manager_id = request.args.get("manager_id", "").strip()
    company_id = request.args.get("company_id", "").strip()

    filters = ["1=1"]
    params = []
    if q:
        parts = q.split()
        reversed_q = " ".join(parts[1:] + parts[:1]) if len(parts) > 1 else q
        filters.append("(u.full_name LIKE ? OR u.full_name LIKE ? OR u.login LIKE ? OR c.name LIKE ?)")
        params.extend([f"%{q}%", f"%{reversed_q}%", f"%{q}%", f"%{q}%"])
    if role in ROLE_VALUES:
        filters.append("u.role = ?")
        params.append(role)
    if department:
        filters.append("u.department = ?")
        params.append(department)
    if status == "active":
        filters.append("u.active = 1")
    elif status == "inactive":
        filters.append("u.active = 0")
    if manager_id:
        filters.append("u.manager_id = ?")
        params.append(manager_id)
    if company_id:
        filters.append("u.company_id = ?")
        params.append(company_id)

    users = conn.execute(f"""
        SELECT u.*, m.full_name AS manager_name, c.name AS company_name,
               COALESCE(lr.requests_count, 0) AS requests_count
        FROM users u
        LEFT JOIN users m ON u.manager_id = m.id
        LEFT JOIN companies c ON u.company_id = c.id
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS requests_count
            FROM leave_requests
            GROUP BY user_id
        ) lr ON lr.user_id = u.id
        WHERE {' AND '.join(filters)}
    """, params).fetchall()
    users = sorted(users, key=lambda row: (not bool(row["active"]), surname_first(row["full_name"]).casefold()))

    stats = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_count,
            SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END) AS inactive_count,
            SUM(CASE WHEN role = 'menedzer' THEN 1 ELSE 0 END) AS managers_count
        FROM users
    """).fetchone()
    departments, managers, companies = _employee_lists(conn)
    conn.close()
    return render_template(
        "employees.html",
        users=users,
        departments=departments,
        managers=managers,
        companies=companies,
        roles=_available_roles(),
        contract_types=CONTRACT_TYPES,
        stats=stats,
        default_vacation_days=default_uop,
        default_vacation_days_uop=default_uop,
        default_vacation_days_zlecenie=default_zlecenie,
    )


@bp.route("/employees/<int:user_id>")
@login_required
@role_required("admin", "kadry")
def employee_profile(user_id):
    conn = get_db()
    user = _employee_or_404(conn, user_id)
    if not user:
        conn.close()
        flash("Nie znaleziono pracownika.")
        return redirect(url_for("employees.employees_view"))

    requests_list = conn.execute("""
        SELECT lr.*, d.full_name AS decider_name, r.full_name AS replacement_name
        FROM leave_requests lr
        LEFT JOIN users d ON lr.decided_by = d.id
        LEFT JOIN users r ON lr.replacement_user_id = r.id
        WHERE lr.user_id = ?
        ORDER BY lr.created_at DESC
    """, (user_id,)).fetchall()
    request_stats = conn.execute("""
        SELECT COUNT(*) AS total
        FROM leave_requests
        WHERE user_id = ?
    """, (user_id,)).fetchone()
    audit_logs = conn.execute("""
        SELECT al.*, actor.full_name AS actor_name
        FROM audit_logs al
        LEFT JOIN users actor ON al.actor_user_id = actor.id
        WHERE al.entity_type = 'user' AND al.entity_id = ?
        ORDER BY al.created_at DESC
        LIMIT 20
    """, (user_id,)).fetchall()
    departments, managers, companies = _employee_lists(conn)
    summary = vacation_summary(conn, user)
    can_manage = not (user["role"] == "admin" and session.get("role") != "admin")
    conn.close()
    return render_template(
        "employee_profile_clean.html",
        user=user,
        requests_list=requests_list,
        request_stats=request_stats,
        audit_logs=audit_logs,
        departments=departments,
        managers=managers,
        companies=companies,
        roles=_available_roles(),
        contract_types=CONTRACT_TYPES,
        vacation_summary=summary,
        can_manage=can_manage,
    )


@bp.route("/employees/<int:user_id>/edit", methods=["POST"])
@login_required
@role_required("admin", "kadry")
def edit_employee(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Nie znaleziono pracownika.")
        return redirect(url_for("employees.employees_view"))

    try:
        _ensure_can_manage_target(user)
        login_value = request.form.get("login", "").strip()
        full_name = surname_first_to_storage(request.form.get("full_name", ""))
        if not full_name or len(full_name) > 160:
            raise ValueError("Nazwisko i imię są wymagane i mogą mieć maksymalnie 160 znaków.")
        _ensure_login_available(conn, login_value, user_id)

        role = _validate_role(request.form.get("role"))
        active = 1 if request.form.get("active") == "1" else 0
        if user_id == session.get("user_id") and not active:
            raise ValueError("Nie możesz wyłączyć własnego konta z poziomu profilu.")
        _ensure_not_last_admin(conn, user, new_role=role, new_active=active)

        contract_type = _validate_contract(request.form.get("contract_type"))
        department = _validate_department(conn, request.form.get("department"))
        company_id = _company_id_from_form(conn)
        manager_id = _manager_id_from_form(conn, user_id)
        employment_start = _clean_date(request.form.get("employment_start"))
        employment_end = _clean_date(request.form.get("employment_end"))
        if employment_start and employment_end and employment_end < employment_start:
            raise ValueError("Data zakończenia nie może być wcześniejsza niż data zatrudnienia.")
        fte_percent = _fte_percent(request.form.get("fte_percent"))
        hr_note = request.form.get("hr_note", "").strip()[:2000]

        conn.execute("""
            UPDATE users
            SET login=?, full_name=?, role=?, active=?, department=?, manager_id=?,
                contract_type=?, company_id=?, employment_start=?, employment_end=?,
                fte_percent=?, hr_note=?
            WHERE id=?
        """, (
            login_value,
            full_name,
            role,
            active,
            department,
            manager_id,
            contract_type,
            company_id,
            employment_start,
            employment_end,
            fte_percent,
            hr_note,
            user_id,
        ))

        changes = []
        if user["role"] != role:
            changes.append(f"rola {user['role']} → {role}")
        if bool(user["active"]) != bool(active):
            changes.append(f"status {'aktywny' if user['active'] else 'nieaktywny'} → {'aktywny' if active else 'nieaktywny'}")
        if (user["department"] or "") != department:
            changes.append(f"dział {user['department'] or '—'} → {department}")
        if (user["contract_type"] or "") != contract_type:
            changes.append(f"umowa {user['contract_type'] or '—'} → {contract_type}")
        if (user["employment_start"] or "") != (employment_start or ""):
            changes.append(f"zatrudnienie od {employment_start or '—'}")
        if (user["employment_end"] or "") != (employment_end or ""):
            changes.append(f"zatrudnienie do {employment_end or '—'}")
        if (user["fte_percent"] or 100) != fte_percent:
            changes.append(f"etat {user['fte_percent'] or 100}% → {fte_percent}%")

        log_action(conn, "edytowano pracownika", "user", user_id, "; ".join(changes) or "dane podstawowe")
        conn.commit()
        flash("Pracownik zaktualizowany.")
    except (ValueError, PermissionError) as error:
        conn.rollback()
        flash(str(error))
    except Exception as error:
        conn.rollback()
        flash(f"Nie udało się zapisać zmian. Błąd: {error}")
    conn.close()
    return _next()


@bp.route("/employees/<int:user_id>/password", methods=["POST"])
@login_required
@role_required("admin", "kadry")
def change_employee_password(user_id):
    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("Hasło musi mieć minimum 8 znaków.")
        return _next()

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Nie znaleziono pracownika.")
        return redirect(url_for("employees.employees_view"))
    try:
        _ensure_can_manage_target(user)
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), user_id))
        log_action(conn, "zmieniono hasło pracownika", "user", user_id, user["full_name"])
        conn.commit()
        flash("Hasło zostało zmienione.")
    except PermissionError as error:
        conn.rollback()
        flash(str(error))
    conn.close()
    return _next()


@bp.route("/employees/<int:user_id>/toggle", methods=["POST"])
@login_required
@role_required("admin", "kadry")
def toggle_employee(user_id):
    if user_id == session.get("user_id"):
        flash("Nie możesz wyłączyć własnego konta.")
        return _next()

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Nie znaleziono pracownika.")
        return _next()

    try:
        _ensure_can_manage_target(user)
        new_status = 0 if user["active"] else 1
        _ensure_not_last_admin(conn, user, new_active=new_status)

        if new_status and user["employment_end"]:
            try:
                ended = datetime.strptime(user["employment_end"], "%Y-%m-%d").date() < date.today()
            except ValueError:
                ended = False
            if ended and get_app_setting(conn, "auto_deactivate_after_end_date", "0") == "1":
                raise ValueError("Najpierw zmień datę zakończenia współpracy — automatyczne wyłączanie kont jest włączone.")

        conn.execute("UPDATE users SET active = ? WHERE id = ?", (new_status, user_id))
        if not new_status:
            conn.execute("DELETE FROM request_notifications WHERE recipient_user_id = ?", (user_id,))
        log_action(
            conn,
            "zmieniono aktywność pracownika",
            "user",
            user_id,
            f"{'włączono' if new_status else 'wyłączono'} konto",
        )
        conn.commit()
        flash("Status pracownika zmieniony.")
    except (ValueError, PermissionError) as error:
        conn.rollback()
        flash(str(error))
    conn.close()
    return _next()


@bp.route("/employees/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin", "kadry")
def delete_employee(user_id):
    """Archiwizuje konto zamiast kasować historię kadrową i wnioski."""
    if user_id == session.get("user_id"):
        flash("Nie możesz archiwizować własnego konta.")
        return _next()

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Nie znaleziono pracownika.")
        return redirect(url_for("employees.employees_view"))

    try:
        _ensure_can_manage_target(user)
        _ensure_not_last_admin(conn, user, new_active=0)
        if not user["active"]:
            flash("Konto jest już nieaktywne.")
        else:
            conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
            conn.execute("UPDATE users SET manager_id = NULL WHERE manager_id = ?", (user_id,))
            conn.execute("DELETE FROM request_notifications WHERE recipient_user_id = ?", (user_id,))
            log_action(conn, "zarchiwizowano pracownika", "user", user_id, user["full_name"])
            conn.commit()
            flash("Pracownik został zarchiwizowany. Historia wniosków i sald została zachowana.")
    except (ValueError, PermissionError) as error:
        conn.rollback()
        flash(str(error))
    except Exception as error:
        conn.rollback()
        flash(f"Nie udało się zarchiwizować pracownika. Błąd: {error}")
    conn.close()
    return redirect(url_for("employees.employees_view"))
