"""CSV exporter.

CSV represents one table, not the whole document.  The exporter therefore
requires explicitly known tables from the IR:

* one selected table  -> a single ``.csv``;
* several tables      -> a ``.zip`` with one CSV per table and ``manifest.json``;
* no reliable table   -> ``NO_TABLES_FOUND`` (the caller offers TXT instead).

It never turns arbitrary text lines into columns.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import Any

from exports.base import MIME_TYPES, ExportError, ExportOptions, ExportResult, validate_options
from models.document import DocumentIR, TableStructure

_UNSAFE = re.compile(r"[^\w.\-]+", re.UNICODE)
_NUMERIC = re.compile(r"^-?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$|^-?\d+(?:\.\d+)?$")
_PT_NUMBER = re.compile(r"^-?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$")
_DOT_THOUSANDS = re.compile(r"^-?\d{1,3}(?:\.\d{3})+$")
_FORMULA_START = {"=", "+", "-", "@"}


@dataclass(slots=True)
class CsvTable:
    table_id: str
    name: str
    rows: list[list[str]]

    @property
    def n_rows(self) -> int:
        return len(self.rows)


def export_csv(
    ir: DocumentIR,
    options: ExportOptions | None = None,
    *,
    filename_base: str | None = None,
) -> ExportResult:
    options = validate_options(options or ExportOptions(format="csv"))
    if options.format != "csv":
        raise ExportError("UNSUPPORTED_FORMAT", "Este exportador produz somente CSV.")

    tables = _select_tables(ir, options.table_ids)
    if not tables:
        raise ExportError(
            "NO_TABLES_FOUND",
            "Nenhuma tabela confiável foi identificada. Exporte como TXT para obter o texto do documento.",
            details={"available_formats": ["txt", "xlsx"]},
        )

    warnings: list[dict[str, Any]] = []
    prepared: list[tuple[CsvTable, list[list[str]], int]] = []
    for table in tables:
        rows, modified = _prepare_rows(table.rows, options)
        if modified:
            warnings.append(
                {
                    "code": "CSV_FORMULA_NEUTRALIZED",
                    "table_id": table.table_id,
                    "count": modified,
                    "message": (
                        f"{modified} campo(s) iniciados por caractere de fórmula foram "
                        "neutralizados com apóstrofo para abertura segura em planilhas."
                    ),
                }
            )
        prepared.append((table, rows, modified))

    base = _safe_base(filename_base or ir.file_name or "documento")
    if len(prepared) == 1:
        table, rows, _modified = prepared[0]
        data = _render_csv(rows, options)
        manifest = _manifest(ir, prepared, single=True)
        manifest["file"] = f"{base}.csv"
        manifest["sha256"] = hashlib.sha256(data).hexdigest()
        return ExportResult(
            format="csv",
            filename=f"{base}.csv",
            data=data,
            mime_type=MIME_TYPES["csv"],
            warnings=warnings,
            manifest=manifest,
            options=options.to_dict(),
        )

    buffer = io.BytesIO()
    used: set[str] = set()
    entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for index, (table, rows, _modified) in enumerate(prepared, start=1):
            filename = _unique_name(_table_slug(table, index), used) + ".csv"
            payload = _render_csv(rows, options)
            bundle.writestr(filename, payload)
            entries.append(
                {
                    "table_id": table.table_id,
                    "name": table.name,
                    "file": filename,
                    "rows": len(rows),
                    "columns": max((len(row) for row in rows), default=0),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )
        manifest = _manifest(ir, prepared, single=False)
        manifest["files"] = entries
        bundle.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    data = buffer.getvalue()
    return ExportResult(
        format="csv",
        filename=f"{base}_tabelas.zip",
        data=data,
        mime_type=MIME_TYPES["zip"],
        warnings=warnings,
        manifest=manifest,
        options=options.to_dict(),
    )


def _select_tables(ir: DocumentIR, table_ids: list[str] | None) -> list[CsvTable]:
    if not ir.tables:
        return []
    selected: list[TableStructure] = list(ir.tables)
    if table_ids:
        wanted = {str(value) for value in table_ids}
        selected = [
            table
            for index, table in enumerate(ir.tables)
            if table.table_id in wanted or str(index) in wanted
        ]
        if not selected:
            raise ExportError(
                "TABLE_NOT_FOUND",
                "Nenhuma das tabelas selecionadas pertence a esta conversão.",
                details={"requested": sorted(wanted)},
            )
    result: list[CsvTable] = []
    for index, table in enumerate(selected, start=1):
        rows: list[list[str]] = []
        if table.headers:
            rows.append([str(cell) if cell is not None else "" for cell in table.headers])
        for row in table.rows:
            rows.append([str(cell) if cell is not None else "" for cell in row])
        if not any(any(cell.strip() for cell in row) for row in rows):
            continue
        result.append(
            CsvTable(
                table_id=table.table_id,
                name=table.title or f"Tabela {index}",
                rows=rows,
            )
        )
    return result


def _prepare_rows(rows: list[list[str]], options: ExportOptions) -> tuple[list[list[str]], int]:
    modified = 0
    out: list[list[str]] = []
    for row in rows:
        prepared_row: list[str] = []
        for cell in row:
            value = cell
            if options.value_mode == "canonical":
                value = _canonical(value)
            if options.formula_policy == "safe" and _needs_neutralising(value):
                value = "'" + value
                modified += 1
            prepared_row.append(value)
        out.append(prepared_row)
    return out, modified


def _canonical(value: str) -> str:
    """Best-effort canonical number rendering; never changes free text.

    Only values that unambiguously parse as pt-BR/en numbers are normalised.
    Identifiers such as ``00123`` are left untouched.
    """
    raw = value.strip()
    if not raw or raw != value:
        return value
    if raw.startswith("0") and len(raw) > 1 and raw[1].isdigit():
        return value
    if "," in raw:
        # pt-BR: "." groups thousands, "," is the decimal mark.
        if _PT_NUMBER.match(raw):
            return raw.replace(".", "").replace(",", ".")
        return value
    if "." in raw and _DOT_THOUSANDS.match(raw):
        # Only dotted thousands groups ("1.234") lose their dots; a single
        # dot such as "3.14" or "0.5" is a decimal mark and is kept.
        return raw.replace(".", "")
    return value


def _needs_neutralising(value: str) -> bool:
    stripped = value.lstrip(" \t\r\n\x0b\x0c")
    if not stripped:
        return False
    first = stripped[0]
    if first in _FORMULA_START:
        # A negative numeric value is data, not a formula.
        if first == "-" and _NUMERIC.match(stripped):
            return False
        return True
    return unicodedata.category(first) == "Cc"


def _render_csv(rows: list[list[str]], options: ExportOptions) -> bytes:
    if options.csv_profile == "excel_br":
        encoding = "utf-8-sig"
        delimiter = options.csv_delimiter or ";"
    else:
        encoding = "utf-8"
        delimiter = options.csv_delimiter or ","
    if options.encoding.lower() in {"utf-8-sig", "utf8-sig"}:
        encoding = "utf-8-sig"
    elif options.encoding.lower() in {"utf-8", "utf8"} and options.csv_profile == "interoperable":
        encoding = "utf-8"

    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer,
        delimiter=delimiter,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
        doublequote=True,
    )
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode(encoding)


def _manifest(ir: DocumentIR, prepared, *, single: bool) -> dict[str, Any]:
    return {
        "format": "csv",
        "single_table": single,
        "source_sha256": ir.sha256,
        "tables": [
            {
                "table_id": table.table_id,
                "name": table.name,
                "rows": len(rows),
                "columns": max((len(row) for row in rows), default=0),
            }
            for table, rows, _modified in prepared
        ],
        "notes": [
            "CSV não preserva tipos nem formatação do XLSX.",
            "Identificadores numéricos com zeros iniciais podem ser reinterpretados pelo Excel; "
            "use XLSX quando a preservação for essencial.",
        ],
    }


def _table_slug(table: CsvTable, index: int) -> str:
    value = unicodedata.normalize("NFKD", table.name or "").encode("ascii", "ignore").decode()
    value = _UNSAFE.sub("_", value).strip("_ .").lower()
    return (value or f"tabela_{index:02d}")[:60]


def _unique_name(base: str, used: set[str]) -> str:
    candidate = base
    counter = 2
    while candidate.lower() in used:
        candidate = f"{base}_{counter}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _safe_base(name: str) -> str:
    base = (name or "documento").replace("\\", "/").split("/")[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    base = _UNSAFE.sub("_", base).strip(" .") or "documento"
    return base[:120]


__all__ = ["CsvTable", "export_csv"]
