import os

DATABASE = os.environ.get("DATABASE", "database.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-before-production")

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
