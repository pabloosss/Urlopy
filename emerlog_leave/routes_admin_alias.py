from flask import Blueprint, redirect, url_for

from .services import login_required, role_required

bp = Blueprint("admin", __name__)


@bp.route("/admin/reports")
@login_required
@role_required("admin", "kadry", "menedzer")
def reports_view():
    return redirect(url_for("reports.reports_view"))


@bp.route("/admin/settings")
@login_required
@role_required("admin", "kadry")
def settings_view():
    return "<html><head><link rel='stylesheet' href='/static/style.css'></head><body><main class='simple-page'><div class='card'><h1>Ustawienia</h1><p>Ten ekran jest przygotowany jako etap następny. Działy i typy nieobecności są już w bazie i konfiguracji.</p><a class='btn-primary' href='/dashboard'>Wróć</a></div></main></body></html>"


@bp.route("/admin/audit")
@login_required
@role_required("admin", "kadry")
def audit_view():
    return redirect(url_for("reports.audit_view"))
