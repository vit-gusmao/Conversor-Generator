"""Standalone calendar/executor loop (T8, doc 05 §9).

Started by the API lifespan only when ``AUTOMATION_EXECUTOR_ENABLED=true``.
The loop creates due occurrences, claims them with a lease, and hands the
advance step to the executor. A crash is recovered by leases: rows left
``running`` past their lease are re-claimed with a bumped fence.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from automation.executor import AutomationExecutor
from automation.scheduler import LEASE_SECONDS, AutomationScheduler
from automation.repository import AutomationRepository
from models.db import get_session_factory

logger = logging.getLogger("automation.loop")

RECOVERABLE_STATES = ("running", "waiting_children", "cancelling")


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for timezone-aware columns."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AutomationLoop:
    """Daemon loop; `stop()` joins it gracefully."""

    def __init__(self, *, poll_seconds: float = 5.0, worker_id: str | None = None) -> None:
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.worker_id = worker_id or "standalone"
        self.scheduler = AutomationScheduler(worker_id=self.worker_id)
        self.executor = AutomationExecutor()
        self.repository = AutomationRepository()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="automation-loop"
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    # ------------------------------------------------------------------- loop
    def _run(self) -> None:
        self.recover()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                logger.exception("automation tick failed")
            self._stop.wait(self.poll_seconds)

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        """One deterministic iteration: create due occurrences, claim, advance."""
        now = now or datetime.now(UTC)
        created = 0
        session = get_session_factory()()
        try:
            created = len(self.scheduler.create_due_occurrences(session, now=now))
            session.commit()
        finally:
            session.close()

        claimed = 0
        while True:
            session = get_session_factory()()
            try:
                lease = self.scheduler.claim_due(session, now=now, worker_id=self.worker_id)
                session.commit()
            finally:
                session.close()
            if lease is None:
                break
            claimed += 1
            self.executor.advance(lease.run_id)
        return {"created": created, "claimed": claimed}

    # --------------------------------------------------------------- recovery
    def recover(self) -> dict[str, int]:
        """Reconcile occurrences abandoned by a crash before dispatching new work."""
        session = get_session_factory()()
        recovered = 0
        try:
            rows = [
                run
                for run in session.query(_run_model()).all()
                if run.state in RECOVERABLE_STATES
                and (
                    run.lease_until is None
                    or _as_utc(run.lease_until) < datetime.now(UTC)
                )
            ]
            for run in rows:
                run.state = "queued"
                run.lease_owner = None
                run.lease_until = None
                run.fence = (run.fence or 0) + 1
                self.repository.append_event(
                    session, run_id=run.id, type="LEASE_RECOVERED", payload={"recovered": True}
                )
                recovered += 1
            session.commit()
        finally:
            session.close()
        try:
            from knowledge.job_runner import KnowledgeJobRunner

            jobs = len(KnowledgeJobRunner().recover())
        except Exception:  # noqa: BLE001
            jobs = 0
        if recovered or jobs:
            logger.info("automation recovery: %s occurrences, %s index jobs", recovered, jobs)
        return {"occurrences": recovered, "index_jobs": jobs}


def _run_model():
    from automation.models import AutomationRunRow

    return AutomationRunRow


__all__ = ["AutomationLoop", "LEASE_SECONDS"]
