"""Worker entry points shared by the inline pool and Celery.

`execute_run` is deliberately idempotent at the run level: it recreates the
working copy, re-runs the pipeline and replaces the persisted rows for that run.
Re-execution after a crash therefore converges instead of duplicating data.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from audit.events import EVT_RUN_CANCELLED, AuditRecorder
from config import settings
from engine.pipeline import ConversionError, ConversionPipeline, PipelineOptions
from engine.streaming.source import DocumentSource, head_bytes, sha256_file
from ingestion.registry import InputProbe, probe_input_path
from repositories.conversion_repository import ConversionRepository, LeaseLost, RunCancelled
from storage import get_storage
from version import ENGINE_VERSION

logger = logging.getLogger(__name__)

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off", ""})


def _flag(value: Any, default: bool = False) -> bool:
    """Reads a boolean option. A string such as "false" must stay False."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        return default
    return bool(value)


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
    job_id: int | None = None,
    fence: int | None = None,
    resume: bool = False,
) -> None:
    """Runs one document unit; the result is published only under the job fence.

    A worker that lost its lease gets `LeaseLost` from the publication step; the
    new owner's state is left untouched and nothing is raised to the caller.
    The materialized work copies are removed on every path, including early
    returns and failures.
    """
    try:
        _execute_run(
            run_id=run_id,
            conversion_id=conversion_id,
            storage_key=storage_key,
            filename=filename,
            options=options,
            cancel_check=cancel_check,
            job_id=job_id,
            fence=fence,
            resume=resume,
        )
    except LeaseLost:
        logger.warning("run %s: lease lost before publication; result discarded", run_id)
    finally:
        _remove_work_copies(run_id)


def _remove_work_copies(run_id: str) -> None:
    """Deletes `work/<run_id>.*`: our copies only, never the storage object."""
    work_dir = Path(os.environ.get("WORK_DIR") or (settings.data_dir / "work"))
    try:
        for leftover in work_dir.glob(glob.escape(run_id) + ".*"):
            leftover.unlink(missing_ok=True)
    except OSError:
        pass


def _execute_run(
    *,
    run_id: str,
    conversion_id: str,
    storage_key: str,
    filename: str,
    options: dict[str, Any] | None,
    cancel_check: Callable[[], bool] | None,
    job_id: int | None,
    fence: int | None,
    resume: bool,
) -> None:
    repo = ConversionRepository()
    logger.info(
        "run %s starting (job=%s fence=%s)", run_id, job_id if job_id is not None else "-", fence if fence is not None else "-"
    )
    repo.create_run(
        run_id=run_id,
        conversion_id=conversion_id,
        engine_version=ENGINE_VERSION,
        config=options or {},
    )
    recorder = AuditRecorder(run_id, sink=repo.progress_sink(run_id))
    work_dir = Path(os.environ.get("WORK_DIR") or (settings.data_dir / "work"))
    work_dir.mkdir(parents=True, exist_ok=True)

    storage = get_storage()
    try:
        # Materialize the document on disk and work from the path: the whole
        # file is never held in memory just to start a conversion.
        pdf_path = storage.materialize(storage_key, work_dir / f"{run_id}.source")
        size_bytes = pdf_path.stat().st_size
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
            job_id=job_id,
            fence=fence,
        )
        _notify_node(run_id, conversion_id, {"status": "failed", "message": "Falha de armazenamento"})
        return

    # Content-based detection, never the file name: a PNG named `.pdf` is an
    # image, and unknown bytes fail before any pipeline starts. Only the head
    # of the file is read here.
    probe = probe_input_path(pdf_path, filename=filename)
    if probe.kind == "unknown":
        repo.finish_run(
            run_id,
            status="failed",
            quality="failed",
            stats={},
            validation=None,
            error_code="INVALID_FILE",
            error_message="O arquivo não é um PDF nem uma imagem suportada.",
            job_id=job_id,
            fence=fence,
        )
        _notify_node(run_id, conversion_id, {"status": "failed", "message": "Arquivo inválido"})
        return
    suffix = _suffix_for(probe)
    if pdf_path.suffix.lower() != suffix:
        # Keep the parser's extension hint aligned with the real content. The
        # storage backend may hand back its own object: only a copy under
        # `work/` may be moved; a stored object is copied, never moved.
        renamed = work_dir / f"{run_id}{suffix}"
        if pdf_path.resolve().parent == work_dir.resolve():
            try:
                pdf_path.replace(renamed)
            except OSError:
                shutil.copyfile(pdf_path, renamed)
        else:
            shutil.copyfile(pdf_path, renamed)
        pdf_path = renamed

    source = DocumentSource(path=pdf_path, sha256=sha256_file(pdf_path), size_bytes=size_bytes)
    try:
        pipeline_options = _options_from(options, probe)
        pipeline_options.source_name = filename
        if probe.kind == "spreadsheet":
            from ingestion.spreadsheet_pipeline import SpreadsheetConversionPipeline

            pipeline = SpreadsheetConversionPipeline(
                spreadsheet_path=pdf_path,
                run_id=run_id,
                conversion_id=conversion_id,
                recorder=recorder,
                sink=repo.progress_sink(run_id),
                options=pipeline_options,
                cancel_check=cancel_check,
            )
        elif probe.kind == "image":
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
                source=source,
                resume=resume,
            )
        result = pipeline.run()
        repo.persist(result, recorder, job_id=job_id, fence=fence)
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
            job_id=job_id,
            fence=fence,
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
    except LeaseLost:
        # Not ours any more: the new owner publishes; no failure is recorded.
        raise
    except ConversionError as exc:
        repo.finish_run(
            run_id,
            status="failed",
            quality="failed",
            stats={},
            validation=None,
            error_code=exc.code,
            error_message=exc.message,
            job_id=job_id,
            fence=fence,
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
            job_id=job_id,
            fence=fence,
        )
        _notify_node(run_id, conversion_id, {"status": "failed", "message": str(exc)})
    finally:
        # Never delete an object the storage layer owns; only a materialized
        # copy under `work/` is ours to remove (see `_remove_work_copies`).
        try:
            if pdf_path.resolve().parent == work_dir.resolve():
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

    spreadsheet_section = raw.get("spreadsheet")
    spreadsheet_section = (
        spreadsheet_section if isinstance(spreadsheet_section, dict) else None
    )

    ocr = raw.get("ocr")
    if isinstance(ocr, dict):
        ocr_enabled = _flag(ocr.get("enabled", settings.converter_ocr_enabled))
        dpi = ocr.get("dpi") or settings.converter_render_dpi
    else:
        ocr_enabled = _flag(raw.get("ocr", raw.get("ocr_enabled", settings.converter_ocr_enabled)))
        dpi = raw.get("dpi") or settings.converter_render_dpi

    text = raw.get("text") if isinstance(raw.get("text"), dict) else {}
    csv_options = raw.get("csv") if isinstance(raw.get("csv"), dict) else {}

    return PipelineOptions(
        ocr_enabled=ocr_enabled,
        max_pages=None,  # no page ceiling: the pipeline processes the whole document
        dpi=dpi,
        target_formats=target,
        text_newline=str(text.get("newline") or raw.get("text_newline") or "lf"),
        page_separator=str(text.get("page_separator") or raw.get("page_separator") or "form_feed"),
        include_table_text=_flag(
            text.get("include_table_text", raw.get("include_table_text", True)), default=True
        ),
        csv_profile=str(csv_options.get("profile") or raw.get("csv_profile") or "interoperable"),
        csv_delimiter=csv_options.get("delimiter") or raw.get("csv_delimiter"),
        csv_table_ids=csv_options.get("table_ids") or raw.get("csv_table_ids"),
        csv_formula_policy=str(
            csv_options.get("formula_policy") or raw.get("csv_formula_policy") or "safe"
        ),
        csv_value_mode=str(csv_options.get("value_mode") or raw.get("csv_value_mode") or "original"),
        spreadsheet=spreadsheet_section,
    )


def _suffix_for(probe: InputProbe) -> str:
    if probe.kind == "spreadsheet":
        return ".csv" if (probe.format or "").upper() == "CSV" else ".xlsx"
    if probe.kind == "image":
        fmt = (probe.format or "img").lower()
        return {
            "jpeg": ".jpg",
            "jpeg2000": ".jp2",
            "tiff": ".tif",
        }.get(fmt, f".{fmt}")
    return ".pdf"


__all__ = ["execute_run"]
