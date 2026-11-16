"""Exporter registry.

The registry is the single place that maps a public format name to an
exporter.  Adding a format means adding an entry here — never a new route that
behaves differently per file suffix.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from exports.base import ExportError, ExportOptions, ExportResult
from exports.csv_writer import export_csv
from exports.text_plan import TextArtifactPlan
from exports.text_writer import export_text
from models.document import DocumentIR


@dataclass(slots=True)
class ExporterSpec:
    format: str
    mime_type: str
    extension: str
    description: str
    available: bool = True


EXPORT_SPECS: dict[str, ExporterSpec] = {
    "txt": ExporterSpec(
        format="txt",
        mime_type="text/plain; charset=utf-8",
        extension=".txt",
        description="Texto por ordem de leitura, com separadores de página explícitos.",
    ),
    "csv": ExporterSpec(
        format="csv",
        mime_type="text/csv; charset=utf-8",
        extension=".csv",
        description="Uma tabela por CSV; várias tabelas em pacote ZIP com manifesto.",
    ),
    "xlsx": ExporterSpec(
        format="xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extension=".xlsx",
        description="Planilha tipada com proveniência por célula.",
    ),
}


def export_formats() -> list[dict[str, object]]:
    return [
        {
            "format": spec.format,
            "mime_type": spec.mime_type,
            "extension": spec.extension,
            "description": spec.description,
            "available": spec.available,
        }
        for spec in EXPORT_SPECS.values()
    ]


def run_export(
    fmt: str,
    *,
    ir: DocumentIR,
    text_plan: TextArtifactPlan | None = None,
    options: ExportOptions | None = None,
) -> ExportResult:
    fmt = (fmt or "").lower()
    if fmt == "txt":
        if text_plan is None:
            raise ExportError("EXPORT_PLAN_MISSING", "Plano textual indisponível para o TXT.")
        merged = options or ExportOptions(format="txt")
        merged.format = "txt"
        return export_text(text_plan, merged)
    if fmt == "csv":
        merged = options or ExportOptions(format="csv")
        merged.format = "csv"
        return export_csv(ir, merged)
    raise ExportError(
        "UNSUPPORTED_FORMAT",
        f"Formato {fmt!r} não é produzido por este exportador.",
        details={"available": ["txt", "csv"]},
    )


Exporter = Callable[..., ExportResult]

__all__ = ["EXPORT_SPECS", "ExporterSpec", "export_formats", "run_export", "Exporter"]
