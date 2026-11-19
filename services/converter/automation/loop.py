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

from sqlalchemy import select

from automation.executor import AutomationExecutor
from automation.scheduler import HEARTBEAT_SECONDS, LEASE_SECONDS, AutomationScheduler
from automation.repository import AutomationRepository
from models.db import get_session_factory

logger = logging.getLogger("automation.loop")

RECOVERABLE_STATES = ("running", "waiting_children", "cancelling")


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for timezone-aware columns."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _LeaseKeeper:
    """Renews one fenced claim while a long advance runs.

    The advance blocks the loop thread, so renewal happens on a side thread.
    A renewal rejected by the fence (the claim was reclaimed) stops the keeper;
    the stale attempt then cannot finalize anything because its fence no longer
    matches.
    """

    def __init__(
        self,
        scheduler: AutomationScheduler,
        run_id: str,
        fence: int,
        *,
        interval: float | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.run_id = run_id
        self.fence = fence
        self.interval = float(interval if interval is not None else HEARTBEAT_SECONDS)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def renew(self, *, now: datetime | None = None) -> bool:
        session = get_session_factory()()
        try:
            renewed = self.scheduler.heartbeat(session, self.run_id, self.fence, now=now)
            session.commit()
            return renewed
        finally:
            session.close()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if not self.renew():
                    return
            except Exception:  # noqa: BLE001 - a missed renewal is retried next tick
                logger.exception("lease renewal failed for %s", self.run_id)

    def __enter__(self) -> "_LeaseKeeper":
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"lease-{self.run_id[-8:]}"
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        return False


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
        self._last_cleanup = None
        self._jobs = None

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
        """One iteration: retention, due occurrences, claim, advance."""
        now = now or datetime.now(UTC)
        self._maybe_cleanup(now)
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
            # The fence travels with the claim: a reclaimed attempt bumps it
            # and the old worker can no longer finalize the run. The keeper
            # renews the lease while a long advance blocks this thread.
            with _LeaseKeeper(self.scheduler, lease.run_id, lease.fence):
                self.executor.advance(lease.run_id, fence=lease.fence)
        index_jobs = self._dispatch_index_jobs(now)
        return {"created": created, "claimed": claimed, "index_jobs": index_jobs}

    def _dispatch_index_jobs(self, now: datetime) -> int:
        """Execute index jobs left queued, or leased by a dead worker, by a crash."""
        from knowledge.job_runner import KnowledgeJobRunner

        try:
            if self._jobs is None:
                self._jobs = KnowledgeJobRunner()
            return self._jobs.run_pending(worker_id=self.worker_id, now=now)
        except Exception:  # noqa: BLE001 - index dispatch must not stop the calendar
            logger.exception("index job dispatch failed")
            return 0

    def _maybe_cleanup(self, now: datetime) -> None:
        """Retention runs at most once a day; never touches artifacts."""
        from automation.cleanup import run_cleanup

        current = now.date()
        if getattr(self, "_last_cleanup", None) == current:
            return
        try:
            result = run_cleanup(now=now)
            logger.info("automation cleanup: %s", result)
            self._last_cleanup = current
        except Exception:  # noqa: BLE001 - cleanup must not stop the calendar
            logger.exception("automation cleanup failed")

    # --------------------------------------------------------------- recovery
    def recover(self) -> dict[str, int]:
        """Reconcile occurrences abandoned by a crash before dispatching new work."""
        session = get_session_factory()()
        recovered = 0
        try:
            # Only non-terminal occurrences are read; the lease check stays in
            # Python because it is applied to a handful of rows (S17).
            now = datetime.now(UTC)
            run_model = _run_model()
            rows = [
                run
                for run in session.execute(
                    select(run_model).where(run_model.state.in_(RECOVERABLE_STATES))
                ).scalars()
                if run.lease_until is None or _as_utc(run.lease_until) < now
            ]
            for run in rows:
                # A cancellation requested before the crash is honoured: the
                # executor sees `cancelling` and finishes as `cancelled`.
                if run.state != "cancelling":
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
