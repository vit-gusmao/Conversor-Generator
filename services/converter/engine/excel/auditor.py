"""Post-generation XLSX audit.

A file that opens in Excel is not proof of a successful conversion.  This
module reopens the produced package and checks, independently of the writer:

* ZIP/XML integrity;
* the absence of **any** embedded media (`xl/media/`) or image relationship;
* the absence of formulas;
* that every sheet and every planned cell actually exists with the expected
  value.

The result is never "trust me": each check is a measured value.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import load_workbook

from engine.excel.writer import sanitize_text
from models.document import CellPlan, WorkbookPlan


@dataclass(slots=True)
class XlsxAudit:
    ok: bool
    checks: list[dict[str, str | None]] = field(default_factory=list)
    sheets: int = 0
    cells: int = 0
    header_cells: int = 0
    images: int = 0
    formulas: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "sheets": self.sheets,
            "cells": self.cells,
            "images": self.images,
            "formulas": self.formulas,
            "header_cells": self.header_cells,
            "checks": self.checks,
        }


def audit_xlsx(data: bytes, plan: WorkbookPlan) -> XlsxAudit:
    checks: list[dict[str, str | None]] = []
    audit = XlsxAudit(ok=True)

    def check(name: str, passed: bool, details: str, measured: str | None = None, expected: str | None = None) -> None:
        checks.append(
            {
                "check": name,
                "status": "pass" if passed else "fail",
                "details": details,
                "measured": measured,
                "expected": expected,
            }
        )
        if not passed:
            audit.ok = False

    # -- package integrity --------------------------------------------------
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        bad = archive.testzip()
        check("xlsx_zip_integrity", bad is None, "Pacote ZIP legível" if bad is None else f"Entrada corrompida: {bad}")
        names = archive.namelist()
    except Exception as exc:  # noqa: BLE001
        check("xlsx_zip_integrity", False, f"Falha ao abrir o pacote XLSX: {exc}")
        audit.checks = checks
        audit.ok = False
        return audit

    required = ["[Content_Types].xml", "xl/workbook.xml"]
    missing = [part for part in required if part not in names]
    check(
        "xlsx_required_parts",
        not missing,
        "Partes obrigatórias presentes" if not missing else f"Ausentes: {missing}",
    )

    media = [name for name in names if name.startswith("xl/media/")]
    audit.images = len(media)
    check(
        "xlsx_no_media",
        not media,
        "Nenhuma mídia incorporada" if not media else f"{len(media)} arquivo(s) em xl/media/",
        measured=str(len(media)),
        expected="0",
    )

    drawing_images = 0
    unreadable_drawings = 0
    for name in names:
        if not name.startswith("xl/drawings/") or not name.endswith(".xml"):
            continue
        try:
            content = archive.read(name).decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            # Swallowing this used to make the check "pass" without reading the
            # part at all: an unreadable drawing part is a failed verification.
            unreadable_drawings += 1
            continue
        drawing_images += content.count("<a:blip") + content.count("r:embed")
    check(
        "xlsx_no_image_relationships",
        drawing_images == 0 and unreadable_drawings == 0,
        "Nenhum relacionamento de imagem"
        if drawing_images == 0 and unreadable_drawings == 0
        else f"{drawing_images} referência(s) de imagem, {unreadable_drawings} parte(s) ilegível(is)",
        measured=str(drawing_images),
        expected="0",
    )

    archive.close()

    # -- reopen with openpyxl ----------------------------------------------
    try:
        workbook = load_workbook(io.BytesIO(data), data_only=False)
    except Exception as exc:  # noqa: BLE001
        check("xlsx_reopen", False, f"O arquivo não pôde ser reaberto: {exc}")
        audit.checks = checks
        audit.ok = False
        return audit
    check("xlsx_reopen", True, "Arquivo reaberto com openpyxl")
    audit.sheets = len(workbook.sheetnames)

    expected_names = [sheet.name[:31] for sheet in plan.sheets]
    check(
        "xlsx_sheet_names",
        workbook.sheetnames == expected_names,
        f"Abas: {workbook.sheetnames}",
        measured=str(len(workbook.sheetnames)),
        expected=str(len(expected_names)),
    )

    formula_count = 0
    total_cells = 0
    header_cells = 0
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                if cell.row == 1 and worksheet.title:
                    # Headers are counted separately: `ExcelResult.cells` (what
                    # the writer reports) counts data cells only, and two
                    # different totals for the same file are a metrics defect.
                    header_cells += 1
                    continue
                total_cells += 1
                if cell.data_type == "f":
                    formula_count += 1
    audit.cells = total_cells
    audit.header_cells = header_cells
    audit.formulas = formula_count
    check(
        "xlsx_no_formulas",
        formula_count == 0,
        "Nenhuma fórmula gerada" if formula_count == 0 else f"{formula_count} fórmula(s) encontradas",
        measured=str(formula_count),
        expected="0",
    )

    images_in_workbook = sum(len(getattr(ws, "_images", [])) for ws in workbook.worksheets)
    check(
        "xlsx_no_images_object",
        images_in_workbook == 0,
        "Zero objetos de imagem no workbook" if images_in_workbook == 0 else f"{images_in_workbook} imagem(ns)",
        measured=str(images_in_workbook),
        expected="0",
    )

    check(
        "xlsx_has_cells",
        total_cells > 0,
        f"{total_cells} célula(s) com conteúdo",
        measured=str(total_cells),
        expected="> 0",
    )

    # -- value reconciliation ---------------------------------------------
    mismatches: list[str] = []
    checked = 0
    for sheet_plan in plan.sheets:
        title = sheet_plan.name[:31]
        if title not in workbook.sheetnames:
            check(
                "xlsx_sheets_present",
                False,
                f"A aba '{title}' não existe no arquivo gerado.",
                measured=",".join(workbook.sheetnames)[:200],
                expected=title,
            )
            continue
        worksheet = workbook[title]
        for row_index, row in enumerate(sheet_plan.rows, start=2):
            for col_index, cell_plan in enumerate(row, start=1):
                if cell_plan is None:
                    continue
                actual = worksheet.cell(row=row_index, column=col_index).value
                checked += 1
                if not _values_match(cell_plan, actual):
                    mismatches.append(
                        f"{sheet_plan.name}!{worksheet.cell(row=row_index, column=col_index).coordinate}"
                    )
                    if len(mismatches) >= 20:
                        break
            if len(mismatches) >= 20:
                break
    check(
        "xlsx_values_match_plan",
        not mismatches,
        "Todos os valores conferem com o plano" if not mismatches else f"Divergências: {mismatches[:10]}",
        measured=f"{checked - len(mismatches)}/{checked}",
        expected=f"{checked}/{checked}",
    )

    # -- headers and number formats ---------------------------------------
    bad_headers: list[str] = []
    bad_formats: list[str] = []
    for sheet_plan in plan.sheets:
        title = sheet_plan.name[:31]
        if title not in workbook.sheetnames:
            continue
        worksheet = workbook[title]
        for col_index, header in enumerate(sheet_plan.headers, start=1):
            actual = worksheet.cell(row=1, column=col_index).value
            if (actual or "") != sanitize_text(header or ""):
                bad_headers.append(f"{title}!{worksheet.cell(row=1, column=col_index).coordinate}")
        for row_index, row in enumerate(sheet_plan.rows, start=2):
            for col_index, cell_plan in enumerate(row, start=1):
                if cell_plan is None or not cell_plan.number_format:
                    continue
                actual = worksheet.cell(row=row_index, column=col_index)
                if actual.number_format != cell_plan.number_format:
                    bad_formats.append(f"{title}!{actual.coordinate}")
                    if len(bad_formats) >= 20:
                        break
            if len(bad_formats) >= 20:
                break

    check(
        "xlsx_headers_match_plan",
        not bad_headers,
        "Cabeçalhos conferem com o plano" if not bad_headers else f"Divergências: {bad_headers[:10]}",
        measured=str(len(bad_headers)),
        expected="0",
    )
    check(
        "xlsx_number_formats_applied",
        not bad_formats,
        "Formatos numéricos aplicados" if not bad_formats else f"Divergências: {bad_formats[:10]}",
        measured=str(len(bad_formats)),
        expected="0",
    )

    workbook.close()
    audit.checks = checks
    return audit


def _values_match(cell: CellPlan, actual) -> bool:
    expected = cell.value
    if expected is None:
        return actual in (None, "")
    if isinstance(expected, bool):
        return bool(actual) == expected
    if isinstance(expected, (datetime, date)):
        if isinstance(actual, (datetime, date)):
            actual_date = actual.date() if isinstance(actual, datetime) else actual
            expected_date = expected.date() if isinstance(expected, datetime) else expected
            return actual_date == expected_date
        return str(actual)[:10] == str(expected)[:10]
    if isinstance(expected, (int, float)):
        try:
            return abs(float(actual) - float(expected)) < 0.0051
        except (TypeError, ValueError):
            return False
    if isinstance(expected, str):
        if actual is None:
            return not expected.strip()
        # The writer replaces characters XML forbids (control bytes) by U+FFFD
        # to keep the file valid; the audit must compare against that same
        # transformation, otherwise a repaired cell looks like a divergence.
        expected = sanitize_text(expected)
        if isinstance(actual, str):
            return actual.strip() == expected.strip()
        return str(actual) == expected
    return str(actual) == str(expected)


__all__ = ["audit_xlsx", "XlsxAudit"]
