"""Celery task definitions (imported by the worker process)."""

from __future__ import annotations

from worker.celery_app import celery_app
from worker.tasks import execute_run


@celery_app.task(name="converter.execute_run", bind=True, max_retries=1)
def converter_execute_run(  # pragma: no cover - requires a broker
    self,
    run_id: str,
    conversion_id: str,
    storage_key: str,
    filename: str = "documento.pdf",
    options: dict | None = None,
) -> None:
    execute_run(
        run_id=run_id,
        conversion_id=conversion_id,
        storage_key=storage_key,
        filename=filename,
        options=options,
    )


__all__ = ["converter_execute_run"]
