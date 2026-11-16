"""Unified input probe.

The public API receives PDF or image bytes.  Detection is content-based: a
``.png`` extension on a PDF is a PDF, and a file with a valid image signature
that cannot be decoded is an explicit ``CODEC_UNAVAILABLE``.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def probe_input(data: bytes) -> InputProbe:
    if sniff_is_pdf(data):
        return InputProbe(kind="pdf", format="PDF", mime_type="application/pdf", supported=True)
    signature = detect_signature(data)
    if signature is None:
        return InputProbe(
            kind="unknown",
            format=None,
            mime_type=None,
            supported=False,
            reason="O arquivo não é um PDF nem uma imagem reconhecida.",
        )
    available = _format_available(signature)
    return InputProbe(
        kind="image",
        format=signature,
        mime_type=declared_mime(signature),
        supported=available,
        reason=None if available else "CODEC_UNAVAILABLE",
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
