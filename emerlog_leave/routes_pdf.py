import calendar
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .config import CONTRACT_TYPES, normalize_contract_type
from .database import get_db
from .services import login_required, role_required, visible_user_ids

bp = Blueprint("request_pdf", __name__)


@bp.route("/request/<int:request_id>/print-pdf")
@login_required
def print_request_pdf(request_id):
    conn = get_db()
    row = conn.execute(
        """
        SELECT lr.*, u.full_name, u.department, u.contract_type, c.name AS company_name,
               r.full_name AS replacement_name
        FROM leave_requests lr
        JOIN users u ON u.id = lr.user_id
        LEFT JOIN companies c ON u.company_id = c.id
        LEFT JOIN users r ON lr.replacement_user_id = r.id
        WHERE lr.id = ?
        """,
        (request_id,),
    ).fetchone()

    if not row:
        conn.close()
        flash("Nie znaleziono wniosku.")
        return redirect(url_for("main.my_leave"))

    visible_ids = set(visible_user_ids(conn))
    if row["user_id"] != session.get("user_id") and row["user_id"] not in visible_ids:
        conn.close()
        flash("Brak uprawnień do tego wniosku.")
        return redirect(url_for("main.dashboard"))

    conn.close()
    return render_template("request_pdf.html", req=row)


@bp.route("/requests/bulk-pdf")
@login_required
@role_required("admin", "kadry")
def bulk_requests_pdf():
    month_raw = (request.args.get("month") or "").strip()
    contract_raw = (request.args.get("contract_type") or "").strip()

    try:
        year, month = [int(part) for part in month_raw.split("-", 1)]
        if year < 2000 or year > 2100 or month < 1 or month > 12:
            raise ValueError
    except Exception:
        flash("Wybierz poprawny miesiąc dla PDF zbiorczego.")
        return redirect(url_for("requests.all_requests_view", date_from="", date_to=""))

    if contract_raw not in CONTRACT_TYPES:
        flash("Wybierz poprawny rodzaj umowy dla PDF zbiorczego.")
        return redirect(url_for("requests.all_requests_view", date_from="", date_to=""))

    month_start = date(year, month, 1).isoformat()
    month_end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()

    conn = get_db()
    rows = conn.execute(
        """
        SELECT lr.*, u.full_name, u.department, u.contract_type, c.name AS company_name,
               r.full_name AS replacement_name
        FROM leave_requests lr
        JOIN users u ON u.id = lr.user_id
        LEFT JOIN companies c ON u.company_id = c.id
        LEFT JOIN users r ON lr.replacement_user_id = r.id
        WHERE lr.status = 'zaakceptowany'
          AND lr.date_from <= ?
          AND lr.date_to >= ?
        ORDER BY lr.date_from ASC, lr.created_at ASC, lr.id ASC
        """,
        (month_end, month_start),
    ).fetchall()
    conn.close()

    normalized_contract = normalize_contract_type(contract_raw)
    rows = [row for row in rows if normalize_contract_type(row["contract_type"]) == normalized_contract]

    if not rows:
        flash("Brak zaakceptowanych wniosków dla wybranego miesiąca i rodzaju umowy.")
        return redirect(url_for("requests.all_requests_view", date_from="", date_to=""))

    return render_template(
        "requests_bulk_pdf.html",
        requests_list=rows,
        selected_month=month_raw,
        selected_contract=contract_raw,
    )
