"""In-process worker pool (standalone mode)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from config import settings

from .base import JobQueue

logger = logging.getLogger(__name__)


class InlineQueue(JobQueue):
    name = "inline"

    def __init__(self, workers: int | None = None, timeout_seconds: int | None = None) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers or settings.converter_workers),
            thread_name_prefix="pdf2x-worker",
        )
        self._timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.converter_task_timeout_seconds
        )
        self._cancelled: set[str] = set()
        self._timed_out: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, run_id: str, payload: dict) -> None:
        with self._lock:
            self._cancelled.discard(run_id)
            self._timed_out.discard(run_id)
        self._executor.submit(self._run, run_id, payload)

    def _run(self, run_id: str, payload: dict) -> None:
        from worker.tasks import execute_run

        timer = threading.Timer(max(1, self._timeout), self._on_timeout, args=(run_id,))
        timer.daemon = True
        timer.start()
        try:
            execute_run(
                run_id=run_id,
                conversion_id=payload.get("conversion_id", ""),
                storage_key=payload.get("storage_key", ""),
                filename=payload.get("filename", "documento.pdf"),
                options=payload.get("options") or {},
                cancel_check=self._check(run_id),
            )
        except Exception:  # noqa: BLE001 - the worker must never kill the pool
            logger.exception("inline worker crashed for run %s", run_id)
        finally:
            timer.cancel()
            with self._lock:
                self._timed_out.discard(run_id)

    def _on_timeout(self, run_id: str) -> None:
        with self._lock:
            self._timed_out.add(run_id)
            self._cancelled.add(run_id)
        logger.warning(
            "run %s exceeded the %ss time limit and was asked to stop", run_id, self._timeout
        )

    def _check(self, run_id: str):
        """Cancellation gate handed to the pipeline.

        Raises a ``TIME_LIMIT`` conversion error (instead of a plain cancel) so
        the run is recorded as a failure that ran out of time, not as something
        the user asked to stop.  The check runs between pages: a single blocking
        native call inside a page cannot be interrupted without process
        isolation, which this in-process queue does not provide.
        """

        def check() -> bool:
            from engine.pipeline import ConversionError

            with self._lock:
                timed_out = run_id in self._timed_out
                cancelled = run_id in self._cancelled
            if timed_out:
                raise ConversionError(
                    "TIME_LIMIT",
                    f"A conversão excedeu o tempo máximo de {self._timeout}s.",
                    retryable=True,
                )
            return cancelled

        return check

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            self._cancelled.add(run_id)
        return True

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled

    def health(self) -> str:
        return "up"

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["InlineQueue"]
