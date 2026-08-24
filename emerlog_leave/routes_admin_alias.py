from datetime import date
import re

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .database import get_db
from .services import get_app_setting, log_action, login_required, role_required, set_app_setting

bp = Blueprint("admin", __name__)


@bp.route("/admin/reports")
@login_required
@role_required("admin", "kadry", "menedzer")
def reports_view():
    return redirect(url_for("reports.reports_view"))


@bp.route("/admin/settings", methods=["GET", "POST"])
@login_required
@role_required("admin", "kadry")
def settings_view():
    conn = get_db()

    if request.method == "POST":
        try:
            default_days = int(request.form.get("default_vacation_days", "26"))
        except (TypeError, ValueError):
            default_days = -1

        closed_through = request.form.get("hr_closed_through", "").strip()
        closed_valid = not closed_through or bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", closed_through))

        if default_days < 0 or default_days > 60:
            flash("Domyślny limit musi mieścić się w zakresie 0–60 dni.")
        elif not closed_valid:
            flash("Niepoprawny miesiąc zamknięcia.")
        else:
            settings = {
                "default_vacation_days": str(default_days),
                "carryover_enabled": "1" if request.form.get("carryover_enabled") == "1" else "0",
                "require_spedycja_replacement": "1" if request.form.get("require_spedycja_replacement") == "1" else "0",
                "allow_past_requests": "1" if request.form.get("allow_past_requests") == "1" else "0",
                "allow_employee_cancel": "1" if request.form.get("allow_employee_cancel") == "1" else "0",
                "hr_closed_through": closed_through,
            }
            for key, value in settings.items():
                set_app_setting(conn, key, value)

            log_action(
                conn,
                "zmieniono ustawienia Kadr",
                "settings",
                None,
                (
                    f"limit od nowego roku: {default_days}; "
                    f"przenoszenie: {settings['carryover_enabled']}; "
                    f"zastępstwo Spedycja: {settings['require_spedycja_replacement']}; "
                    f"wnioski wstecz: {settings['allow_past_requests']}; "
                    f"samodzielne anulowanie: {settings['allow_employee_cancel']}; "
                    f"zamknięte do: {closed_through or 'brak'}"
                ),
            )
            conn.commit()
            flash("Ustawienia zostały zapisane.")

    values = {
        "default_vacation_days": int(get_app_setting(conn, "default_vacation_days", "26") or 26),
        "carryover_enabled": get_app_setting(conn, "carryover_enabled", "1") == "1",
        "require_spedycja_replacement": get_app_setting(conn, "require_spedycja_replacement", "1") == "1",
        "allow_past_requests": get_app_setting(conn, "allow_past_requests", "1") == "1",
        "allow_employee_cancel": get_app_setting(conn, "allow_employee_cancel", "1") == "1",
        "hr_closed_through": get_app_setting(conn, "hr_closed_through", "") or "",
        "next_year": date.today().year + 1,
    }
    conn.close()
    return render_template("admin_settings.html", **values)


@bp.route("/admin/audit")
@login_required
@role_required("admin", "kadry")
def audit_view():
    return redirect(url_for("reports.audit_view"))
