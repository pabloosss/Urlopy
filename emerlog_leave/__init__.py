from flask import Flask, redirect, request, session
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import SECRET_KEY, LEAVE_TYPES, FORCE_HTTPS, SESSION_COOKIE_SECURE
from .database import get_db, init_db
from .services import format_pl_date, is_hr, is_manager
from .routes_main import bp as main_bp
from .routes_requests import bp as requests_bp
from .routes_pdf import bp as request_pdf_bp
from .routes_notifications import bp as request_notifications_bp
from .routes_employees import bp as employees_bp
from .routes_limits import bp as limits_bp
from .routes_reports import bp as reports_bp
from .routes_admin_alias import bp as admin_alias_bp


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

    app.register_blueprint(main_bp)
    app.register_blueprint(requests_bp)
    app.register_blueprint(request_pdf_bp)
    app.register_blueprint(request_notifications_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(limits_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_alias_bp)

    app.template_filter("pldate")(format_pl_date)

    @app.before_request
    def enforce_https():
        if FORCE_HTTPS and not request.is_secure:
            return redirect(request.url.replace("http://", "https://", 1), code=301)
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
        }

        if session.get("role") == "admin" and session.get("user_id"):
            conn = get_db()
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
            conn.close()
            ids = {row["request_id"] for row in rows}
            values["new_request_ids"] = ids
            values["new_requests_count"] = len(ids)

        return values

    return app
