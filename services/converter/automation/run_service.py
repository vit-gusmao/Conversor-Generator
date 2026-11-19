"""Occurrence read model + cancellation (T7/T8).

Read paths are owner-scoped. Cancellation is durable: the request is stored on
the row and the executor cooperates at the next phase boundary — a result
already published stays in the history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from automation.models import AutomationEventRow, AutomationRunRow
from automation.repository import AutomationRepository

TERMINAL = ("succeeded", "partial", "no_changes", "failed", "cancelled", "skipped")


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
            if run.state != "cancelling":
                run.state = "cancelling"
                self.repository.append_event(
                    session, run_id=run.id, type="RUN_CANCEL_REQUESTED", payload={}
                )
                session.commit()
            return {"cancel_requested": True, "state": run.state}
        finally:
            session.close()

    def start_async(self, run_id: str, owner_id: str) -> None:
        """Advance the occurrence in a daemon thread (standalone executor)."""
        import threading

        thread = threading.Thread(
            target=self._advance_safely,
            args=(run_id, owner_id),
            daemon=True,
            name=f"automation-{run_id[:8]}",
        )
        thread.start()

    def _advance_safely(self, run_id: str, owner_id: str) -> None:
        from automation.executor import AutomationExecutor

        try:
            AutomationExecutor().advance(run_id, owner_id)
        except Exception as exc:  # noqa: BLE001 - never let a daemon die silently
            import traceback

            from models.db import get_session_factory

            session = get_session_factory()()
            try:
                run = session.get(AutomationRunRow, run_id)
                if run is not None and run.owner_id == owner_id and run.state not in TERMINAL:
                    run.state = "failed"
                    run.error_code = "EXECUTOR_INTERNAL_ERROR"
                    run.error_message = f"{exc}\n{traceback.format_exc(limit=6)}"
                    run.finished_at = datetime.now(UTC)
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
            "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
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
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }

    @staticmethod
    def event_payload(event: AutomationEventRow) -> dict[str, Any]:
        return {
            "run_id": event.run_id,
            "sequence": event.sequence,
            "type": event.type,
            "payload": event.payload_json,
            "created_at": event.created_at.isoformat(),
        }


__all__ = ["AutomationRunService", "TERMINAL"]
