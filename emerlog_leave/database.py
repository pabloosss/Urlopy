import sqlite3

from .config import DATABASE


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _columns(cur, table):
    return {row[1] for row in cur.execute(f"PRAGMA table_info({table})")}


def _ensure_column(cur, table, name, definition):
    if name not in _columns(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # WAL ogranicza błędy "database is locked" przy równoległej pracy Gunicorna,
    # a NORMAL jest bezpiecznym kompromisem dla bazy działającej w WAL.
    cur.execute("PRAGMA journal_mode = WAL")
    cur.execute("PRAGMA synchronous = NORMAL")

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
        "employment_start": "TEXT",
        "employment_end": "TEXT",
        "fte_percent": "INTEGER NOT NULL DEFAULT 100",
        "hr_note": "TEXT DEFAULT ''",
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS balance_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            changed_by INTEGER,
            old_available_days INTEGER NOT NULL,
            new_available_days INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for key, value in [
        ("default_vacation_days", "26"),
        ("default_vacation_days_uop", "26"),
        ("default_vacation_days_zlecenie", "20"),
        ("enforce_uop_vacation_limit", "1"),
        ("carryover_enabled", "1"),
        ("max_carryover_days", "0"),
        ("require_spedycja_replacement", "1"),
        ("allow_past_requests", "1"),
        ("allow_employee_cancel", "1"),
        ("min_notice_days", "0"),
        ("max_request_days", "0"),
        ("hr_closed_through", ""),
        ("contract_alert_days", "45"),
        ("carryover_alert_threshold", "1"),
        ("auto_deactivate_after_end_date", "0"),
        ("include_inactive_in_hr_exports", "0"),
        ("backup_auto_enabled", "0"),
        ("backup_auto_frequency", "daily"),
        ("backup_auto_time", "02:00"),
        ("backup_auto_weekday", "0"),
        ("backup_auto_keep", "14"),
        ("backup_preimport_keep", "10"),
        ("backup_safety_keep", "10"),
    ]:
        cur.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, value))

    cur.execute("""
        CREATE TABLE IF NOT EXISTS vacation_year_balances (
            user_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            base_days INTEGER NOT NULL DEFAULT 0,
            opening_carryover INTEGER NOT NULL DEFAULT 0,
            opening_used_days INTEGER NOT NULL DEFAULT 0,
            availability_adjustment INTEGER NOT NULL DEFAULT 0,
            source_used_days INTEGER,
            request_used_baseline INTEGER NOT NULL DEFAULT 0,
            used_days INTEGER,
            carried_to_next INTEGER,
            processed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, year)
        )
    """)
    for name, definition in {
        "opening_used_days": "INTEGER NOT NULL DEFAULT 0",
        "availability_adjustment": "INTEGER NOT NULL DEFAULT 0",
        "source_used_days": "INTEGER",
        "request_used_baseline": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _ensure_column(cur, "vacation_year_balances", name, definition)

    # Indeksy pod najczęstsze widoki: kalendarz, obecność, limity, historia i powiadomienia.
    for statement in [
        "CREATE INDEX IF NOT EXISTS idx_users_active ON users(active)",
        "CREATE INDEX IF NOT EXISTS idx_users_manager ON users(manager_id)",
        "CREATE INDEX IF NOT EXISTS idx_users_department ON users(department)",
        "CREATE INDEX IF NOT EXISTS idx_leave_user_status_dates ON leave_requests(user_id, status, date_from, date_to)",
        "CREATE INDEX IF NOT EXISTS idx_leave_status_dates ON leave_requests(status, date_from, date_to)",
        "CREATE INDEX IF NOT EXISTS idx_notifications_recipient_seen ON request_notifications(recipient_user_id, seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_logs(entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_limit_adjustments_user ON limit_adjustments(user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_balance_adjustments_user ON balance_adjustments(user_id, created_at)",
    ]:
        cur.execute(statement)

    for dep in ["Spedycja", "Księgowość", "Kadry", "Administracja", "IT", "Zarząd"]:
        cur.execute("INSERT OR IGNORE INTO departments (name) VALUES (?)", (dep,))

    # Nie tworzymy żadnych kont demonstracyjnych ani haseł domyślnych.
    # Konta produkcyjne powstają wyłącznie z panelu Kadr/Admina albo przez importer.
    if default_company_id:
        cur.execute("UPDATE users SET company_id = ? WHERE company_id IS NULL", (default_company_id,))

    conn.commit()
    conn.close()
