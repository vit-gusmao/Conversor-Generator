"""Resumable occurrence executor (T8, doc 05 §6/§8).

Phases are small and durable: ``scan → snapshot → submit → convert → index →
finalize``. Every step re-reads the frozen snapshot, so an edit made after the
occurrence was created never changes which files that run processes; revoking
the root still blocks reads. A crash at any boundary resumes from the persisted
``import_id`` and stable client keys — never a duplicate batch or conversion.

Failures that can succeed later (gateway timeouts/unavailability, HTTP 5xx,
408/429, connection errors) return the occurrence to ``queued`` with a
not-before time from ``RETRY_DELAYS``; the attempt that follows resumes the same
import. Any other failure, or the attempt after the last delay, is terminal.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select

from automation.models import AutomationRunRow
from automation.repository import AutomationRepository
from automation.types import RunProgress
from models.db import get_session_factory

TERMINAL_STATES = ("succeeded", "partial", "no_changes", "failed", "cancelled", "skipped")
#: Not-before delay after the 1st, 2nd and 3rd failed attempt (then terminal).
RETRY_DELAYS = (30, 120, 300)
CONVERT_WAIT_SECONDS = 120
TRANSIENT_CODES = frozenset({"GATEWAY_TIMEOUT", "GATEWAY_UNAVAILABLE"})
TRANSIENT_HTTP_STATUS = frozenset({408, 429})
#: How a finished index job closes its occurrence (a failed index is partial).
_RUN_STATE_FOR_JOB = {"completed": "succeeded", "partial": "partial", "failed": "partial"}


def _is_transient(exc: BaseException) -> bool:
    """True only for failures that a later attempt can plausibly clear."""
    from api.deps import ApiError
    from sources.gateway_submission import GatewaySubmissionError

    if isinstance(exc, ApiError):
        envelope = exc.detail.get("error", {}) if isinstance(exc.detail, dict) else {}
        return (
            exc.status_code in TRANSIENT_HTTP_STATUS
            or exc.status_code >= 500
            or envelope.get("code") in TRANSIENT_CODES
        )
    if isinstance(exc, GatewaySubmissionError):
        status = int(exc.details.get("status") or 0)
        return exc.code in TRANSIENT_CODES or status in TRANSIENT_HTTP_STATUS or status >= 500
    return isinstance(exc, (TimeoutError, ConnectionError))


def _error_code_message(exc: BaseException) -> tuple[str, str]:
    from api.deps import ApiError
    from sources.gateway_submission import GatewaySubmissionError

    if isinstance(exc, ApiError):
        envelope = (exc.detail or {}).get("error", {}) if isinstance(exc.detail, dict) else {}
        return envelope.get("code", "IMPORT_FAILED"), envelope.get("message", str(exc))
    if isinstance(exc, GatewaySubmissionError):
        return exc.code, exc.message
    return "EXECUTOR_ERROR", str(exc)


class AutomationExecutor:
    def __init__(
        self,
        *,
        submission=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = AutomationRepository()
        self._submission = submission
        self._clock = clock or (lambda: datetime.now(UTC))

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
                index_detail = self._index_phase(run_id, owner, snapshot, result)
                if index_detail:
                    detail["index"] = index_detail
                    if index_detail.get("status") == "queued":
                        # Indexing runs as a durable job; the occurrence waits
                        # for it instead of declaring itself complete early.
                        if not self._mark_waiting_children(run_id, self.fence):
                            # A cancellation (S15) or a reclaim landed first:
                            # honour it instead of overwriting. _finish is
                            # fence-checked, so a lost fence writes nothing.
                            self._finish(run_id, "cancelled", detail)
                            return RunProgress(
                                run_id=run_id, state="cancelled", phase="index",
                                done=True, detail=detail,
                            )
                        self._reconcile_waiting(index_detail["job_id"])
                        return RunProgress(
                            run_id=run_id,
                            state="waiting_children",
                            phase="index",
                            done=False,
                            detail=detail,
                        )
                    if index_detail.get("status") == "terminal":
                        state = _RUN_STATE_FOR_JOB.get(index_detail.get("job_status"), "partial")
            self._finish(run_id, state, detail)
            return RunProgress(run_id=run_id, state=state, phase="finalize", done=True, detail=detail)
        except Exception as exc:  # noqa: BLE001 - terminal with diagnostics
            import traceback

            from api.deps import ApiError

            transient = _is_transient(exc)
            if transient:
                code, message = _error_code_message(exc)
                retry = self._schedule_retry(run_id, code, message)
                if retry is not None:
                    return retry

            # Exhausted transient failures keep their original code (e.g.
            # GATEWAY_TIMEOUT) instead of collapsing into EXECUTOR_ERROR.
            if transient or isinstance(exc, ApiError):
                code, detail_message = _error_code_message(exc)
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
        """A stale worker must not write results.

        The fence alone decides ownership. Reclaiming an expired lease bumps the
        fence, so a holder that is still alive keeps its right to finalize even
        when a long attempt outlives the wall-clock lease (its heartbeat renews
        it). Comparing the lease time here dropped valid finalizations (AU1-12).
        """
        expected = self.fence
        if expected is None:
            return True
        return run.fence == expected

    def _mark_waiting_children(self, run_id: str, fence: int | None = None) -> bool:
        """Move an occurrence to ``waiting_children`` under its fence.

        Conditional on the state: a cancellation that arrived meanwhile
        (``cancelling``) or a terminal result is never overwritten (S15). False
        means the transition did not apply. Without a fence (legacy unfenced
        advance) only the state guard applies.
        """
        from sqlalchemy import update

        criteria = [
            AutomationRunRow.id == run_id,
            AutomationRunRow.state.notin_(("cancelling",) + TERMINAL_STATES),
        ]
        if fence is not None:
            criteria.append(AutomationRunRow.fence == fence)
        session = get_session_factory()()
        try:
            result = session.execute(
                update(AutomationRunRow)
                .where(*criteria)
                .values(state="waiting_children")
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

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

    def _index_job_id(self, run_id: str) -> str | None:
        session = get_session_factory()()
        try:
            run = session.get(AutomationRunRow, run_id)
            return run.index_job_id if run is not None else None
        finally:
            session.close()

    def _index_phase(
        self, run_id: str, owner_id: str, snapshot: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Create the occurrence's index job once, or report the one it already has."""
        collection_id = snapshot.get("index_collection_id")
        if not collection_id:
            return None
        from knowledge.job_runner import KnowledgeJobRunner

        runner = KnowledgeJobRunner()
        known_job = self._index_job_id(run_id)
        if known_job:
            existing = runner.get(known_job, owner_id)
            if existing["status"] != "not_found":
                return self._index_detail(known_job, existing["status"])

        conversion_ids = self._successful_conversion_ids(owner_id, result.get("id"))
        if not conversion_ids:
            return {"status": "skipped", "reason": "no_published_conversions"}
        # Deterministic id per occurrence: a crash between create and the run
        # update cannot leave a second job behind on the next advance.
        job = runner.create(
            owner_id=owner_id,
            collection_id=collection_id,
            conversion_ids=conversion_ids,
            schedule_run_id=run_id,
            job_id=f"idx_{run_id}",
        )
        self._set_run_fields(run_id, index_job_id=job["job_id"])
        if job["status"] == "queued":
            runner.dispatch_async(job["job_id"], owner_id)
        return self._index_detail(job["job_id"], job["status"])

    @staticmethod
    def _index_detail(job_id: str, job_status: str) -> dict[str, Any]:
        if job_status in {"queued", "running"}:
            return {"status": "queued", "job_id": job_id, "job_status": job_status}
        return {"status": "terminal", "job_id": job_id, "job_status": job_status}

    @staticmethod
    def _reconcile_waiting(job_id: str) -> None:
        """Close the occurrence if its job finished before it was marked waiting."""
        from knowledge.job_runner import KnowledgeJobRunner

        KnowledgeJobRunner().finalize_parent_run(job_id)

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

    def _schedule_retry(self, run_id: str, code: str, message: str) -> RunProgress | None:
        """Requeue after a transient failure; None when the attempt is terminal."""
        session = get_session_factory()()
        try:
            run = session.get(AutomationRunRow, run_id)
            if run is None or not self._fence_valid(session, run):
                return None
            attempt = max(int(run.attempt or 0), 1)
            if attempt > len(RETRY_DELAYS):
                return None
            delay = RETRY_DELAYS[attempt - 1]
            phase = run.phase or "scan"
            run.state = "queued"
            run.phase = phase
            run.lease_until = None
            run.lease_owner = None
            run.next_attempt_at = self._clock() + timedelta(seconds=delay)
            run.error_code = code
            run.error_message = (message or "")[:2000]
            self.repository.append_event(
                session,
                run_id=run_id,
                type="RUN_RETRY_SCHEDULED",
                payload={"attempt": attempt, "retry_in_seconds": delay, "code": code},
            )
            session.commit()
        finally:
            session.close()
        return RunProgress(
            run_id=run_id,
            state="queued",
            phase=phase,
            done=False,
            detail={"code": code, "attempt": attempt, "retry_in_seconds": delay},
        )

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
            run.finished_at = self._clock()
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
