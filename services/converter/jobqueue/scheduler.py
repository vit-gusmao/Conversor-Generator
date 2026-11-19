"""Bounded, durable scheduler.

One loop claims at most `slots` units at a time from the database backlog and
runs each on the shared pool, renewing its lease while it works. Nothing is
submitted to the pool "just in case": a document that cannot start yet stays
in the database instead of occupying memory.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from config import settings

from .repository import HEARTBEAT_SECONDS, JobLease, JobRepository

logger = logging.getLogger(__name__)


class DurableScheduler:
    def __init__(
        self,
        *,
        executor,
        repository: JobRepository | None = None,
        worker_id: str | None = None,
        slots: int | None = None,
        poll_seconds: float = 0.5,
        runner: Callable[..., None] | None = None,
    ) -> None:
        self.executor = executor
        self.repo = repository or JobRepository()
        self.worker_id = worker_id or f"worker-{threading.get_ident()}"
        self.slots = max(1, slots or settings.converter_workers)
        self.poll_seconds = poll_seconds
        self._runner = runner
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._in_flight = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pdf2x-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def wake(self) -> None:
        """Signals new work without waiting for the next poll."""
        self._wake.set()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    # -- loop --------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.dispatch_available()
            except Exception:  # noqa: BLE001 - the loop must never die
                logger.exception("scheduler dispatch failed")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def dispatch_available(self) -> int:
        """Claims and starts as many units as there are free slots."""
        claimed = 0
        while not self._stop.is_set():
            with self._lock:
                if self._in_flight >= self.slots:
                    break
            lease = self.repo.claim(self.worker_id)
            if lease is None:
                break
            with self._lock:
                self._in_flight += 1
            try:
                self.executor.submit(self._run_lease, lease)
            except RuntimeError:
                # Pool is shutting down: give the unit back.
                with self._lock:
                    self._in_flight -= 1
                self.repo.fail(lease.job_id, lease.fence, error="executor_shutdown", retry_in=None)
                break
            claimed += 1
        return claimed

    def _run_lease(self, lease: JobLease) -> None:
        stop_heartbeat = threading.Event()
        #: Set by the heartbeat when another owner holds the fence. The running
        #: unit reads it through its cancel check and stops before publishing.
        lease_lost = threading.Event()

        def _heartbeat() -> None:
            while not stop_heartbeat.wait(HEARTBEAT_SECONDS):
                try:
                    if not self.repo.heartbeat(lease.job_id, lease.fence):
                        logger.warning(
                            "lease lost for run %s (fence %s); the worker will stop publishing",
                            lease.run_id,
                            lease.fence,
                        )
                        lease_lost.set()
                        return
                except Exception:  # noqa: BLE001
                    logger.debug("heartbeat failed for job %s", lease.job_id)

        beat = threading.Thread(
            target=_heartbeat, name=f"pdf2x-hb-{lease.job_id}", daemon=True
        )
        beat.start()
        try:
            runner = self._runner or _default_runner
            runner(lease=lease, repo=self.repo, lease_lost=lease_lost)
        except Exception:  # noqa: BLE001
            logger.exception("job %s crashed outside the worker error handling", lease.job_id)
            try:
                self.repo.fail(lease.job_id, lease.fence, error="worker_crashed")
            except Exception:  # noqa: BLE001
                pass
        finally:
            stop_heartbeat.set()
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)
            self.wake()


def _default_runner(
    *,
    lease: JobLease,
    repo: JobRepository,
    lease_lost: threading.Event | None = None,
) -> None:
    """Runs the document unit through the shared worker entry point."""
    from worker.tasks import execute_run

    payload: dict[str, Any] = {}
    reference = lease.payload_ref or ""
    # `payload_ref` is either an inline JSON document or a storage key; the
    # inline form keeps the standalone path dependency-free.
    if reference.startswith("{"):
        try:
            payload = json.loads(reference)
        except ValueError:
            payload = {}
    elif reference:
        try:
            from storage import get_storage

            payload = json.loads(get_storage().get_bytes(reference).decode("utf-8"))
        except Exception:  # noqa: BLE001
            logger.warning("could not load payload for run %s", lease.run_id)
            payload = {}

    execute_run(
        run_id=lease.run_id,
        conversion_id=str(payload.get("conversion_id") or ""),
        storage_key=str(payload.get("storage_key") or ""),
        filename=str(payload.get("filename") or "documento.pdf"),
        options=payload.get("options") or {},
        cancel_check=lambda: _lease_is_lost(lease_lost) or repo.cancel_requested(lease.run_id),
        job_id=lease.job_id,
        fence=lease.fence,
        resume=int(lease.attempt) > 1,
    )


def _lease_is_lost(event: threading.Event | None) -> bool:
    return event is not None and event.is_set()


__all__ = ["DurableScheduler"]
