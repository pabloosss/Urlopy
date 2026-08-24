from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .database import get_db
from .services import login_required, role_required, vacation_summary, log_action, surname_first, sync_user_year_balance

bp = Blueprint("limits", __name__)


@bp.route("/limits", methods=["GET", "POST"])
@login_required
@role_required("admin", "kadry")
def limits_view():
    conn = get_db()
    if request.method == "POST":
        reason = " ".join(request.form.get("reason", "").split())
        if not reason:
            conn.close()
            flash("Podaj powód korekty.")
            return redirect(url_for("limits.limits_view"))
        if len(reason) > 500:
            conn.close()
            flash("Powód korekty może mieć maksymalnie 500 znaków.")
            return redirect(url_for("limits.limits_view"))

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
    selected_user_id = request.args.get("user_id", "").strip()
    if selected_user_id and not selected_user_id.isdigit():
        selected_user_id = ""
    conn.close()
    return render_template(
        "limits.html",
        summaries=summaries,
        adjustments=adjustments,
        selected_user_id=selected_user_id,
    )
