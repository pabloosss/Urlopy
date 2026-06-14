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
    return redirect(url_for("reports.settings_view"))


@bp.route("/admin/audit")
@login_required
@role_required("admin", "kadry")
def audit_view():
    return redirect(url_for("reports.audit_view"))
