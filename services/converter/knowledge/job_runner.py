"""Durable knowledge indexing dispatcher (T8, doc 05 §7).

Index jobs are rows with a lease and a fence. Every execution goes through the
same path, whichever process asks for it:

* ``claim_job`` / ``claim_next`` move ``queued -> running`` by compare-and-set
  and set ``lease_expires_at``; each claim bumps ``fence`` and ``attempt``.
  Only one worker can win a given fence.
* ``heartbeat`` extends the lease while the claim is still current.
* ``finish_claim`` writes the terminal result only while the fence matches, so
  a worker whose lease was reclaimed cannot overwrite the newer attempt.
* ``recover`` requeues ``running`` jobs whose lease expired (or never existed).
* ``run_pending`` (called from the automation loop tick) recovers, then claims
  and executes queued jobs. ``dispatch_async`` is an in-process accelerator that
  uses the same claim, so a job runs once even when both paths see it.

The job row is written before any thread starts; a thread is never the only
record of the work. Legacy user-facing jobs (``dispatch`` marker, see
``KnowledgeIndexer.LEGACY_DISPATCH``) run on their own thread without a lease
and are never recovered or claimed here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from knowledge.indexer import KnowledgeIndexer
from knowledge.repository import KnowledgeRepository
from models.db import KnowledgeIndexJobRow, get_session_factory

logger = logging.getLogger("knowledge.jobs")

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
TERMINAL = ("completed", "partial", "failed")
CLAIM_SCAN_LIMIT = 50
THREAD_WORKER_ID = "knowledge-thread"


@dataclass(slots=True, frozen=True)
class JobLease:
    job_id: str
    owner_id: str
    fence: int
    lease_expires_at: datetime


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for timezone-aware columns."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_legacy(row: KnowledgeIndexJobRow) -> bool:
    return (row.source_json or {}).get("dispatch") == KnowledgeIndexer.LEGACY_DISPATCH


class KnowledgeJobRunner:
    def __init__(self) -> None:
        self.repository = KnowledgeRepository()
        self.indexer = KnowledgeIndexer(self.repository)

    # ------------------------------------------------------------ enqueue
    def create(
        self,
        *,
        owner_id: str,
        collection_id: str,
        conversion_ids: list[str],
        schedule_run_id: str | None = None,
        source_id: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist the durable job row without executing it.

        ``job_id`` makes creation idempotent: a retry after a crash returns the
        row that already exists instead of creating a second job.
        """
        if job_id is not None:
            existing = self.get(job_id, owner_id)
            if existing["status"] != "not_found":
                return existing
        source = {
            "conversion_ids": list(conversion_ids),
            "source_paths": [],
            "scope": "documents",
            "schedule_run_id": schedule_run_id,
            "source_id": source_id,
        }
        try:
            job = self.repository.create_job(
                owner_id=owner_id, collection_id=collection_id, source=source, job_id=job_id
            )
        except IntegrityError:
            if job_id is None:
                raise
            existing = self.get(job_id, owner_id)
            if existing["status"] == "not_found":
                raise
            return existing
        return self.get(job["job_id"], owner_id)

    def enqueue(
        self,
        *,
        owner_id: str,
        collection_id: str,
        conversion_ids: list[str],
        schedule_run_id: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """Create the durable row and start the in-process accelerator."""
        job = self.create(
            owner_id=owner_id,
            collection_id=collection_id,
            conversion_ids=conversion_ids,
            schedule_run_id=schedule_run_id,
            source_id=source_id,
        )
        self.dispatch_async(job["job_id"], owner_id)
        return job

    def dispatch_async(self, job_id: str, owner_id: str) -> None:
        thread = threading.Thread(
            target=self._dispatch_quietly,
            args=(job_id, owner_id),
            daemon=True,
            name=f"knowledge-{job_id[:8]}",
        )
        thread.start()

    def _dispatch_quietly(self, job_id: str, owner_id: str) -> None:
        try:
            self.dispatch(job_id, owner_id)
        except Exception:  # noqa: BLE001 - the durable row is retried by the loop
            logger.exception("knowledge job %s dispatch failed", job_id)

    def dispatch(self, job_id: str, owner_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Claim one job by id and execute it, if it is still queued."""
        lease = self.claim_job(job_id, worker_id=THREAD_WORKER_ID, now=now, owner_id=owner_id)
        if lease is None:
            return self.get(job_id, owner_id)
        return self.execute(lease, now=now)

    def run_job(self, job_id: str, owner_id: str) -> dict[str, Any]:
        return self.dispatch(job_id, owner_id)

    # ------------------------------------------------------------- claims
    def claim_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
        owner_id: str | None = None,
    ) -> JobLease | None:
        now = _now(now)
        session = get_session_factory()()
        try:
            row = session.get(KnowledgeIndexJobRow, job_id)
            if row is None or row.status != "queued" or _is_legacy(row):
                return None
            if owner_id is not None and row.owner_id != owner_id:
                return None
            lease = self._cas_claim(session, row, worker_id=worker_id, now=now)
            session.commit()
            return lease
        finally:
            session.close()

    def claim_next(self, *, worker_id: str, now: datetime | None = None) -> JobLease | None:
        """Claim the oldest queued job that this worker wins by compare-and-set."""
        now = _now(now)
        session = get_session_factory()()
        try:
            candidates = list(
                session.execute(
                    select(KnowledgeIndexJobRow)
                    .where(KnowledgeIndexJobRow.status == "queued")
                    .order_by(KnowledgeIndexJobRow.created_at)
                    .limit(CLAIM_SCAN_LIMIT)
                ).scalars()
            )
            for row in candidates:
                if _is_legacy(row):
                    continue
                lease = self._cas_claim(session, row, worker_id=worker_id, now=now)
                if lease is not None:
                    session.commit()
                    return lease
            return None
        finally:
            session.close()

    @staticmethod
    def _cas_claim(
        session, row: KnowledgeIndexJobRow, *, worker_id: str, now: datetime
    ) -> JobLease | None:
        observed_fence = row.fence or 0
        new_fence = observed_fence + 1
        expires = now + timedelta(seconds=LEASE_SECONDS)
        result = session.execute(
            update(KnowledgeIndexJobRow)
            .where(
                KnowledgeIndexJobRow.id == row.id,
                KnowledgeIndexJobRow.status == "queued",
                KnowledgeIndexJobRow.fence == observed_fence,
            )
            .values(
                status="running",
                lease_owner=worker_id[:64],
                lease_expires_at=expires,
                fence=new_fence,
                attempt=KnowledgeIndexJobRow.attempt + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        return JobLease(
            job_id=row.id, owner_id=row.owner_id, fence=new_fence, lease_expires_at=expires
        )

    def heartbeat(self, job_id: str, fence: int, *, now: datetime | None = None) -> bool:
        """Extend the lease of the current claim. False means the claim was lost."""
        now = _now(now)
        session = get_session_factory()()
        try:
            result = session.execute(
                update(KnowledgeIndexJobRow)
                .where(
                    KnowledgeIndexJobRow.id == job_id,
                    KnowledgeIndexJobRow.fence == fence,
                    KnowledgeIndexJobRow.status == "running",
                )
                .values(lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
                .execution_options(synchronize_session=False)
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()

    # ---------------------------------------------------------- execution
    def execute(self, lease: JobLease, *, now: datetime | None = None) -> dict[str, Any]:
        """Index the claimed job's conversions and record the fenced result.

        A side thread renews the lease while a single conversion is still
        indexing, so a long document cannot look abandoned to a recovering
        dispatcher. The thread stops as soon as the fence is lost.
        """
        job_id, owner_id, fence = lease.job_id, lease.owner_id, lease.fence
        session = get_session_factory()()
        try:
            row = session.get(KnowledgeIndexJobRow, job_id)
            source = dict(row.source_json or {}) if row is not None else {}
            fallback_collection = row.collection_id if row is not None else None
        finally:
            session.close()

        conversion_ids = [str(item) for item in source.get("conversion_ids") or []]
        collection_id = source.get("collection_id") or fallback_collection or ""
        documents: list[dict[str, Any]] = []
        total = 0
        stop = threading.Event()
        keeper = threading.Thread(
            target=self._renew_until_stopped, args=(job_id, fence, stop), daemon=True,
            name=f"knowledge-lease-{job_id[-8:]}",
        )
        keeper.start()
        try:
            for conversion_id in conversion_ids:
                if not self.heartbeat(job_id, fence, now=now):
                    # The claim was reclaimed: another attempt owns the job now.
                    return self.get(job_id, owner_id)
                result = self.indexer._index_conversion(owner_id, collection_id, conversion_id)
                documents.append(result)
                total += int(result.get("chunks") or 0)
            failures = [item for item in documents if item.get("status") != "indexed"]
            if failures and not total:
                status = "failed"
            elif failures:
                status = "partial"
            else:
                status = "completed"
            self.finish_claim(
                job_id,
                fence,
                status=status,
                stats={"documents": documents, "chunks": total, "failed": len(failures)},
                error={"code": "INDEX_PARTIAL"} if failures else None,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - job failure is recorded, not hidden
            self.finish_claim(
                job_id,
                fence,
                status="failed",
                stats={},
                error={"code": "INDEX_FAILED", "message": str(exc)},
                now=now,
            )
        finally:
            stop.set()
            keeper.join(timeout=5.0)
        return self.get(job_id, owner_id)

    def _renew_until_stopped(self, job_id: str, fence: int, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                if not self.heartbeat(job_id, fence):
                    return
            except Exception:  # noqa: BLE001 - a missed renewal is retried next interval
                logger.exception("knowledge job %s lease renewal failed", job_id)

    def finish_claim(
        self,
        job_id: str,
        fence: int,
        *,
        status: str,
        stats: dict[str, Any],
        error: dict[str, Any] | None,
        now: datetime | None = None,  # noqa: ARG002 - kept for symmetry with heartbeat
    ) -> bool:
        """Record a terminal result only while this claim is still current."""
        if status not in TERMINAL:
            raise ValueError(f"status terminal inválido: {status}")
        session = get_session_factory()()
        try:
            result = session.execute(
                update(KnowledgeIndexJobRow)
                .where(
                    KnowledgeIndexJobRow.id == job_id,
                    KnowledgeIndexJobRow.fence == fence,
                    KnowledgeIndexJobRow.status == "running",
                )
                .values(
                    status=status,
                    stats_json=stats,
                    error_code=(error or {}).get("code"),
                    error_message=(error or {}).get("message"),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            session.commit()
            written = result.rowcount == 1
        finally:
            session.close()
        if written:
            self.finalize_parent_run(job_id)
        return written

    def finalize_parent_run(self, job_id: str) -> bool:
        """Close the occurrence waiting on this job (idempotent).

        Only an occurrence in ``waiting_children`` is closed here, so a worker
        that still holds the occurrence's own claim is never overwritten.
        """
        from automation.models import AutomationRunRow
        from automation.repository import AutomationRepository

        session = get_session_factory()()
        try:
            job = session.get(KnowledgeIndexJobRow, job_id)
            if job is None or job.status not in TERMINAL:
                return False
            run = session.execute(
                select(AutomationRunRow).where(
                    AutomationRunRow.index_job_id == job_id,
                    AutomationRunRow.state == "waiting_children",
                )
            ).scalars().first()
            if run is None:
                return False
            state = "succeeded" if job.status == "completed" else "partial"
            run.state = state
            run.phase = "finalize"
            run.finished_at = datetime.now(UTC)
            run.lease_until = None
            run.lease_owner = None
            AutomationRepository().append_event(
                session, run_id=run.id, type="RUN_FINISHED", payload={"state": state}
            )
            session.commit()
            return True
        finally:
            session.close()

    # ------------------------------------------------------------ recovery
    def recover(self, *, now: datetime | None = None) -> list[str]:
        """Requeue `running` jobs whose lease expired (or was never written)."""
        now = _now(now)
        session = get_session_factory()()
        try:
            rows = list(
                session.execute(
                    select(KnowledgeIndexJobRow).where(KnowledgeIndexJobRow.status == "running")
                ).scalars()
            )
            recovered: list[str] = []
            for row in rows:
                if _is_legacy(row):
                    continue
                expires = _as_utc(row.lease_expires_at)
                if expires is not None and expires >= now:
                    continue
                row.status = "queued"
                row.lease_owner = None
                row.lease_expires_at = None
                recovered.append(row.id)
            session.commit()
            return recovered
        finally:
            session.close()

    def run_pending(
        self, *, worker_id: str, now: datetime | None = None, limit: int = 10
    ) -> int:
        """Recover expired leases, then claim and execute queued jobs.

        Called from the automation loop tick. Returns how many jobs this call
        executed.
        """
        now = _now(now)
        self.recover(now=now)
        executed = 0
        while executed < limit:
            lease = self.claim_next(worker_id=worker_id, now=now)
            if lease is None:
                break
            self.execute(lease, now=now)
            executed += 1
        return executed

    def pending(self) -> list[Any]:
        session = get_session_factory()()
        try:
            return list(
                session.execute(
                    select(KnowledgeIndexJobRow).where(KnowledgeIndexJobRow.status == "queued")
                ).scalars()
            )
        finally:
            session.close()

    # ------------------------------------------------------------- reads
    def get(self, job_id: str, owner_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            row = session.get(KnowledgeIndexJobRow, job_id)
            if row is None or row.owner_id != owner_id:
                return {"job_id": job_id, "status": "not_found"}
            return self._payload(row)
        finally:
            session.close()

    @staticmethod
    def _payload(row: KnowledgeIndexJobRow) -> dict[str, Any]:
        return {
            "job_id": row.id,
            "status": row.status,
            "collection_id": row.collection_id,
            "source": row.source_json,
            "stats": row.stats_json,
            "error": (
                {"code": row.error_code, "message": row.error_message}
                if row.error_code
                else None
            ),
        }


__all__ = ["JobLease", "KnowledgeJobRunner", "LEASE_SECONDS"]
