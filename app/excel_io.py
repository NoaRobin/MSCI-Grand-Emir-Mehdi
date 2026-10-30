"""Lecture des fichiers d'ISIN et ecriture de l'export au format exact du template.

Le template (data/template.xlsx) est la copie des deux feuilles « Template Import Titres » et
« Template Import Fonds » du fichier mapping.xlsx de l'equipe : en-tetes, ordre, styles et
largeurs sont conserves ; seules les lignes de donnees sont ajoutees.
"""
from __future__ import annotations

import csv
import io
from copy import copy
from pathlib import Path

from openpyxl import load_workbook

from . import isin as isin_tools

DATA = Path(__file__).parent / "data"
TEMPLATE = DATA / "template.xlsx"
SHEETS = {"issuer": "Template Import Titres", "fund": "Template Import Fonds"}
# Ordre de preference des colonnes contenant l'ISIN de l'entree.
ISIN_HEADERS = ("ISIN", "FUND_ISIN", "ISSUER_ISIN", "CODE ISIN", "ISIN CODE")
FIELD_HEADERS = {"Client_ID": ("CLIENT_ID", "CLIENT ID", "ID CLIENT"),
                 "Security_Status": ("SECURITY_STATUS", "SECURITY STATUS", "STATUT")}
MAX_ROWS = 20000


def template_columns() -> dict[str, list[str]]:
    workbook = load_workbook(TEMPLATE, read_only=True)
    try:
        return {entity: [c.value for c in next(workbook[name].iter_rows(max_row=1))]
                for entity, name in SHEETS.items()}
    finally:
        workbook.close()


def _sheet_entity(name: str) -> str | None:
    low = name.lower()
    if "fond" in low or "fund" in low:
        return "fund"
    if any(k in low for k in ("titre", "issuer", "emett", "émett", "action", "oblig", "securit")):
        return "issuer"
    return None


def _header_index(header: list) -> tuple[int | None, dict[str, int]]:
    labels = [str(h).strip().upper() if h is not None else "" for h in header]
    isin_col = None
    for wanted in ISIN_HEADERS:
        if wanted in labels:
            isin_col = labels.index(wanted)
            break
    if isin_col is None:
        isin_col = next((i for i, h in enumerate(labels) if "ISIN" in h), None)
    fields = {}
    for key, names in FIELD_HEADERS.items():
        for i, h in enumerate(labels):
            if h in names:
                fields[key] = i
                break
    return isin_col, fields


def _extract(rows: list[list], sheet: str) -> list[dict]:
    """Detecte la ligne d'en-tete (10 premieres lignes) ou, a defaut, la colonne d'ISIN."""
    header_row, isin_col, fields = None, None, {}
    for index, row in enumerate(rows[:10]):
        col, found = _header_index(list(row))
        if col is not None:
            header_row, isin_col, fields = index, col, found
            break
    if isin_col is None:
        width = max((len(r) for r in rows), default=0)
        scores = [sum(isin_tools.looks_like_isin(r[c]) for r in rows if c < len(r)) for c in range(width)]
        if not scores or max(scores) == 0:
            return []
        isin_col = scores.index(max(scores))
        header_row = -1
    found_rows = []
    for number, row in enumerate(rows[header_row + 1:], start=header_row + 2):
        raw = row[isin_col] if isin_col < len(row) else None
        if raw is None or str(raw).strip() == "":
            continue
        entry = {"ISIN": isin_tools.normalize(raw), "source": f"{sheet} · ligne {number}"}
        for key, col in fields.items():
            value = row[col] if col < len(row) else None
            entry[key] = None if value is None else str(value).strip()
        found_rows.append(entry)
    return found_rows


def read_isins(filename: str, content: bytes) -> dict:
    """Retourne {"issuer": [...], "fund": [...], "auto": [...], "sheets": [...]}."""
    result: dict = {"issuer": [], "fund": [], "auto": [], "sheets": []}
    suffix = Path(filename).suffix.lower()
    if suffix in (".csv", ".txt"):
        text = content.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t") if text.strip() else csv.excel
        rows = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
        found = _extract(rows, Path(filename).name)
        result["auto"].extend(found)
        result["sheets"].append({"name": Path(filename).name, "entity": None, "rows": len(found)})
        return result
    if suffix not in (".xlsx", ".xlsm"):
        raise ValueError("Format non pris en charge : utiliser un fichier .xlsx, .xlsm ou .csv.")
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception:
        raise ValueError("Fichier Excel illisible ou protégé par mot de passe.") from None
    try:
        for sheet in workbook.worksheets:
            rows = [list(r) for _, r in zip(range(MAX_ROWS + 10), sheet.iter_rows(values_only=True))]
            found = _extract(rows, sheet.title)
            if not found:
                continue
            entity = _sheet_entity(sheet.title)
            result[entity or "auto"].extend(found)
            result["sheets"].append({"name": sheet.title, "entity": entity, "rows": len(found)})
    finally:
        workbook.close()
    if not any(result[k] for k in ("issuer", "fund", "auto")):
        raise ValueError("Aucune colonne d'ISIN trouvée. Ajouter un en-tête « ISIN » au-dessus de la liste.")
    return result


def export(rows: dict[str, list[dict]]) -> bytes:
    """Remplit une copie du template ; styles de la ligne 2 (ou 1) recopiés sur chaque ligne."""
    workbook = load_workbook(TEMPLATE)
    try:
        for entity, name in SHEETS.items():
            sheet = workbook[name]
            columns = [cell.value for cell in sheet[1]]
            model_row = 2 if sheet.max_row >= 2 else None
            for index, row in enumerate(rows.get(entity, []), start=2):
                for col_index, header in enumerate(columns, start=1):
                    cell = sheet.cell(index, col_index)
                    if model_row and sheet.cell(model_row, col_index).has_style:
                        cell._style = copy(sheet.cell(model_row, col_index)._style)
                    value = row.get(header)
                    cell.value = value
                    if isinstance(value, str):
                        cell.data_type = "s"  # texte force : jamais de formule importee
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()
    finally:
        workbook.close()
