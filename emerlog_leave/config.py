import os

DATABASE = os.environ.get("DATABASE", "database.db")
BACKUP_DIR = os.environ.get(
    "BACKUP_DIR",
    os.path.join(os.path.dirname(os.path.abspath(DATABASE)), "backups"),
)

DEFAULT_SECRET_KEY = "dev-secret-change-before-production"
SECRET_KEY = os.environ.get("SECRET_KEY", DEFAULT_SECRET_KEY)
USING_DEFAULT_SECRET_KEY = SECRET_KEY == DEFAULT_SECRET_KEY

FORCE_HTTPS = os.environ.get("FORCE_HTTPS", "0") == "1"
_secure_cookie_env = os.environ.get("SESSION_COOKIE_SECURE")
SESSION_COOKIE_SECURE = (
    _secure_cookie_env == "1" if _secure_cookie_env is not None else FORCE_HTTPS
)
try:
    SESSION_HOURS = max(1, min(72, int(os.environ.get("SESSION_HOURS", "12"))))
except ValueError:
    SESSION_HOURS = 12

CONTRACT_UOP = "Umowa o pracę"
CONTRACT_ZLECENIE = "Umowa zlecenie"
CONTRACT_OTHER = "Inne"
CONTRACT_TYPES = [CONTRACT_UOP, CONTRACT_ZLECENIE, CONTRACT_OTHER]

# UoP ma pełny zestaw rodzajów nieobecności używany przez Kadry.
UOP_LEAVE_TYPES = [
    "Urlop wypoczynkowy",
    "Urlop na żądanie",
    "Urlop okolicznościowy",
    "L4 / chorobowe",
    "Urlop bezpłatny",
    "Odbiór dnia wolnego",
    "Inne",
]

# Zlecenie ma prosty wariant: planowana nieobecność albo urlop bezpłatny.
ZLECENIE_LEAVE_TYPES = [
    "Urlop wypoczynkowy",
    "Urlop bezpłatny",
]

# Dla typu „Inne” zostawiamy neutralnie zestaw UoP, dopóki Kadry nie ustalą osobnej listy.
OTHER_LEAVE_TYPES = list(UOP_LEAVE_TYPES)

LEAVE_TYPES = UOP_LEAVE_TYPES
CONTRACT_LEAVE_TYPES = {
    CONTRACT_UOP: UOP_LEAVE_TYPES,
    CONTRACT_ZLECENIE: ZLECENIE_LEAVE_TYPES,
    CONTRACT_OTHER: OTHER_LEAVE_TYPES,
}
LIMIT_TYPES = {"Urlop wypoczynkowy", "Urlop na żądanie"}
HR_ROLES = {"admin", "kadry"}
MANAGER_ROLE = "menedzer"
STATUSES = ["oczekuje", "zaakceptowany", "odrzucony", "anulowany", "cofniety"]


def normalize_contract_type(value):
    text = (value or "").strip().lower()
    if "zlecen" in text:
        return CONTRACT_ZLECENIE
    if text == CONTRACT_OTHER.lower() or text.startswith("inne"):
        return CONTRACT_OTHER
    return CONTRACT_UOP


def normalize_department(value):
    return (value or "").strip().lower()


def leave_types_for_contract(value):
    return CONTRACT_LEAVE_TYPES.get(normalize_contract_type(value), UOP_LEAVE_TYPES)


def leave_types_for_user(contract_type, department):
    # Rodzaj umowy określa listę typów urlopu. Dział wpływa na inne reguły
    # (np. obowiązkowe zastępstwo w Spedycji), ale nie zmienia listy typów.
    return leave_types_for_contract(contract_type)


def uses_vacation_balance(contract_type):
    """Saldo jest pokazywane i egzekwowane wyłącznie dla UoP."""
    return normalize_contract_type(contract_type) == CONTRACT_UOP
