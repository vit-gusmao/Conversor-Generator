"""T7 — occurrence claims: CAS, unique keys, leases/fences, misfire/overlap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from automation.calendar import Calendar, occurrence_key
from automation.scheduler import AutomationScheduler, ClaimError
from automation.models import AutomationRunRow, AutomationScheduleRow
from automation.repository import AutomationRepository
from automation.types import DailyRule, IntervalRule
from models.db import get_engine, get_session_factory, init_db


@pytest.fixture(autouse=True)
def _clean_automation_tables():
    """The shared test database persists between tests: reset automation rows."""
    from sqlalchemy import delete as _delete

    from automation.models import AutomationEventRow as _Event
    from automation.models import AutomationRunRow as _Run
    from automation.models import AutomationScheduleRow as _Schedule
    from models.db import get_session_factory as _factory, init_db as _init

    _init()
    session = _factory()()
    try:
        session.execute(_delete(_Event))
        session.execute(_delete(_Run))
        session.execute(_delete(_Schedule))
        session.commit()
    finally:
        session.close()
    yield


def _now() -> datetime:
    return datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _schedule(
    session,
    *,
    owner: str = "owner-1",
    rule: dict | None = None,
    next_run_at: datetime | None = None,
    overlap: str = "skip",
    misfire: str = "run_once_latest",
    source_id: str = "src-1",
    timezone: str = "UTC",
):
    return AutomationRepository().create_schedule(
        session,
        owner_id=owner,
        name="Agenda",
        source_id=source_id,
        rule=rule or {"kind": "interval", "every_seconds": 3600, "anchor_at": "2026-11-19T11:00:00Z"},
        timezone=timezone,
        next_run_at=next_run_at or datetime(2026, 11, 19, 11, 0, tzinfo=UTC),
        overlap_policy=overlap,
        misfire_policy=misfire,
    )


def _runs(session) -> list[AutomationRunRow]:
    return list(session.execute(select(AutomationRunRow)).scalars())


def test_due_schedule_creates_exactly_one_occurrence():
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a")
    session = get_session_factory()()
    try:
        schedule = _schedule(session)
        session.commit()
        created = scheduler.create_due_occurrences(session, now=_now())
        session.commit()
        assert len(created) == 1
        run = created[0]
        assert run.state == "queued"
        assert run.occurrence_key == occurrence_key(schedule.id, schedule.revision, run.scheduled_for)
        # next_run_at advanced in the SAME transaction; the missed 11:00 was
        # coalesced into the 12:00 instant (last eligible before now).
        refreshed = session.get(AutomationScheduleRow, schedule.id)
        assert run.scheduled_for == datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
        assert refreshed.next_run_at == datetime(2026, 11, 19, 13, 0, tzinfo=UTC)
    finally:
        session.close()


def test_occurrence_key_is_unique_globally():
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a")
    session = get_session_factory()()
    try:
        schedule = _schedule(session)
        session.commit()
        first = scheduler.create_due_occurrences(session, now=_now())
        session.commit()
        # Rewind next_run_at (simulating a stale reader) — the unique key must
        # prevent a duplicate instead of creating another occurrence.
        refreshed = session.get(AutomationScheduleRow, schedule.id)
        refreshed.next_run_at = first[0].scheduled_for
        session.commit()
        second = scheduler.create_due_occurrences(session, now=_now())
        session.commit()
        assert len(second) == 0
        assert len(_runs(session)) == 1
    finally:
        session.close()


def test_two_schedulers_competing_create_one_occurrence():
    init_db()
    session_a = get_session_factory()()
    session_b = get_session_factory()()
    try:
        schedule = _schedule(session_a)
        session_a.commit()
        observed = session_a.get(AutomationScheduleRow, schedule.id)
        observed_next = observed.next_run_at
        observed_revision = observed.revision
        session_b.expire_all()

        scheduler_a = AutomationScheduler(worker_id="worker-a")
        scheduler_b = AutomationScheduler(worker_id="worker-b")

        # A wins the CAS; B's CAS fails and must not create anything.
        advanced = scheduler_a._advance_and_record(
            session_a,
            schedule_id=schedule.id,
            observed_revision=observed_revision,
            observed_next_run_at=observed_next,
            now=_now(),
        )
        session_a.commit()
        assert advanced is not None

        with pytest.raises(ClaimError):
            scheduler_b._advance_and_record(
                session_b,
                schedule_id=schedule.id,
                observed_revision=observed_revision,
                observed_next_run_at=observed_next,
                now=_now(),
            )
        session_b.rollback()
        assert len(_runs(session_a)) == 1
    finally:
        session_a.close()
        session_b.close()


def test_lease_claim_and_fence_protect_updates():
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a", lease_seconds=120)
    session = get_session_factory()()
    try:
        schedule = _schedule(session)
        session.commit()
        scheduler.create_due_occurrences(session, now=_now())
        session.commit()

        lease = scheduler.claim_due(session, now=_now(), worker_id="worker-a")
        session.commit()
        assert lease is not None
        assert lease.fence == 1

        # second claim by another worker finds nothing (lease held)
        other = scheduler.claim_due(session, now=_now(), worker_id="worker-b")
        session.commit()
        assert other is None

        # recovery after expiry bumps the fence; the old fence can't finish
        later = _now() + timedelta(seconds=200)
        recovered = scheduler.claim_due(session, now=later, worker_id="worker-b")
        session.commit()
        assert recovered is not None and recovered.fence == 2
        assert scheduler.finish(session, recovered.run_id, recovered.fence, "succeeded") is True
        session.commit()
        assert scheduler.finish(session, recovered.run_id, 1, "succeeded") is False
    finally:
        session.close()


def test_overlap_skip_records_skipped_occurrence():
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a")
    session = get_session_factory()()
    try:
        schedule = _schedule(session, next_run_at=datetime(2026, 11, 19, 11, 0, tzinfo=UTC))
        session.commit()
        first = scheduler.create_due_occurrences(session, now=_now())
        session.commit()
        assert first[0].state == "queued"

        # Make the first occurrence active
        run = session.get(AutomationRunRow, first[0].id)
        run.state = "running"
        run.lease_until = _now() + timedelta(seconds=120)
        session.commit()

        # The next calendar instant is 13:00; it is due a minute later and the
        # still-running occurrence triggers the overlap policy.
        refreshed = session.get(AutomationScheduleRow, schedule.id)
        refreshed.next_run_at = datetime(2026, 11, 19, 13, 0, tzinfo=UTC)
        session.commit()
        second = scheduler.create_due_occurrences(session, now=_now() + timedelta(hours=1, minutes=1))
        session.commit()
        assert len(second) == 1
        assert second[0].state == "skipped"
        assert second[0].snapshot_json.get("skip_reason") == "overlap"
    finally:
        session.close()


def test_misfire_coalesces_missed_occurrences():
    """Computer off for three days: one recovery occurrence + a coalesced event."""
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a")
    session = get_session_factory()()
    try:
        rule = {"kind": "daily", "local_time": "08:00"}
        schedule = _schedule(
            session,
            rule=rule,
            next_run_at=datetime(2026, 11, 16, 11, 0, tzinfo=UTC),  # 08:00 SP
            misfire="run_once_latest",
            timezone="America/Sao_Paulo",
        )
        session.commit()
        now = datetime(2026, 11, 19, 13, 0, tzinfo=UTC)  # 10:00 SP, 4th day
        created = scheduler.create_due_occurrences(session, now=now)
        session.commit()
        assert len(created) == 1
        run = created[0]
        assert run.scheduled_for == datetime(2026, 11, 19, 11, 0, tzinfo=UTC)
        assert run.snapshot_json.get("coalesced", 0) >= 2
        # next is tomorrow at 08:00 local — no unbounded backlog
        refreshed = session.get(AutomationScheduleRow, schedule.id)
        assert refreshed.next_run_at == datetime(2026, 11, 20, 11, 0, tzinfo=UTC)
    finally:
        session.close()


def test_misfire_skip_records_and_moves_on():
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a")
    session = get_session_factory()()
    try:
        schedule = _schedule(
            session,
            next_run_at=datetime(2026, 11, 18, 11, 0, tzinfo=UTC),
            misfire="skip",
        )
        session.commit()
        created = scheduler.create_due_occurrences(session, now=_now())
        session.commit()
        assert len(created) == 1
        assert created[0].state == "skipped"
        assert created[0].snapshot_json.get("skip_reason") == "misfire_skip"
        refreshed = session.get(AutomationScheduleRow, schedule.id)
        assert refreshed.next_run_at > _now()
        from automation.models import AutomationEventRow

        events = list(session.execute(select(AutomationEventRow)).scalars())
        assert any(event.type == "MISFIRE_COALESCED" for event in events)
    finally:
        session.close()


def test_queue_one_keeps_at_most_one_pending():
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a")
    session = get_session_factory()()
    try:
        schedule = _schedule(
            session,
            rule={"kind": "interval", "every_seconds": 60, "anchor_at": "2026-11-19T11:00:00Z"},
            next_run_at=datetime(2026, 11, 19, 11, 0, tzinfo=UTC),
            overlap="queue_one",
        )
        session.commit()
        created = scheduler.create_due_occurrences(session, now=_now())
        session.commit()
        assert len(created) == 1
        # simulate a long downtime with many missed intervals
        refreshed = session.get(AutomationScheduleRow, schedule.id)
        refreshed.next_run_at = datetime(2026, 11, 19, 11, 0, tzinfo=UTC)
        session.commit()
        many = scheduler.create_due_occurrences(session, now=_now() + timedelta(hours=2))
        session.commit()
        pending = [run for run in _runs(session) if run.state in {"queued", "running"}]
        assert len(pending) <= 2  # active + at most one queued
    finally:
        session.close()


def test_paused_schedule_creates_nothing_and_resume_recalculates():
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a")
    repo = AutomationRepository()
    session = get_session_factory()()
    try:
        schedule = _schedule(session)
        repo.pause_schedule(session, schedule_id=schedule.id, owner_id=schedule.owner_id)
        session.commit()
        assert scheduler.create_due_occurrences(session, now=_now()) == []

        next_run = Calendar.next_after(
            IntervalRule(kind="interval", every_seconds=3600, anchor_at=None),
            "UTC",
            _now(),
        )
        repo.resume_schedule(
            session, schedule_id=schedule.id, owner_id=schedule.owner_id, next_run_at=next_run
        )
        session.commit()
        refreshed = session.get(AutomationScheduleRow, schedule.id)
        assert refreshed.next_run_at == next_run  # no backlog from the pause
    finally:
        session.close()


def test_run_now_does_not_shift_calendar():
    init_db()
    scheduler = AutomationScheduler(worker_id="worker-a")
    session = get_session_factory()()
    try:
        schedule = _schedule(session)
        session.commit()
        before = session.get(AutomationScheduleRow, schedule.id).next_run_at
        manual = scheduler.create_manual_occurrence(
            session,
            schedule_id=schedule.id,
            owner_id=schedule.owner_id,
            request_key="run-now-1",
            now=_now(),
        )
        session.commit()
        assert manual.trigger == "manual"
        after = session.get(AutomationScheduleRow, schedule.id).next_run_at
        assert after == before
        # idempotent request key
        again = scheduler.create_manual_occurrence(
            session,
            schedule_id=schedule.id,
            owner_id=schedule.owner_id,
            request_key="run-now-1",
            now=_now(),
        )
        session.commit()
        assert again.id == manual.id
    finally:
        session.close()
