"""Resumable occurrence executor (T8, doc 05 §6/§8).

Phases are small and durable: ``scan → snapshot → submit → convert → index →
finalize``. Every step re-reads the frozen snapshot, so an edit made after the
occurrence was created never changes which files that run processes; revoking
the root still blocks reads. A crash at any boundary resumes from the persisted
``import_id`` and stable client keys — never a duplicate batch or conversion.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from automation.models import AutomationRunRow
from automation.repository import AutomationRepository
from automation.types import RunProgress
from models.db import get_session_factory

TERMINAL_STATES = ("succeeded", "partial", "no_changes", "failed", "cancelled", "skipped")
RETRY_DELAYS = (30, 120, 300)
CONVERT_WAIT_SECONDS = 120


class AutomationExecutor:
    def __init__(self, *, submission=None) -> None:
        self.repository = AutomationRepository()
        self._submission = submission

    # ------------------------------------------------------------------ entry
    def advance(
        self, run_id: str, owner_id: str | None = None, *, fence: int | None = None
    ) -> RunProgress:
        """Advance one occurrence as far as it can go right now.

        ``fence`` is the token received with the claim: a stale worker (whose
        lease expired and was reclaimed) cannot finalize the new attempt.
        """
        self._fence = fence
        run, owner = self._load(run_id, owner_id)
        if run is None:
            return RunProgress(run_id=run_id, state="failed", phase="finalize", done=True,
                               detail={"code": "RUN_NOT_FOUND"})
        if run.state in TERMINAL_STATES:
            return RunProgress(run_id=run_id, state=run.state, phase=run.phase or "finalize", done=True)

        if run.state == "skipped":
            return RunProgress(run_id=run_id, state="skipped", phase="finalize", done=True)

        if self._cancel_requested(run_id):
            self._finish(run_id, "cancelled", {})
            return RunProgress(
                run_id=run_id, state="cancelled", phase=run.phase or "scan", done=True
            )

        snapshot = run.snapshot_json or {}
        try:
            import_record = self._ensure_import(run, owner, snapshot)
            if import_record is None:
                return RunProgress(run_id=run_id, state="failed", phase=run.phase, done=True)
            import_id = import_record["id"]

            # Delegate the heavy lifting to the durable importer; phases inside
            # it are the same ones this executor reports.
            from sources.importer import ImportService

            importer = ImportService(submission=self._submission_for(owner))
            result = importer.run(import_id, owner)

            self._set_run_fields(run_id, import_id=import_id)
            state = self._state_from_import(result)
            detail = {"import_id": import_id, "counts": result.get("counts")}

            if state == "succeeded":
                index_detail = self._index_if_requested(run_id, owner, snapshot, result)
                if index_detail:
                    detail["index"] = index_detail
                    if index_detail.get("status") == "queued":
                        # Indexing runs as a durable job; the occurrence waits
                        # for it instead of declaring itself complete early.
                        self._set_run_fields(run_id, state="waiting_children")
                        return RunProgress(
                            run_id=run_id,
                            state="waiting_children",
                            phase="index",
                            done=False,
                            detail=detail,
                        )
                    if index_detail.get("status") not in {"indexed", "skipped", None}:
                        state = "partial"
            self._finish(run_id, state, detail)
            return RunProgress(run_id=run_id, state=state, phase="finalize", done=True, detail=detail)
        except Exception as exc:  # noqa: BLE001 - terminal with diagnostics
            import traceback

            from api.deps import ApiError

            if isinstance(exc, ApiError):
                envelope = (
                    (exc.detail or {}).get("error", {}) if isinstance(exc.detail, dict) else {}
                )
                code = envelope.get("code", "IMPORT_FAILED")
                detail_message = envelope.get("message", str(exc))
                self._finish(run_id, "failed", {"code": code, "message": detail_message})
                return RunProgress(
                    run_id=run_id,
                    state="failed",
                    phase="finalize",
                    done=True,
                    detail={"code": code, "message": detail_message},
                )
            message = f"{exc}\n{traceback.format_exc(limit=6)}"
            self._finish(run_id, "failed", {"code": "EXECUTOR_ERROR", "message": message})
            return RunProgress(
                run_id=run_id, state="failed", phase="finalize", done=True,
                detail={"code": "EXECUTOR_ERROR", "message": str(exc)},
            )

    # -------------------------------------------------------------- internals
    def _load(self, run_id: str, owner_id: str | None):
        session = get_session_factory()()
        try:
            run = session.get(AutomationRunRow, run_id)
            if run is None or (owner_id is not None and run.owner_id != owner_id):
                return None, None
            return run, run.owner_id
        finally:
            session.close()

    def _cancel_requested(self, run_id: str) -> bool:
        session = get_session_factory()()
        try:
            run = session.get(AutomationRunRow, run_id)
            return bool(
                run is not None
                and (run.cancel_requested_at is not None or run.state == "cancelling")
            )
        finally:
            session.close()

    @property
    def fence(self) -> int | None:
        return getattr(self, "_fence", None)

    def _fence_valid(self, session, run: AutomationRunRow) -> bool:
        """A stale worker (reclaimed lease) must not write results."""
        expected = self.fence
        if expected is None:
            return True
        if run.fence != expected:
            return False
        if run.lease_until is not None:
            lease = run.lease_until
            now = datetime.now(UTC)
            if lease.tzinfo is None:
                lease = lease.replace(tzinfo=UTC)
            if lease < now:
                return False
        return True

    def _ensure_import(self, run: AutomationRunRow, owner_id: str, snapshot: dict[str, Any]):
        """Idempotent import creation from the frozen snapshot."""
        if run.import_id:
            from sources.importer import ImportService

            existing = ImportService().get(run.import_id, owner_id)
            if existing.get("status") not in TERMINAL_STATES:
                return existing
            if existing.get("status") in {"completed", "partial", "failed", "cancelled"}:
                return existing

        source_id = snapshot.get("source_id")
        if not source_id:
            self._finish(run.id, "failed", {"code": "SNAPSHOT_INCOMPLETE", "message": "source_id ausente."})
            return None
        from sources.importer import ImportService
        from sources.types import SourceDefinitionSnapshot

        service = ImportService(submission=self._submission_for(owner_id))
        frozen = snapshot.get("source_definition")
        if frozen:
            # Frozen definition: later edits to the source must not change
            # which files this occurrence processes.
            record = service.start_from_snapshot(
                owner_id=owner_id,
                definition=SourceDefinitionSnapshot.from_json(frozen),
                request={
                    "options": snapshot.get("options") or {},
                    "collection_id": snapshot.get("index_collection_id"),
                },
                idempotency_key=f"schedule-run:{run.id}",
                schedule_run_id=run.id,
            )
        else:
            record = service.start(
                owner_id=owner_id,
                source_id=source_id,
                request={
                    "options": snapshot.get("options") or {},
                    "collection_id": snapshot.get("index_collection_id"),
                },
                idempotency_key=f"schedule-run:{run.id}",
                schedule_run_id=run.id,
            )
        self._set_run_fields(run.id, import_id=record["id"])
        return record

    def _index_if_requested(
        self, run_id: str, owner_id: str, snapshot: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any] | None:
        collection_id = snapshot.get("index_collection_id")
        if not collection_id:
            return None
        from knowledge.job_runner import KnowledgeJobRunner

        runner = KnowledgeJobRunner()
        conversion_ids = self._successful_conversion_ids(owner_id, result.get("id"))
        if not conversion_ids:
            return {"status": "skipped", "reason": "no_published_conversions"}
        job = runner.enqueue(
            owner_id=owner_id,
            collection_id=collection_id,
            conversion_ids=conversion_ids,
            schedule_run_id=run_id,
        )
        self._set_run_fields(run_id, index_job_id=job["job_id"])
        return {"status": "queued", "job_id": job["job_id"]}

    def _successful_conversion_ids(self, owner_id: str, import_id: str | None) -> list[str]:
        if not import_id:
            return []
        from sources.importer import ImportService

        items = ImportService().items(import_id, owner_id)
        return [
            item["conversion_id"]
            for item in items
            if item.get("status") == "completed" and item.get("conversion_id")
        ]

    @staticmethod
    def _state_from_import(result: dict[str, Any]) -> str:
        status = result.get("status")
        counts = result.get("counts") or {}
        if status == "completed":
            if counts.get("total") and not counts.get("imported") and counts.get("skipped"):
                return "no_changes"
            return "succeeded"
        if status == "partial":
            return "partial"
        if status == "cancelled":
            return "cancelled"
        return "failed"

    def _submission_for(self, owner_id: str):
        """Full mode selects the gateway port; standalone uses the local one."""
        if self._submission is not None:
            return self._submission
        from config import settings

        if getattr(settings, "runtime_mode", "standalone") == "full":
            from sources.gateway_submission import GatewaySubmissionPort

            return GatewaySubmissionPort(
                getattr(settings, "converter_public_url", "http://localhost:3000"),
                getattr(settings, "converter_internal_token", ""),
            )
        from sources.submission import StandaloneSubmissionPort

        return StandaloneSubmissionPort()

    def _set_run_fields(self, run_id: str, **fields: Any) -> None:
        session = get_session_factory()()
        try:
            run = session.get(AutomationRunRow, run_id)
            if run is None:
                return
            for key, value in fields.items():
                setattr(run, key, value)
            session.commit()
        finally:
            session.close()

    def _finish(self, run_id: str, state: str, detail: dict[str, Any]) -> None:
        session = get_session_factory()()
        try:
            run = session.get(AutomationRunRow, run_id)
            if run is None:
                return
            if not self._fence_valid(session, run):
                # Another worker owns this attempt now: leave its state alone.
                return
            if run.cancel_requested_at is not None and state != "cancelled":
                state = "cancelled"
            if run.state == "cancelled" and state != "cancelled":
                state = "cancelled"
            run.state = state
            run.phase = "finalize"
            run.lease_until = None
            run.lease_owner = None
            run.finished_at = datetime.now(UTC)
            if detail.get("code"):
                run.error_code = str(detail["code"])
            if detail.get("message"):
                run.error_message = str(detail["message"])
            self.repository.append_event(
                session,
                run_id=run_id,
                type="RUN_FINISHED",
                payload={"state": state, **{k: v for k, v in detail.items() if k != "message"}},
            )
            session.commit()
        finally:
            session.close()


__all__ = ["AutomationExecutor", "RETRY_DELAYS", "TERMINAL_STATES"]
