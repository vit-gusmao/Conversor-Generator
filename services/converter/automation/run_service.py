"""Occurrence read model + cancellation (T7/T8).

Read paths are owner-scoped. Cancellation is durable: the request is stored on
the row and the executor cooperates at the next phase boundary — a result
already published stays in the history.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.serialization import iso_utc
from automation.models import AutomationEventRow, AutomationRunRow
from automation.repository import AutomationRepository
from automation.types import FeatureDisabled
from config import settings

TERMINAL = ("succeeded", "partial", "no_changes", "failed", "cancelled", "skipped")
#: Worker identity recorded on claims made by run-now requests.
RUN_NOW_WORKER = "api-run-now"

logger = logging.getLogger("automation.run_service")


def executor_enabled() -> bool:
    """True when this process may execute occurrences (AUTOMATION_EXECUTOR_ENABLED)."""
    return bool(getattr(settings, "automation_executor_enabled", False))


class AutomationRunService:
    def __init__(self) -> None:
        self.repository = AutomationRepository()

    # ------------------------------------------------------------------ reads
    def list_runs(self, session: Session, *, owner_id: str, schedule_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.repository.list_runs(
            session, owner_id=owner_id, schedule_id=schedule_id, limit=limit
        )
        return [self.payload(row) for row in rows]

    def get_run(self, session: Session, *, owner_id: str, run_id: str) -> dict[str, Any] | None:
        run = self.repository.get_run(session, run_id=run_id, owner_id=owner_id)
        return self.payload(run) if run else None

    def events(self, session: Session, *, run_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.repository.events_since(session, run_id=run_id, after=after, limit=limit)
        return [self.event_payload(row) for row in rows]

    def events_owner(self, run_id: str, owner_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        from models.db import get_session_factory

        session = get_session_factory()()
        try:
            run = self.repository.get_run(session, run_id=run_id, owner_id=owner_id)
            if run is None:
                return []
            return self.events(session, run_id=run_id, after=after)
        finally:
            session.close()

    # ------------------------------------------------------------ cancellation
    def request_cancel(self, run_id: str, owner_id: str) -> dict[str, Any]:
        from models.db import get_session_factory

        session = get_session_factory()()
        try:
            run = self.repository.get_run(session, run_id=run_id, owner_id=owner_id)
            if run is None:
                from api.deps import ApiError

                raise ApiError(404, "RUN_NOT_FOUND", "Ocorrência inexistente ou de outro dono.")
            if run.state in TERMINAL:
                # Idempotent: cancelling a finished run keeps the real result.
                return {"cancel_requested": False, "state": run.state}
            if run.cancel_requested_at is None:
                # Durable request: survives restarts and is checked by the
                # executor at every phase boundary.
                run.cancel_requested_at = datetime.now(UTC)
                run.state = "cancelling" if run.state != "queued" else "cancelling"
                self.repository.append_event(
                    session, run_id=run.id, type="RUN_CANCEL_REQUESTED", payload={}
                )
                if run.import_id:
                    from models.db import SourceImportRow

                    record = session.get(SourceImportRow, run.import_id)
                    if record is not None:
                        record.cancel_requested_at = datetime.now(UTC)
                session.commit()
            return {"cancel_requested": True, "state": run.state}
        finally:
            session.close()

    def start_async(self, run_id: str, owner_id: str) -> bool:
        """Claim the occurrence, then advance it in a daemon thread.

        The claim happens here, before the thread starts, under the same fence
        rules as the loop. A second trigger, or the loop, then sees a live lease
        and cannot advance the same occurrence twice (AU1-13).

        Returns True when this call claimed and started the attempt, False when
        the occurrence was already claimed, finished, or is not this owner's.
        Raises ``FeatureDisabled`` when the executor flag is off.
        """
        if not executor_enabled():
            raise FeatureDisabled(
                "Execução de agendamentos está desativada neste processo.",
            )

        from automation.scheduler import AutomationScheduler
        from models.db import get_session_factory

        scheduler = AutomationScheduler(worker_id=RUN_NOW_WORKER)
        session = get_session_factory()()
        try:
            lease = scheduler.claim_run(session, run_id=run_id, owner_id=owner_id)
            session.commit()
        finally:
            session.close()
        if lease is None:
            return False

        thread = threading.Thread(
            target=self._advance_leased,
            args=(run_id, owner_id, lease.fence),
            daemon=True,
            name=f"automation-{run_id[:8]}",
        )
        thread.start()
        return True

    def _advance_leased(self, run_id: str, owner_id: str, fence: int) -> None:
        """Advance one claimed attempt while renewing its lease."""
        from automation.executor import AutomationExecutor
        from automation.loop import _LeaseKeeper
        from automation.scheduler import AutomationScheduler

        scheduler = AutomationScheduler(worker_id=RUN_NOW_WORKER)
        try:
            with _LeaseKeeper(scheduler, run_id, fence):
                AutomationExecutor().advance(run_id, owner_id, fence=fence)
        except Exception as exc:  # noqa: BLE001 - never let a daemon die silently
            logger.exception("run-now attempt %s crashed", run_id)
            self._fail_leased(run_id, owner_id, fence, exc)

    @staticmethod
    def _fail_leased(run_id: str, owner_id: str, fence: int, exc: BaseException) -> None:
        """Close a crashed attempt, but only if this attempt still owns the fence."""
        import traceback

        from models.db import get_session_factory

        session = get_session_factory()()
        try:
            run = session.get(AutomationRunRow, run_id)
            if (
                run is not None
                and run.owner_id == owner_id
                and run.fence == fence
                and run.state not in TERMINAL
            ):
                run.state = "failed"
                run.error_code = "EXECUTOR_INTERNAL_ERROR"
                run.error_message = f"{exc}\n{traceback.format_exc(limit=6)}"
                run.finished_at = datetime.now(UTC)
                run.lease_until = None
                run.lease_owner = None
                session.commit()
        finally:
            session.close()

    # ---------------------------------------------------------------- payload
    @staticmethod
    def payload(run: AutomationRunRow) -> dict[str, Any]:
        return {
            "id": run.id,
            "owner_id": run.owner_id,
            "schedule_id": run.schedule_id,
            "schedule_revision": run.schedule_revision,
            "scheduled_for": iso_utc(run.scheduled_for),
            "trigger": run.trigger,
            "occurrence_key": run.occurrence_key,
            "state": run.state,
            "phase": run.phase,
            "import_id": run.import_id,
            "index_job_id": run.index_job_id,
            "attempt": run.attempt,
            "fence": run.fence,
            "error": (
                {"code": run.error_code, "message": run.error_message}
                if run.error_code
                else None
            ),
            "snapshot": run.snapshot_json,
            "created_at": iso_utc(run.created_at),
            "updated_at": iso_utc(run.updated_at),
            "started_at": iso_utc(run.started_at),
            "finished_at": iso_utc(run.finished_at),
        }

    @staticmethod
    def event_payload(event: AutomationEventRow) -> dict[str, Any]:
        return {
            "run_id": event.run_id,
            "sequence": event.sequence,
            "type": event.type,
            "payload": event.payload_json,
            "created_at": iso_utc(event.created_at),
        }


__all__ = ["AutomationRunService", "TERMINAL"]
