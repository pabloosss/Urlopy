from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .database import get_db
from .services import (
    is_safe_local_path,
    login_required,
    role_required,
    vacation_summary,
    log_action,
    surname_first,
    sync_user_year_balance,
)

bp = Blueprint("limits", __name__)


def _set_available_days(conn, user, available_days, reason):
    old_summary = vacation_summary(conn, user)
    old_available = int(old_summary["available"])
    if old_available == available_days:
        return old_available, False

    year = date.today().year
    sync_user_year_balance(
        conn,
        user["id"],
        user["vacation_days"] or 0,
        user["carryover_days"] or 0,
        year,
    )
    current_summary = vacation_summary(conn, user, year)
    adjustment = available_days - (
        current_summary["base"]
        + current_summary["carryover"]
        - current_summary["accepted"]
    )
    conn.execute(
        """
        UPDATE vacation_year_balances
        SET availability_adjustment = ?
        WHERE user_id = ? AND year = ?
        """,
        (adjustment, user["id"], year),
    )
    conn.execute(
        """
        INSERT INTO balance_adjustments (
            user_id, changed_by, old_available_days, new_available_days, reason
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (user["id"], session["user_id"], old_available, available_days, reason),
    )
    log_action(
        conn,
        "zmieniono pozostałe dni urlopu",
        "user",
        user["id"],
        f"pozostało {old_available} → {available_days}; powód: {reason}",
    )
    return old_available, True


def _safe_next(default_endpoint, **values):
    target = request.form.get("next", "")
    if is_safe_local_path(target):
        return redirect(target)
    return redirect(url_for(default_endpoint, **values))


@bp.route("/limits/user/<int:user_id>/available", methods=["POST"])
@login_required
@role_required("admin")
def set_user_available_days(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Nie znaleziono pracownika.")
        return redirect(url_for("employees.employees_view"))

    reason = " ".join(request.form.get("reason", "").split())
    if not reason:
        conn.close()
        flash("Podaj powód zmiany liczby dni.")
        return _safe_next("employees.employee_profile", user_id=user_id)
    if len(reason) > 500:
        conn.close()
        flash("Powód może mieć maksymalnie 500 znaków.")
        return _safe_next("employees.employee_profile", user_id=user_id)

    try:
        available_days = int(request.form.get("available_days"))
    except (TypeError, ValueError):
        conn.close()
        flash("Wpisz poprawną liczbę dni.")
        return _safe_next("employees.employee_profile", user_id=user_id)

    if not 0 <= available_days <= 732:
        conn.close()
        flash("Liczba dni musi mieścić się w zakresie 0–732.")
        return _safe_next("employees.employee_profile", user_id=user_id)

    try:
        old_available, changed = _set_available_days(conn, user, available_days, reason)
        if not changed:
            conn.close()
            flash(f"Ta osoba ma już {old_available} dni do wykorzystania.")
            return _safe_next("employees.employee_profile", user_id=user_id)
        conn.commit()
        flash(f"Ustawiono {available_days} dni do wykorzystania.")
    except Exception as error:
        conn.rollback()
        flash(f"Nie udało się zmienić liczby dni. Błąd: {error}")
    finally:
        conn.close()
    return _safe_next("employees.employee_profile", user_id=user_id)


@bp.route("/limits", methods=["GET", "POST"])
@login_required
@role_required("admin", "kadry")
def limits_view():
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action", "limit").strip()
        reason = " ".join(request.form.get("reason", "").split())
        if not reason:
            conn.close()
            flash("Podaj powód korekty.")
            return redirect(url_for("limits.limits_view"))
        if len(reason) > 500:
            conn.close()
            flash("Powód korekty może mieć maksymalnie 500 znaków.")
            return redirect(url_for("limits.limits_view"))

        if action == "available":
            try:
                user_id = int(request.form.get("user_id"))
                available_days = int(request.form.get("available_days"))
            except (TypeError, ValueError):
                conn.close()
                flash("Niepoprawna liczba pozostałych dni.")
                return redirect(url_for("limits.limits_view"))

            if not 0 <= available_days <= 732:
                conn.close()
                flash("Pozostałe dni muszą mieścić się w zakresie 0–732.")
                return redirect(url_for("limits.limits_view", user_id=user_id))

            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not user:
                conn.close()
                flash("Nie znaleziono pracownika.")
                return redirect(url_for("limits.limits_view"))

            old_available, changed = _set_available_days(conn, user, available_days, reason)
            if not changed:
                conn.close()
                flash("Ta osoba ma już ustawioną taką liczbę pozostałych dni.")
                return redirect(url_for("limits.limits_view", user_id=user_id))

            conn.commit()
            conn.close()
            flash(f"Ustawiono {available_days} dni do wykorzystania.")
            return redirect(url_for("limits.limits_view", user_id=user_id))

        try:
            user_id = int(request.form.get("user_id"))
            vacation_days = int(request.form.get("vacation_days") or 0)
            carryover_days = int(request.form.get("carryover_days") or 0)
        except (TypeError, ValueError):
            conn.close()
            flash("Niepoprawne dane korekty.")
            return redirect(url_for("limits.limits_view"))

        if not 0 <= vacation_days <= 366 or not 0 <= carryover_days <= 366:
            conn.close()
            flash("Limit i zaległe dni muszą mieścić się w zakresie 0–366.")
            return redirect(url_for("limits.limits_view", user_id=user_id))

        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            conn.close()
            flash("Nie znaleziono pracownika.")
            return redirect(url_for("limits.limits_view"))

        if user["vacation_days"] == vacation_days and (user["carryover_days"] or 0) == carryover_days:
            conn.close()
            flash("Nie ma żadnej zmiany do zapisania.")
            return redirect(url_for("limits.limits_view", user_id=user_id))

        conn.execute("""
            INSERT INTO limit_adjustments (
                user_id, changed_by, old_vacation_days, new_vacation_days,
                old_carryover_days, new_carryover_days, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            session["user_id"],
            user["vacation_days"],
            vacation_days,
            user["carryover_days"],
            carryover_days,
            reason,
        ))
        conn.execute(
            "UPDATE users SET vacation_days = ?, carryover_days = ? WHERE id = ?",
            (vacation_days, carryover_days, user_id),
        )
        sync_user_year_balance(conn, user_id, vacation_days, carryover_days)
        log_action(
            conn,
            "zmieniono limit urlopu",
            "user",
            user_id,
            f"limit {user['vacation_days']} → {vacation_days}; zaległe {user['carryover_days'] or 0} → {carryover_days}; powód: {reason}",
        )
        conn.commit()
        conn.close()
        flash("Limit urlopu zapisany.")
        return redirect(url_for("limits.limits_view", user_id=user_id))

    users = conn.execute("SELECT * FROM users WHERE active = 1").fetchall()
    users = sorted(users, key=lambda user: surname_first(user["full_name"]).casefold())
    summaries = [{"user": user, "summary": vacation_summary(conn, user)} for user in users]
    adjustments = conn.execute("""
        SELECT la.*, u.full_name AS employee_name, a.full_name AS actor_name
        FROM limit_adjustments la
        JOIN users u ON la.user_id = u.id
        LEFT JOIN users a ON la.changed_by = a.id
        ORDER BY la.created_at DESC LIMIT 30
    """).fetchall()
    balance_adjustments = conn.execute("""
        SELECT ba.*, u.full_name AS employee_name, a.full_name AS actor_name
        FROM balance_adjustments ba
        JOIN users u ON ba.user_id = u.id
        LEFT JOIN users a ON ba.changed_by = a.id
        ORDER BY ba.created_at DESC LIMIT 30
    """).fetchall()
    selected_user_id = request.args.get("user_id", "").strip()
    if selected_user_id and not selected_user_id.isdigit():
        selected_user_id = ""
    conn.close()
    return render_template(
        "limits.html",
        summaries=summaries,
        adjustments=adjustments,
        balance_adjustments=balance_adjustments,
        selected_user_id=selected_user_id,
    )
