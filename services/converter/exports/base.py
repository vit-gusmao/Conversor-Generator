"""Export contracts shared by every output format.

An *exporter* receives an immutable plan (IR-derived) plus validated options and
returns bytes written to a private staging buffer.  Publication is a separate
step owned by the artifact service, so an exporter can never make a file
downloadable by itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


class ExportError(Exception):
    """Stable, user-facing export failure.

    ``code`` is part of the public contract (``NO_TABLES_FOUND``,
    ``UNSUPPORTED_FORMAT``...) and must not be replaced by a generic message.
    """

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(slots=True)
class ExportOptions:
    format: str = "txt"
    # TXT
    encoding: str = "utf-8"
    newline: str = "lf"  # lf | crlf
    page_separator: str = "form_feed"  # form_feed | marker | none
    include_table_text: bool = True
    # CSV
    csv_profile: str = "interoperable"  # interoperable | excel_br
    csv_delimiter: str | None = None
    formula_policy: str = "safe"  # safe | raw
    table_ids: list[str] | None = None
    value_mode: str = "original"  # original | canonical

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "encoding": self.encoding,
            "newline": self.newline,
            "page_separator": self.page_separator,
            "include_table_text": self.include_table_text,
            "csv_profile": self.csv_profile,
            "csv_delimiter": self.csv_delimiter,
            "formula_policy": self.formula_policy,
            "table_ids": self.table_ids,
            "value_mode": self.value_mode,
        }

    @property
    def options_hash(self) -> str:
        payload = repr(sorted(self.to_dict().items())).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


@dataclass(slots=True)
class ExportResult:
    format: str
    filename: str
    data: bytes
    mime_type: str
    warnings: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def summary(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "warnings": self.warnings,
            "options": self.options,
            "manifest": self.manifest,
        }


MIME_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "zip": "application/zip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def validate_options(options: ExportOptions) -> ExportOptions:
    """Reject values that would otherwise be silently ignored."""
    fmt = (options.format or "").lower()
    if fmt not in {"txt", "csv"}:
        raise ExportError("UNSUPPORTED_FORMAT", f"Formato de exportação não suportado: {fmt!r}.")
    if options.newline not in {"lf", "crlf"}:
        raise ExportError("INVALID_OPTION", "newline deve ser 'lf' ou 'crlf'.")
    if options.page_separator not in {"form_feed", "marker", "none"}:
        raise ExportError(
            "INVALID_OPTION", "page_separator deve ser 'form_feed', 'marker' ou 'none'."
        )
    if options.encoding.lower() not in {"utf-8", "utf8", "utf-8-sig"}:
        raise ExportError("INVALID_OPTION", "encoding suportado: utf-8 (TXT) ou utf-8-sig (CSV).")
    if options.csv_profile not in {"interoperable", "excel_br"}:
        raise ExportError("INVALID_OPTION", "csv_profile deve ser 'interoperable' ou 'excel_br'.")
    if options.formula_policy not in {"safe", "raw"}:
        raise ExportError("INVALID_OPTION", "formula_policy deve ser 'safe' ou 'raw'.")
    if options.value_mode not in {"original", "canonical"}:
        raise ExportError("INVALID_OPTION", "value_mode deve ser 'original' ou 'canonical'.")
    if options.csv_delimiter is not None and (
        len(options.csv_delimiter) != 1 or options.csv_delimiter in {'"', "\r", "\n"}
    ):
        raise ExportError("INVALID_OPTION", "csv_delimiter deve ser um único caractere válido.")
    return options


__all__ = [
    "ExportError",
    "ExportOptions",
    "ExportResult",
    "MIME_TYPES",
    "validate_options",
]
