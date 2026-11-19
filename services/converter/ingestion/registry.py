"""Unified input probe.

The public API receives PDF or image bytes.  Detection is content-based: a
``.png`` extension on a PDF is a PDF, and a file with a valid image signature
that cannot be decoded is an explicit ``CODEC_UNAVAILABLE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.inspector.pdf_inspector import sniff_is_pdf
from ingestion.image_input import (
    declared_mime,
    decode_image,
    detect_signature,
    image_capabilities,
    is_image,
)


@dataclass(slots=True)
class InputProbe:
    kind: str  # pdf | image
    format: str | None
    mime_type: str | None
    supported: bool
    reason: str | None = None


ZIP_MAGIC = b"PK\x03\x04"


def probe_input(data: bytes) -> InputProbe:
    if sniff_is_pdf(data):
        return InputProbe(kind="pdf", format="PDF", mime_type="application/pdf", supported=True)
    signature = detect_signature(data)
    if signature is None:
        if data[:4] == ZIP_MAGIC:
            return _probe_xlsx_bytes(data)
        return InputProbe(
            kind="unknown",
            format=None,
            mime_type=None,
            supported=False,
            reason="O arquivo não é um PDF, imagem ou planilha reconhecida.",
        )
    available = _format_available(signature)
    return InputProbe(
        kind="image",
        format=signature,
        mime_type=declared_mime(signature),
        supported=available,
        reason=None if available else "CODEC_UNAVAILABLE",
    )


def _probe_xlsx_bytes(data: bytes) -> InputProbe:
    """ZIP magic found: only a package with OOXML markers is a spreadsheet."""
    import os
    import tempfile

    from ingestion.spreadsheet_input import probe_spreadsheet

    handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    try:
        handle.write(data)
        handle.close()
        probe = probe_spreadsheet(Path(handle.name), "xlsx", None)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    return InputProbe(
        kind="spreadsheet",
        format="XLSX",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        supported=probe.supported,
        reason=probe.reason,
    )


def probe_input_bytes(data: bytes, filename: str | None = None) -> InputProbe:
    """Full-content probe for the single-shot admission path.

    XLSX is decided by content (ZIP + OOXML markers). CSV has no magic bytes:
    the declared suffix admits a controlled dialect/encoding probe; anything
    else stays unknown.
    """
    probe = probe_input(data)
    if probe.kind != "unknown":
        return probe
    suffix = filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    if suffix == "csv":
        import os
        import tempfile

        from ingestion.spreadsheet_input import IngestionError, probe_spreadsheet

        handle = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        try:
            handle.write(data)
            handle.close()
            try:
                probe = probe_spreadsheet(Path(handle.name), "csv", None)
            except IngestionError as exc:
                return InputProbe(
                    kind="spreadsheet",
                    format="CSV",
                    mime_type="text/csv",
                    supported=False,
                    reason=exc.message,
                )
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        return InputProbe(
            kind="spreadsheet",
            format="CSV",
            mime_type="text/csv",
            supported=probe.supported,
            reason=probe.reason,
        )
    return probe


def probe_input_path(path, filename: str | None = None) -> InputProbe:
    """Path-based probe for stored files (worker dispatch, upload completion).

    Uses the real file so ZIP central-directory inspection works — the first
    1 KiB of an XLSX does not carry the OOXML markers. CSV has no magic bytes,
    so the declared filename decides the CSV attempt; staging names like
    ``.part``/``.source`` never carry the real extension.
    """
    path = Path(path)
    declared_name = filename or path.name
    with open(path, "rb") as handle:
        head = handle.read(1024)
    if sniff_is_pdf(head):
        return InputProbe(kind="pdf", format="PDF", mime_type="application/pdf", supported=True)
    signature = detect_signature(head)
    if signature is not None:
        available = _format_available(signature)
        return InputProbe(
            kind="image",
            format=signature,
            mime_type=declared_mime(signature),
            supported=available,
            reason=None if available else "CODEC_UNAVAILABLE",
        )
    if head[:4] == ZIP_MAGIC:
        from ingestion.spreadsheet_input import probe_spreadsheet

        try:
            probe = probe_spreadsheet(path, "xlsx", None)
        except Exception as exc:  # budget errors surface as unsupported
            return InputProbe(
                kind="spreadsheet",
                format="XLSX",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                supported=False,
                reason=str(getattr(exc, "message", exc)),
            )
        return InputProbe(
            kind="spreadsheet",
            format="XLSX",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            supported=probe.supported,
            reason=probe.reason,
        )
    declared_suffix = declared_name.rsplit(".", 1)[-1].lower() if "." in declared_name else ""
    if declared_suffix == "csv":
        from ingestion.spreadsheet_input import IngestionError, probe_spreadsheet

        try:
            probe = probe_spreadsheet(path, "csv", None)
        except IngestionError as exc:
            return InputProbe(
                kind="spreadsheet",
                format="CSV",
                mime_type="text/csv",
                supported=False,
                reason=exc.message,
            )
        return InputProbe(
            kind="spreadsheet",
            format="CSV",
            mime_type="text/csv",
            supported=probe.supported,
            reason=probe.reason,
        )
    return InputProbe(
        kind="unknown",
        format=None,
        mime_type=None,
        supported=False,
        reason="O arquivo não é um PDF, imagem ou planilha reconhecida.",
    )


def _format_available(fmt: str) -> bool:
    for group in image_capabilities():
        formats = [str(item).upper() for item in group["formats"]]
        if fmt.upper() in formats or any(fmt.upper().startswith(item) for item in formats):
            return bool(group["available"])
    return fmt in {"PNG", "JPEG", "BMP", "GIF", "TIFF", "WEBP", "ICO", "PNM", "JPEG2000"}


def capabilities_report() -> dict[str, Any]:
    return {
        "inputs": [
            {
                "type": "application/pdf",
                "formats": ["PDF"],
                "outputs": ["xlsx", "txt", "csv"],
                "multipage": True,
                "available": True,
            },
            {
                "type": "spreadsheet",
                "formats": ["XLSX", "CSV"],
                "outputs": ["xlsx", "txt", "csv"],
                "multipage": False,
                "available": True,
            },
            *[
                {
                    "type": group["formats"],
                    "group": group["group"],
                    "outputs": group["outputs"],
                    "multipage": group["multipage"],
                    "available": group["available"],
                    "dependency": group.get("dependency"),
                    "notes": group.get("notes"),
                }
                for group in image_capabilities()
            ],
        ]
    }


__all__ = ["InputProbe", "probe_input", "capabilities_report", "is_image", "decode_image"]
