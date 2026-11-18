"""Durable job backlog.

Claims use a compare-and-set `UPDATE ... WHERE state = 'queued'`, which is
atomic on both SQLite and PostgreSQL, so the same code path serves the
single-process product and a multi-instance deployment without Redis.

The lease carries a fencing token: every claim increments `fence`, and a
worker may only publish a result whose fence is still current. A worker that
lost its lease (expired, reassigned, cancelled) therefore cannot overwrite the
new owner's work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from models.db import EngineJobRow, Run, get_session_factory

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class JobLease:
    job_id: int
    run_id: str
    unit_key: str
    fence: int
    attempt: int
    payload_ref: str | None


@dataclass(slots=True)
class JobReceipt:
    job_id: int
    run_id: str
    unit_key: str
    state: str
    created: bool


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class JobRepository:
    """All state transitions of the backlog."""

    def enqueue_once(
        self,
        run_id: str,
        *,
        unit_key: str = "document",
        payload_ref: str | None = None,
        available_at: datetime | None = None,
    ) -> JobReceipt:
        """Idempotent per (run_id, unit_key): a duplicate delivery is a no-op."""
        session = get_session_factory()()
        try:
            existing = session.execute(
                select(EngineJobRow).where(
                    EngineJobRow.run_id == run_id,
                    EngineJobRow.unit_key == unit_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.state in {"failed", "done", "cancelled"}:
                    # A retry after a terminal outcome is a new request: put the
                    # unit back in the queue instead of silently ignoring it.
                    existing.state = "queued"
                    existing.available_at = available_at or utcnow()
                    existing.lease_owner = None
                    existing.lease_until = None
                    existing.last_error = None
                    existing.payload_ref = payload_ref or existing.payload_ref
                    session.commit()
                    return JobReceipt(existing.id, run_id, unit_key, existing.state, created=False)
                return JobReceipt(existing.id, run_id, unit_key, existing.state, created=False)

            job = EngineJobRow(
                run_id=run_id,
                unit_key=unit_key,
                state="queued",
                available_at=available_at or utcnow(),
                payload_ref=payload_ref,
                fence=0,
                attempt=0,
            )
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.execute(
                    select(EngineJobRow).where(
                        EngineJobRow.run_id == run_id,
                        EngineJobRow.unit_key == unit_key,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return JobReceipt(existing.id, run_id, unit_key, existing.state, created=False)
            session.refresh(job)
            return JobReceipt(job.id, run_id, unit_key, job.state, created=True)
        finally:
            session.close()

    def claim(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = LEASE_SECONDS,
    ) -> JobLease | None:
        """Atomically leases one queued (or lease-expired) job."""
        moment = now or utcnow()
        deadline = moment + timedelta(seconds=lease_seconds)
        session = get_session_factory()()
        try:
            candidates = session.execute(
                select(EngineJobRow.id)
                .where(
                    (
                        (EngineJobRow.state == "queued")
                        & (EngineJobRow.available_at <= moment)
                        & (EngineJobRow.cancel_requested_at.is_(None))
                    )
                    | (
                        (EngineJobRow.state.in_(("leased", "running")))
                        & (EngineJobRow.lease_until.is_not(None))
                        & (EngineJobRow.lease_until < moment)
                    )
                )
                .order_by(EngineJobRow.available_at, EngineJobRow.id)
                .limit(10)
            ).scalars().all()

            for job_id in candidates:
                claimed = session.execute(
                    update(EngineJobRow)
                    .where(
                        EngineJobRow.id == job_id,
                        EngineJobRow.state.in_(("queued", "leased", "running")),
                    )
                    .values(
                        state="leased",
                        lease_owner=worker_id[:128],
                        lease_until=deadline,
                        fence=EngineJobRow.fence + 1,
                        attempt=EngineJobRow.attempt + 1,
                        updated_at=moment,
                    )
                )
                if claimed.rowcount != 1:
                    # Another worker won this row; try the next candidate.
                    session.rollback()
                    continue
                session.commit()
                row = session.get(EngineJobRow, job_id)
                if row is None:  # pragma: no cover - defensive
                    continue
                return JobLease(
                    job_id=row.id,
                    run_id=row.run_id,
                    unit_key=row.unit_key,
                    fence=int(row.fence),
                    attempt=int(row.attempt),
                    payload_ref=row.payload_ref,
                )
            session.rollback()
            return None
        finally:
            session.close()

    def heartbeat(self, job_id: int, fence: int, *, lease_seconds: int = LEASE_SECONDS) -> bool:
        """Renews the lease. Returns False when the worker lost ownership."""
        session = get_session_factory()()
        try:
            moment = utcnow()
            result = session.execute(
                update(EngineJobRow)
                .where(EngineJobRow.id == job_id, EngineJobRow.fence == fence)
                .values(
                    state="running",
                    lease_until=moment + timedelta(seconds=lease_seconds),
                    updated_at=moment,
                )
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    def complete(self, job_id: int, fence: int, *, result_ref: str | None = None) -> bool:
        """Marks the unit done. Only the current fence may do so."""
        session = get_session_factory()()
        try:
            result = session.execute(
                update(EngineJobRow)
                .where(EngineJobRow.id == job_id, EngineJobRow.fence == fence)
                .values(
                    state="done",
                    payload_ref=result_ref,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=utcnow(),
                )
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    def fail(self, job_id: int, fence: int, *, error: str, retry_in: int | None = None) -> bool:
        """Fails the unit, or re-queues it after `retry_in` seconds."""
        session = get_session_factory()()
        try:
            moment = utcnow()
            values = {
                "state": "queued" if retry_in is not None else "failed",
                "last_error": error[:2000],
                "lease_owner": None,
                "lease_until": None,
                "updated_at": moment,
            }
            if retry_in is not None:
                values["available_at"] = moment + timedelta(seconds=retry_in)
            result = session.execute(
                update(EngineJobRow)
                .where(EngineJobRow.id == job_id, EngineJobRow.fence == fence)
                .values(**values)
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    def cancel_run(self, run_id: str) -> bool:
        """Records a durable cancellation request for the whole run.

        Returns True when the request is new. The worker observes it between
        units, and the publication transaction refuses to commit after it.
        """
        session = get_session_factory()()
        try:
            moment = utcnow()
            run = session.execute(select(Run).where(Run.id == run_id)).scalar_one_or_none()
            if run is None:
                return False
            changed = run.cancel_requested_at is None
            if changed:
                run.cancel_requested_at = moment
            session.execute(
                update(EngineJobRow)
                .where(
                    EngineJobRow.run_id == run_id,
                    EngineJobRow.state.in_(("queued", "leased", "running")),
                    EngineJobRow.cancel_requested_at.is_(None),
                )
                .values(cancel_requested_at=moment, updated_at=moment)
            )
            session.commit()
            return changed
        finally:
            session.close()

    def cancel_requested(self, run_id: str) -> bool:
        session = get_session_factory()()
        try:
            value = session.execute(
                select(EngineJobRow.cancel_requested_at).where(
                    EngineJobRow.run_id == run_id,
                    EngineJobRow.cancel_requested_at.is_not(None),
                )
            ).first()
            if value is not None:
                return True
            run = session.execute(
                select(Run.cancel_requested_at).where(Run.id == run_id)
            ).scalar_one_or_none()
            return run is not None
        finally:
            session.close()

    def backlog(self) -> dict[str, int]:
        """Counts by state plus the age of the oldest queued unit."""
        session = get_session_factory()()
        try:
            rows = session.execute(
                select(EngineJobRow.state, func.count()).group_by(EngineJobRow.state)
            ).all()
            oldest = session.execute(
                select(func.min(EngineJobRow.available_at)).where(EngineJobRow.state == "queued")
            ).scalar()
            counts = {str(state): int(count) for state, count in rows}
            oldest_dt = _aware(oldest)
            counts["oldest_queued_seconds"] = (
                int((utcnow() - oldest_dt).total_seconds()) if oldest_dt else 0
            )
            return counts
        finally:
            session.close()

    def reap_expired(self, *, now: datetime | None = None) -> int:
        """Returns expired leases to `queued` so another worker can take them."""
        moment = now or utcnow()
        session = get_session_factory()()
        try:
            result = session.execute(
                update(EngineJobRow)
                .where(
                    EngineJobRow.state.in_(("leased", "running")),
                    EngineJobRow.lease_until.is_not(None),
                    EngineJobRow.lease_until < moment,
                )
                .values(
                    state="queued",
                    lease_owner=None,
                    lease_until=None,
                    available_at=moment,
                    updated_at=moment,
                )
            )
            session.commit()
            return int(result.rowcount or 0)
        finally:
            session.close()


__all__ = [
    "JobRepository",
    "JobLease",
    "JobReceipt",
    "LEASE_SECONDS",
    "HEARTBEAT_SECONDS",
]
