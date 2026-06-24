import os

DATABASE = os.environ.get("DATABASE", "database.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-before-production")
FORCE_HTTPS = os.environ.get("FORCE_HTTPS", "0") == "1"
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

LEAVE_TYPES = [
    "Urlop wypoczynkowy",
    "Urlop na żądanie",
    "Urlop okolicznościowy",
    "L4 / chorobowe",
    "Urlop bezpłatny",
    "Odbiór dnia wolnego",
    "Praca zdalna",
    "Delegacja",
    "Inne",
]
LIMIT_TYPES = {"Urlop wypoczynkowy", "Urlop na żądanie"}
HR_ROLES = {"admin", "kadry"}
MANAGER_ROLE = "menedzer"
STATUSES = ["oczekuje", "zaakceptowany", "odrzucony", "anulowany", "cofniety"]
