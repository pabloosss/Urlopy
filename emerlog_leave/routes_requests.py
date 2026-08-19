from datetime import date
from io import BytesIO
import os

from flask import Blueprint, flash, redirect, render_template, request, send_file, session, url_for

from .config import LEAVE_TYPES, LIMIT_TYPES, STATUSES, leave_types_for_user
from .database import get_db
from .services import login_required, current_user, visible_user_ids, vacation_summary, count_workdays, parse_date, log_action, is_hr

bp = Blueprint("requests", __name__)

LEAVE_TYPE_DESCRIPTIONS = {
    "Urlop wypoczynkowy": "Urlop z limitu rocznego.",
    "Urlop na żądanie": "Urlop z limitu rocznego.",
    "Urlop okolicznościowy": "Ślub, pogrzeb, narodziny dziecka lub inna okoliczność.",
    "L4 / chorobowe": "Nieobecność chorobowa.",
    "Urlop bezpłatny": "Nie schodzi z limitu urlopu wypoczynkowego.",
    "Odbiór dnia wolnego": "Odbiór za pracę w innym terminie.",
    "Inne": "Wymagany komentarz.",
}


def _safe_redirect(next_url, default="requests.requests_view"):
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for(default))


def _query_requests(conn):
    ids = visible_user_ids(conn)
    if not ids:
        return []
    filters = [f"lr.user_id IN ({','.join('?' for _ in ids)})"]
    params = list(ids)
    for field, column in [
        ("department", "u.department"),
        ("status", "lr.status"),
        ("leave_type", "lr.leave_type"),
        ("manager_id", "u.manager_id"),
        ("company_id", "u.company_id"),
    ]:
        value = request.args.get(field, "").strip()
        if value:
            filters.append(f"{column}=?")
            params.append(value)
    employee = request.args.get("employee", "").strip()
    if employee:
        filters.append("(u.full_name LIKE ? OR u.login LIKE ?)")
        params.extend([f"%{employee}%", f"%{employee}%"])
    if request.args.get("date_from"):
        filters.append("lr.date_to >= ?")
        params.append(request.args.get("date_from"))
    if request.args.get("date_to"):
        filters.append("lr.date_from <= ?")
        params.append(request.args.get("date_to"))
    return conn.execute(f"""
        SELECT lr.*, u.full_name, u.login, u.department, c.name AS company_name,
               m.full_name AS manager_name, d.full_name AS decider_name, r.full_name AS replacement_name
        FROM leave_requests lr
        JOIN users u ON u.id=lr.user_id
        LEFT JOIN companies c ON u.company_id=c.id
        LEFT JOIN users m ON u.manager_id=m.id
        LEFT JOIN users d ON lr.decided_by=d.id
        LEFT JOIN users r ON lr.replacement_user_id=r.id
        WHERE {' AND '.join(filters)} ORDER BY lr.created_at DESC
    """, params).fetchall()


def _query_all_requests(conn, default_today=True):
    filters = ["1=1"]
    params = []

    employee = request.args.get("employee", "").strip()
    if employee:
        filters.append("(u.full_name LIKE ? OR u.login LIKE ?)")
        params.extend([f"%{employee}%", f"%{employee}%"])

    for field, column in [
        ("department", "u.department"),
        ("status", "lr.status"),
        ("leave_type", "lr.leave_type"),
        ("manager_id", "u.manager_id"),
        ("company_id", "u.company_id"),
    ]:
        value = request.args.get(field, "").strip()
        if value:
            filters.append(f"{column} = ?")
            params.append(value)

    today = date.today().isoformat()
    has_date_args = "date_from" in request.args or "date_to" in request.args
    if default_today and not has_date_args:
        date_from = today
        date_to = today
    else:
        date_from = request.args.get("date_from", "").strip()
        date_to = request.args.get("date_to", "").strip()

    if date_from:
        filters.append("lr.date_to >= ?")
        params.append(date_from)
    if date_to:
        filters.append("lr.date_from <= ?")
        params.append(date_to)

    rows = conn.execute(f"""
        SELECT lr.*, u.full_name, u.login, u.department, c.name AS company_name,
               m.full_name AS manager_name,
               d.full_name AS decider_name,
               r.full_name AS replacement_name
        FROM leave_requests lr
        JOIN users u ON u.id = lr.user_id
        LEFT JOIN companies c ON u.company_id = c.id
        LEFT JOIN users m ON u.manager_id = m.id
        LEFT JOIN users d ON lr.decided_by = d.id
        LEFT JOIN users r ON lr.replacement_user_id = r.id
        WHERE {' AND '.join(filters)}
        ORDER BY lr.created_at DESC
    """, params).fetchall()
    return rows, date_from, date_to


def _request_for_pdf(conn, request_id):
    return conn.execute("""
        SELECT lr.*, u.full_name, u.department, c.name AS company_name,
               r.full_name AS replacement_name
        FROM leave_requests lr
        JOIN users u ON u.id = lr.user_id
        LEFT JOIN companies c ON u.company_id = c.id
        LEFT JOIN users r ON lr.replacement_user_id = r.id
        WHERE lr.id = ?
    """, (request_id,)).fetchone()


def _register_pdf_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular = "Helvetica"
    bold = "Helvetica-Bold"
    regular_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        if os.path.exists(regular_path) and "UrlopySans" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("UrlopySans", regular_path))
        if os.path.exists(bold_path) and "UrlopySansBold" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("UrlopySansBold", bold_path))
        if "UrlopySans" in pdfmetrics.getRegisteredFontNames():
            regular = "UrlopySans"
        if "UrlopySansBold" in pdfmetrics.getRegisteredFontNames():
            bold = "UrlopySansBold"
    except Exception:
        pass
    return regular, bold


def _draw_logo(canvas, x, y, width, bold_font):
    from reportlab.lib.colors import HexColor

    scale = width / 760.0
    canvas.saveState()
    canvas.translate(x, y)
    canvas.scale(scale, scale)

    def polygon(points, color):
        path = canvas.beginPath()
        path.moveTo(points[0][0], points[0][1])
        for px, py in points[1:]:
            path.lineTo(px, py)
        path.close()
        canvas.setFillColor(HexColor(color))
        canvas.drawPath(path, fill=1, stroke=0)

    polygon([(46, 126), (116, 126), (88, 86), (66, 86), (28, 18), (0, 64)], "#111111")
    polygon([(24, 18), (118, 18), (136, 0), (45, 0)], "#d71920")
    polygon([(104, 86), (176, 86), (190, 52), (170, 24), (86, 24), (98, 46), (142, 46), (142, 62), (88, 62)], "#d71920")
    canvas.setFillColor(HexColor("#050505"))
    canvas.setFont(bold_font, 68)
    canvas.drawString(215, 34, "EMERLOG")
    canvas.restoreState()


def _draw_wrapped_text(canvas, text, x, y, max_width, font_name, font_size=11, leading=16):
    from reportlab.pdfbase.pdfmetrics import stringWidth

    words = (text or "").split()
    line = ""
    for word in words:
        candidate = word if not line else f"{line} {word}"
        if stringWidth(candidate, font_name, font_size) <= max_width:
            line = candidate
        else:
            canvas.drawString(x, y, line)
            y -= leading
            line = word
    if line:
        canvas.drawString(x, y, line)
        y -= leading
    return y


@bp.route("/leave/new", methods=["GET", "POST"])
@login_required
def new_leave_request():
    conn = get_db()
    user = current_user(conn)
    available_leave_types = leave_types_for_user(user["contract_type"], user["department"])
    summary = vacation_summary(conn, user)
    employees = conn.execute("SELECT id, full_name FROM users WHERE active=1 AND id!=? ORDER BY full_name", (user["id"],)).fetchall()

    form_data = {
        "leave_type": available_leave_types[0],
        "date_from": "",
        "date_to": "",
        "replacement_user_id": "",
        "comment": "",
    }

    def render_form(status_code=200):
        conn.close()
        return render_template(
            "leave_form.html",
            user=user,
            employees=employees,
            vacation_summary=summary,
            leave_types=available_leave_types,
            limit_types=sorted(LIMIT_TYPES),
            leave_type_descriptions=LEAVE_TYPE_DESCRIPTIONS,
            today=date.today().isoformat(),
            form_data=form_data,
        ), status_code

    if request.method == "POST":
        form_data = {
            "leave_type": request.form.get("leave_type", "").strip(),
            "date_from": request.form.get("date_from", "").strip(),
            "date_to": request.form.get("date_to", "").strip(),
            "replacement_user_id": request.form.get("replacement_user_id", "").strip(),
            "comment": request.form.get("comment", "").strip(),
        }
        errors = []
        days = 0
        start = None
        end = None
        replacement_id = None

        if form_data["leave_type"] not in available_leave_types:
            errors.append("Wybierz poprawny typ nieobecności dla działu albo typu umowy.")

        if form_data["leave_type"] == "Inne" and not form_data["comment"]:
            errors.append("Dla typu „Inne” komentarz jest wymagany.")

        if not form_data["date_from"] or not form_data["date_to"]:
            errors.append("Uzupełnij datę od i datę do.")
        else:
            try:
                start = parse_date(form_data["date_from"])
                end = parse_date(form_data["date_to"])
                days = count_workdays(start, end)
            except Exception as error:
                errors.append(str(error))

        if days <= 0 and start and end:
            errors.append("Wybrany zakres nie zawiera dni roboczych.")

        if form_data["replacement_user_id"]:
            try:
                replacement_id = int(form_data["replacement_user_id"])
            except ValueError:
                errors.append("Wybierz poprawną osobę na zastępstwo z listy podpowiedzi.")
                replacement_id = None
            if replacement_id == user["id"]:
                errors.append("Nie możesz wybrać siebie jako zastępstwa.")
            elif replacement_id:
                replacement = conn.execute("SELECT id FROM users WHERE id=? AND active=1", (replacement_id,)).fetchone()
                if not replacement:
                    errors.append("Wybrana osoba na zastępstwo nie istnieje albo jest nieaktywna.")

        if not errors and form_data["leave_type"] in LIMIT_TYPES and days > summary["available"]:
            errors.append(f"Brak limitu. Dostępne: {summary['available']} dni, wybrano: {days} dni.")

        if not errors:
            overlap = conn.execute(
                """
                SELECT leave_type, date_from, date_to, status
                FROM leave_requests
                WHERE user_id=?
                  AND status = 'zaakceptowany'
                  AND date_from <= ?
                  AND date_to >= ?
                ORDER BY date_from
                LIMIT 1
                """,
                (user["id"], form_data["date_to"], form_data["date_from"]),
            ).fetchone()
            if overlap:
                errors.append(
                    f"Masz już wniosek w tym terminie: {overlap['leave_type']} "
                    f"({overlap['date_from']} - {overlap['date_to']})."
                )

        if len(form_data["comment"]) > 1000:
            errors.append("Komentarz jest za długi. Maksymalnie 1000 znaków.")

        if errors:
            for error in errors:
                flash(error)
            return render_form(400)

        cur = conn.execute("""
            INSERT INTO leave_requests (
                user_id, leave_type, date_from, date_to, days_count, comment,
                replacement_user_id, status, decided_by, decided_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'zaakceptowany', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (
            user["id"],
            form_data["leave_type"],
            form_data["date_from"],
            form_data["date_to"],
            days,
            form_data["comment"],
            replacement_id,
        ))
        log_action(conn, "złożono wniosek", "leave_request", cur.lastrowid, f"{form_data['leave_type']}: {form_data['date_from']} - {form_data['date_to']}")
        conn.commit()
        request_id = cur.lastrowid
        conn.close()
        flash("Wniosek został zapisany.")
        return redirect(url_for("main.my_leave", created=request_id))

    return render_form()


@bp.route("/new-request", methods=["POST"])
@login_required
def new_request():
    return new_leave_request()


@bp.route("/request/<int:request_id>/pdf")
@login_required
def download_request_pdf(request_id):
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdf_canvas

    conn = get_db()
    row = _request_for_pdf(conn, request_id)
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

    regular_font, bold_font = _register_pdf_fonts()
    buffer = BytesIO()
    pdf = pdf_canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4
    left = 22 * mm
    right = page_width - 22 * mm
    y = page_height - 22 * mm

    _draw_logo(pdf, left, y - 12 * mm, 70 * mm, bold_font)
    pdf.setFillColor(HexColor("#555555"))
    pdf.setFont(regular_font, 9)
    pdf.drawRightString(right, y - 2 * mm, "data")
    pdf.setFillColor(HexColor("#111111"))
    pdf.setFont(bold_font, 10)
    pdf.drawRightString(right, y - 7 * mm, (row["created_at"] or "")[:10])

    y -= 36 * mm
    pdf.setFont(bold_font, 17)
    pdf.drawCentredString(page_width / 2, y, "Wniosek urlopowy")

    y -= 18 * mm
    pdf.setFont(regular_font, 8)
    pdf.setFillColor(HexColor("#666666"))
    pdf.drawString(left, y, "Pracownik")
    y -= 6 * mm
    pdf.setFont(bold_font, 11)
    pdf.setFillColor(HexColor("#111111"))
    pdf.drawString(left, y, row["full_name"])

    y -= 12 * mm
    pdf.setFont(regular_font, 8)
    pdf.setFillColor(HexColor("#666666"))
    pdf.drawString(left, y, "Spółka")
    y -= 6 * mm
    pdf.setFont(bold_font, 11)
    pdf.setFillColor(HexColor("#111111"))
    pdf.drawString(left, y, row["company_name"] or "EMERLOG SP. Z O. O.")

    y -= 18 * mm
    if row["leave_type"] in {"Urlop wypoczynkowy", "Urlop na żądanie"}:
        leave_text = "urlopu wypoczynkowego"
    else:
        leave_text = f"nieobecności: {row['leave_type']}"
    request_text = (
        f"Proszę o udzielenie {leave_text} w liczbie {row['days_count']} dni, "
        f"w terminie od {row['date_from']} do {row['date_to']}."
    )
    pdf.setFont(regular_font, 11)
    y = _draw_wrapped_text(pdf, request_text, left, y, right - left, regular_font, 11, 17)

    y -= 7 * mm
    pdf.setFont(regular_font, 8)
    pdf.setFillColor(HexColor("#666666"))
    pdf.drawString(left, y, "W czasie urlopu zastępować będzie mnie")
    y -= 6 * mm
    pdf.setFont(bold_font, 11)
    pdf.setFillColor(HexColor("#111111"))
    pdf.drawString(left, y, row["replacement_name"] or "—")

    if row["comment"]:
        y -= 14 * mm
        pdf.setFont(regular_font, 8)
        pdf.setFillColor(HexColor("#666666"))
        pdf.drawString(left, y, "Komentarz")
        y -= 6 * mm
        pdf.setFillColor(HexColor("#111111"))
        y = _draw_wrapped_text(pdf, row["comment"], left, y, right - left, regular_font, 10, 15)

    y -= 12 * mm
    pdf.setFont(bold_font, 11)
    pdf.setFillColor(HexColor("#111111"))
    pdf.drawString(left, y, f"Status: {row['status']}")

    pdf.showPage()
    pdf.save()
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"wniosek_{request_id}.pdf",
    )


@bp.route("/requests")
@login_required
def requests_view():
    conn = get_db()
    rows = _query_requests(conn)
    departments = conn.execute("SELECT name FROM departments ORDER BY name").fetchall()
    managers = conn.execute("SELECT id, full_name FROM users WHERE role = 'menedzer' ORDER BY full_name").fetchall()
    companies = conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    conn.close()
    return render_template("requests.html", requests_list=rows, departments=departments, managers=managers, companies=companies, statuses=STATUSES, leave_types=LEAVE_TYPES)


@bp.route("/requests/all")
@login_required
def all_requests_view():
    if not is_hr():
        flash("Brak uprawnień do tej sekcji.")
        return redirect(url_for("main.dashboard"))
    conn = get_db()
    rows, selected_from, selected_to = _query_all_requests(conn)
    departments = conn.execute("SELECT name FROM departments ORDER BY name").fetchall()
    managers = conn.execute("SELECT id, full_name FROM users WHERE role = 'menedzer' ORDER BY full_name").fetchall()
    companies = conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    conn.close()
    return render_template(
        "all_requests.html",
        requests_list=rows,
        departments=departments,
        managers=managers,
        companies=companies,
        statuses=STATUSES,
        leave_types=LEAVE_TYPES,
        selected_from=selected_from,
        selected_to=selected_to,
    )


@bp.route("/request/<int:request_id>/<action>", methods=["POST"])
@login_required
def change_request_status(request_id, action):
    next_url = request.form.get("next", "")
    conn = get_db()
    leave_request = conn.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone()
    if not leave_request:
        conn.close()
        flash("Nie znaleziono wniosku.")
        return _safe_redirect(next_url)

    owner = conn.execute("SELECT * FROM users WHERE id=?", (leave_request["user_id"],)).fetchone()

    if action == "delete":
        if not is_hr():
            conn.close()
            flash("Tylko admin lub kadry mogą usuwać wpisy.")
            return _safe_redirect(next_url)
        details = f"{owner['full_name'] if owner else 'nieznany'} | {leave_request['leave_type']} | {leave_request['date_from']} - {leave_request['date_to']} | status: {leave_request['status']}"
        conn.execute("DELETE FROM leave_requests WHERE id=?", (request_id,))
        log_action(conn, "usunięto wniosek", "leave_request", request_id, details)
        conn.commit()
        conn.close()
        flash("Wniosek został usunięty.")
        return _safe_redirect(next_url)

    if action == "cancel":
        can_cancel = is_hr() or leave_request["user_id"] == session.get("user_id")
        if not can_cancel:
            conn.close()
            flash("Nie można anulować tego wniosku.")
            return _safe_redirect(next_url)
        new_status = "anulowany"
    elif action == "reject":
        if not is_hr():
            conn.close()
            flash("Brak uprawnień do tej operacji.")
            return _safe_redirect(next_url)
        new_status = "odrzucony"
    elif action == "accept":
        if not is_hr():
            conn.close()
            flash("Brak uprawnień do tej operacji.")
            return _safe_redirect(next_url)
        new_status = "zaakceptowany"
    else:
        conn.close()
        flash("Nieznana akcja.")
        return _safe_redirect(next_url)

    conn.execute(
        "UPDATE leave_requests SET status=?, decision_comment='', decided_by=?, decided_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (new_status, session["user_id"] if is_hr() else None, request_id),
    )
    log_action(conn, f"zmieniono status na {new_status}", "leave_request", request_id, "")
    conn.commit()
    conn.close()
    flash(f"Status wniosku zmieniony na: {new_status}.")
    return _safe_redirect(next_url)


@bp.route("/reports/export.csv")
@login_required
def export_report_csv():
    import csv
    import io
    from flask import Response

    conn = get_db()
    rows = _query_requests(conn)
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Pracownik", "Spółka", "Dział", "Typ", "Od", "Do", "Dni", "Status", "Menedżer", "Data złożenia"])
    for row in rows:
        writer.writerow([row["full_name"], row["company_name"] or "", row["department"], row["leave_type"], row["date_from"], row["date_to"], row["days_count"], row["status"], row["manager_name"] or "", row["created_at"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=emerlog_urlopy.csv"})
