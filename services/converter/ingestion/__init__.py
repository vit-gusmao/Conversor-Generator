"""Generalized document ingestion (PDF handled by the engine; images here)."""

from ingestion.base import IngestedDocument, IngestedPage, IngestionError
from ingestion.image_input import (
    declared_mime,
    decode_image,
    detect_signature,
    image_capabilities,
    is_image,
)
from ingestion.registry import InputProbe, capabilities_report, probe_input

__all__ = [
    "IngestedDocument",
    "IngestedPage",
    "IngestionError",
    "declared_mime",
    "decode_image",
    "detect_signature",
    "image_capabilities",
    "is_image",
    "InputProbe",
    "capabilities_report",
    "probe_input",
]
