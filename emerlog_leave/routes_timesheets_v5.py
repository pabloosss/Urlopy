import re

from flask import render_template as _flask_render_template

from .config import CONTRACT_ZLECENIE, normalize_contract_type
from .routes_timesheets_v2 import _month_absences as _base_month_absences
from . import routes_timesheets_v4 as _v4


def _month_absences_for_contract(conn, user_id, start, end):
    """Filtruje nieobecności widoczne w rozliczeniu zgodnie z typem umowy."""
    absences = _base_month_absences(conn, user_id, start, end)
    user = conn.execute(
        "SELECT contract_type FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    contract = normalize_contract_type(user["contract_type"] if user else None)

    # Dla zlecenia w tabeli rozliczenia godzin uwzględniamy wyłącznie
    # zaakceptowany urlop bezpłatny. Urlop wypoczynkowy nie wpływa na tabelę.
    if contract == CONTRACT_ZLECENIE:
        return {
            iso: absence
            for iso, absence in absences.items()
            if absence.get("leave_type") == "Urlop bezpłatny"
        }

    # UoP (i pozostałe typy) zachowują dotychczasową logikę bez zmian.
    return absences


def _render_template_with_timesheet_defaults(template_name, **context):
    html = _flask_render_template(template_name, **context)
    if template_name != "timesheets_v3.html":
        return html

    # Pole łącznej liczby godzin na zleceniu nie powinno startować puste.
    # Ustawiamy 0 tylko wtedy, gdy po renderze wartość faktycznie jest pusta;
    # zapisane wartości użytkownika pozostają bez zmian.
    return re.sub(
        r'(<input[^>]*\bid="hoursTotal"[^>]*\bvalue=")\s*(")',
        r'\g<1>0\2',
        html,
        count=1,
    )


def register_timesheet_routes(bp):
    """Aktywna warstwa reguł biznesowych nad stabilnym modułem v4."""
    _v4._month_absences = _month_absences_for_contract
    _v4.render_template = _render_template_with_timesheet_defaults
    return _v4.register_timesheet_routes(bp)
