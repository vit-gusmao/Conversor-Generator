"""Spreadsheet input: XLSX (OOXML) and CSV readers (T3, doc 04 §5/§6).

Content-based detection for XLSX: a ZIP that does not contain the OOXML
markers is not a spreadsheet, whatever its extension. CSV has no magic bytes,
so the declared format plus an explicit (or controlled, validated) dialect
decide acceptance. Budgets bound expansion so a renamed ZIP bomb or a huge
sheet cannot consume the machine; rejected files fail with specific codes
instead of being truncated silently.

The reader never executes macros, resolves external links or recalculates
formulas: a formula without a cached value stays text with a warning.
"""

from __future__ import annotations

import csv as csv_module
import io
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ingestion.base import IngestionError

#: Stamped into normalized v3 options and fingerprints (matches the contract).
SPREADSHEET_READER_VERSION = "spreadsheet-reader-v1"

#: Safety budgets (operational values, enforced, never silent truncation).
XLSX_MAX_ENTRIES = 2048
#: Cells actually present in the sheet XML (empty cells are not counted).
XLSX_MAX_CELLS = 2_000_000
#: Used extents; the declared <dimension> is never trusted.
XLSX_MAX_ROWS = 1_048_576
XLSX_MAX_COLUMNS = 16_384
#: One merged range larger than this is recorded by anchor only.
XLSX_MAX_MERGED_CELLS = 10_000
#: Bounding box of the grid we are willing to materialize. A file whose
#: declared extent is vastly larger than its content (padding attack) is
#: rejected instead of allocating an enormous ragged grid.
XLSX_MAX_GRID_AREA = 50_000_000
XLSX_MAX_EXPANSION_BYTES = 512 * 1024 * 1024  # sum of inflated entry sizes
XLSX_MAX_EXPANSION_RATIO = 500  # legit OOXML compresses ~10-50x; bombs exceed 1000x
XLSX_MAX_READ_SECONDS = 30
CSV_MAX_BYTES = 256 * 1024 * 1024
CSV_SNIFF_SAMPLE_BYTES = 64 * 1024

CSV_DELIMITERS = (",", ";", "\t", "|")
#: Encodings probed in order when the client does not declare one.
CSV_DEFAULT_ENCODINGS = ("utf-8-sig", "utf-8")


@dataclass
class SpreadsheetOptions:
    """Mirrors the v3 contract section (api/service.py normalization)."""

    sheet_names: list[str] | None = None
    header_row: bool | None = None
    csv_encoding: str | None = None
    csv_delimiter: str | None = None
    formula_mode: str = "cached_or_text"
    reader_version: str = SPREADSHEET_READER_VERSION

    @classmethod
    def from_mapping(cls, payload: dict[str, Any] | None) -> "SpreadsheetOptions":
        payload = payload or {}
        return cls(
            sheet_names=payload.get("sheet_names"),
            header_row=payload.get("header_row"),
            csv_encoding=payload.get("csv_encoding"),
            csv_delimiter=payload.get("csv_delimiter"),
            formula_mode=payload.get("formula_mode") or "cached_or_text",
            reader_version=payload.get("reader_version") or SPREADSHEET_READER_VERSION,
        )


@dataclass
class SpreadsheetProbe:
    kind: str = "spreadsheet"
    format: str | None = None  # xlsx | csv
    mime_type: str | None = None
    supported: bool = False
    reason: str | None = None
    reason_code: str | None = None
    sheet_names: list[str] | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SpreadsheetCell:
    """One original cell: value, type and real address provenance."""

    row: int  # 1-based physical row in the sheet
    column: int  # 1-based column index
    cell_address: str  # e.g. "B2"
    value: Any  # safe value: cached result, literal or formula text
    value_type: str  # text | number | date | boolean | empty | formula
    original: str  # text as read, used for display/original preservation
    formula_text: str | None = None
    is_merged_anchor: bool = False
    merged_in_anchor: str | None = None  # address of the anchor cell
    physical_lines: tuple[int, int] | None = None  # CSV multiline provenance

    @property
    def is_empty(self) -> bool:
        return self.value_type == "empty"


@dataclass
class SpreadsheetSheet:
    name: str
    state: str  # visible | hidden
    rows: list[list[SpreadsheetCell | None]]
    max_column: int

    def non_empty_rows(self) -> list[list[SpreadsheetCell | None]]:
        return [
            row
            for row in self.rows
            if any(cell is not None and not cell.is_empty for cell in row)
        ]


@dataclass
class SpreadsheetDocument:
    format: str  # xlsx | csv
    sheets: list[SpreadsheetSheet]
    warnings: list[dict[str, Any]]
    reader_version: str = SPREADSHEET_READER_VERSION
    date_system: str | None = None
    dialect: str | None = None
    detected_encoding: str | None = None
    workbook_filename: str = ""

    @property
    def page_count(self) -> int:
        return len(self.sheets)


_SHEET_KIND = "table"


def _address(row: int, column: int) -> str:
    """1-based row/column to A1 notation."""
    label = ""
    remaining = column
    while remaining > 0:
        remaining, offset = divmod(remaining - 1, 26)
        label = chr(ord("A") + offset) + label
    return f"{label}{row}"


def _unsupported(suffix: str) -> IngestionError:
    return IngestionError(
        "UNSUPPORTED_SPREADSHEET_FORMAT",
        f"Formato de planilha não suportado nesta versão: .{suffix}. "
        "Formatos aceitos: .xlsx e .csv.",
        details={"suffix": suffix, "supported": ["xlsx", "csv"]},
    )


def _looks_like_xlsx(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return "[Content_Types].xml" in names and any(
        name == "xl/workbook.xml" or name.endswith("/workbook.xml") for name in names
    )


def _check_xlsx_budgets(path: Path) -> list[dict[str, Any]]:
    """Rejects bombs/corrupted packages before any sheet is parsed."""
    warnings: list[dict[str, Any]] = []
    total_inflated = 0
    total_compressed = 0
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > XLSX_MAX_ENTRIES:
                raise IngestionError(
                    "SPREADSHEET_BUDGET_EXCEEDED",
                    f"O pacote XLSX tem {len(entries)} entradas (limite {XLSX_MAX_ENTRIES}).",
                    details={"entries": len(entries), "limit": XLSX_MAX_ENTRIES},
                )
            for entry in entries:
                total_inflated += entry.file_size
                total_compressed += entry.compress_size
    except zipfile.BadZipFile as exc:
        raise IngestionError(
            "SPREADSHEET_CORRUPT",
            "O arquivo não é um pacote XLSX legível.",
            details={"error": str(exc)},
        ) from exc
    if total_inflated > XLSX_MAX_EXPANSION_BYTES:
        raise IngestionError(
            "SPREADSHEET_BUDGET_EXCEEDED",
            "O conteúdo descompactado do XLSX excede o orçamento operacional.",
            details={"inflated_bytes": total_inflated, "limit": XLSX_MAX_EXPANSION_BYTES},
        )
    if total_compressed > 0 and total_inflated / total_compressed > XLSX_MAX_EXPANSION_RATIO:
        raise IngestionError(
            "SPREADSHEET_BUDGET_EXCEEDED",
            "A razão de expansão do pacote excede o limite de segurança.",
            details={
                "inflated_bytes": total_inflated,
                "compressed_bytes": total_compressed,
                "limit_ratio": XLSX_MAX_EXPANSION_RATIO,
            },
        )
    return warnings


def probe_spreadsheet(
    path: Path,
    declared_format: str | None = None,
    options: SpreadsheetOptions | None = None,
) -> SpreadsheetProbe:
    """Inspect a spreadsheet candidate without parsing sheet values."""
    suffix = path.suffix.lstrip(".").lower()
    normalized = (declared_format or suffix).lower()

    if normalized in {"xls", "xlsm", "xlsb", "ods"}:
        raise _unsupported(normalized)
    if normalized not in {"xlsx", "csv"}:
        raise _unsupported(normalized or "?")

    if normalized == "xlsx":
        if not _looks_like_xlsx(path):
            return SpreadsheetProbe(
                format="xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                supported=False,
                reason="O arquivo não é um pacote XLSX (conteúdo OOXML esperado).",
                reason_code="NOT_SPREADSHEET",
            )
        _check_xlsx_budgets(path)
        sheet_names: list[str] = []
        try:
            from openpyxl import load_workbook

            started = time.perf_counter()
            workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
            sheet_names = list(workbook.sheetnames)
            workbook.close()
            elapsed = time.perf_counter() - started
            if elapsed > XLSX_MAX_READ_SECONDS:
                raise IngestionError(
                    "SPREADSHEET_BUDGET_EXCEEDED",
                    "A leitura da estrutura do XLSX excedeu o tempo operacional.",
                    details={"elapsed_seconds": round(elapsed, 2)},
                )
        except IngestionError:
            raise
        except Exception as exc:  # openpyxl raises assorted exceptions on damage
            return SpreadsheetProbe(
                format="xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                supported=False,
                reason="O pacote XLSX está corrompido ou incompleto.",
                reason_code="SPREADSHEET_CORRUPT",
                warnings=[str(exc)],
            )
        return SpreadsheetProbe(
            format="xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            supported=True,
            sheet_names=sheet_names,
        )

    # CSV: declared/detected encoding + validated dialect.
    return _probe_csv(path, options or SpreadsheetOptions())


def _probe_csv(path: Path, options: SpreadsheetOptions) -> SpreadsheetProbe:
    size = path.stat().st_size
    if size > CSV_MAX_BYTES:
        raise IngestionError(
            "SPREADSHEET_BUDGET_EXCEEDED",
            "O arquivo CSV excede o orçamento operacional.",
            details={"size_bytes": size, "limit": CSV_MAX_BYTES},
        )
    sample = path.read_bytes()[:CSV_SNIFF_SAMPLE_BYTES]

    encoding = options.csv_encoding
    if encoding:
        try:
            sample.decode(encoding)
        except LookupError as exc:
            raise IngestionError(
                "CSV_ENCODING_REQUIRED",
                f"Encoding declarado é desconhecido: {encoding}.",
                details={"csv_encoding": encoding},
            ) from exc
        except UnicodeDecodeError as exc:
            raise IngestionError(
                "CSV_ENCODING_REQUIRED",
                f"O arquivo não decodifica como {encoding}: {exc}.",
                details={"csv_encoding": encoding, "error": str(exc)},
            ) from exc
        detected = encoding
    else:
        detected = None
        for candidate in CSV_DEFAULT_ENCODINGS:
            try:
                sample.decode(candidate)
                detected = candidate
                break
            except UnicodeDecodeError:
                continue
        if detected is None:
            return SpreadsheetProbe(
                format="csv",
                mime_type="text/csv",
                supported=False,
                reason=(
                    "O CSV não está em UTF-8. Declare o encoding explicitamente "
                    "(ex.: spreadsheet.csv_encoding='cp1252')."
                ),
                reason_code="CSV_ENCODING_REQUIRED",
            )

    text = sample.decode(detected)
    delimiter = options.csv_delimiter
    if not delimiter:
        counts = {candidate: text.count(candidate) for candidate in CSV_DELIMITERS}
        best_count = max(counts.values())
        if best_count == 0:
            return SpreadsheetProbe(
                format="csv",
                mime_type="text/csv",
                supported=True,
                reason="Nenhum delimitador encontrado (arquivo de coluna única).",
                warnings=["CSV sem delimitador reconhecido; tratado como coluna única."],
            )
        winners = [candidate for candidate, count in counts.items() if count == best_count]
        if len(winners) > 1:
            return SpreadsheetProbe(
                format="csv",
                mime_type="text/csv",
                supported=False,
                reason=(
                    "Dialeto CSV ambíguo. Declare spreadsheet.csv_delimiter "
                    "(';', ',' '\\t' ou '|')."
                ),
                reason_code="CSV_DIALECT_REQUIRED",
            )
        delimiter = winners[0]

    return SpreadsheetProbe(
        format="csv",
        mime_type="text/csv",
        supported=True,
        sheet_names=None,
    )


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _read_xlsx(path: Path, options: SpreadsheetOptions) -> SpreadsheetDocument:
    from openpyxl import load_workbook

    warnings: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        workbook = load_workbook(path, read_only=False, data_only=False, keep_links=False)
    except Exception as exc:
        raise IngestionError(
            "SPREADSHEET_CORRUPT",
            "O pacote XLSX não pôde ser lido.",
            details={"error": str(exc)},
        ) from exc

    try:
        date_system = "1900"
        try:
            epoch = workbook.epoch
            date_system = "1904" if epoch and epoch.year == 1904 else "1900"
        except Exception:  # pragma: no cover - attribute always present in 3.x
            pass

        selected = workbook.sheetnames
        if options.sheet_names is not None:
            missing = [name for name in options.sheet_names if name not in workbook.sheetnames]
            if missing:
                raise IngestionError(
                    "SPREADSHEET_SHEET_NOT_FOUND",
                    f"Abas solicitadas não existem: {missing}.",
                    details={"missing": missing, "available": workbook.sheetnames},
                )
            selected = [name for name in workbook.sheetnames if name in options.sheet_names]

        sheets: list[SpreadsheetSheet] = []
        for name in selected:
            worksheet: Worksheet = workbook[name]
            state = worksheet.sheet_state or "visible"
            if state != "visible":
                warnings.append(
                    {
                        "code": "HIDDEN_SHEET",
                        "message": f"Aba oculta '{name}' foi lida (padrão lê todas as abas).",
                        "sheet": name,
                        "state": state,
                    }
                )

            merged_lookup, merged_anchors = _merged_anchor_map(worksheet, name, warnings)
            # Only cells that actually exist in the sheet XML are parsed: a
            # declared dimension reaching XFD1048576 (common in exporter
            # output) must never allocate a 17-billion-cell grid.
            present = _present_cells(worksheet)
            if len(present) > XLSX_MAX_CELLS:
                raise IngestionError(
                    "SPREADSHEET_BUDGET_EXCEEDED",
                    f"A aba '{name}' tem {len(present)} celulas (limite {XLSX_MAX_CELLS}).",
                    details={"sheet": name, "cells": len(present), "limit": XLSX_MAX_CELLS},
                )
            if time.perf_counter() - started > XLSX_MAX_READ_SECONDS:
                raise IngestionError(
                    "SPREADSHEET_BUDGET_EXCEEDED",
                    "A leitura do XLSX excedeu o tempo operacional.",
                    details={
                        "sheet": name,
                        "elapsed_seconds": round(time.perf_counter() - started, 2),
                    },
                )
            sparse: dict[int, dict[int, SpreadsheetCell | None]] = {}
            max_row = 0
            max_column = 1
            sheet_formulas = 0
            for (row_index, column_index), cell in present:
                parsed = _parse_cell(cell, row_index, column_index, merged_lookup, merged_anchors)
                if parsed is not None and parsed.value_type == "formula":
                    sheet_formulas += 1
                sparse.setdefault(row_index, {})[column_index] = parsed
                if row_index > max_row:
                    max_row = row_index
                if column_index > max_column:
                    max_column = column_index
            if max_row > XLSX_MAX_ROWS or max_column > XLSX_MAX_COLUMNS:
                raise IngestionError(
                    "SPREADSHEET_BUDGET_EXCEEDED",
                    f"A aba '{name}' usa {max_row} linha(s) x {max_column} coluna(s), acima do limite.",
                    details={
                        "sheet": name,
                        "rows": max_row,
                        "columns": max_column,
                        "limit_rows": XLSX_MAX_ROWS,
                        "limit_columns": XLSX_MAX_COLUMNS,
                    },
                )
            if max_row * max_column > XLSX_MAX_GRID_AREA:
                raise IngestionError(
                    "SPREADSHEET_BUDGET_EXCEEDED",
                    (
                        f"A aba '{name}' declara uma área de {max_row}x{max_column} "
                        f"(limite {XLSX_MAX_GRID_AREA} celulas de grade)."
                    ),
                    details={
                        "sheet": name,
                        "rows": max_row,
                        "columns": max_column,
                        "limit_area": XLSX_MAX_GRID_AREA,
                    },
                )
            # Ragged rows: a wide cell in one row does not pad every other row.
            rows: list[list[SpreadsheetCell | None]] = []
            for row_index in range(1, max_row + 1):
                row_map = sparse.get(row_index, {})
                width = max(row_map) if row_map else 0
                rows.append([row_map.get(column_index) for column_index in range(1, width + 1)])
            if sheet_formulas:
                warnings.append(
                    {
                        "code": "FORMULA_WITHOUT_CACHE",
                        "message": (
                            f"Aba '{name}' tem {sheet_formulas} fórmula(s) sem valor em cache; "
                            "elas entram como texto e não são recalculadas."
                        ),
                        "sheet": name,
                        "count": sheet_formulas,
                    }
                )
            if not any(
                cell is not None and not cell.is_empty for row in rows for cell in row
            ):
                warnings.append(
                    {"code": "EMPTY_SHEET", "message": f"Aba sem valores: {name}.", "sheet": name}
                )
            sheets.append(SpreadsheetSheet(name=name, state=state, rows=rows, max_column=max_column))
    finally:
        workbook.close()

    elapsed = time.perf_counter() - started
    if elapsed > XLSX_MAX_READ_SECONDS:
        raise IngestionError(
            "SPREADSHEET_BUDGET_EXCEEDED",
            "A leitura do XLSX excedeu o tempo operacional.",
            details={"elapsed_seconds": round(elapsed, 2)},
        )
    return SpreadsheetDocument(
        format="xlsx",
        sheets=sheets,
        warnings=warnings,
        date_system=date_system,
        workbook_filename=path.name,
    )


def _present_cells(worksheet) -> list[tuple[tuple[int, int], Any]]:
    """Cells that exist in the XML, never the declared grid rectangle."""
    raw = getattr(worksheet, "_cells", None)
    if isinstance(raw, dict):
        return [((int(row), int(col)), cell) for (row, col), cell in raw.items()]
    # Fallback (future openpyxl): bounded iteration with a hard cell counter.
    collected: list[tuple[tuple[int, int], Any]] = []
    for row_cells in worksheet.iter_rows():
        for cell in row_cells:
            if cell is None or cell.value is None:
                continue
            collected.append(((int(cell.row), int(cell.column)), cell))
            if len(collected) > XLSX_MAX_CELLS:
                raise IngestionError(
                    "SPREADSHEET_BUDGET_EXCEEDED",
                    "A aba excede o orcamento de celulas.",
                    details={"limit": XLSX_MAX_CELLS},
                )
    return collected


def _merged_anchor_map(
    worksheet, sheet_name: str = "", warnings: list[dict[str, Any]] | None = None
) -> tuple[dict[tuple[int, int], str], set[tuple[int, int]]]:
    """Maps non-anchor coordinates of merged ranges to their anchor address.

    A range larger than ``XLSX_MAX_MERGED_CELLS`` is recorded by its anchor
    only (with a warning) so a crafted file cannot force a huge expansion.
    """
    lookup: dict[tuple[int, int], str] = {}
    anchors: set[tuple[int, int]] = set()
    try:
        ranges = list(worksheet.merged_cells.ranges)
    except Exception:  # pragma: no cover - read-only guards
        ranges = []
    for span in ranges:
        min_col, min_row, max_col, max_row = span.min_col, span.min_row, span.max_col, span.max_row
        anchor = _address(min_row, min_col)
        anchors.add((min_row, min_col))
        area = (max_row - min_row + 1) * (max_col - min_col + 1)
        if area > XLSX_MAX_MERGED_CELLS:
            if warnings is not None:
                warnings.append(
                    {
                        "code": "MERGED_RANGE_TOO_LARGE",
                        "message": (
                            f"Mescla {anchor}:{_address(max_row, max_col)} com {area} celulas "
                            "foi registrada apenas pela ancora."
                        ),
                        "sheet": sheet_name,
                        "area": area,
                    }
                )
            continue
        for row_index in range(min_row, max_row + 1):
            for column_index in range(min_col, max_col + 1):
                if (row_index, column_index) != (min_row, min_col):
                    lookup[(row_index, column_index)] = anchor
    return lookup, anchors


def _parse_cell(
    cell, row: int, column: int, merged_lookup: dict, merged_anchors: set[tuple[int, int]]
) -> SpreadsheetCell | None:
    address = _address(row, column)
    anchor = merged_lookup.get((row, column))
    is_anchor = (row, column) in merged_anchors
    raw = cell.value
    data_type = cell.data_type  # 's' | 'f' | 'n' | 'b' | 'e' | None
    if anchor is not None and raw is None:
        # non-anchor part of a merged range: reference only
        return SpreadsheetCell(
            row=row,
            column=column,
            cell_address=address,
            value=None,
            value_type="empty",
            original="",
            merged_in_anchor=anchor,
        )

    if raw is None:
        return SpreadsheetCell(
            row=row, column=column, cell_address=address, value=None,
            value_type="empty", original="", merged_in_anchor=anchor,
        )

    if data_type == "f" or isinstance(raw, str) and raw.startswith("="):
        return SpreadsheetCell(
            row=row,
            column=column,
            cell_address=address,
            value=None,  # never recalculate, never invent a cached result
            value_type="formula",
            original=str(raw),
            formula_text=str(raw),
            is_merged_anchor=is_anchor,
            merged_in_anchor=anchor,
        )

    if isinstance(raw, bool):
        return SpreadsheetCell(
            row=row, column=column, cell_address=address, value=raw,
            value_type="boolean", original="TRUE" if raw else "FALSE", merged_in_anchor=anchor,
        )

    if isinstance(raw, (datetime, date)):
        return SpreadsheetCell(
            row=row, column=column, cell_address=address, value=raw,
            value_type="date", original=raw.isoformat(), merged_in_anchor=anchor,
        )

    if isinstance(raw, (int, float)):
        return SpreadsheetCell(
            row=row, column=column, cell_address=address, value=raw,
            value_type="number", original=_number_original(cell, raw), merged_in_anchor=anchor,
        )

    text = str(raw)
    return SpreadsheetCell(
        row=row, column=column, cell_address=address, value=text,
        value_type="text", original=text, merged_in_anchor=anchor,
    )


def _number_original(cell, value) -> str:
    """Text as displayed by the stored format when determinable."""
    try:
        fmt = cell.number_format
    except Exception:  # pragma: no cover
        fmt = None
    if fmt in (None, "General"):
        return repr(value)
    return repr(value)


def _read_csv(path: Path, options: SpreadsheetOptions) -> SpreadsheetDocument:
    warnings: list[dict[str, Any]] = []
    data = path.read_bytes()
    encoding = options.csv_encoding
    if encoding:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise IngestionError(
                "CSV_ENCODING_REQUIRED",
                f"O conteúdo não decodifica como {encoding}.",
                details={"csv_encoding": encoding, "error": str(exc)},
            ) from exc
        except LookupError as exc:
            raise IngestionError(
                "CSV_ENCODING_REQUIRED",
                f"Encoding desconhecido: {encoding}.",
                details={"csv_encoding": encoding},
            ) from exc
        detected = encoding
    else:
        detected = None
        for candidate in CSV_DEFAULT_ENCODINGS:
            try:
                text = data.decode(candidate)
                detected = candidate
                break
            except UnicodeDecodeError:
                continue
        if detected is None:
            raise IngestionError(
                "CSV_ENCODING_REQUIRED",
                "O CSV não está em UTF-8. Declare spreadsheet.csv_encoding explicitamente.",
                details={"size_bytes": len(data)},
            )

    delimiter = options.csv_delimiter
    if not delimiter:
        sample = text[:CSV_SNIFF_SAMPLE_BYTES]
        counts = {candidate: sample.count(candidate) for candidate in CSV_DELIMITERS}
        best_count = max(counts.values())
        winners = [candidate for candidate, count in counts.items() if count == best_count]
        if best_count == 0:
            delimiter = ","
            warnings.append(
                {"code": "CSV_SINGLE_COLUMN", "message": "Nenhum delimitador; tratado como coluna única."}
            )
        elif len(winners) > 1:
            raise IngestionError(
                "CSV_DIALECT_REQUIRED",
                "Dialeto CSV ambíguo; declare spreadsheet.csv_delimiter.",
                details={"candidates": winners},
            )
        else:
            delimiter = winners[0]

    reader = csv_module.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[list[SpreadsheetCell | None]] = []
    physical_line = 1
    for record in _csv_records(reader):
        if not record:
            physical_line += 1
            continue
        cells: list[SpreadsheetCell | None] = []
        # csv.reader consumes embedded newlines; compute physical span by scanning
        row_lines = _physical_span(text, physical_line, record)
        for column_index, field_value in enumerate(record, start=1):
            cells.append(
                SpreadsheetCell(
                    row=len(rows) + 1,
                    column=column_index,
                    cell_address=_address(len(rows) + 1, column_index),
                    value=field_value,
                    value_type="text",
                    original=field_value,
                    physical_lines=row_lines,
                )
            )
        rows.append(cells)
        physical_line = row_lines[1] + 1

    max_column = max((len(row) for row in rows), default=1)
    name = path.stem or "dados"
    sheets = [SpreadsheetSheet(name=name, state="visible", rows=rows, max_column=max_column)]
    return SpreadsheetDocument(
        format="csv",
        sheets=sheets,
        warnings=warnings,
        dialect=delimiter,
        detected_encoding=detected,
        workbook_filename=path.name,
    )


def _csv_records(reader):
    """Surface parser failures (oversized field, bad dialect) as IngestionError."""
    try:
        yield from reader
    except csv_module.Error as exc:
        raise IngestionError(
            "CSV_PARSE_FAILED",
            f"O CSV não pôde ser lido: {exc}.",
            details={"error": str(exc), "field_size_limit": csv_module.field_size_limit()},
        ) from exc


def _physical_span(text: str, start_line: int, record: list[str]) -> tuple[int, int]:
    """Find the physical line range a logical record occupies."""
    if len(record) == 0:
        return (start_line, start_line)
    joined = delimiter_join(record)
    if joined.endswith("\r\n"):
        joined = joined[:-2]  # drop the writer's own line terminator
    lines_used = joined.count("\n") + 1
    return (start_line, start_line + lines_used - 1)


def delimiter_join(record: list[str]) -> str:
    # Re-join with a placeholder delimiter and quotes as csv.writer would;
    # counting newlines inside quoted fields is what matters.
    out = io.StringIO()
    writer = csv_module.writer(out, delimiter=",", quoting=csv_module.QUOTE_MINIMAL)
    writer.writerow(record)
    return out.getvalue()


def read_spreadsheet(path: Path, options: SpreadsheetOptions | None = None) -> SpreadsheetDocument:
    """Full read: probe budgets first, then parse values with provenance."""
    options = options or SpreadsheetOptions()
    probe = probe_spreadsheet(path, path.suffix.lstrip(".").lower(), options)
    if not probe.supported:
        raise IngestionError(
            probe.reason_code or "NOT_SPREADSHEET",
            probe.reason or "Arquivo não reconhecido como planilha.",
            details={"format": probe.format},
        )
    if probe.format == "xlsx":
        return _read_xlsx(path, options)
    return _read_csv(path, options)


__all__ = [
    "SPREADSHEET_READER_VERSION",
    "SpreadsheetCell",
    "SpreadsheetDocument",
    "SpreadsheetOptions",
    "SpreadsheetProbe",
    "SpreadsheetSheet",
    "probe_spreadsheet",
    "read_spreadsheet",
]
