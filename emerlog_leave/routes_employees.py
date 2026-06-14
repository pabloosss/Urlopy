from flask import Blueprint, flash, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

from .database import get_db
from .services import login_required, role_required, log_action

bp = Blueprint("employees", __name__)


@bp.route("/employees", methods=["GET", "POST"])
@login_required
@role_required("admin", "kadry")
def employees_view():
    conn = get_db()
    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        full_name = request.form.get("full_name", "").strip()
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
                    generate_password_hash(request.form.get("password") or "Start123!"),
                    full_name,
                    request.form.get("email"),
                    request.form.get("role"),
                    int(request.form.get("vacation_days") or 26),
                    request.form.get("department"),
                    request.form.get("job_title"),
                    request.form.get("manager_id") or None,
                    request.form.get("contract_type"),
                    int(request.form.get("carryover_days") or 0),
                ))
                log_action(conn, "dodano pracownika", "user", cur.lastrowid, full_name)
                conn.commit()
                flash("Pracownik dodany.")
            except Exception:
                flash("Nie udało się dodać pracownika. Sprawdź, czy login nie istnieje.")

    users = conn.execute("""
        SELECT u.*, m.full_name AS manager_name
        FROM users u
        LEFT JOIN users m ON u.manager_id = m.id
        ORDER BY u.active DESC, u.full_name
    """).fetchall()
    departments = conn.execute("SELECT name FROM departments ORDER BY name").fetchall()
    managers = conn.execute("SELECT id, full_name FROM users WHERE role IN ('menedzer','admin','kadry') ORDER BY full_name").fetchall()
    conn.close()
    return render_template("employees.html", users=users, departments=departments, managers=managers)


@bp.route("/employees/<int:user_id>/toggle", methods=["POST"])
@login_required
@role_required("admin", "kadry")
def toggle_employee(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        new_status = 0 if user["active"] else 1
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (new_status, user_id))
        log_action(conn, "zmieniono aktywność pracownika", "user", user_id)
        conn.commit()
        flash("Status pracownika zmieniony.")
    conn.close()
    return redirect(url_for("employees.employees_view"))
