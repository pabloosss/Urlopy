from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .database import get_db
from .services import (
    get_app_setting,
    log_action,
    login_required,
    role_required,
    set_app_setting,
    vacation_summary,
)

bp = Blueprint("admin", __name__)


@bp.route("/admin/reports")
@login_required
@role_required("admin", "kadry", "menedzer")
def reports_view():
    return redirect(url_for("reports.reports_view"))


@bp.route("/admin/settings", methods=["GET", "POST"])
@login_required
@role_required("admin")
def settings_view():
    conn = get_db()

    if request.method == "POST":
        try:
            default_days = int(request.form.get("default_vacation_days", "26"))
        except (TypeError, ValueError):
            default_days = -1

        if default_days < 0 or default_days > 60:
            flash("Domyślny limit musi mieścić się w zakresie 0–60 dni.")
        else:
            carryover_enabled = "1" if request.form.get("carryover_enabled") == "1" else "0"
            set_app_setting(conn, "default_vacation_days", default_days)
            set_app_setting(conn, "carryover_enabled", carryover_enabled)
            log_action(
                conn,
                "zmieniono ustawienia urlopowe",
                "settings",
                None,
                f"domyślny limit: {default_days}; przenoszenie: {'tak' if carryover_enabled == '1' else 'nie'}",
            )
            conn.commit()
            flash("Ustawienia urlopowe zostały zapisane.")

    current_year = date.today().year
    default_days = int(get_app_setting(conn, "default_vacation_days", "26") or 26)
    carryover_enabled = get_app_setting(conn, "carryover_enabled", "1") == "1"

    users = conn.execute(
        """
        SELECT u.*, c.name AS company_name
        FROM users u
        LEFT JOIN companies c ON u.company_id = c.id
        WHERE u.active = 1
        ORDER BY u.department, u.full_name
        """
    ).fetchall()

    balances = []
    unused_total = 0
    unused_people = 0
    for user in users:
        summary = vacation_summary(conn, user, current_year)
        if summary["unused"] > 0:
            unused_people += 1
            unused_total += summary["unused"]
        balances.append({"user": user, "summary": summary})

    rollover_history = conn.execute(
        """
        SELECT vyb.*, u.full_name
        FROM vacation_year_balances vyb
        JOIN users u ON u.id = vyb.user_id
        WHERE vyb.processed_at IS NOT NULL
        ORDER BY vyb.year DESC, u.full_name
        LIMIT 30
        """
    ).fetchall()

    conn.close()
    return render_template(
        "admin_settings.html",
        current_year=current_year,
        default_vacation_days=default_days,
        carryover_enabled=carryover_enabled,
        balances=balances,
        unused_people=unused_people,
        unused_total=unused_total,
        rollover_history=rollover_history,
    )


@bp.route("/admin/audit")
@login_required
@role_required("admin", "kadry")
def audit_view():
    return redirect(url_for("reports.audit_view"))
