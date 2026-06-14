from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .database import get_db
from .services import login_required, role_required, vacation_summary, log_action

bp = Blueprint("limits", __name__)


@bp.route("/limits", methods=["GET", "POST"])
@login_required
@role_required("admin", "kadry")
def limits_view():
    conn = get_db()
    if request.method == "POST":
        user_id = int(request.form.get("user_id"))
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        vacation_days = int(request.form.get("vacation_days") or 0)
        carryover_days = int(request.form.get("carryover_days") or 0)
        reason = request.form.get("reason", "")
        conn.execute("""
            INSERT INTO limit_adjustments (
                user_id, changed_by, old_vacation_days, new_vacation_days,
                old_carryover_days, new_carryover_days, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, session["user_id"], user["vacation_days"], vacation_days, user["carryover_days"], carryover_days, reason))
        conn.execute("UPDATE users SET vacation_days = ?, carryover_days = ? WHERE id = ?", (vacation_days, carryover_days, user_id))
        log_action(conn, "zmieniono limit urlopu", "user", user_id, reason)
        conn.commit()
        flash("Limit urlopu zapisany.")

    users = conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY full_name").fetchall()
    summaries = [{"user": user, "summary": vacation_summary(conn, user)} for user in users]
    adjustments = conn.execute("""
        SELECT la.*, u.full_name AS employee_name, a.full_name AS actor_name
        FROM limit_adjustments la
        JOIN users u ON la.user_id = u.id
        LEFT JOIN users a ON la.changed_by = a.id
        ORDER BY la.created_at DESC LIMIT 30
    """).fetchall()
    conn.close()
    return render_template("limits.html", summaries=summaries, adjustments=adjustments)
