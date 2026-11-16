"""Worker entry points shared by the inline pool and Celery.

`execute_run` is deliberately idempotent at the run level: it recreates the
working copy, re-runs the pipeline and replaces the persisted rows for that run.
Re-execution after a crash therefore converges instead of duplicating data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from audit.events import EVT_RUN_CANCELLED, AuditRecorder
from config import settings
from engine.pipeline import ConversionError, ConversionPipeline, PipelineOptions
from ingestion.registry import InputProbe, probe_input
from repositories.conversion_repository import ConversionRepository
from storage import get_storage
from version import ENGINE_VERSION

logger = logging.getLogger(__name__)


def _notify_node(run_id: str, conversion_id: str, payload: dict[str, Any]) -> None:
    """Best-effort push to the public API (avoids waiting for the poller)."""
    url = settings.converter_callback_url
    if not url:
        return
    try:
        import httpx

        httpx.post(
            url,
            json={"run_id": run_id, "conversion_id": conversion_id, **payload},
            headers={"x-internal-token": settings.converter_internal_token},
            timeout=3.0,
        )
    except Exception:  # noqa: BLE001 - notification is not durable state
        logger.debug("callback failed for run %s", run_id)


def execute_run(
    *,
    run_id: str,
    conversion_id: str,
    storage_key: str,
    filename: str = "documento.pdf",
    options: dict[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    repo = ConversionRepository()
    repo.create_run(
        run_id=run_id,
        conversion_id=conversion_id,
        engine_version=ENGINE_VERSION,
        config=options or {},
    )
    recorder = AuditRecorder(run_id, sink=repo.progress_sink(run_id))
    work_dir = settings.data_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        data = get_storage().get_bytes(storage_key)
    except Exception as exc:  # noqa: BLE001
        recorder.emit(
            "RUN_FAILED",
            "validate",
            "O arquivo original não pôde ser recuperado do armazenamento.",
            severity="error",
            payload={"error": str(exc), "storage_key": storage_key},
        )
        repo.finish_run(
            run_id,
            status="failed",
            quality="failed",
            stats={},
            validation=None,
            error_code="STORAGE_READ_FAILED",
            error_message="O arquivo original não pôde ser recuperado do armazenamento.",
        )
        _notify_node(run_id, conversion_id, {"status": "failed", "message": "Falha de armazenamento"})
        return

    # Content-based detection, never the file name: a PNG named `.pdf` is an
    # image, and unknown bytes fail before any pipeline starts.
    probe = probe_input(data)
    if probe.kind == "unknown":
        repo.finish_run(
            run_id,
            status="failed",
            quality="failed",
            stats={},
            validation=None,
            error_code="INVALID_FILE",
            error_message="O arquivo não é um PDF nem uma imagem suportada.",
        )
        _notify_node(run_id, conversion_id, {"status": "failed", "message": "Arquivo inválido"})
        return
    suffix = _suffix_for(probe)
    pdf_path = work_dir / f"{run_id}{suffix}"

    pdf_path.write_bytes(data)
    try:
        pipeline_options = _options_from(options, probe)
        pipeline_options.source_name = filename
        if probe.kind == "image":
            from ingestion.image_pipeline import ImageConversionPipeline

            pipeline = ImageConversionPipeline(
                image_path=pdf_path,
                run_id=run_id,
                conversion_id=conversion_id,
                recorder=recorder,
                sink=repo.progress_sink(run_id),
                options=pipeline_options,
                cancel_check=cancel_check,
            )
        else:
            pipeline = ConversionPipeline(
                pdf_path=pdf_path,
                run_id=run_id,
                conversion_id=conversion_id,
                recorder=recorder,
                sink=repo.progress_sink(run_id),
                options=pipeline_options,
                cancel_check=cancel_check,
            )
        result = pipeline.run()
        repo.persist(result, recorder)
        stats = result.stats
        status = result.status
        if result.error and result.error.code == "CANCELLED":
            status = "cancelled"
            recorder.emit(EVT_RUN_CANCELLED, "deliver", "Conversão cancelada pelo usuário.")
        repo.finish_run(
            run_id,
            status=status,
            quality=result.quality,
            stats=stats,
            validation=result.validation,
            error_code=result.error.code if result.error else None,
            error_message=result.error.message if result.error else None,
        )
        _notify_node(
            run_id,
            conversion_id,
            {
                "status": status,
                "quality": result.quality,
                "progress": 1.0,
                "stage": "done" if not result.error else "failed",
                "stats": stats,
                "validation": result.validation,
                "error": (
                    {"code": result.error.code, "message": result.error.message}
                    if result.error
                    else None
                ),
            },
        )
    except ConversionError as exc:
        repo.finish_run(
            run_id,
            status="failed",
            quality="failed",
            stats={},
            validation=None,
            error_code=exc.code,
            error_message=exc.message,
        )
        _notify_node(run_id, conversion_id, {"status": "failed", "message": exc.message})
    except Exception as exc:  # noqa: BLE001
        logger.exception("run %s crashed", run_id)
        repo.finish_run(
            run_id,
            status="failed",
            quality="failed",
            stats={},
            validation=None,
            error_code="INTERNAL_ERROR",
            error_message="Falha interna durante a conversão.",
        )
        _notify_node(run_id, conversion_id, {"status": "failed", "message": str(exc)})
    finally:
        try:
            pdf_path.unlink(missing_ok=True)
        except OSError:
            pass


def _options_from(raw: dict[str, Any] | None, probe: InputProbe | None = None) -> PipelineOptions:
    raw = raw or {}
    target = raw.get("target_formats")
    if isinstance(target, str):
        target = [item.strip() for item in target.split(",") if item.strip()]
    if not target:
        target = settings.target_formats()
    if probe is not None and probe.kind == "image":
        # The conversion matrix defines image -> TXT; other targets are refused
        # by the API, and the worker does not silently produce a fake XLSX.
        target = [item for item in target if item == "txt"] or ["txt"]

    ocr = raw.get("ocr")
    if isinstance(ocr, dict):
        ocr_enabled = bool(ocr.get("enabled", settings.converter_ocr_enabled))
        dpi = ocr.get("dpi") or settings.converter_render_dpi
    else:
        ocr_enabled = bool(raw.get("ocr", raw.get("ocr_enabled", settings.converter_ocr_enabled)))
        dpi = raw.get("dpi") or settings.converter_render_dpi

    text = raw.get("text") if isinstance(raw.get("text"), dict) else {}
    csv_options = raw.get("csv") if isinstance(raw.get("csv"), dict) else {}

    return PipelineOptions(
        ocr_enabled=ocr_enabled,
        max_pages=raw.get("max_pages") or settings.max_pages(),
        dpi=dpi,
        target_formats=target,
        text_newline=str(text.get("newline") or raw.get("text_newline") or "lf"),
        page_separator=str(text.get("page_separator") or raw.get("page_separator") or "form_feed"),
        csv_profile=str(csv_options.get("profile") or raw.get("csv_profile") or "interoperable"),
        csv_table_ids=csv_options.get("table_ids") or raw.get("csv_table_ids"),
        csv_formula_policy=str(
            csv_options.get("formula_policy") or raw.get("csv_formula_policy") or "safe"
        ),
    )


def _suffix_for(probe: InputProbe) -> str:
    if probe.kind == "image":
        fmt = (probe.format or "img").lower()
        return {
            "jpeg": ".jpg",
            "jpeg2000": ".jp2",
            "tiff": ".tif",
        }.get(fmt, f".{fmt}")
    return ".pdf"


__all__ = ["execute_run"]
