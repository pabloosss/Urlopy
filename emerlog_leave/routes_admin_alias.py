from datetime import date
import re

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .database import get_db
from .services import get_app_setting, log_action, login_required, role_required, set_app_setting

bp = Blueprint("admin", __name__)


def _int_value(name, default, minimum, maximum):
    try:
        value = int(request.form.get(name, str(default)) or default)
    except (TypeError, ValueError):
        raise ValueError(f"Niepoprawna wartość pola: {name}.")
    if value < minimum or value > maximum:
        raise ValueError(f"Wartość pola {name} musi mieścić się w zakresie {minimum}–{maximum}.")
    return value


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
        closed_through = request.form.get("hr_closed_through", "").strip()
        closed_valid = not closed_through or bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", closed_through))

        try:
            uop_days = _int_value("default_vacation_days_uop", 26, 0, 60)
            zlecenie_days = _int_value("default_vacation_days_zlecenie", 20, 0, 60)
            max_carryover = _int_value("max_carryover_days", 0, 0, 60)
            min_notice_days = _int_value("min_notice_days", 0, 0, 90)
            max_request_days = _int_value("max_request_days", 0, 0, 60)
            contract_alert_days = _int_value("contract_alert_days", 45, 1, 365)
            carryover_alert_threshold = _int_value("carryover_alert_threshold", 1, 0, 60)
            if not closed_valid:
                raise ValueError("Niepoprawny miesiąc zamknięcia.")
        except ValueError as error:
            flash(str(error))
        else:
            settings = {
                "default_vacation_days": str(uop_days),
                "default_vacation_days_uop": str(uop_days),
                "default_vacation_days_zlecenie": str(zlecenie_days),
                "enforce_uop_vacation_limit": "1" if request.form.get("enforce_uop_vacation_limit") == "1" else "0",
                "carryover_enabled": "1" if request.form.get("carryover_enabled") == "1" else "0",
                "max_carryover_days": str(max_carryover),
                "require_spedycja_replacement": "1" if request.form.get("require_spedycja_replacement") == "1" else "0",
                "allow_past_requests": "1" if request.form.get("allow_past_requests") == "1" else "0",
                "allow_employee_cancel": "1" if request.form.get("allow_employee_cancel") == "1" else "0",
                "min_notice_days": str(min_notice_days),
                "max_request_days": str(max_request_days),
                "hr_closed_through": closed_through,
                "contract_alert_days": str(contract_alert_days),
                "carryover_alert_threshold": str(carryover_alert_threshold),
                "auto_deactivate_after_end_date": "1" if request.form.get("auto_deactivate_after_end_date") == "1" else "0",
                "include_inactive_in_hr_exports": "1" if request.form.get("include_inactive_in_hr_exports") == "1" else "0",
            }
            for key, value in settings.items():
                set_app_setting(conn, key, value)

            log_action(
                conn,
                "zmieniono ustawienia Kadr",
                "settings",
                None,
                (
                    f"UoP={uop_days}; limit UoP={settings['enforce_uop_vacation_limit']}; "
                    f"zlecenie={zlecenie_days}; przenoszenie={settings['carryover_enabled']}; "
                    f"max zaległych={max_carryover}; wyprzedzenie={min_notice_days}; max wniosek={max_request_days}; "
                    f"alert umów={contract_alert_days}; próg zaległych={carryover_alert_threshold}; "
                    f"auto wyłączenie={settings['auto_deactivate_after_end_date']}; zamknięte do={closed_through or 'brak'}"
                ),
            )
            conn.commit()
            flash("Ustawienia zostały zapisane.")

    legacy_default = int(get_app_setting(conn, "default_vacation_days", "26") or 26)
    values = {
        "default_vacation_days_uop": int(get_app_setting(conn, "default_vacation_days_uop", str(legacy_default)) or legacy_default),
        "default_vacation_days_zlecenie": int(get_app_setting(conn, "default_vacation_days_zlecenie", "20") or 20),
        "enforce_uop_vacation_limit": get_app_setting(conn, "enforce_uop_vacation_limit", "1") == "1",
        "carryover_enabled": get_app_setting(conn, "carryover_enabled", "1") == "1",
        "max_carryover_days": int(get_app_setting(conn, "max_carryover_days", "0") or 0),
        "require_spedycja_replacement": get_app_setting(conn, "require_spedycja_replacement", "1") == "1",
        "allow_past_requests": get_app_setting(conn, "allow_past_requests", "1") == "1",
        "allow_employee_cancel": get_app_setting(conn, "allow_employee_cancel", "1") == "1",
        "min_notice_days": int(get_app_setting(conn, "min_notice_days", "0") or 0),
        "max_request_days": int(get_app_setting(conn, "max_request_days", "0") or 0),
        "hr_closed_through": get_app_setting(conn, "hr_closed_through", "") or "",
        "contract_alert_days": int(get_app_setting(conn, "contract_alert_days", "45") or 45),
        "carryover_alert_threshold": int(get_app_setting(conn, "carryover_alert_threshold", "1") or 1),
        "auto_deactivate_after_end_date": get_app_setting(conn, "auto_deactivate_after_end_date", "0") == "1",
        "include_inactive_in_hr_exports": get_app_setting(conn, "include_inactive_in_hr_exports", "0") == "1",
        "next_year": date.today().year + 1,
    }
    conn.close()
    return render_template("admin_settings.html", **values)


@bp.route("/admin/audit")
@login_required
@role_required("admin", "kadry")
def audit_view():
    return redirect(url_for("reports.audit_view"))
