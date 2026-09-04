"""Kompatybilność po scaleniu generatora godzin z głównym modułem rozliczeń.

Właściwe trasy są rejestrowane przez routes_timesheets.register_timesheet_routes.
Funkcja zostaje, żeby starszy import w routes_hr.py nie powodował błędu.
"""


def register_employee_timesheet_routes(bp):
    return None
