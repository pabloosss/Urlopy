from flask import Flask

from .config import SECRET_KEY, LEAVE_TYPES
from .database import init_db
from .services import format_pl_date, is_hr, is_manager
from .routes_main import bp as main_bp
from .routes_requests import bp as requests_bp
from .routes_employees import bp as employees_bp
from .routes_limits import bp as limits_bp
from .routes_reports import bp as reports_bp


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = SECRET_KEY

    init_db()

    app.register_blueprint(main_bp)
    app.register_blueprint(requests_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(limits_bp)
    app.register_blueprint(reports_bp)

    app.template_filter("pldate")(format_pl_date)

    @app.context_processor
    def inject_globals():
        return {"is_hr": is_hr, "is_manager": is_manager, "leave_types": LEAVE_TYPES}

    return app
