"""Stage 11 — XLSX generation.

Hard rules enforced by construction:

* **no images** — `add_image` is never called, and the writer asserts that the
  workbook holds zero images before serialising;
* **no formulas** — every value is written as a literal (`data_type` stays
  `s`/`n`/`d`); text starting with `=` is stored as a string;
* **typed cells** — numbers, dates and money use Excel types and number
  formats; identifiers use the text format so leading zeros survive;
* **limits respected** — the resolver splits oversized sheets; the writer
  truncates only individual cell text above 32,767 characters and reports it.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from models.document import CellPlan, SheetPlan, WorkbookPlan

MAX_CELL_CHARS = 32_767

_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
_BODY_FONT = Font(size=11)
_THIN = Side(style="thin", color="D4D4D8")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


@dataclass(slots=True)
class WrittenCell:
    sheet: str
    cell: str
    value_type: str
    value: object


@dataclass(slots=True)
class ExcelResult:
    data: bytes
    sheets: int
    cells: int
    size_bytes: int
    truncated_cells: int = 0
    truncated_refs: list[str] = field(default_factory=list)
    #: characters that Excel cannot store and were replaced by U+FFFD
    sanitized_chars: int = 0
    sanitized_refs: list[str] = field(default_factory=list)
    cell_map: list[WrittenCell] = field(default_factory=list)


#: A string starting with any of these is interpreted by Excel/openpyxl as a
#: formula or a special value.  Such text is always stored as a literal string
#: (data_type "s"), preserving the original characters byte for byte.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _force_literal_text(cell, value: str) -> None:
    """Store *value* as a text cell even when it looks like a formula.

    openpyxl infers ``data_type="f"`` for any string starting with ``=``, which
    turns document text into an executable formula inside the workbook
    (Formula Injection).  Setting ``data_type`` after the assignment keeps the
    original text and forces the string type, so the cell is never evaluated.
    """
    if value.startswith(_FORMULA_PREFIXES):
        cell.data_type = "s"


#: Characters XML 1.0 forbids inside a cell.  Excel cannot store them and
#: openpyxl raises `IllegalCharacterError`, which used to abort the whole
#: conversion at the last step.  PDFs do contain them: broken text mapping,
#: NUL bytes used as field separators (Stripe invoices), OCR artefacts.
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: U+FFFD REPLACEMENT CHARACTER: marks the position without inventing data.
_REPLACEMENT = "\ufffd"


def sanitize_text(value: str) -> str:
    """Public form: text exactly as it will exist inside the workbook."""
    return _sanitize(value)[0]


def _sanitize(value: str) -> tuple[str, int]:
    """Return (clean value, number of characters replaced).

    The replacement is recorded by the caller: the text still reaches the
    sheet, and the audit states that a character was not representable.
    """
    if not value:
        return value, 0
    found = _ILLEGAL_XML_RE.findall(value)
    if not found:
        return value, 0
    return _ILLEGAL_XML_RE.sub(_REPLACEMENT, value), len(found)



def write_workbook(plan: WorkbookPlan, *, run_id: str = "", title: str | None = None) -> ExcelResult:
    if not plan.sheets:
        raise ValueError("Nenhuma aba para gerar: o documento não produziu conteúdo tabular.")

    workbook = Workbook()
    workbook.remove(workbook.active)

    cell_map: list[WrittenCell] = []
    truncated = 0
    truncated_refs: list[str] = []
    sanitized = 0
    sanitized_refs: list[str] = []
    written = 0

    for sheet_plan in plan.sheets:
        worksheet = workbook.create_sheet(title=_safe_title(sheet_plan.name))
        header_replacements = _write_header(worksheet, sheet_plan)
        if header_replacements:
            sanitized += header_replacements
            sanitized_refs.append(sheet_plan.name)
        widths: dict[int, int] = {}
        for row_index, row in enumerate(sheet_plan.rows, start=2):
            for col_index, cell_plan in enumerate(row, start=1):
                if cell_plan is None:
                    continue
                ref = f"{get_column_letter(col_index)}{row_index}"
                value, was_truncated = _excel_value(cell_plan)
                if isinstance(value, str):
                    value, replaced = _sanitize(value)
                    if replaced:
                        sanitized += replaced
                        sanitized_refs.append(f"{sheet_plan.name}!{ref}")
                if was_truncated:
                    truncated += 1
                    truncated_refs.append(f"{sheet_plan.name}!{ref}")
                target = worksheet.cell(row=row_index, column=col_index)
                target.value = value
                if isinstance(value, str):
                    _force_literal_text(target, value)
                target.font = _BODY_FONT
                target.border = _BORDER
                if cell_plan.number_format:
                    target.number_format = cell_plan.number_format
                target.alignment = Alignment(
                    vertical="top",
                    wrap_text=bool(isinstance(value, str) and len(value) > 40),
                    horizontal="right" if isinstance(value, (int, float)) and not isinstance(value, bool) else "left",
                )
                if value is not None:
                    written += 1
                    widths[col_index] = max(
                        widths.get(col_index, 0), len(str(cell_plan.display or value))
                    )
                    cell_map.append(
                        WrittenCell(
                            sheet=sheet_plan.name,
                            cell=ref,
                            value_type=cell_plan.value_type,
                            value=value,
                        )
                    )
        _write_widths(worksheet, sheet_plan, widths)
        worksheet.freeze_panes = "A2"
        if sheet_plan.headers:
            worksheet.auto_filter.ref = (
                f"A1:{get_column_letter(max(1, len(sheet_plan.headers)))}{max(1, len(sheet_plan.rows) + 1)}"
            )
        worksheet.sheet_view.showGridLines = True

    workbook.properties.title = title or plan.title or "Conversão PDF para Excel"
    workbook.properties.creator = "PDF to Excel Platform"
    workbook.properties.description = (
        f"Conversão auditável gerada pelo motor pdf2x. Execução: {run_id or 'n/d'}. "
        "Somente texto e dados; nenhuma imagem incorporada."
    )

    # Absolute guarantee for this code path: images live on worksheets, not on
    # the workbook container, so both levels are checked.
    images = list(getattr(workbook, "_images", []) or [])
    for worksheet in workbook.worksheets:
        images.extend(getattr(worksheet, "_images", []) or [])
    if images:
        raise RuntimeError("O gerador de Excel tentou incorporar imagens; operação abortada.")

    buffer = io.BytesIO()
    workbook.save(buffer)
    data = buffer.getvalue()
    return ExcelResult(
        data=data,
        sheets=len(plan.sheets),
        cells=written,
        size_bytes=len(data),
        truncated_cells=truncated,
        truncated_refs=truncated_refs,
        sanitized_chars=sanitized,
        sanitized_refs=sanitized_refs,
        cell_map=cell_map,
    )


def _write_header(worksheet, sheet_plan: SheetPlan) -> int:
    """Write the header row; returns how many characters had to be replaced."""
    replaced_total = 0
    for col_index, header in enumerate(sheet_plan.headers, start=1):
        value = header
        if isinstance(value, str):
            value, replaced = _sanitize(value)
            replaced_total += replaced
        cell = worksheet.cell(row=1, column=col_index)
        cell.value = value
        if isinstance(value, str):
            _force_literal_text(cell, value)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.border = _BORDER
        cell.alignment = Alignment(vertical="center", horizontal="left", wrap_text=True)
    worksheet.row_dimensions[1].height = 22
    return replaced_total


def _write_widths(worksheet, sheet_plan: SheetPlan, widths: dict[int, int]) -> None:
    for col_index, header in enumerate(sheet_plan.headers, start=1):
        measured = max(widths.get(col_index, 0), len(header or ""))
        worksheet.column_dimensions[get_column_letter(col_index)].width = min(60, max(10, measured + 2))


def _excel_value(cell: CellPlan) -> tuple[object, bool]:
    value = cell.value
    if isinstance(value, str):
        if len(value) > MAX_CELL_CHARS:
            return value[: MAX_CELL_CHARS - 1] + "…", True
        return value, False
    if isinstance(value, datetime):
        return value, False
    if isinstance(value, date):
        return value, False
    return value, False


def _safe_title(name: str) -> str:
    for char in "[]:*?/\\":
        name = name.replace(char, " ")
    name = " ".join(name.split())[:31].strip()
    return name or "Planilha"


__all__ = ["write_workbook", "sanitize_text", "ExcelResult", "WrittenCell"]
