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
            # Legacy single-shot body limit. The resumable path has no
            # product ceiling on document bytes; `upload` describes it.
            "max_bytes": settings.max_bytes(),
            # `null`: the whole document is processed.
            "max_pages": settings.max_pages(),
            "max_image_frames": settings.converter_max_image_frames,
            "max_image_megapixels": settings.converter_max_image_megapixels,
            "max_job_megapixels": settings.converter_max_job_megapixels,
        },
        "upload": {
            "resumable": settings.batch_upload_enabled,
            "accepts_multiple": settings.batch_upload_enabled,
            "part_size_bytes": settings.upload_part_bytes,
            "max_part_bytes": settings.upload_part_bytes,
            "max_file_bytes": None,
            "max_files_per_batch": settings.max_files_per_batch,
        },
        "assistant": {
            "enabled": settings.assistant_enabled,
            "model": settings.assistant_model,
            "local": settings.assistant_host in {"127.0.0.1", "localhost", "::1"},
        },
        # Effective flags, not just the env var's existence.
        "features": {
            "source_imports": bool(getattr(settings, "source_imports_enabled", True)),
            "spreadsheet_input": bool(getattr(settings, "spreadsheet_input_enabled", True)),
            "automation_executor": bool(
                getattr(settings, "automation_executor_enabled", False)
            ),
            "desktop_host": bool(getattr(settings, "desktop_host_enabled", False)),
            "runtime_mode": settings.runtime_mode,
        },
    }


__all__ = ["capabilities_payload"]
