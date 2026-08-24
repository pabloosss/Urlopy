from datetime import datetime, timedelta
from io import BytesIO
import os
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo
import zipfile

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, session, url_for

from .config import BACKUP_DIR, DATABASE
from .database import get_db
from .services import get_app_setting, log_action, login_required, role_required, set_app_setting

bp = Blueprint("backups", __name__)
WARSAW_TZ = ZoneInfo("Europe/Warsaw")
FREQUENCIES = {"daily", "weekdays", "weekly"}
WEEKDAYS = [
    (0, "Poniedziałek"),
    (1, "Wtorek"),
    (2, "Środa"),
    (3, "Czwartek"),
    (4, "Piątek"),
    (5, "Sobota"),
    (6, "Niedziela"),
]


def _now():
    return datetime.now(WARSAW_TZ)


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


def _copy_database(source_path, target_path):
    source = sqlite3.connect(str(source_path), timeout=30)
    target = sqlite3.connect(str(target_path), timeout=30)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()


def _kind_key(filename):
    if "_safety_" in filename:
        return "safety"
    if "_manual_" in filename:
        return "manual"
    if "_automatic_" in filename:
        return "automatic"
    if "_preimport_" in filename:
        return "preimport"
    return "other"


def _kind_label(kind):
    return {
        "safety": "Bezpieczeństwa",
        "manual": "Ręczny",
        "automatic": "Automatyczny",
        "preimport": "Przed importem",
        "other": "Systemowy",
    }.get(kind, "Backup")


def _setting_int(conn, key, default, minimum, maximum):
    try:
        value = int(get_app_setting(conn, key, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _backup_settings(conn=None):
    own_conn = conn is None
    conn = conn or get_db()
    try:
        frequency = (get_app_setting(conn, "backup_auto_frequency", "daily") or "daily").strip()
        if frequency not in FREQUENCIES:
            frequency = "daily"
        time_value = (get_app_setting(conn, "backup_auto_time", "02:00") or "02:00").strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_value):
            time_value = "02:00"
        return {
            "auto_enabled": get_app_setting(conn, "backup_auto_enabled", "0") == "1",
            "frequency": frequency,
            "time": time_value,
            "weekday": _setting_int(conn, "backup_auto_weekday", 0, 0, 6),
            "auto_keep": _setting_int(conn, "backup_auto_keep", 14, 1, 365),
            "preimport_keep": _setting_int(conn, "backup_preimport_keep", 10, 1, 100),
            "safety_keep": _setting_int(conn, "backup_safety_keep", 10, 1, 100),
        }
    finally:
        if own_conn:
            conn.close()


def _list_backups():
    directory = _backup_directory()
    items = []
    for path in directory.glob("urlopy_*.db"):
        try:
            stat = path.stat()
        except OSError:
            continue

        kind = _kind_key(path.name)
        items.append({
            "filename": path.name,
            "kind": _kind_label(kind),
            "kind_key": kind,
            "created_at": datetime.fromtimestamp(stat.st_mtime, WARSAW_TZ),
            "size_bytes": stat.st_size,
            "size_kb": max(1, round(stat.st_size / 1024)),
        })

    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items


def _prune_kind(kind, keep):
    matching = [item for item in _list_backups() if item["kind_key"] == kind]
    removed = 0
    for item in matching[keep:]:
        path = _resolve_backup(item["filename"])
        if path:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _apply_retention(kind):
    key_map = {
        "automatic": "auto_keep",
        "preimport": "preimport_keep",
        "safety": "safety_keep",
    }
    setting_key = key_map.get(kind)
    if not setting_key:
        return 0
    try:
        settings = _backup_settings()
        return _prune_kind(kind, settings[setting_key])
    except Exception:
        return 0


def _create_backup(kind="manual"):
    directory = _backup_directory()
    timestamp = _now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    filename = f"urlopy_{kind}_{timestamp}.db"
    path = directory / filename

    _copy_database(DATABASE, path)
    _verify_database(path)
    _apply_retention(kind)
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


def _scheduled_slot(now, settings):
    hour, minute = [int(part) for part in settings["time"].split(":", 1)]
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    frequency = settings["frequency"]

    if frequency == "daily":
        if candidate > now:
            candidate -= timedelta(days=1)
        return candidate

    if frequency == "weekdays":
        if candidate > now:
            candidate -= timedelta(days=1)
        while candidate.weekday() > 4:
            candidate -= timedelta(days=1)
        return candidate

    weekday = settings["weekday"]
    days_back = (candidate.weekday() - weekday) % 7
    candidate -= timedelta(days=days_back)
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def _next_scheduled(now, settings):
    hour, minute = [int(part) for part in settings["time"].split(":", 1)]
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    frequency = settings["frequency"]

    if frequency == "daily":
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if frequency == "weekdays":
        if candidate <= now:
            candidate += timedelta(days=1)
        while candidate.weekday() > 4:
            candidate += timedelta(days=1)
        return candidate

    weekday = settings["weekday"]
    days_forward = (weekday - candidate.weekday()) % 7
    candidate += timedelta(days=days_forward)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _automatic_state(settings, backups=None):
    backups = backups if backups is not None else _list_backups()
    automatic = [item for item in backups if item["kind_key"] == "automatic"]
    last = automatic[0]["created_at"] if automatic else None
    now = _now()
    return {
        "last": last,
        "next": _next_scheduled(now, settings) if settings["auto_enabled"] else None,
        "count": len(automatic),
    }


def _acquire_auto_lock():
    lock_path = _backup_directory() / ".automatic-backup.lock"
    try:
        if lock_path.exists():
            age = _now().timestamp() - lock_path.stat().st_mtime
            if age > 900:
                lock_path.unlink(missing_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(_now().timestamp()).encode("ascii"))
        return fd, lock_path
    except FileExistsError:
        return None, lock_path


def maybe_run_automatic_backup():
    """Tworzy zaległy backup przy pierwszym ruchu w aplikacji po zaplanowanej godzinie."""
    try:
        settings = _backup_settings()
        if not settings["auto_enabled"]:
            return None

        now = _now()
        slot = _scheduled_slot(now, settings)
        automatic = [item for item in _list_backups() if item["kind_key"] == "automatic"]
        if automatic and automatic[0]["created_at"] >= slot:
            return None

        fd, lock_path = _acquire_auto_lock()
        if fd is None:
            return None
        try:
            # Po zdobyciu blokady sprawdzamy jeszcze raz, bo drugi worker mógł zdążyć zrobić kopię.
            automatic = [item for item in _list_backups() if item["kind_key"] == "automatic"]
            if automatic and automatic[0]["created_at"] >= slot:
                return None
            path = _create_backup("automatic")
            try:
                conn = get_db()
                conn.execute(
                    """
                    INSERT INTO audit_logs (actor_user_id, action, entity_type, entity_id, details)
                    VALUES (NULL, 'utworzono automatyczny backup', 'backup', NULL, ?)
                    """,
                    (path.name,),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
            return path
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            lock_path.unlink(missing_ok=True)
    except Exception:
        current_app.logger.exception("Automatyczny backup nie powiódł się")
        return None


def _summary(backups):
    total_bytes = sum(item["size_bytes"] for item in backups)
    counts = {key: 0 for key in ["automatic", "manual", "preimport", "safety", "other"]}
    for item in backups:
        counts[item["kind_key"]] = counts.get(item["kind_key"], 0) + 1
    return {
        "total": len(backups),
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "counts": counts,
    }


@bp.route("/admin/backups")
@login_required
@role_required("admin")
def backups_view():
    backups = _list_backups()
    settings = _backup_settings()
    return render_template(
        "admin_backups.html",
        backups=backups,
        settings=settings,
        auto_state=_automatic_state(settings, backups),
        summary=_summary(backups),
        weekdays=WEEKDAYS,
    )


@bp.route("/admin/backups/settings", methods=["POST"])
@login_required
@role_required("admin")
def save_backup_settings():
    frequency = request.form.get("backup_auto_frequency", "daily").strip()
    time_value = request.form.get("backup_auto_time", "02:00").strip()
    try:
        weekday = int(request.form.get("backup_auto_weekday", "0"))
        auto_keep = int(request.form.get("backup_auto_keep", "14"))
        preimport_keep = int(request.form.get("backup_preimport_keep", "10"))
        safety_keep = int(request.form.get("backup_safety_keep", "10"))
    except (TypeError, ValueError):
        flash("Sprawdź wartości w ustawieniach backupu.")
        return redirect(url_for("backups.backups_view"))

    errors = []
    if frequency not in FREQUENCIES:
        errors.append("Niepoprawna częstotliwość")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_value):
        errors.append("Niepoprawna godzina")
    if weekday < 0 or weekday > 6:
        errors.append("Niepoprawny dzień tygodnia")
    if not 1 <= auto_keep <= 365:
        errors.append("Liczba automatycznych kopii musi być od 1 do 365")
    if not 1 <= preimport_keep <= 100:
        errors.append("Liczba kopii przed importem musi być od 1 do 100")
    if not 1 <= safety_keep <= 100:
        errors.append("Liczba kopii bezpieczeństwa musi być od 1 do 100")

    if errors:
        flash("; ".join(errors) + ".")
        return redirect(url_for("backups.backups_view"))

    conn = get_db()
    values = {
        "backup_auto_enabled": "1" if request.form.get("backup_auto_enabled") == "1" else "0",
        "backup_auto_frequency": frequency,
        "backup_auto_time": time_value,
        "backup_auto_weekday": str(weekday),
        "backup_auto_keep": str(auto_keep),
        "backup_preimport_keep": str(preimport_keep),
        "backup_safety_keep": str(safety_keep),
    }
    for key, value in values.items():
        set_app_setting(conn, key, value)
    log_action(
        conn,
        "zmieniono ustawienia backupu",
        "backup_settings",
        None,
        f"auto={values['backup_auto_enabled']}; tryb={frequency}; godzina={time_value}; auto={auto_keep}; import={preimport_keep}; safety={safety_keep}",
    )
    conn.commit()
    conn.close()

    _prune_kind("automatic", auto_keep)
    _prune_kind("preimport", preimport_keep)
    _prune_kind("safety", safety_keep)
    flash("Ustawienia backupu zostały zapisane.")
    return redirect(url_for("backups.backups_view"))


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
        flash("Ręczny backup został utworzony i sprawdzony.")
    except Exception as error:
        flash(f"Nie udało się utworzyć backupu: {error}")
    return redirect(url_for("backups.backups_view"))


@bp.route("/admin/backups/cleanup-auto", methods=["POST"])
@login_required
@role_required("admin")
def cleanup_automatic_backups():
    settings = _backup_settings()
    removed = _prune_kind("automatic", settings["auto_keep"])
    flash(f"Usunięto {removed} starych automatycznych kopii." if removed else "Nie ma starych automatycznych kopii do usunięcia.")
    return redirect(url_for("backups.backups_view"))


@bp.route("/admin/backups/download-all")
@login_required
@role_required("admin")
def download_all_backups():
    backups = _list_backups()
    if not backups:
        flash("Nie ma backupów do pobrania.")
        return redirect(url_for("backups.backups_view"))

    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in backups:
            path = _resolve_backup(item["filename"])
            if path:
                archive.write(path, arcname=path.name)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"urlopy_backupy_{_now().strftime('%Y-%m-%d')}.zip",
        mimetype="application/zip",
    )


@bp.route("/admin/backups/download/<filename>")
@login_required
@role_required("admin")
def download_backup(filename):
    path = _resolve_backup(filename)
    if not path:
        flash("Nie znaleziono backupu.")
        return redirect(url_for("backups.backups_view"))
    return send_file(str(path), as_attachment=True, download_name=path.name, mimetype="application/x-sqlite3")


@bp.route("/admin/backups/delete/<filename>", methods=["POST"])
@login_required
@role_required("admin")
def delete_backup(filename):
    path = _resolve_backup(filename)
    if not path:
        flash("Nie znaleziono backupu.")
        return redirect(url_for("backups.backups_view"))
    try:
        path.unlink()
        conn = get_db()
        log_action(conn, "usunięto backup bazy", "backup", None, filename)
        conn.commit()
        conn.close()
        flash("Backup został usunięty.")
    except Exception as error:
        flash(f"Nie udało się usunąć backupu: {error}")
    return redirect(url_for("backups.backups_view"))


@bp.route("/admin/backups/restore/<filename>", methods=["POST"])
@login_required
@role_required("admin")
def restore_backup(filename):
    path = _resolve_backup(filename)
    if not path:
        flash("Nie znaleziono backupu.")
        return redirect(url_for("backups.backups_view"))

    safety_path = None
    restore_started = False
    try:
        _verify_database(path)

        # Zanim nadpiszemy bazę, zawsze zachowujemy aktualny stan.
        safety_path = _create_backup("safety")

        restore_started = True
        _copy_database(path, DATABASE)
        _verify_database(Path(DATABASE).resolve())
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
        rollback_note = ""
        if safety_path and restore_started:
            try:
                _copy_database(safety_path, DATABASE)
                _verify_database(Path(DATABASE).resolve())
                current_app.config["VACATION_YEAR_CHECKED"] = None
                rollback_note = " Aktualny stan został automatycznie odtworzony z backupu bezpieczeństwa."
            except Exception:
                rollback_note = " Backup bezpieczeństwa został zapisany, ale automatyczne odtworzenie nie powiodło się."
        flash(f"Nie udało się przywrócić backupu: {error}.{rollback_note}")
        return redirect(url_for("backups.backups_view"))
