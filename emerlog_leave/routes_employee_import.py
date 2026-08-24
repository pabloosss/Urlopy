from datetime import date
from html import escape
from io import BytesIO
from pathlib import PurePosixPath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from flask import Blueprint, flash, render_template, request, send_file, session
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
MAX_XLSX_BYTES = 8 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED = 64 * 1024 * 1024
MAX_XLSX_ENTRIES = 250
MAX_ROWS = 5000
MAX_COLUMNS = 100

TEMPLATE_HEADERS = [
    "Typ",
    "Pracownik (nazwa źródłowa)",
    "Nazwisko i imię",
    "Login",
    "Hasło",
    "Urlop należny",
    "Urlop zaległy",
    "Urlop wykorzystany",
    "Urlop pozostały",
    "Uwagi",
]

HEADER_ALIASES = {
    "typ": "type",
    "rodzaj": "type",
    "typ umowy": "type",
    "pracownik nazwa zrodlowa": "source_name",
    "nazwa zrodlowa": "source_name",
    "stara nazwa": "source_name",
    "stare nazwisko i imie": "source_name",
    "nazwisko i imie": "display_name",
    "imie i nazwisko": "display_name",
    "pracownik aktualny": "display_name",
    "login": "login",
    "uzytkownik": "login",
    "haslo": "password",
    "haslo startowe": "password",
    "urlop nalezny": "base",
    "limit urlopu": "base",
    "limit": "base",
    "urlop zalegly": "carryover",
    "zalegle": "carryover",
    "urlop wykorzystany": "used",
    "wykorzystane": "used",
    "urlop pozostaly": "remaining",
    "pozostalo": "remaining",
    "dostepne": "remaining",
    "uwagi": "notes",
    "komentarz": "notes",
}


def _clean(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\u00a0", " ").split()).strip()


def _normalized_text(value):
    text = _clean(value).casefold().replace("ł", "l")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


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
    tokens = re.findall(r"[a-z0-9]+", _normalized_text(value))
    return "|".join(sorted(tokens))


def _column_index(cell_ref):
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    result = 0
    for ch in letters:
        result = result * 26 + (ord(ch) - 64)
    return result - 1


def _safe_archive_read(archive, name):
    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise ValueError("Plik XLSX jest niekompletny.") from error
    if info.file_size > 32 * 1024 * 1024:
        raise ValueError("Jeden z elementów pliku XLSX jest zbyt duży.")
    return archive.read(name)


def _xlsx_rows(file_storage):
    payload = file_storage.read(MAX_XLSX_BYTES + 1)
    if not payload:
        raise ValueError("Plik jest pusty.")
    if len(payload) > MAX_XLSX_BYTES:
        raise ValueError("Plik jest za duży. Maksymalny rozmiar to 8 MB.")

    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise ValueError("To nie jest poprawny plik XLSX.") from error

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_XLSX_ENTRIES:
            raise ValueError("Plik XLSX zawiera zbyt dużo elementów.")
        if any(info.flag_bits & 0x1 for info in infos):
            raise ValueError("Szyfrowane pliki XLSX nie są obsługiwane.")
        if sum(info.file_size for info in infos) > MAX_XLSX_UNCOMPRESSED:
            raise ValueError("Plik XLSX po rozpakowaniu jest zbyt duży.")

        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(_safe_archive_read(archive, "xl/sharedStrings.xml"))
            for item in shared_root.findall(f"{{{MAIN_NS}}}si"):
                shared_strings.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))

        workbook_root = ET.fromstring(_safe_archive_read(archive, "xl/workbook.xml"))
        sheet = workbook_root.find(f".//{{{MAIN_NS}}}sheet")
        if sheet is None:
            raise ValueError("Nie znaleziono arkusza w pliku.")
        relation_id = sheet.attrib.get(f"{{{REL_NS}}}id")

        rels_root = ET.fromstring(_safe_archive_read(archive, "xl/_rels/workbook.xml.rels"))
        target = None
        for rel in rels_root.findall(f"{{{PKG_REL_NS}}}Relationship"):
            if rel.attrib.get("Id") == relation_id:
                target = rel.attrib.get("Target")
                break
        if not target:
            raise ValueError("Nie udało się odczytać pierwszego arkusza.")

        target = str(PurePosixPath(target)).lstrip("/")
        sheet_path = str(PurePosixPath("xl") / target).replace("xl/../", "")
        if not sheet_path.startswith("xl/") or sheet_path not in archive.namelist():
            sheet_path = "xl/worksheets/sheet1.xml"
        if sheet_path not in archive.namelist():
            raise ValueError("Nie znaleziono danych arkusza.")

        sheet_root = ET.fromstring(_safe_archive_read(archive, sheet_path))
        rows = []
        for row_node in sheet_root.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
            if len(rows) >= MAX_ROWS:
                raise ValueError(f"Plik zawiera więcej niż {MAX_ROWS} wierszy.")
            values = {}
            max_col = -1
            for cell in row_node.findall(f"{{{MAIN_NS}}}c"):
                ref = cell.attrib.get("r", "A1")
                col = _column_index(ref)
                if col < 0 or col >= MAX_COLUMNS:
                    continue
                max_col = max(max_col, col)
                cell_type = cell.attrib.get("t")
                value_node = cell.find(f"{{{MAIN_NS}}}v")

                if cell_type == "inlineStr":
                    inline = cell.find(f"{{{MAIN_NS}}}is")
                    value = "" if inline is None else "".join(node.text or "" for node in inline.iter(f"{{{MAIN_NS}}}t"))
                elif value_node is None:
                    value = ""
                elif cell_type == "s":
                    try:
                        index = int(value_node.text or 0)
                    except ValueError:
                        index = -1
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

            width = max(10, max_col + 1)
            rows.append([values.get(index, "") for index in range(width)])
        return rows


def _find_header_row(rows):
    for row_number, row in enumerate(rows[:15], start=1):
        columns = {}
        for index, value in enumerate(row):
            key = HEADER_ALIASES.get(_normalized_text(value))
            if key and key not in columns:
                columns[key] = index
        required = {"type", "display_name", "login", "base", "carryover", "used"}
        if required.issubset(columns):
            return row_number, columns
    raise ValueError(
        "Nie znaleziono nagłówków. Użyj wzoru z kolumnami: Typ, Nazwisko i imię, Login, "
        "Urlop należny, Urlop zaległy, Urlop wykorzystany."
    )


def _value(row, columns, key):
    index = columns.get(key)
    if index is None or index >= len(row):
        return ""
    return row[index]


def _contract_type(value):
    normalized = _normalized_text(value)
    if normalized.startswith("zlecen") or normalized in {"umowa zlecenie", "zleceniobiorca"}:
        return CONTRACT_ZLECENIE
    if normalized.startswith("pracownik") or normalized in {"uop", "umowa o prace"}:
        return CONTRACT_UOP
    return None


def _parse_people(rows):
    people = []
    errors = []
    seen_logins = {}
    header_row, columns = _find_header_row(rows)

    for row_number, row in enumerate(rows[header_row:], start=header_row + 1):
        row = list(row)
        raw_values = [_clean(value) for value in row]
        if not any(raw_values):
            continue

        type_value = _clean(_value(row, columns, "type"))
        source_name = _clean(_value(row, columns, "source_name"))
        current_name = _clean(_value(row, columns, "display_name"))
        login = _clean(_value(row, columns, "login")).upper()
        password = _clean(_value(row, columns, "password"))
        base = _to_int(_value(row, columns, "base"))
        carryover = _to_int(_value(row, columns, "carryover"), 0)
        used = _to_int(_value(row, columns, "used"), 0)
        remaining = _to_int(_value(row, columns, "remaining"))
        notes = _clean(_value(row, columns, "notes"))[:1000]

        contract_type = _contract_type(type_value)
        row_errors = []
        if not contract_type:
            row_errors.append("Typ musi być „Pracownik” albo „Zlecenie”")
        if not current_name or len(current_name) > 160:
            row_errors.append("niepoprawne „Nazwisko i imię”")
        if not login or len(login) > 120 or any(ch.isspace() for ch in login):
            row_errors.append("niepoprawny login")
        if login and login.casefold() in seen_logins:
            row_errors.append(f"login powtarza się z wierszem {seen_logins[login.casefold()]}")
        if base is None or not 0 <= base <= 366:
            row_errors.append("„Urlop należny” musi być liczbą 0–366")
        if carryover is None or not 0 <= carryover <= 366:
            row_errors.append("„Urlop zaległy” musi być liczbą 0–366")
        if used is None or not 0 <= used <= 732:
            row_errors.append("„Urlop wykorzystany” musi być liczbą 0–732")
        if remaining is not None and not -732 <= remaining <= 1098:
            row_errors.append("niepoprawny „Urlop pozostały”")
        if password and len(password) < 8:
            row_errors.append("hasło musi mieć minimum 8 znaków")

        if row_errors:
            errors.append(f"Wiersz {row_number}: " + "; ".join(row_errors) + ".")
            continue

        seen_logins[login.casefold()] = row_number
        if remaining is None:
            remaining = base + carryover - used

        people.append(
            {
                "row_number": row_number,
                "login": login,
                "password": password,
                "display_name": current_name,
                "storage_name": surname_first_to_storage(current_name),
                "aliases": [value for value in [source_name, current_name] if value],
                "contract_type": contract_type,
                "base": base,
                "carryover": carryover,
                "used": used,
                "remaining": remaining,
                "notes": notes,
            }
        )

    return people, errors


def _find_existing_user(conn, person):
    by_login = conn.execute(
        "SELECT * FROM users WHERE lower(login) = lower(?)",
        (person["login"],),
    ).fetchone()
    if by_login:
        return by_login, None

    candidate_keys = {
        _name_key(value)
        for value in person["aliases"] + [person["display_name"]]
        if _name_key(value)
    }
    matches = []
    for user in conn.execute("SELECT * FROM users").fetchall():
        stored_key = _name_key(user["full_name"])
        display_key = _name_key(surname_first(user["full_name"]))
        if stored_key in candidate_keys or display_key in candidate_keys:
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
    adjustment = person["remaining"] - (
        person["base"] + person["carryover"] - accepted_after_import
    )

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


def _column_letter(index):
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _template_xlsx():
    output = BytesIO()
    sheet_cells = []
    for index, header in enumerate(TEMPLATE_HEADERS):
        cell_ref = f"{_column_letter(index)}1"
        sheet_cells.append(
            f'<c r="{cell_ref}" t="inlineStr" s="1"><is><t>{escape(header)}</t></is></c>'
        )

    widths = [14, 28, 26, 20, 20, 16, 16, 18, 16, 34]
    cols_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )

    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{MAIN_NS}">
  <dimension ref="A1:J1"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>{cols_xml}</cols>
  <sheetData><row r="1" ht="24" customHeight="1">{''.join(sheet_cells)}</row></sheetData>
  <autoFilter ref="A1:J1"/>
</worksheet>'''

    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}"><sheets><sheet name="Dane" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    workbook_rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG_REL_NS}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    root_rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG_REL_NS}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'''
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F2937"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        archive.writestr("xl/styles.xml", styles_xml)

    output.seek(0)
    return output


@bp.route("/admin/import-employees/template")
@login_required
@role_required("admin")
def download_import_template():
    return send_file(
        _template_xlsx(),
        as_attachment=True,
        download_name="WZOR_IMPORT_PRACOWNIKOW.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


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
                    imported_rows.append({"name": person["display_name"], "login": person["login"], "status": "Pominięto", "details": match_error})
                    continue
                if existing and existing["role"] == "admin":
                    skipped += 1
                    imported_rows.append({"name": person["display_name"], "login": person["login"], "status": "Pominięto", "details": "konto administratora nie jest aktualizowane przez importer"})
                    continue

                savepoint = f"import_row_{person['row_number']}"
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    if existing:
                        login_conflict = conn.execute(
                            "SELECT id FROM users WHERE lower(login)=lower(?) AND id<>?",
                            (person["login"], existing["id"]),
                        ).fetchone()
                        if login_conflict:
                            raise ValueError("login jest już przypisany do innego konta")

                        if person["password"]:
                            conn.execute(
                                """UPDATE users SET login=?, password_hash=?, full_name=?, vacation_days=?, carryover_days=?, contract_type=? WHERE id=?""",
                                (person["login"], generate_password_hash(person["password"]), person["storage_name"], person["base"], person["carryover"], person["contract_type"], existing["id"]),
                            )
                        else:
                            conn.execute(
                                """UPDATE users SET login=?, full_name=?, vacation_days=?, carryover_days=?, contract_type=? WHERE id=?""",
                                (person["login"], person["storage_name"], person["base"], person["carryover"], person["contract_type"], existing["id"]),
                            )
                        user_id = existing["id"]
                        action = "Zaktualizowano"
                    else:
                        if not person["password"]:
                            raise ValueError("nowe konto wymaga hasła")
                        cursor = conn.execute(
                            """
                            INSERT INTO users (
                                login, password_hash, full_name, email, role, vacation_days,
                                active, department, job_title, manager_id, contract_type,
                                carryover_days, company_id
                            ) VALUES (?, ?, ?, '', 'pracownik', ?, 1, '', '', NULL, ?, ?, ?)
                            """,
                            (person["login"], generate_password_hash(person["password"]), person["storage_name"], person["base"], person["contract_type"], person["carryover"], default_company_id),
                        )
                        user_id = cursor.lastrowid
                        action = "Dodano"

                    request_used, adjustment = _save_balance(conn, user_id, person, year)
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    if action == "Dodano":
                        added += 1
                    else:
                        updated += 1

                    details = f"{person['base']} + {person['carryover']} zaległych, wykorzystane {person['used']}, pozostało {person['remaining']}"
                    if request_used:
                        details += f"; w systemie jest już {request_used} dni z wniosków"
                    if person["notes"]:
                        details += f"; uwaga: {person['notes']}"
                    if adjustment:
                        details += f"; korekta bilansu {adjustment:+d}"
                    if existing and not person["password"]:
                        details += "; hasło bez zmian"

                    imported_rows.append({"name": person["display_name"], "login": person["login"], "status": action, "details": details})
                    if user_id == session.get("user_id"):
                        session["login"] = person["login"]
                        session["full_name"] = person["storage_name"]
                except Exception as row_error:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    skipped += 1
                    imported_rows.append({"name": person["display_name"], "login": person["login"], "status": "Pominięto", "details": str(row_error)})

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
