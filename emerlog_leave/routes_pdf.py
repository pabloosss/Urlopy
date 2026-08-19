from flask import Blueprint, flash, redirect, render_template, session, url_for

from .database import get_db
from .services import login_required, visible_user_ids

bp = Blueprint("request_pdf", __name__)


@bp.route("/request/<int:request_id>/print-pdf")
@login_required
def print_request_pdf(request_id):
    conn = get_db()
    row = conn.execute(
        """
        SELECT lr.*, u.full_name, u.department, c.name AS company_name,
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
