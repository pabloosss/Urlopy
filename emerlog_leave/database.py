import sqlite3
from werkzeug.security import generate_password_hash

from .config import DATABASE


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(cur, table):
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}


def _ensure_column(cur, table, name, definition):
    if name not in _columns(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT,
            role TEXT NOT NULL,
            vacation_days INTEGER DEFAULT 26,
            active INTEGER DEFAULT 1
        )
    """)
    for name, definition in {
        "department": "TEXT DEFAULT 'Spedycja'",
        "job_title": "TEXT DEFAULT ''",
        "manager_id": "INTEGER",
        "contract_type": "TEXT DEFAULT 'Umowa o pracę'",
        "carryover_days": "INTEGER DEFAULT 0",
        "company_id": "INTEGER",
    }.items():
        _ensure_column(cur, "users", name, definition)

    cur.execute("CREATE TABLE IF NOT EXISTS companies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL)")
    for company in ["EMERLOG SP. Z O. O.", "RMSPED SP. Z O. O.", "RM LOGISTIC SP. Z O. O."]:
        cur.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (company,))
    default_company = cur.execute("SELECT id FROM companies WHERE name = ?", ("EMERLOG SP. Z O. O.",)).fetchone()
    default_company_id = default_company["id"] if default_company else None

    cur.execute("CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            leave_type TEXT NOT NULL,
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            days_count INTEGER NOT NULL,
            comment TEXT,
            status TEXT DEFAULT 'zaakceptowany',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    for name, definition in {
        "decision_comment": "TEXT",
        "decided_by": "INTEGER",
        "decided_at": "TEXT",
        "replacement_user_id": "INTEGER",
        "attachment_note": "TEXT",
        "updated_at": "TEXT",
    }.items():
        _ensure_column(cur, "leave_requests", name, definition)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS request_notifications (
            request_id INTEGER NOT NULL,
            recipient_user_id INTEGER NOT NULL,
            seen_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (request_id, recipient_user_id)
        )
    """)
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS notify_admins_after_leave_request_insert
        AFTER INSERT ON leave_requests
        BEGIN
            INSERT OR IGNORE INTO request_notifications (request_id, recipient_user_id)
            SELECT NEW.id, id
            FROM users
            WHERE role = 'admin' AND active = 1;
        END
    """)
    cur.execute("""
        CREATE TRIGGER IF NOT EXISTS cleanup_notifications_after_leave_request_delete
        AFTER DELETE ON leave_requests
        BEGIN
            DELETE FROM request_notifications WHERE request_id = OLD.id;
        END
    """)

    cur.execute("""
        UPDATE leave_requests
        SET status = 'zaakceptowany',
            decided_by = NULL,
            decided_at = COALESCE(decided_at, created_at),
            updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)
        WHERE status = 'oczekuje'
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS limit_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            changed_by INTEGER,
            old_vacation_days INTEGER,
            new_vacation_days INTEGER,
            old_carryover_days INTEGER,
            new_carryover_days INTEGER,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Globalne ustawienia administracyjne.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('default_vacation_days', '26')")
    cur.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('carryover_enabled', '1')")

    # Migawka limitu dla każdego roku. Wpis dla pierwszego roku tworzy się
    # przy starcie aplikacji, dzięki czemu wdrożenie nie nalicza nic wstecz.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vacation_year_balances (
            user_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            base_days INTEGER NOT NULL DEFAULT 0,
            opening_carryover INTEGER NOT NULL DEFAULT 0,
            used_days INTEGER,
            carried_to_next INTEGER,
            processed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, year)
        )
    """)

    for dep in ["Spedycja", "Księgowość", "Kadry", "Administracja", "IT", "Zarząd"]:
        cur.execute("INSERT OR IGNORE INTO departments (name) VALUES (?)", (dep,))

    demo_users = [
        ("jan", "jan123", "Jan Kowalski", "jan.kowalski@emerlog.pl", "pracownik", "Spedycja", "Spedytor", "anna", 26, 0),
        ("anna", "anna123", "Anna Nowak", "anna.nowak@emerlog.pl", "menedzer", "Spedycja", "Kierownik spedycji", "ewa", 26, 2),
        ("pawel", "pawel123", "Paweł Pisarczyk", "pawel.pisarczyk@emerlog.pl", "menedzer", "IT", "Menedżer IT", "ewa", 26, 0),
        ("ewa", "ewa123", "Ewa Dusińska", "ewa.dusinska@emerlog.pl", "admin", "Kadry", "Kadry / Admin", None, 26, 0),
        ("kadry", "kadry123", "Kadry EMERLOG", "kadry@emerlog.pl", "kadry", "Kadry", "Kadry", "ewa", 26, 0),
        ("admin", "admin123", "Administrator", "admin@emerlog.pl", "admin", "IT", "Administrator", None, 26, 0),
    ]

    for login, password, full_name, email, role, dep, job, manager_login, vacation, carry in demo_users:
        if not cur.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone():
            cur.execute("""
                INSERT INTO users (
                    login, password_hash, full_name, email, role, vacation_days,
                    active, department, job_title, contract_type, carryover_days, company_id
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'Umowa o pracę', ?, ?)
            """, (login, generate_password_hash(password), full_name, email, role, vacation, dep, job, carry, default_company_id))

    if default_company_id:
        cur.execute("UPDATE users SET company_id = ? WHERE company_id IS NULL", (default_company_id,))

    conn.commit()
    for login, _, _, _, _, _, _, manager_login, _, _ in demo_users:
        if manager_login:
            manager = cur.execute("SELECT id FROM users WHERE login = ?", (manager_login,)).fetchone()
            employee = cur.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone()
            if manager and employee:
                cur.execute("UPDATE users SET manager_id = ? WHERE id = ?", (manager["id"], employee["id"]))

    conn.commit()
    conn.close()
