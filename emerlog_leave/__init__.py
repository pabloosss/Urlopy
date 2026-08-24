from calendar import monthrange
from datetime import date, datetime, timedelta
import hmac
import secrets
import threading
import time

from flask import Flask, abort, flash, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import (
    FORCE_HTTPS,
    LEAVE_TYPES,
    SECRET_KEY,
    SESSION_COOKIE_SECURE,
    SESSION_HOURS,
    USING_DEFAULT_SECRET_KEY,
)
from .database import get_db, init_db
from .services import (
    count_workdays,
    ensure_vacation_years,
    format_pl_date,
    get_app_setting,
    is_hr,
    is_manager,
    parse_date,
    repair_leave_request_day_counts,
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
from .routes_backups import bp as backups_bp, maybe_run_automatic_backup, restore_in_progress
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


def _backup_scheduler_loop(app):
    """Niezależny scheduler backupów; blokada plikowa chroni przed duplikatami workerów."""
    while True:
        try:
            with app.app_context():
                maybe_run_automatic_backup()
        except Exception:
            app.logger.exception("Błąd schedulera automatycznych backupów")
        time.sleep(60)


def _ensure_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = SECRET_KEY
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
        SESSION_COOKIE_NAME="emerlog_urlopy_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
        PREFERRED_URL_SCHEME="https" if FORCE_HTTPS else "http",
    )

    if USING_DEFAULT_SECRET_KEY:
        app.logger.warning(
            "Aplikacja używa domyślnego SECRET_KEY. Na produkcji ustaw zmienną środowiskową SECRET_KEY."
        )

    init_db()
    conn = get_db()
    ensure_vacation_years(conn)
    repaired_days = repair_leave_request_day_counts(conn)
    _auto_deactivate_finished_users(conn)
    if repaired_days:
        conn.execute(
            """
            INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, details)
            VALUES (NULL, 'przeliczono dni wniosków', 'maintenance', NULL, ?)
            """,
            (f"poprawiono {repaired_days} wpisów",),
        )
    conn.commit()
    conn.close()
    app.config["VACATION_YEAR_CHECKED"] = date.today().year
    app.config["LAST_ACCOUNT_MAINTENANCE_HOUR"] = None

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

    backup_thread = threading.Thread(
        target=_backup_scheduler_loop,
        args=(app,),
        name="urlopy-backup-scheduler",
        daemon=True,
    )
    backup_thread.start()

    @app.before_request
    def security_and_business_policies():
        if FORCE_HTTPS and not request.is_secure:
            return redirect(request.url.replace("http://", "https://", 1), code=308)

        # Przywracanie bazy jest krótką operacją administracyjną. W tym czasie
        # nie dopuszczamy innych żądań do pracy na częściowo odtworzonych danychch.
        if restore_in_progress():
            return (
                "Trwa przywracanie danych. Spróbuj ponownie za kilka sekund.",
                503,
                {"Retry-After": "5", "Cache-Control": "no-store"},
            )

        current_year = date.today().year
        if app.config.get("VACATION_YEAR_CHECKED") != current_year:
            conn = get_db()
            ensure_vacation_years(conn, current_year)
            conn.commit()
            conn.close()
            app.config["VACATION_YEAR_CHECKED"] = current_year

        maintenance_hour = datetime.now().strftime("%Y-%m-%d-%H")
        if app.config.get("LAST_ACCOUNT_MAINTENANCE_HOUR") != maintenance_hour:
            conn = get_db()
            _auto_deactivate_finished_users(conn)
            conn.close()
            app.config["LAST_ACCOUNT_MAINTENANCE_HOUR"] = maintenance_hour

        # Sesja nie jest źródłem prawdy o roli i aktywności. Odświeżamy ją z bazy.
        if session.get("user_id"):
            conn = get_db()
            db_user = conn.execute(
                "SELECT id, login, full_name, role, active FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()
            conn.close()
            if not db_user or not db_user["active"]:
                session.clear()
                flash("Konto jest nieaktywne albo nie istnieje. Zaloguj się ponownie.")
                return redirect(url_for("main.login"))
            session["login"] = db_user["login"]
            session["full_name"] = db_user["full_name"]
            session["role"] = db_user["role"]
            session.permanent = True

        # Ochrona wszystkich operacji modyfikujących dane, także logowania i uploadów.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("_csrf_token")
            provided = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
            if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
                abort(400, description="Nieprawidłowy lub wygasły token formularza. Odśwież stronę i spróbuj ponownie.")

        if request.method != "POST" or not session.get("user_id"):
            return None

        if request.endpoint in {"requests.new_leave_request", "requests.new_request"}:
            conn = get_db()
            allow_past = get_app_setting(conn, "allow_past_requests", "1") == "1"
            require_replacement = get_app_setting(conn, "require_spedycja_replacement", "1") == "1"
            min_notice_days = int(get_app_setting(conn, "min_notice_days", "0") or 0)
            max_request_days = int(get_app_setting(conn, "max_request_days", "0") or 0)
            closed_cutoff = _closed_through_cutoff(conn)
            user = conn.execute(
                "SELECT department, employment_start, employment_end FROM users WHERE id = ?",
                (session["user_id"],),
            ).fetchone()

            date_from = request.form.get("date_from", "").strip()
            date_to = request.form.get("date_to", "").strip()
            leave_type = request.form.get("leave_type", "").strip()
            start = None
            end = None
            try:
                if date_from:
                    start = parse_date(date_from)
                if date_to:
                    end = parse_date(date_to)
            except Exception:
                pass

            if not allow_past and start and start < date.today():
                conn.close()
                flash("Nie można złożyć wniosku z datą wcześniejszą niż dzisiejsza.")
                return redirect(url_for("requests.new_leave_request"))

            if start and end and start.year != end.year:
                conn.close()
                flash("Wniosek nie może przechodzić przez dwa lata. Złóż osobny wniosek dla każdego roku.")
                return redirect(url_for("requests.new_leave_request"))

            if user and start and user["employment_start"]:
                try:
                    employment_start = parse_date(user["employment_start"])
                except Exception:
                    employment_start = None
                if employment_start and start < employment_start:
                    conn.close()
                    flash("Wniosek zaczyna się przed datą zatrudnienia.")
                    return redirect(url_for("requests.new_leave_request"))

            if user and end and user["employment_end"]:
                try:
                    employment_end = parse_date(user["employment_end"])
                except Exception:
                    employment_end = None
                if employment_end and end > employment_end:
                    conn.close()
                    flash("Wniosek kończy się po dacie zakończenia współpracy.")
                    return redirect(url_for("requests.new_leave_request"))

            if not is_hr() and closed_cutoff and start and start <= closed_cutoff:
                conn.close()
                flash(f"Okres do {closed_cutoff.strftime('%m.%Y')} jest zamknięty przez Kadry.")
                return redirect(url_for("requests.new_leave_request"))

            if not is_hr() and min_notice_days > 0 and start and leave_type != "Urlop na żądanie":
                notice = (start - date.today()).days
                if notice < min_notice_days:
                    conn.close()
                    flash(f"Ten wniosek trzeba złożyć co najmniej {min_notice_days} dni wcześniej.")
                    return redirect(url_for("requests.new_leave_request"))

            if max_request_days > 0 and start and end and end >= start:
                request_days = count_workdays(start, end)
                if request_days > max_request_days:
                    conn.close()
                    flash(f"Jeden wniosek może obejmować maksymalnie {max_request_days} dni roboczych.")
                    return redirect(url_for("requests.new_leave_request"))

            is_spedycja = user and (user["department"] or "").strip().lower() == "spedycja"
            replacement_raw = request.form.get("replacement_user_id", "").strip()
            if require_replacement and is_spedycja and not replacement_raw:
                conn.close()
                flash("Dla Spedycji wybór osoby zastępującej jest wymagany.")
                return redirect(url_for("requests.new_leave_request"))

            if replacement_raw and start and end:
                try:
                    replacement_id = int(replacement_raw)
                except ValueError:
                    replacement_id = None
                if replacement_id:
                    conflict = conn.execute(
                        """
                        SELECT 1
                        FROM leave_requests
                        WHERE user_id = ?
                          AND status = 'zaakceptowany'
                          AND date_from <= ?
                          AND date_to >= ?
                        LIMIT 1
                        """,
                        (replacement_id, end.isoformat(), start.isoformat()),
                    ).fetchone()
                    if conflict:
                        conn.close()
                        flash("Wybrana osoba zastępująca ma już nieobecność w tym terminie.")
                        return redirect(url_for("requests.new_leave_request"))

            conn.close()

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
                    if next_url.startswith("/") and not next_url.startswith("//"):
                        return redirect(next_url)
                    return redirect(url_for("main.my_leave"))

                if closed_cutoff and leave_request and leave_request["date_from"] <= closed_cutoff.isoformat():
                    flash(f"Okres do {closed_cutoff.strftime('%m.%Y')} jest zamknięty przez Kadry.")
                    next_url = request.form.get("next", "")
                    if next_url.startswith("/") and not next_url.startswith("//"):
                        return redirect(next_url)
                    return redirect(url_for("main.my_leave"))

        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if session.get("user_id"):
            response.headers.setdefault("Cache-Control", "no-store, private")
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
            "csrf_token": _ensure_csrf_token(),
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
