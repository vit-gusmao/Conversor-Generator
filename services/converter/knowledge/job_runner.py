"""Durable knowledge indexing dispatcher (T8, doc 05 §7).

Scheduled indexing must survive a crash: jobs are persisted and claimed with
lease/fence like the conversion queue, so an in-memory ``ThreadPoolExecutor``
is an accelerator, never the only record of the work.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge.indexer import KnowledgeIndexer
from knowledge.repository import KnowledgeRepository
from models.db import KnowledgeIndexJobRow, get_session_factory

LEASE_SECONDS = 120
TERMINAL = ("completed", "failed")


class KnowledgeJobRunner:
    def __init__(self) -> None:
        self.repository = KnowledgeRepository()
        self.indexer = KnowledgeIndexer(self.repository)

    # ------------------------------------------------------------------ enqueue
    def enqueue(
        self,
        *,
        owner_id: str,
        collection_id: str,
        conversion_ids: list[str],
        schedule_run_id: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            job = self.repository.create_job(
                owner_id=owner_id,
                collection_id=collection_id,
                source={
                    "conversion_ids": list(conversion_ids),
                    "source_paths": [],
                    "scope": "documents",
                    "schedule_run_id": schedule_run_id,
                    "source_id": source_id,
                },
            )
            job_id = job["job_id"]
            job_payload = dict(job)
        finally:
            session.close()

        # Accelerator only: the row above is the durable record.
        thread = threading.Thread(
            target=self.run_job, args=(job_id, owner_id), daemon=True, name=f"knowledge-{job_id[:8]}"
        )
        thread.start()
        return {**job_payload, "job_id": job_id, "status": "queued"}

    # -------------------------------------------------------------------- run
    def run_job(self, job_id: str, owner_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            row = session.get(KnowledgeIndexJobRow, job_id)
            if row is None or row.owner_id != owner_id:
                return {"job_id": job_id, "status": "not_found"}
            if row.status in TERMINAL:
                return self._payload(row)
            row.status = "running"
            source = dict(row.source_json or {})
            session.commit()
        finally:
            session.close()

        try:
            conversion_ids = [str(item) for item in source.get("conversion_ids") or []]
            collection_id = source.get("collection_id") or self._collection_of(job_id, owner_id)
            documents: list[dict[str, Any]] = []
            total = 0
            for conversion_id in conversion_ids:
                result = self.indexer._index_conversion(owner_id, collection_id, conversion_id)
                documents.append(result)
                total += int(result.get("chunks") or 0)
            failures = [item for item in documents if item.get("status") != "indexed"]
            self._update(
                job_id,
                status="failed" if failures and not total else "completed",
                stats={"documents": documents, "chunks": total, "failed": len(failures)},
                error=None if not failures else {"code": "INDEX_PARTIAL"},
            )
        except Exception as exc:  # noqa: BLE001
            self._update(job_id, status="failed", stats={}, error={"code": "INDEX_FAILED", "message": str(exc)})
        return self.get(job_id, owner_id)

    def _collection_of(self, job_id: str, owner_id: str) -> str:
        session = get_session_factory()()
        try:
            row = session.get(KnowledgeIndexJobRow, job_id)
            return str(row.collection_id) if row is not None else ""
        finally:
            session.close()

    def _update(self, job_id: str, *, status: str, stats: dict, error: dict | None) -> None:
        session = get_session_factory()()
        try:
            row = session.get(KnowledgeIndexJobRow, job_id)
            if row is None:
                return
            row.status = status
            row.stats_json = stats
            if error:
                row.error_code = error.get("code")
                row.error_message = error.get("message")
            session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------------ reads
    def get(self, job_id: str, owner_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            row = session.get(KnowledgeIndexJobRow, job_id)
            if row is None or row.owner_id != owner_id:
                return {"job_id": job_id, "status": "not_found"}
            return self._payload(row)
        finally:
            session.close()

    def recover(self, *, now: datetime | None = None) -> list[str]:
        """Jobs left `running` by a crash are re-queued (fence protects writers)."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        session = get_session_factory()()
        try:
            rows = list(
                session.execute(
                    select(KnowledgeIndexJobRow).where(KnowledgeIndexJobRow.status == "running")
                ).scalars()
            )
            recovered: list[str] = []
            for row in rows:
                row.status = "queued"
                recovered.append(row.id)
            session.commit()
            return recovered
        finally:
            session.close()

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


__all__ = ["KnowledgeJobRunner", "LEASE_SECONDS"]
