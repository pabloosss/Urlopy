from calendar import monthrange
from datetime import date

from flask import Flask, flash, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import SECRET_KEY, LEAVE_TYPES, FORCE_HTTPS, SESSION_COOKIE_SECURE
from .database import get_db, init_db
from .services import (
    count_workdays,
    ensure_vacation_years,
    format_pl_date,
    get_app_setting,
    is_hr,
    is_manager,
    parse_date,
    surname_first,
)
from .routes_main import bp as main_bp
from .routes_requests import bp as requests_bp
from .routes_pdf import bp as request_pdf_bp
from .routes_notifications import bp as request_notifications_bp
from .routes_employees import bp as employees_bp
from .routes_limits import bp as limits_bp
from .routes_reports import bp as reports_bp
from .routes_admin_alias import bp as admin_alias_bp
from .routes_backups import bp as backups_bp, maybe_run_automatic_backup
from .routes_employee_import import bp as employee_import_bp
from .routes_hr import bp as hr_tools_bp


def _closed_through_cutoff(conn):
    value = (get_app_setting(conn, "hr_closed_through", "") or "").strip()
    if not value:
        return None
    try:
        year, month = [int(part) for part in value.split("-", 1)]
        return date(year, month, monthrange(year, month)[1])
    except Exception:
        return None


def _auto_deactivate_finished_users(conn):
    if get_app_setting(conn, "auto_deactivate_after_end_date", "0") != "1":
        return set()

    today = date.today().isoformat()
    rows = conn.execute(
        """
        SELECT id, full_name, employment_end
        FROM users
        WHERE active = 1
          AND employment_end IS NOT NULL
          AND employment_end != ''
          AND employment_end < ?
        """,
        (today,),
    ).fetchall()
    ids = {row["id"] for row in rows}
    for row in rows:
        conn.execute("UPDATE users SET active = 0 WHERE id = ?", (row["id"],))
        conn.execute(
            """
            INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, details)
            VALUES (NULL, 'automatycznie wyłączono konto', 'user', ?, ?)
            """,
            (row["id"], f"data zakończenia: {row['employment_end']}"),
        )
    if rows:
        conn.commit()
    return ids


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = SECRET_KEY
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
        PREFERRED_URL_SCHEME="https" if FORCE_HTTPS else "http",
    )

    init_db()
    conn = get_db()
    ensure_vacation_years(conn)
    _auto_deactivate_finished_users(conn)
    conn.commit()
    conn.close()
    app.config["VACATION_YEAR_CHECKED"] = date.today().year

    app.register_blueprint(main_bp)
    app.register_blueprint(requests_bp)
    app.register_blueprint(request_pdf_bp)
    app.register_blueprint(request_notifications_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(limits_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_alias_bp)
    app.register_blueprint(backups_bp)
    app.register_blueprint(employee_import_bp)
    app.register_blueprint(hr_tools_bp)

    app.template_filter("pldate")(format_pl_date)
    app.template_filter("surname_first")(surname_first)

    @app.before_request
    def enforce_https_year_and_policies():
        if FORCE_HTTPS and not request.is_secure:
            return redirect(request.url.replace("http://", "https://", 1), code=301)

        maybe_run_automatic_backup()

        current_year = date.today().year
        if app.config.get("VACATION_YEAR_CHECKED") != current_year:
            conn = get_db()
            ensure_vacation_years(conn, current_year)
            conn.commit()
            conn.close()
            app.config["VACATION_YEAR_CHECKED"] = current_year

        conn = get_db()
        deactivated_ids = _auto_deactivate_finished_users(conn)
        conn.close()
        if session.get("user_id") in deactivated_ids:
            session.clear()
            flash("Konto zostało wyłączone po dacie zakończenia współpracy.")
            return redirect(url_for("main.login"))

        if request.method != "POST" or not session.get("user_id"):
            return None

        if request.endpoint in {"requests.new_leave_request", "requests.new_request"}:
            conn = get_db()
            allow_past = get_app_setting(conn, "allow_past_requests", "1") == "1"
            require_replacement = get_app_setting(conn, "require_spedycja_replacement", "1") == "1"
            min_notice_days = int(get_app_setting(conn, "min_notice_days", "0") or 0)
            max_request_days = int(get_app_setting(conn, "max_request_days", "0") or 0)
            closed_cutoff = _closed_through_cutoff(conn)
            user = conn.execute("SELECT department FROM users WHERE id = ?", (session["user_id"],)).fetchone()
            conn.close()

            date_from = request.form.get("date_from", "").strip()
            date_to = request.form.get("date_to", "").strip()
            leave_type = request.form.get("leave_type", "").strip()

            if not allow_past and date_from and date_from < date.today().isoformat():
                flash("Nie można złożyć wniosku z datą wcześniejszą niż dzisiejsza.")
                return redirect(url_for("requests.new_leave_request"))

            if not is_hr() and closed_cutoff and date_from and date_from <= closed_cutoff.isoformat():
                flash(f"Okres do {closed_cutoff.strftime('%m.%Y')} jest zamknięty przez Kadry.")
                return redirect(url_for("requests.new_leave_request"))

            if not is_hr() and min_notice_days > 0 and date_from and leave_type != "Urlop na żądanie":
                try:
                    notice = (parse_date(date_from) - date.today()).days
                except Exception:
                    notice = None
                if notice is not None and notice < min_notice_days:
                    flash(f"Ten wniosek trzeba złożyć co najmniej {min_notice_days} dni wcześniej.")
                    return redirect(url_for("requests.new_leave_request"))

            if max_request_days > 0 and date_from and date_to:
                try:
                    request_days = count_workdays(parse_date(date_from), parse_date(date_to))
                except Exception:
                    request_days = 0
                if request_days > max_request_days:
                    flash(f"Jeden wniosek może obejmować maksymalnie {max_request_days} dni roboczych.")
                    return redirect(url_for("requests.new_leave_request"))

            is_spedycja = user and (user["department"] or "").strip().lower() == "spedycja"
            if require_replacement and is_spedycja and not request.form.get("replacement_user_id", "").strip():
                flash("Dla Spedycji wybór osoby zastępującej jest wymagany.")
                return redirect(url_for("requests.new_leave_request"))

        if request.endpoint == "requests.change_request_status" and request.view_args:
            if request.view_args.get("action") == "cancel" and not is_hr():
                conn = get_db()
                allow_cancel = get_app_setting(conn, "allow_employee_cancel", "1") == "1"
                closed_cutoff = _closed_through_cutoff(conn)
                leave_request = conn.execute(
                    "SELECT date_from FROM leave_requests WHERE id = ?",
                    (request.view_args.get("request_id"),),
                ).fetchone()
                conn.close()

                if not allow_cancel:
                    flash("Samodzielne anulowanie wniosków jest wyłączone przez administratora.")
                    next_url = request.form.get("next", "")
                    if next_url.startswith("/"):
                        return redirect(next_url)
                    return redirect(url_for("main.my_leave"))

                if closed_cutoff and leave_request and leave_request["date_from"] <= closed_cutoff.isoformat():
                    flash(f"Okres do {closed_cutoff.strftime('%m.%Y')} jest zamknięty przez Kadry.")
                    next_url = request.form.get("next", "")
                    if next_url.startswith("/"):
                        return redirect(next_url)
                    return redirect(url_for("main.my_leave"))

        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.context_processor
    def inject_globals():
        values = {
            "is_hr": is_hr,
            "is_manager": is_manager,
            "leave_types": LEAVE_TYPES,
            "new_requests_count": 0,
            "new_request_ids": set(),
            "allow_employee_cancel": True,
            "allow_past_requests": True,
            "require_spedycja_replacement": True,
        }

        if session.get("user_id"):
            conn = get_db()
            values["allow_employee_cancel"] = get_app_setting(conn, "allow_employee_cancel", "1") == "1"
            values["allow_past_requests"] = get_app_setting(conn, "allow_past_requests", "1") == "1"
            values["require_spedycja_replacement"] = get_app_setting(conn, "require_spedycja_replacement", "1") == "1"

            if session.get("role") == "admin":
                rows = conn.execute(
                    """
                    SELECT rn.request_id
                    FROM request_notifications rn
                    JOIN leave_requests lr ON lr.id = rn.request_id
                    WHERE rn.recipient_user_id = ? AND rn.seen_at IS NULL
                    ORDER BY lr.created_at DESC
                    """,
                    (session["user_id"],),
                ).fetchall()
                ids = {row["request_id"] for row in rows}
                values["new_request_ids"] = ids
                values["new_requests_count"] = len(ids)
            conn.close()

        return values

    return app
