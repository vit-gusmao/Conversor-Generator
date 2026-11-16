"""Capability report shared by the public and internal APIs."""

from __future__ import annotations

from typing import Any

from config import settings
from engine.ocr.engines import ocr_capability
from exports.registry import export_formats
from ingestion.registry import capabilities_report


def capabilities_payload() -> dict[str, Any]:
    capability = ocr_capability()
    return {
        **capabilities_report(),
        "outputs": export_formats(),
        "ocr": {
            "available": bool(capability.get("available")),
            "selected": capability.get("selected"),
            "engines": capability.get("engines"),
        },
        "limits": {
            "max_bytes": settings.max_bytes(),
            "max_pages": settings.max_pages(),
            "max_image_frames": settings.converter_max_image_frames,
            "max_image_megapixels": settings.converter_max_image_megapixels,
            "max_job_megapixels": settings.converter_max_job_megapixels,
        },
        "assistant": {
            "enabled": settings.assistant_enabled,
            "model": settings.assistant_model,
            "local": settings.assistant_host in {"127.0.0.1", "localhost", "::1"},
        },
    }


__all__ = ["capabilities_payload"]
