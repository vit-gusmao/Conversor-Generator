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
    kind: str  # pdf | image | spreadsheet | unknown
    format: str | None
    mime_type: str | None
    supported: bool
    reason: str | None = None
    #: Precise machine code (e.g. CSV_ENCODING_REQUIRED) when the probe refuses.
    reason_code: str | None = None


ZIP_MAGIC = b"PK\x03\x04"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def spreadsheet_enabled() -> bool:
    from config import settings

    return bool(getattr(settings, "spreadsheet_input_enabled", True))


def probe_input(data: bytes) -> InputProbe:
    if sniff_is_pdf(data):
        return InputProbe(kind="pdf", format="PDF", mime_type="application/pdf", supported=True)
    signature = detect_signature(data)
    if signature is None:
        if data[:4] == ZIP_MAGIC and spreadsheet_enabled():
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
        reason_code=None if available else "CODEC_UNAVAILABLE",
    )


def _probe_xlsx_bytes(data: bytes) -> InputProbe:
    """ZIP magic found: only a package with OOXML markers is a spreadsheet."""
    import os
    import tempfile

    from ingestion.spreadsheet_input import IngestionError, probe_spreadsheet

    handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    try:
        handle.write(data)
        handle.close()
        try:
            probe = probe_spreadsheet(Path(handle.name), "xlsx", None)
        except IngestionError as exc:
            # Budget and corruption errors are answers about the file, not
            # failures of the request: they must not escape as a 500.
            return InputProbe(
                kind="spreadsheet",
                format="XLSX",
                mime_type=XLSX_MIME,
                supported=False,
                reason=exc.message,
                reason_code=exc.code,
            )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    return InputProbe(
        kind="spreadsheet",
        format="XLSX",
        mime_type=XLSX_MIME,
        supported=probe.supported,
        reason=probe.reason,
        reason_code=probe.reason_code,
    )


def _declared_suffix(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _looks_like_text(head: bytes) -> bool:
    """Plausible CSV content: no NULs, and almost every byte is printable."""
    sample = head[:4096]
    if not sample or b"\x00" in sample:
        return False
    printable = sum(
        1 for byte in sample if 0x20 <= byte < 0x7F or byte in (0x09, 0x0A, 0x0D) or byte >= 0x80
    )
    return printable / len(sample) >= 0.95


def _declared_csv_over_raster(head: bytes, filename: str | None) -> bool:
    """A file declared ``.csv`` whose content is text is CSV, even when its first
    bytes happen to look like a raster magic (``P1``, ``BM``)."""
    if _declared_suffix(filename) != "csv":
        return False
    signature = detect_signature(head)
    return signature is not None and _looks_like_text(head)


def probe_input_bytes(data: bytes, filename: str | None = None) -> InputProbe:
    """Full-content probe for the single-shot admission path.

    XLSX is decided by content (ZIP + OOXML markers). CSV has no magic bytes:
    the declared suffix admits a controlled dialect/encoding probe; anything
    else stays unknown.
    """
    csv_declared = _declared_csv_over_raster(data, filename)
    if not csv_declared:
        probe = probe_input(data)
        if probe.kind != "unknown":
            return probe
    suffix = _declared_suffix(filename)
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
                    reason_code=exc.code,
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
            reason_code=probe.reason_code,
        )
    return InputProbe(
        kind="unknown",
        format=None,
        mime_type=None,
        supported=False,
        reason="O arquivo não é um PDF, imagem ou planilha reconhecida.",
    )


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
    csv_declared = _declared_csv_over_raster(head, declared_name)
    signature = None if csv_declared else detect_signature(head)
    if signature is not None:
        available = _format_available(signature)
        return InputProbe(
            kind="image",
            format=signature,
            mime_type=declared_mime(signature),
            supported=available,
            reason=None if available else "CODEC_UNAVAILABLE",
            reason_code=None if available else "CODEC_UNAVAILABLE",
        )
    if head[:4] == ZIP_MAGIC and spreadsheet_enabled():
        from ingestion.spreadsheet_input import IngestionError, probe_spreadsheet

        try:
            probe = probe_spreadsheet(path, "xlsx", None)
        except Exception as exc:  # budget errors surface as unsupported
            return InputProbe(
                kind="spreadsheet",
                format="XLSX",
                mime_type=XLSX_MIME,
                supported=False,
                reason=str(getattr(exc, "message", exc)),
                reason_code=getattr(exc, "code", None) if isinstance(exc, IngestionError) else None,
            )
        return InputProbe(
            kind="spreadsheet",
            format="XLSX",
            mime_type=XLSX_MIME,
            supported=probe.supported,
            reason=probe.reason,
            reason_code=probe.reason_code,
        )
    if _declared_suffix(declared_name) == "csv" and spreadsheet_enabled():
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
                reason_code=exc.code,
            )
        return InputProbe(
            kind="spreadsheet",
            format="CSV",
            mime_type="text/csv",
            supported=probe.supported,
            reason=probe.reason,
            reason_code=probe.reason_code,
        )
    return InputProbe(
        kind="unknown",
        format=None,
        mime_type=None,
        supported=False,
        reason="O arquivo não é um PDF, imagem ou planilha reconhecida.",
    )


def _format_available(fmt: str) -> bool:
    """Availability of one format, matched by exact name.

    Group entries may name several formats with ``/`` (``HEIF/HEIC``).
    Matching is exact so ``JPEG2000`` is not judged by the ``JPEG`` group.
    """
    wanted = fmt.upper()
    for group in image_capabilities():
        names = {
            part.strip().upper()
            for item in group["formats"]
            for part in str(item).split("/")
        }
        if wanted in names:
            return bool(group["available"])
    return wanted in {"PNG", "JPEG", "BMP", "GIF", "TIFF", "WEBP", "ICO", "PNM", "JPEG2000"}


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
