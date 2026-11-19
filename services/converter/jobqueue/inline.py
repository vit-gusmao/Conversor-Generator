"""In-process worker pool backed by the durable backlog (standalone mode).

`submit()` writes a row to `engine_jobs` and wakes the scheduler; it never
hands the task straight to the pool. A crash between "job accepted" and "job
started" therefore leaves a queued row that the next process picks up, instead
of losing the work.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from config import settings

from .base import JobQueue
from .repository import JobRepository
from .scheduler import DurableScheduler

logger = logging.getLogger(__name__)


class InlineQueue(JobQueue):
    name = "inline"

    def __init__(self, workers: int | None = None, timeout_seconds: int | None = None) -> None:
        self._slots = max(1, workers or settings.converter_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=self._slots,
            thread_name_prefix="pdf2x-worker",
        )
        #: 0 disables the whole-document timer. A document is no longer bounded
        #: by wall-clock time; individual OCR calls have their own timeout.
        self._timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.converter_task_timeout_seconds
        )
        self._cancelled: set[str] = set()
        self._timed_out: set[str] = set()
        self._lock = threading.Lock()
        self.repo = JobRepository()
        self._scheduler = DurableScheduler(
            executor=self._executor,
            repository=self.repo,
            slots=self._slots,
            runner=self._run_lease,
        )
        self._scheduler.start()

    # -- submission --------------------------------------------------------
    def submit(self, run_id: str, payload: dict) -> None:
        with self._lock:
            self._cancelled.discard(run_id)
            self._timed_out.discard(run_id)
        # The payload travels with the row: a restarted process can rebuild the
        # work without asking the API again.
        self.repo.enqueue_once(run_id, payload_ref=json.dumps(payload, ensure_ascii=False))
        self._scheduler.wake()

    # -- execution ---------------------------------------------------------
    def _run_lease(self, *, lease, repo: JobRepository, lease_lost: threading.Event | None = None) -> None:
        from worker.tasks import execute_run

        payload: dict = {}
        if lease.payload_ref:
            try:
                payload = json.loads(lease.payload_ref)
            except ValueError:
                payload = {}

        # The timer may fire at any moment, including just after the unit has
        # finished. `finished` is set under `gate` before the timer is cancelled,
        # so a late firing can no longer mark a completed unit as timed out.
        gate = threading.Lock()
        finished = False

        def _fire_timeout() -> None:
            with gate:
                if finished:
                    return
                self._on_timeout(lease.run_id)

        timer: threading.Timer | None = None
        if self._timeout and self._timeout > 0:
            timer = threading.Timer(max(1, self._timeout), _fire_timeout)
            timer.daemon = True
            timer.start()

        succeeded = False
        try:
            execute_run(
                run_id=lease.run_id,
                conversion_id=payload.get("conversion_id", ""),
                storage_key=payload.get("storage_key", ""),
                filename=payload.get("filename", "documento.pdf"),
                options=payload.get("options") or {},
                cancel_check=self._check(lease.run_id, lease_lost),
                job_id=lease.job_id,
                fence=lease.fence,
                # A re-claim (attempt > 1) is a redelivery: it may adopt the
                # checkpoints written by the attempt that died.
                resume=int(lease.attempt) > 1,
            )
            succeeded = True
        except Exception:  # noqa: BLE001 - the worker must never kill the pool
            logger.exception("inline worker crashed for run %s", lease.run_id)
        finally:
            with gate:
                finished = True
            if timer is not None:
                timer.cancel()
            with self._lock:
                timed_out = lease.run_id in self._timed_out
                self._timed_out.discard(lease.run_id)

            if succeeded and not timed_out:
                repo.complete(lease.job_id, lease.fence)
            elif timed_out:
                repo.fail(
                    lease.job_id,
                    lease.fence,
                    error=f"A conversão excedeu o tempo máximo de {self._timeout}s.",
                )
            else:
                # `execute_run` records its own failure state; the unit itself
                # is finished either way, so it is not retried automatically.
                repo.fail(lease.job_id, lease.fence, error="worker_exception")

    def _on_timeout(self, run_id: str) -> None:
        with self._lock:
            self._timed_out.add(run_id)
            self._cancelled.add(run_id)
        logger.warning(
            "run %s exceeded the %ss time limit and was asked to stop", run_id, self._timeout
        )

    def _check(self, run_id: str, lease_lost: threading.Event | None = None):
        """Cancellation gate handed to the pipeline.

        The in-process flags cover the local case; the durable request is read
        too, so a cancellation issued by another process is observed between
        pages as well. A lost lease also stops the unit: another owner holds it.
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
            if cancelled or (lease_lost is not None and lease_lost.is_set()):
                return True
            try:
                return self.repo.cancel_requested(run_id)
            except Exception:  # noqa: BLE001 - a DB hiccup must not abort a run
                return False

        return check

    # -- control -----------------------------------------------------------
    def cancel(self, run_id: str) -> bool:
        with self._lock:
            self._cancelled.add(run_id)
        try:
            self.repo.cancel_run(run_id)
        except Exception:  # noqa: BLE001
            logger.debug("could not persist the cancellation of %s", run_id)
        return True

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._cancelled:
                return True
        try:
            return self.repo.cancel_requested(run_id)
        except Exception:  # noqa: BLE001
            return False

    def health(self) -> str:
        try:
            self.repo.backlog()
            return "up"
        except Exception:  # noqa: BLE001
            return "degraded"

    def backlog(self) -> dict[str, int]:
        return self.repo.backlog()

    def shutdown(self) -> None:
        self._scheduler.stop(timeout=2.0)
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["InlineQueue"]
