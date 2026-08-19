from datetime import datetime
from pathlib import Path
import os
import sqlite3

from flask import Blueprint, current_app, flash, redirect, render_template, send_file, session, url_for

from .config import BACKUP_DIR, DATABASE
from .database import get_db
from .services import log_action, login_required, role_required

bp = Blueprint("backups", __name__)


def _backup_directory():
    directory = Path(BACKUP_DIR).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _verify_database(path):
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("Kontrola integralności bazy nie powiodła się.")
    finally:
        conn.close()


def _create_backup(kind="manual"):
    directory = _backup_directory()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    filename = f"urlopy_{kind}_{timestamp}.db"
    path = directory / filename

    source = sqlite3.connect(DATABASE, timeout=30)
    target = sqlite3.connect(str(path), timeout=30)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()

    _verify_database(path)
    return path


def _resolve_backup(filename):
    if not filename or Path(filename).name != filename:
        return None
    if not filename.startswith("urlopy_") or not filename.endswith(".db"):
        return None

    directory = _backup_directory()
    path = (directory / filename).resolve()
    if path.parent != directory or not path.is_file():
        return None
    return path


def _list_backups():
    directory = _backup_directory()
    items = []
    for path in directory.glob("urlopy_*.db"):
        try:
            stat = path.stat()
        except OSError:
            continue

        if "_safety_" in path.name:
            kind = "Backup bezpieczeństwa"
        elif "_manual_" in path.name:
            kind = "Ręczny"
        else:
            kind = "Backup"

        items.append({
            "filename": path.name,
            "kind": kind,
            "created_at": datetime.fromtimestamp(stat.st_mtime),
            "size_kb": max(1, round(stat.st_size / 1024)),
        })

    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items


@bp.route("/admin/backups")
@login_required
@role_required("admin")
def backups_view():
    return render_template(
        "admin_backups.html",
        backups=_list_backups(),
        backup_dir=str(_backup_directory()),
    )


@bp.route("/admin/backups/create", methods=["POST"])
@login_required
@role_required("admin")
def create_backup_view():
    try:
        path = _create_backup("manual")
        conn = get_db()
        log_action(conn, "utworzono backup bazy", "backup", None, path.name)
        conn.commit()
        conn.close()
        flash("Backup został zapisany.")
    except Exception as error:
        flash(f"Nie udało się utworzyć backupu: {error}")
    return redirect(url_for("backups.backups_view"))


@bp.route("/admin/backups/download/<filename>")
@login_required
@role_required("admin")
def download_backup(filename):
    path = _resolve_backup(filename)
    if not path:
        flash("Nie znaleziono backupu.")
        return redirect(url_for("backups.backups_view"))
    return send_file(str(path), as_attachment=True, download_name=path.name, mimetype="application/x-sqlite3")


@bp.route("/admin/backups/restore/<filename>", methods=["POST"])
@login_required
@role_required("admin")
def restore_backup(filename):
    path = _resolve_backup(filename)
    if not path:
        flash("Nie znaleziono backupu.")
        return redirect(url_for("backups.backups_view"))

    try:
        _verify_database(path)

        # Zanim nadpiszemy bazę, zawsze zachowujemy aktualny stan.
        safety_path = _create_backup("safety")

        source = sqlite3.connect(str(path), timeout=30)
        target = sqlite3.connect(DATABASE, timeout=30)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()

        _verify_database(Path(DATABASE))
        current_app.config["VACATION_YEAR_CHECKED"] = None

        conn = get_db()
        restored_admin = conn.execute(
            "SELECT id, role, active FROM users WHERE id = ?",
            (session.get("user_id"),),
        ).fetchone()
        if restored_admin and restored_admin["role"] == "admin" and restored_admin["active"]:
            log_action(
                conn,
                "przywrócono backup bazy",
                "backup",
                None,
                f"{path.name}; backup bezpieczeństwa: {safety_path.name}",
            )
            conn.commit()
            conn.close()
            flash("Backup został przywrócony. Przed operacją zapisano też backup bezpieczeństwa.")
            return redirect(url_for("backups.backups_view"))

        conn.close()
        session.clear()
        flash("Backup został przywrócony. Twoje konto nie ma w nim aktywnych uprawnień administratora — zaloguj się ponownie.")
        return redirect(url_for("main.login"))
    except Exception as error:
        flash(f"Nie udało się przywrócić backupu: {error}")
        return redirect(url_for("backups.backups_view"))
