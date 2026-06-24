from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from .database import get_db
from .services import login_required, role_required, log_action, vacation_summary

bp = Blueprint("employees", __name__)

ROLES = [
    ("pracownik", "Pracownik"),
    ("menedzer", "Menedżer"),
    ("kadry", "Kadry"),
    ("admin", "Admin"),
]


def _to_int(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _next(default="employees.employees_view"):
    target = request.form.get("next", "")
    if target.startswith("/"):
        return redirect(target)
    return redirect(url_for(default))


def _employee_lists(conn):
    departments = conn.execute("SELECT name FROM departments ORDER BY name").fetchall()
    managers = conn.execute("""
        SELECT id, full_name
        FROM users
        WHERE role IN ('menedzer','admin','kadry') AND active=1
        ORDER BY full_name
    """).fetchall()
    return departments, managers


def _employee_or_404(conn, user_id):
    return conn.execute("""
        SELECT u.*, m.full_name AS manager_name
        FROM users u
        LEFT JOIN users m ON u.manager_id = m.id
        WHERE u.id = ?
    """, (user_id,)).fetchone()


@bp.route("/employees", methods=["GET", "POST"])
@login_required
@role_required("admin", "kadry")
def employees_view():
    conn = get_db()

    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "").strip() or "Start123!"
        if not login_value or not full_name:
            flash("Login i imię/nazwisko są wymagane.")
        else:
            try:
                cur = conn.execute("""
                    INSERT INTO users (
                        login, password_hash, full_name, email, role, vacation_days,
                        active, department, job_title, manager_id, contract_type, carryover_days
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """, (
                    login_value,
                    generate_password_hash(password),
                    full_name,
                    request.form.get("email", "").strip(),
                    request.form.get("role") or "pracownik",
                    _to_int(request.form.get("vacation_days"), 26),
                    request.form.get("department", "").strip(),
                    request.form.get("job_title", "").strip(),
                    request.form.get("manager_id") or None,
                    request.form.get("contract_type", "").strip(),
                    _to_int(request.form.get("carryover_days"), 0),
                ))
                log_action(conn, "dodano pracownika", "user", cur.lastrowid, full_name)
                conn.commit()
                flash("Pracownik dodany.")
            except Exception as error:
                conn.rollback()
                flash(f"Nie udało się dodać pracownika. Sprawdź login. Błąd: {error}")

    q = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip()
    department = request.args.get("department", "").strip()
    status = request.args.get("status", "").strip()
    manager_id = request.args.get("manager_id", "").strip()

    filters = ["1=1"]
    params = []
    if q:
        filters.append("(u.full_name LIKE ? OR u.login LIKE ? OR u.email LIKE ? OR u.job_title LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])
    if role:
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

    users = conn.execute(f"""
        SELECT u.*, m.full_name AS manager_name,
               COALESCE(lr.requests_count, 0) AS requests_count
        FROM users u
        LEFT JOIN users m ON u.manager_id = m.id
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS requests_count
            FROM leave_requests
            GROUP BY user_id
        ) lr ON lr.user_id = u.id
        WHERE {' AND '.join(filters)}
        ORDER BY u.active DESC, u.full_name
    """, params).fetchall()

    stats = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_count,
            SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END) AS inactive_count,
            SUM(CASE WHEN role = 'menedzer' THEN 1 ELSE 0 END) AS managers_count
        FROM users
    """).fetchone()
    departments, managers = _employee_lists(conn)
    conn.close()
    return render_template("employees.html", users=users, departments=departments, managers=managers, roles=ROLES, stats=stats)


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
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'oczekuje' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'zaakceptowany' THEN days_count ELSE 0 END) AS accepted_days,
            SUM(CASE WHEN status = 'odrzucony' THEN 1 ELSE 0 END) AS rejected
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
    departments, managers = _employee_lists(conn)
    summary = vacation_summary(conn, user)
    conn.close()
    return render_template(
        "employee_profile.html",
        user=user,
        requests_list=requests_list,
        request_stats=request_stats,
        audit_logs=audit_logs,
        departments=departments,
        managers=managers,
        roles=ROLES,
        vacation_summary=summary,
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

    login_value = request.form.get("login", "").strip()
    full_name = request.form.get("full_name", "").strip()
    if not login_value or not full_name:
        conn.close()
        flash("Login i imię/nazwisko są wymagane.")
        return _next()

    try:
        manager_id = request.form.get("manager_id") or None
        if manager_id and int(manager_id) == user_id:
            manager_id = None
        active = 1 if request.form.get("active") == "1" else 0
        conn.execute("""
            UPDATE users
            SET login=?, full_name=?, email=?, role=?, vacation_days=?, active=?,
                department=?, job_title=?, manager_id=?, contract_type=?, carryover_days=?
            WHERE id=?
        """, (
            login_value,
            full_name,
            request.form.get("email", "").strip(),
            request.form.get("role") or "pracownik",
            _to_int(request.form.get("vacation_days"), 26),
            active,
            request.form.get("department", "").strip(),
            request.form.get("job_title", "").strip(),
            manager_id,
            request.form.get("contract_type", "").strip(),
            _to_int(request.form.get("carryover_days"), 0),
            user_id,
        ))
        log_action(conn, "edytowano pracownika", "user", user_id, full_name)
        conn.commit()
        flash("Pracownik zaktualizowany.")
    except Exception as error:
        conn.rollback()
        flash(f"Nie udało się zapisać zmian. Błąd: {error}")
    conn.close()
    return _next()


@bp.route("/employees/<int:user_id>/password", methods=["POST"])
@login_required
@role_required("admin", "kadry")
def change_employee_password(user_id):
    new_password = request.form.get("new_password", "").strip()
    if len(new_password) < 6:
        flash("Hasło musi mieć minimum 6 znaków.")
        return _next()

    conn = get_db()
    user = conn.execute("SELECT full_name FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Nie znaleziono pracownika.")
        return redirect(url_for("employees.employees_view"))

    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), user_id))
    log_action(conn, "zmieniono hasło pracownika", "user", user_id, user["full_name"])
    conn.commit()
    conn.close()
    flash("Hasło zostało zmienione.")
    return _next()


@bp.route("/employees/<int:user_id>/toggle", methods=["POST"])
@login_required
@role_required("admin", "kadry")
def toggle_employee(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        new_status = 0 if user["active"] else 1
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (new_status, user_id))
        log_action(conn, "zmieniono aktywność pracownika", "user", user_id, user["full_name"])
        conn.commit()
        flash("Status pracownika zmieniony.")
    conn.close()
    return _next()


@bp.route("/employees/<int:user_id>/delete", methods=["POST"])
@login_required
@role_required("admin", "kadry")
def delete_employee(user_id):
    if user_id == session.get("user_id"):
        flash("Nie możesz usunąć aktualnie zalogowanego konta.")
        return _next()

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Nie znaleziono pracownika.")
        return redirect(url_for("employees.employees_view"))

    try:
        name = user["full_name"]
        conn.execute("UPDATE users SET manager_id = NULL WHERE manager_id = ?", (user_id,))
        conn.execute("UPDATE leave_requests SET decided_by = NULL WHERE decided_by = ?", (user_id,))
        conn.execute("UPDATE leave_requests SET replacement_user_id = NULL WHERE replacement_user_id = ?", (user_id,))
        conn.execute("DELETE FROM leave_requests WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM limit_adjustments WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE limit_adjustments SET changed_by = NULL WHERE changed_by = ?", (user_id,))
        conn.execute("UPDATE audit_logs SET actor_user_id = NULL WHERE actor_user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        log_action(conn, "usunięto pracownika", "user", user_id, name)
        conn.commit()
        flash("Pracownik został usunięty.")
    except Exception as error:
        conn.rollback()
        flash(f"Nie udało się usunąć pracownika. Błąd: {error}")
    conn.close()
    return redirect(url_for("employees.employees_view"))
