from datetime import date
from io import BytesIO
from pathlib import PurePosixPath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from flask import Blueprint, flash, render_template, request, session
from werkzeug.security import generate_password_hash

from .config import CONTRACT_UOP, CONTRACT_ZLECENIE
from .database import get_db
from .routes_backups import _create_backup
from .services import (
    log_action,
    login_required,
    role_required,
    surname_first,
    surname_first_to_storage,
    vacation_days_used_in_year,
)

bp = Blueprint("employee_import", __name__)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _clean(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\u00a0", " ").split()).strip()


def _to_int(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(round(value))
    match = re.search(r"-?\d+", _clean(value))
    return int(match.group(0)) if match else default


def _name_key(value):
    text = _clean(value).casefold().replace("ł", "l")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z0-9]+", text)
    return "|".join(sorted(tokens))


def _column_index(cell_ref):
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    result = 0
    for ch in letters:
        result = result * 26 + (ord(ch) - 64)
    return result - 1


def _xlsx_rows(file_storage):
    payload = file_storage.read()
    if not payload:
        raise ValueError("Plik jest pusty.")
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("Plik jest za duży. Maksymalny rozmiar to 8 MB.")

    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ValueError("To nie jest poprawny plik XLSX.") from error

    with archive:
        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall(f"{{{MAIN_NS}}}si"):
                shared_strings.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))

        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook_root.find(f".//{{{MAIN_NS}}}sheet")
        if sheet is None:
            raise ValueError("Nie znaleziono arkusza w pliku.")
        relation_id = sheet.attrib.get(f"{{{REL_NS}}}id")

        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = None
        for rel in rels_root.findall(f"{{{PKG_REL_NS}}}Relationship"):
            if rel.attrib.get("Id") == relation_id:
                target = rel.attrib.get("Target")
                break
        if not target:
            raise ValueError("Nie udało się odczytać pierwszego arkusza.")

        sheet_path = str(PurePosixPath("xl") / target).replace("xl/../", "")
        if sheet_path not in archive.namelist():
            sheet_path = "xl/worksheets/sheet1.xml"
        if sheet_path not in archive.namelist():
            raise ValueError("Nie znaleziono danych arkusza.")

        sheet_root = ET.fromstring(archive.read(sheet_path))
        rows = []
        for row_node in sheet_root.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
            values = {}
            max_col = -1
            for cell in row_node.findall(f"{{{MAIN_NS}}}c"):
                ref = cell.attrib.get("r", "A1")
                col = _column_index(ref)
                max_col = max(max_col, col)
                cell_type = cell.attrib.get("t")
                value_node = cell.find(f"{{{MAIN_NS}}}v")

                if cell_type == "inlineStr":
                    inline = cell.find(f"{{{MAIN_NS}}}is")
                    value = "" if inline is None else "".join(node.text or "" for node in inline.iter(f"{{{MAIN_NS}}}t"))
                elif value_node is None:
                    value = ""
                elif cell_type == "s":
                    index = int(value_node.text or 0)
                    value = shared_strings[index] if 0 <= index < len(shared_strings) else ""
                elif cell_type in {"str", "e"}:
                    value = value_node.text or ""
                else:
                    raw = value_node.text or ""
                    try:
                        number = float(raw)
                        value = int(number) if number.is_integer() else number
                    except ValueError:
                        value = raw
                values[col] = value

            width = max(9, max_col + 1)
            rows.append([values.get(index, "") for index in range(width)])
        return rows


def _parse_people(rows):
    people = []
    errors = []
    contract_type = CONTRACT_UOP

    for row_number, row in enumerate(rows, start=1):
        row = list(row) + [""] * max(0, 9 - len(row))
        first = _clean(row[0])
        if first.casefold().startswith("zlecen"):
            contract_type = CONTRACT_ZLECENIE
            continue

        login = _clean(row[2]).upper()
        password = _clean(row[3])
        if not login and not password:
            continue
        if not login or not password:
            errors.append(f"Wiersz {row_number}: brak loginu albo hasła.")
            continue

        if contract_type == CONTRACT_UOP:
            current_name = _clean(row[4]) or _clean(row[1]) or first
            base = _to_int(row[5])
            carryover = _to_int(row[6], 0)
            used = _to_int(row[7], 0)
            remaining_raw = row[8]
            aliases = [row[0], row[1], row[4]]
        else:
            current_name = _clean(row[1]) or first
            base = _to_int(row[4])
            carryover = _to_int(row[5], 0)
            used = _to_int(row[6], 0)
            remaining_raw = row[7]
            aliases = [row[0], row[1]]

        remaining = _to_int(remaining_raw)
        if not current_name or base is None or carryover is None or used is None:
            errors.append(f"Wiersz {row_number}: nie udało się odczytać danych urlopowych.")
            continue
        if remaining is None:
            remaining = base + carryover - used

        people.append({
            "row_number": row_number,
            "login": login,
            "password": password,
            "display_name": current_name,
            "storage_name": surname_first_to_storage(current_name),
            "aliases": [_clean(value) for value in aliases if _clean(value)],
            "contract_type": contract_type,
            "base": max(0, base),
            "carryover": max(0, carryover),
            "used": max(0, used),
            "remaining": remaining,
            "remaining_note": _clean(remaining_raw) if isinstance(remaining_raw, str) and not _clean(remaining_raw).isdigit() else "",
        })

    return people, errors


def _find_existing_user(conn, person):
    by_login = conn.execute(
        "SELECT * FROM users WHERE lower(login) = lower(?)",
        (person["login"],),
    ).fetchone()
    if by_login:
        return by_login, None

    candidate_keys = {_name_key(value) for value in person["aliases"] + [person["display_name"]] if _name_key(value)}
    matches = []
    for user in conn.execute("SELECT * FROM users").fetchall():
        if _name_key(user["full_name"]) in candidate_keys or _name_key(surname_first(user["full_name"])) in candidate_keys:
            matches.append(user)

    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "pasuje do więcej niż jednego istniejącego konta"
    return None, None


def _save_balance(conn, user_id, person, year):
    request_used = vacation_days_used_in_year(conn, user_id, year)
    opening_used = max(0, person["used"] - request_used)
    accepted_after_import = opening_used + request_used
    adjustment = person["remaining"] - (person["base"] + person["carryover"] - accepted_after_import)

    conn.execute(
        """
        INSERT INTO vacation_year_balances (
            user_id, year, base_days, opening_carryover,
            opening_used_days, availability_adjustment
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, year) DO UPDATE SET
            base_days=excluded.base_days,
            opening_carryover=excluded.opening_carryover,
            opening_used_days=excluded.opening_used_days,
            availability_adjustment=excluded.availability_adjustment
        """,
        (
            user_id,
            year,
            person["base"],
            person["carryover"],
            opening_used,
            adjustment,
        ),
    )
    return request_used, adjustment


@bp.route("/admin/import-employees", methods=["GET", "POST"])
@login_required
@role_required("admin")
def import_employees_view():
    result = None

    if request.method == "POST":
        uploaded = request.files.get("excel_file")
        if not uploaded or not uploaded.filename:
            flash("Wybierz plik XLSX.")
            return render_template("admin_employee_import.html", result=None)
        if not uploaded.filename.lower().endswith(".xlsx"):
            flash("Obsługiwany jest tylko plik XLSX.")
            return render_template("admin_employee_import.html", result=None)

        try:
            rows = _xlsx_rows(uploaded)
            people, parse_errors = _parse_people(rows)
        except Exception as error:
            flash(f"Nie udało się odczytać pliku: {error}")
            return render_template("admin_employee_import.html", result=None)

        if not people:
            flash("W pliku nie znaleziono kont do importu.")
            return render_template("admin_employee_import.html", result={"errors": parse_errors, "rows": []})

        conn = get_db()
        imported_rows = []
        added = 0
        updated = 0
        skipped = 0
        year = date.today().year

        try:
            backup_path = _create_backup("preimport")
            default_company = conn.execute(
                "SELECT id FROM companies WHERE name = 'EMERLOG SP. Z O. O.' LIMIT 1"
            ).fetchone()
            default_company_id = default_company["id"] if default_company else None

            for person in people:
                existing, match_error = _find_existing_user(conn, person)
                if match_error:
                    skipped += 1
                    imported_rows.append({
                        "name": person["display_name"],
                        "login": person["login"],
                        "status": "Pominięto",
                        "details": match_error,
                    })
                    continue

                try:
                    if existing:
                        login_conflict = conn.execute(
                            "SELECT id FROM users WHERE lower(login)=lower(?) AND id<>?",
                            (person["login"], existing["id"]),
                        ).fetchone()
                        if login_conflict:
                            raise ValueError("login jest już przypisany do innego konta")

                        conn.execute(
                            """
                            UPDATE users
                            SET login=?, password_hash=?, full_name=?, vacation_days=?,
                                carryover_days=?, contract_type=?
                            WHERE id=?
                            """,
                            (
                                person["login"],
                                generate_password_hash(person["password"]),
                                person["storage_name"],
                                person["base"],
                                person["carryover"],
                                person["contract_type"],
                                existing["id"],
                            ),
                        )
                        user_id = existing["id"]
                        action = "Zaktualizowano"
                        updated += 1
                    else:
                        cursor = conn.execute(
                            """
                            INSERT INTO users (
                                login, password_hash, full_name, email, role, vacation_days,
                                active, department, job_title, manager_id, contract_type,
                                carryover_days, company_id
                            ) VALUES (?, ?, ?, '', 'pracownik', ?, 1, '', '', NULL, ?, ?, ?)
                            """,
                            (
                                person["login"],
                                generate_password_hash(person["password"]),
                                person["storage_name"],
                                person["base"],
                                person["contract_type"],
                                person["carryover"],
                                default_company_id,
                            ),
                        )
                        user_id = cursor.lastrowid
                        action = "Dodano"
                        added += 1

                    request_used, adjustment = _save_balance(conn, user_id, person, year)
                    details = f"{person['base']} + {person['carryover']} zaległych, wykorzystane {person['used']}, pozostało {person['remaining']}"
                    if request_used:
                        details += f"; w systemie jest już {request_used} dni z wniosków"
                    if person["remaining_note"]:
                        details += f"; zastosowano notatkę z Excela"
                    if adjustment:
                        details += f"; korekta bilansu {adjustment:+d}"

                    imported_rows.append({
                        "name": person["display_name"],
                        "login": person["login"],
                        "status": action,
                        "details": details,
                    })

                    if user_id == session.get("user_id"):
                        session["login"] = person["login"]
                        session["full_name"] = person["storage_name"]
                except Exception as row_error:
                    skipped += 1
                    imported_rows.append({
                        "name": person["display_name"],
                        "login": person["login"],
                        "status": "Pominięto",
                        "details": str(row_error),
                    })

            log_action(
                conn,
                "import pracowników z Excela",
                "employee_import",
                None,
                f"dodano={added}, zaktualizowano={updated}, pominięto={skipped}, backup={backup_path.name}",
            )
            conn.commit()

            result = {
                "added": added,
                "updated": updated,
                "skipped": skipped,
                "total": len(people),
                "backup": backup_path.name,
                "errors": parse_errors,
                "rows": imported_rows,
            }
            flash("Import zakończony. Przed zmianami utworzono backup bezpieczeństwa.")
        except Exception as error:
            conn.rollback()
            flash(f"Import został przerwany: {error}")
            result = {"errors": parse_errors + [str(error)], "rows": []}
        finally:
            conn.close()

    return render_template("admin_employee_import.html", result=result)
