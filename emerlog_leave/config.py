import os

DATABASE = os.environ.get("DATABASE", "database.db")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-before-production")
FORCE_HTTPS = os.environ.get("FORCE_HTTPS", "0") == "1"
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

CONTRACT_UOP = "Umowa o pracę"
CONTRACT_ZLECENIE = "Umowa zlecenie"
CONTRACT_TYPES = [CONTRACT_UOP, CONTRACT_ZLECENIE]

UOP_LEAVE_TYPES = [
    "Urlop wypoczynkowy",
    "Urlop na żądanie",
    "Urlop okolicznościowy",
    "L4 / chorobowe",
    "Urlop bezpłatny",
    "Odbiór dnia wolnego",
    "Inne",
]

ZLECENIE_LEAVE_TYPES = [
    "Urlop wypoczynkowy",
    "Urlop bezpłatny",
]

SPEDYCJA_LEAVE_TYPES = [
    "Urlop wypoczynkowy",
    "Inne",
]

LEAVE_TYPES = UOP_LEAVE_TYPES
CONTRACT_LEAVE_TYPES = {
    CONTRACT_UOP: UOP_LEAVE_TYPES,
    CONTRACT_ZLECENIE: ZLECENIE_LEAVE_TYPES,
}
LIMIT_TYPES = {"Urlop wypoczynkowy", "Urlop na żądanie"}
HR_ROLES = {"admin", "kadry"}
MANAGER_ROLE = "menedzer"
STATUSES = ["oczekuje", "zaakceptowany", "odrzucony", "anulowany", "cofniety"]


def normalize_contract_type(value):
    text = (value or "").strip().lower()
    if "zlecen" in text:
        return CONTRACT_ZLECENIE
    return CONTRACT_UOP


def normalize_department(value):
    return (value or "").strip().lower()


def leave_types_for_contract(value):
    return CONTRACT_LEAVE_TYPES.get(normalize_contract_type(value), UOP_LEAVE_TYPES)


def leave_types_for_user(contract_type, department):
    if normalize_department(department) == "spedycja":
        return SPEDYCJA_LEAVE_TYPES
    return leave_types_for_contract(contract_type)
