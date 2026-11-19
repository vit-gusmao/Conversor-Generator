"""Transactional operations for the automation domain (T2).

Occurrence creation uses compare-and-set on the schedule (enabled, revision,
``next_run_at`` observed) plus the global unique ``occurrence_key``, so two
competing schedulers create exactly one row. Stale CAS attempts re-read the
state; they never "compensate" by inserting another occurrence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from automation.models import (
    AutomationEventRow,
    AutomationRunRow,
    AutomationScheduleRow,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class AutomationRepository:
    """Owner-scoped persistence for schedules, occurrences and events."""

    # -------------------------------------------------------------- schedules
    def create_schedule(
        self,
        session: Session,
        *,
        owner_id: str,
        name: str,
        source_id: str,
        rule: dict[str, Any],
        timezone: str,
        next_run_at: datetime | None = None,
        misfire_policy: str = "run_once_latest",
        overlap_policy: str = "skip",
        options: dict[str, Any] | None = None,
        index_collection_id: str | None = None,
    ) -> AutomationScheduleRow:
        schedule = AutomationScheduleRow(
            id=f"sch_{self._hex()}",
            owner_id=owner_id,
            name=name,
            source_id=source_id,
            rule_json=rule,
            timezone=timezone,
            next_run_at=next_run_at,
            anchor_at=datetime.fromisoformat(rule["anchor_at"].replace("Z", "+00:00"))
            if rule.get("kind") == "interval" and rule.get("anchor_at")
            else None,
            misfire_policy=misfire_policy,
            overlap_policy=overlap_policy,
            options_json=options or {},
            index_collection_id=index_collection_id,
        )
        session.add(schedule)
        # Flush so column defaults (revision, timestamps) are materialized
        # before callers read them.
        session.flush()
        return schedule

    def get_schedule(
        self, session: Session, *, schedule_id: str, owner_id: str
    ) -> AutomationScheduleRow | None:
        return session.execute(
            select(AutomationScheduleRow).where(
                AutomationScheduleRow.id == schedule_id,
                AutomationScheduleRow.owner_id == owner_id,
                AutomationScheduleRow.archived.is_(False),
            )
        ).scalar_one_or_none()

    def list_schedules(self, session: Session, *, owner_id: str) -> list[AutomationScheduleRow]:
        return list(
            session.execute(
                select(AutomationScheduleRow)
                .where(
                    AutomationScheduleRow.owner_id == owner_id,
                    AutomationScheduleRow.archived.is_(False),
                )
                .order_by(AutomationScheduleRow.created_at)
            ).scalars()
        )

    def update_schedule(
        self,
        session: Session,
        *,
        schedule_id: str,
        owner_id: str,
        expected_revision: int,
        changes: dict[str, Any],
        next_run_at: datetime | None = None,
        recompute_next_run: bool = False,
    ) -> AutomationScheduleRow | None:
        """Optimistic edit: bumps ``revision``; stale expectations return None.

        The revision check is part of the UPDATE itself (``WHERE revision = ?``),
        so two writers holding the same expectation cannot both succeed (S4). A
        read-then-write check would let the second writer slip in between.
        """
        allowed = {
            "name",
            "rule_json",
            "timezone",
            "misfire_policy",
            "overlap_policy",
            "options_json",
            "index_collection_id",
            "enabled",
            "anchor_at",
            "source_id",
        }
        values: dict[str, Any] = {key: value for key, value in changes.items() if key in allowed}
        values["revision"] = expected_revision + 1
        if recompute_next_run:
            values["next_run_at"] = next_run_at
        result = session.execute(
            update(AutomationScheduleRow)
            .where(
                AutomationScheduleRow.id == schedule_id,
                AutomationScheduleRow.owner_id == owner_id,
                AutomationScheduleRow.archived.is_(False),
                AutomationScheduleRow.revision == expected_revision,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        schedule = session.get(AutomationScheduleRow, schedule_id)
        if schedule is not None:
            session.refresh(schedule)
        return schedule

    def pause_schedule(
        self, session: Session, *, schedule_id: str, owner_id: str
    ) -> AutomationScheduleRow | None:
        """Idempotent pause; active occurrences keep running."""
        schedule = self.get_schedule(session, schedule_id=schedule_id, owner_id=owner_id)
        if schedule is None:
            return None
        if schedule.enabled:
            schedule.enabled = False
        return schedule

    def resume_schedule(
        self, session: Session, *, schedule_id: str, owner_id: str, next_run_at: datetime
    ) -> AutomationScheduleRow | None:
        """Resume starts from the next future instant; no backlog."""
        schedule = self.get_schedule(session, schedule_id=schedule_id, owner_id=owner_id)
        if schedule is None:
            return None
        if not schedule.enabled:
            schedule.enabled = True
        schedule.next_run_at = next_run_at
        return schedule

    def archive_schedule(self, session: Session, *, schedule_id: str, owner_id: str) -> bool:
        schedule = session.execute(
            select(AutomationScheduleRow).where(
                AutomationScheduleRow.id == schedule_id,
                AutomationScheduleRow.owner_id == owner_id,
            )
        ).scalar_one_or_none()
        if schedule is None:
            return False
        schedule.archived = True
        schedule.enabled = False
        return True

    def compare_and_advance(
        self,
        session: Session,
        *,
        schedule_id: str,
        observed_revision: int,
        observed_next_run_at: datetime,
        next_run_at: datetime,
    ) -> AutomationScheduleRow | None:
        """CAS: advance ``next_run_at`` only if the observed state still holds."""
        schedule = session.get(AutomationScheduleRow, schedule_id)
        if schedule is None:
            return None
        if not schedule.enabled or schedule.revision != observed_revision:
            return None
        if schedule.next_run_at != observed_next_run_at:
            return None
        schedule.next_run_at = next_run_at
        return schedule

    # ------------------------------------------------------------------ runs
    def record_run(
        self,
        session: Session,
        *,
        owner_id: str,
        schedule: AutomationScheduleRow,
        scheduled_for: datetime,
        trigger: str,
        occurrence_key: str,
        snapshot: dict[str, Any],
    ) -> AutomationRunRow:
        """Insert one occurrence; caller commits. IntegrityError = duplicate key."""
        run = AutomationRunRow(
            id=f"arun_{self._hex()}",
            owner_id=owner_id,
            schedule_id=schedule.id,
            schedule_revision=schedule.revision,
            scheduled_for=scheduled_for,
            trigger=trigger,
            occurrence_key=occurrence_key,
            snapshot_json=snapshot,
        )
        session.add(run)
        return run

    def get_run(self, session: Session, *, run_id: str, owner_id: str) -> AutomationRunRow | None:
        return session.execute(
            select(AutomationRunRow).where(
                AutomationRunRow.id == run_id,
                AutomationRunRow.owner_id == owner_id,
            )
        ).scalar_one_or_none()

    def get_run_by_occurrence_key(
        self, session: Session, occurrence_key: str
    ) -> AutomationRunRow | None:
        return session.execute(
            select(AutomationRunRow).where(AutomationRunRow.occurrence_key == occurrence_key)
        ).scalar_one_or_none()

    def list_runs(
        self,
        session: Session,
        *,
        owner_id: str,
        schedule_id: str,
        limit: int = 50,
    ) -> list[AutomationRunRow]:
        return list(
            session.execute(
                select(AutomationRunRow)
                .where(
                    AutomationRunRow.owner_id == owner_id,
                    AutomationRunRow.schedule_id == schedule_id,
                )
                .order_by(AutomationRunRow.scheduled_for.desc())
                .limit(limit)
            ).scalars()
        )

    def update_run_state(
        self,
        session: Session,
        *,
        run_id: str,
        expected_fence: int,
        state: str | None = None,
        phase: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        lease_owner: str | None = None,
        lease_until: datetime | None = None,
        bump_fence: bool = False,
        import_id: str | None = None,
        index_job_id: str | None = None,
        set_started: bool = False,
        set_finished: bool = False,
    ) -> AutomationRunRow | None:
        """Fence-checked state transition; stale fences return None."""
        run = session.get(AutomationRunRow, run_id)
        if run is None or run.fence != expected_fence:
            return None
        if state is not None:
            run.state = state
        if phase is not None:
            run.phase = phase
        if error_code is not None:
            run.error_code = error_code
        if error_message is not None:
            run.error_message = error_message
        if lease_owner is not None or lease_until is not None:
            run.lease_owner = lease_owner
            run.lease_until = lease_until
        if bump_fence:
            run.fence = expected_fence + 1
        if import_id is not None:
            run.import_id = import_id
        if index_job_id is not None:
            run.index_job_id = index_job_id
        if set_started and run.started_at is None:
            run.started_at = utcnow()
        if set_finished:
            run.finished_at = utcnow()
        return run

    # ---------------------------------------------------------------- events
    def append_event(
        self,
        session: Session,
        *,
        run_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
    ) -> AutomationEventRow:
        """Sequence = max+1 inside the same transaction (unique per run)."""
        max_sequence = session.execute(
            select(AutomationEventRow.sequence)
            .where(AutomationEventRow.run_id == run_id)
            .order_by(AutomationEventRow.sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        event = AutomationEventRow(
            run_id=run_id,
            sequence=(max_sequence or 0) + 1,
            type=type,
            payload_json=payload or {},
        )
        session.add(event)
        return event

    def events_since(
        self, session: Session, *, run_id: str, after: int = 0, limit: int = 200
    ) -> list[AutomationEventRow]:
        return list(
            session.execute(
                select(AutomationEventRow)
                .where(AutomationEventRow.run_id == run_id, AutomationEventRow.sequence > after)
                .order_by(AutomationEventRow.sequence)
                .limit(limit)
            ).scalars()
        )

    @staticmethod
    def _hex() -> str:
        import uuid

        return uuid.uuid4().hex
