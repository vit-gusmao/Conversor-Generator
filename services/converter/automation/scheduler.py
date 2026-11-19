"""Occurrence creation and fenced claims (T7, doc 05 §4/§6).

The calendar transaction is short and does no I/O: read due schedules, apply
misfire/overlap policy, insert the occurrence with its unique key and advance
``next_run_at`` in the same commit. Conflicts re-read state; they never
"compensate" by inserting another occurrence.

Leases are 120 s with a 30 s heartbeat; recovery bumps the fence so a stale
worker can no longer finalize results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from automation.calendar import Calendar, occurrence_key
from automation.models import AutomationEventRow, AutomationRunRow, AutomationScheduleRow
from automation.repository import AutomationRepository
from automation.types import (
    INTERVAL_MAX_SECONDS,
    DailyRule,
    IntervalRule,
    RunLease,
)

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
MISFIRE_TOLERANCE_SECONDS = 30
MAX_DUE_BATCH = 50
ACTIVE_STATES = ("running", "waiting_children", "cancelling")


class ClaimError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class _Due:
    schedule: AutomationScheduleRow
    scheduled_for: datetime
    coalesced: int


class AutomationScheduler:
    def __init__(
        self,
        *,
        worker_id: str = "calendar",
        lease_seconds: int = LEASE_SECONDS,
    ) -> None:
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.repository = AutomationRepository()

    # ------------------------------------------------------------- calendar
    def create_due_occurrences(
        self, session: Session, *, now: datetime | None = None
    ) -> list[AutomationRunRow]:
        """Create occurrences for due schedules (bounded batch, short txn)."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        due_rows = list(
            session.execute(
                select(AutomationScheduleRow)
                .where(
                    AutomationScheduleRow.enabled.is_(True),
                    AutomationScheduleRow.archived.is_(False),
                    AutomationScheduleRow.next_run_at.is_not(None),
                    AutomationScheduleRow.next_run_at <= now,
                )
                .order_by(AutomationScheduleRow.next_run_at)
                .limit(MAX_DUE_BATCH)
            ).scalars()
        )
        created: list[AutomationRunRow] = []
        for schedule in due_rows:
            observed_revision = schedule.revision
            observed_next = schedule.next_run_at
            try:
                run = self._advance_and_record(
                    session,
                    schedule_id=schedule.id,
                    observed_revision=observed_revision,
                    observed_next_run_at=observed_next,
                    now=now,
                )
            except ClaimError:
                session.rollback()
                continue
            except IntegrityError:
                # Another process inserted the same occurrence key first.
                session.rollback()
                continue
            if run is not None:
                created.append(run)
        return created

    def _advance_and_record(
        self,
        session: Session,
        *,
        schedule_id: str,
        observed_revision: int,
        observed_next_run_at: datetime,
        now: datetime,
    ) -> AutomationRunRow | None:
        schedule = session.get(AutomationScheduleRow, schedule_id)
        if schedule is None or not schedule.enabled or schedule.archived:
            raise ClaimError("SCHEDULE_INACTIVE", "Agenda inativa.")
        if schedule.revision != observed_revision:
            raise ClaimError("REVISION_CHANGED", "A revisão mudou desde a leitura.")
        if schedule.next_run_at != observed_next_run_at:
            raise ClaimError("NEXT_RUN_CHANGED", "O próximo horário já avançou.")

        rule = self._rule(schedule)
        due = self._resolve_due(schedule, rule, observed_next_run_at, now)

        if due is None:
            # misfire=skip: the missed instant is recorded as a terminal
            # `skipped` occurrence (audited, no backlog) and the calendar moves
            # straight to the next future instant.
            next_after = Calendar.next_after(rule, schedule.timezone, now)
            key = occurrence_key(schedule.id, schedule.revision, observed_next_run_at)
            existing = session.execute(
                select(AutomationRunRow).where(AutomationRunRow.occurrence_key == key)
            ).scalar_one_or_none()
            if existing is not None:
                raise ClaimError("OCCURRENCE_EXISTS", "A ocorrência já foi registrada.")
            snapshot = self._snapshot(schedule)
            snapshot["skip_reason"] = "misfire_skip"
            snapshot["missed_from"] = observed_next_run_at.isoformat()
            snapshot["next_run_at"] = next_after.isoformat()
            skipped = AutomationRunRow(
                id=f"arun_{self._new_id()}",
                owner_id=schedule.owner_id,
                schedule_id=schedule.id,
                schedule_revision=schedule.revision,
                scheduled_for=observed_next_run_at,
                trigger="schedule",
                occurrence_key=key,
                snapshot_json=snapshot,
                state="skipped",
                phase=None,
            )
            session.add(skipped)
            schedule.next_run_at = next_after
            session.flush()
            self._event(
                session,
                schedule,
                "MISFIRE_COALESCED",
                {
                    "policy": "skip",
                    "missed_from": observed_next_run_at.isoformat(),
                    "next_run_at": next_after.isoformat(),
                },
                run_id=skipped.id,
            )
            return skipped

        scheduled_for = due.scheduled_for
        key = occurrence_key(schedule.id, schedule.revision, scheduled_for)

        existing = session.execute(
            select(AutomationRunRow).where(AutomationRunRow.occurrence_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            raise ClaimError("OCCURRENCE_EXISTS", "A ocorrência já foi criada.")

        state = "queued"
        snapshot = self._snapshot(schedule)
        if due.coalesced:
            snapshot["coalesced"] = due.coalesced
        overlap = self._active_occurrence(session, schedule)
        if overlap is not None:
            if schedule.overlap_policy == "skip":
                state = "skipped"
                snapshot["skip_reason"] = "overlap"
            else:  # queue_one: at most one extra pending occurrence
                queued = self._pending_occurrences(session, schedule)
                if len(queued) >= 1:
                    state = "skipped"
                    snapshot["skip_reason"] = "overlap_coalesced"
                    snapshot["coalesced"] = int(snapshot.get("coalesced", 0)) + 1

        run = AutomationRunRow(
            id=f"arun_{self._new_id()}",
            owner_id=schedule.owner_id,
            schedule_id=schedule.id,
            schedule_revision=schedule.revision,
            scheduled_for=scheduled_for,
            trigger="schedule",
            occurrence_key=key,
            snapshot_json=snapshot,
            state=state,
            phase="scan" if state == "queued" else None,
        )
        session.add(run)
        schedule.next_run_at = Calendar.next_after(rule, schedule.timezone, scheduled_for)
        session.flush()
        self._event(
            session,
            schedule,
            "OCCURRENCE_DUE",
            {"run_id": run.id, "scheduled_for": scheduled_for.isoformat(), "state": state},
            run_id=run.id,
        )
        if due.coalesced:
            self._event(
                session,
                schedule,
                "MISFIRE_COALESCED",
                {
                    "policy": schedule.misfire_policy,
                    "coalesced": due.coalesced,
                    "scheduled_for": scheduled_for.isoformat(),
                },
                run_id=run.id,
            )
        return run

    def _resolve_due(
        self,
        schedule: AutomationScheduleRow,
        rule: DailyRule | IntervalRule,
        next_run_at: datetime,
        now: datetime,
    ) -> _Due | None:
        if schedule.misfire_policy == "skip":
            tolerance = timedelta(seconds=MISFIRE_TOLERANCE_SECONDS)
            if next_run_at + tolerance < now:
                return None
            return _Due(schedule, next_run_at, 0)

        # run_once_latest: collapse every missed instant into the last one.
        latest = next_run_at
        coalesced = 0
        cursor = next_run_at
        guard = 0
        while True:
            candidate = Calendar.next_after(rule, schedule.timezone, cursor)
            if candidate > now:
                break
            latest = candidate
            coalesced += 1
            cursor = candidate
            guard += 1
            if guard > 100 * 366:  # hard bound, never an unbounded loop
                break
        return _Due(schedule, latest, coalesced)

    # ---------------------------------------------------------------- claims
    def claim_due(
        self, session: Session, *, now: datetime | None = None, worker_id: str | None = None
    ) -> RunLease | None:
        """Claim one queued (or lease-expired) occurrence, bumping the fence."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        worker = worker_id or self.worker_id
        expiry = now
        run = session.execute(
            select(AutomationRunRow)
            .where(
                (AutomationRunRow.state == "queued")
                # A queued cancellation is reclaimed so it can finish as
                # `cancelled` instead of staying "cancelling" forever.
                | (AutomationRunRow.state == "cancelling")
                | (
                    AutomationRunRow.state.in_(ACTIVE_STATES)
                    & AutomationRunRow.lease_until.is_not(None)
                    & (AutomationRunRow.lease_until < expiry)
                )
            )
            .order_by(AutomationRunRow.scheduled_for)
            .limit(1)
        ).scalars().first()
        if run is None:
            return None

        observed_fence = run.fence
        recovered = run.state in ACTIVE_STATES
        run.state = "running"
        run.lease_owner = worker
        run.lease_until = now + timedelta(seconds=self.lease_seconds)
        run.fence = observed_fence + 1
        run.attempt = (run.attempt or 0) + 1
        if run.started_at is None:
            run.started_at = now
        session.flush()
        self.repository.append_event(
            session,
            run_id=run.id,
            type="LEASE_RECOVERED" if recovered else "RUN_CLAIMED",
            payload={"worker": worker, "fence": run.fence},
        )
        return RunLease(run_id=run.id, fence=run.fence, lease_until=run.lease_until)

    def heartbeat(self, session: Session, run_id: str, fence: int, *, now: datetime | None = None) -> bool:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        run = session.get(AutomationRunRow, run_id)
        if run is None or run.fence != fence or run.state not in ACTIVE_STATES + ("running",):
            return False
        run.lease_until = now + timedelta(seconds=self.lease_seconds)
        session.flush()
        return True

    def finish(self, session: Session, run_id: str, fence: int, state: str, error: dict | None = None) -> bool:
        run = session.get(AutomationRunRow, run_id)
        if run is None or run.fence != fence:
            return False
        run.state = state
        run.lease_until = None
        run.lease_owner = None
        run.finished_at = datetime.now(UTC)
        if error:
            run.error_code = error.get("code")
            run.error_message = error.get("message")
        session.flush()
        self.repository.append_event(
            session,
            run_id=run.id,
            type="RUN_FINISHED",
            payload={"state": state, **(error or {})},
        )
        return True

    # ------------------------------------------------------------- manual run
    def create_manual_occurrence(
        self,
        session: Session,
        *,
        schedule_id: str,
        owner_id: str,
        request_key: str,
        now: datetime | None = None,
    ) -> AutomationRunRow:
        """Run-now: idempotent by request key, never shifts the calendar."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        key = f"manual:{schedule_id}:{request_key}"
        existing = session.execute(
            select(AutomationRunRow).where(AutomationRunRow.occurrence_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        schedule = session.get(AutomationScheduleRow, schedule_id)
        if schedule is None or schedule.owner_id != owner_id or schedule.archived:
            raise ClaimError("SCHEDULE_NOT_FOUND", "Agenda inexistente para este dono.")
        snapshot = self._snapshot(schedule)
        snapshot["trigger"] = "manual"
        state = "queued"
        overlap = self._active_occurrence(session, schedule)
        if overlap is not None and schedule.overlap_policy == "skip":
            state = "skipped"
            snapshot["skip_reason"] = "overlap"
        run = AutomationRunRow(
            id=f"arun_{self._new_id()}",
            owner_id=owner_id,
            schedule_id=schedule_id,
            schedule_revision=schedule.revision,
            scheduled_for=now,
            trigger="manual",
            occurrence_key=key,
            snapshot_json=snapshot,
            state=state,
            phase="scan" if state == "queued" else None,
        )
        session.add(run)
        session.flush()
        self.repository.append_event(
            session,
            run_id=run.id,
            type="OCCURRENCE_DUE",
            payload={"trigger": "manual", "state": state},
        )
        return run

    # ---------------------------------------------------------------- helpers
    def _active_occurrence(self, session: Session, schedule: AutomationScheduleRow) -> AutomationRunRow | None:
        return session.execute(
            select(AutomationRunRow)
            .where(
                AutomationRunRow.schedule_id == schedule.id,
                AutomationRunRow.state.in_(("queued",) + ACTIVE_STATES),
            )
            .limit(1)
        ).scalars().first()

    def _pending_occurrences(self, session: Session, schedule: AutomationScheduleRow) -> list[AutomationRunRow]:
        return list(
            session.execute(
                select(AutomationRunRow).where(
                    AutomationRunRow.schedule_id == schedule.id,
                    AutomationRunRow.state == "queued",
                )
            ).scalars()
        )

    def _snapshot(self, schedule: AutomationScheduleRow) -> dict[str, Any]:
        from automation.service import build_snapshot
        from sources.repository import SourceRepository

        definition = None
        session = None
        try:
            from models.db import get_session_factory

            session = get_session_factory()()
            sources = SourceRepository()
            source = sources.get_source(
                session, source_id=schedule.source_id, owner_id=schedule.owner_id
            )
            root = (
                sources.get_root(session, root_id=source.root_id, owner_id=schedule.owner_id)
                if source is not None
                else None
            )
            if source is not None and root is not None:
                definition = {
                    "source_id": source.id,
                    "root_id": root.id,
                    "root_canonical_path": root.canonical_path,
                    "relative_path": source.relative_path,
                    "kind": source.kind,
                    "recursive": bool(source.recursive),
                    "include_patterns": source.include_patterns,
                    "exclude_patterns": source.exclude_patterns,
                    "version": source.version,
                }
        except Exception:  # noqa: BLE001 - snapshot degrades to source_id only
            definition = None
        finally:
            if session is not None:
                session.close()
        return build_snapshot(schedule, definition)

    def _event(
        self,
        session: Session,
        schedule: AutomationScheduleRow,
        type_: str,
        payload: dict[str, Any],
        run_id: str | None = None,
    ) -> None:
        if run_id is None:
            return
        self.repository.append_event(session, run_id=run_id, type=type_, payload=payload)

    @staticmethod
    def _rule(schedule: AutomationScheduleRow) -> DailyRule | IntervalRule:
        payload = schedule.rule_json or {}
        if payload.get("kind") == "daily":
            return DailyRule(kind="daily", local_time=str(payload["local_time"]))
        seconds = int(payload.get("every_seconds") or 60)
        if seconds < 60 or seconds > INTERVAL_MAX_SECONDS:
            raise ClaimError("INVALID_RULE", "Intervalo fora dos limites.")
        return IntervalRule(
            kind="interval", every_seconds=seconds, anchor_at=payload.get("anchor_at")
        )

    @staticmethod
    def _new_id() -> str:
        import uuid

        return uuid.uuid4().hex


__all__ = ["AutomationScheduler", "ClaimError", "HEARTBEAT_SECONDS", "LEASE_SECONDS"]
