"""Persistence and ACL for the knowledge base.

Every query starts from ``owner_id``; there is no code path that filters by
owner after ranking.  A collection and its chunks are always scoped to the
session owner.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select

from knowledge.chunker import CHUNKER_VERSION, PARSER_VERSION, ChunkDraft
from models.db import (
    KnowledgeChunkRow,
    KnowledgeCollectionRow,
    KnowledgeIndexJobRow,
    get_session_factory,
)


def _now() -> datetime:
    return datetime.now(UTC)


class KnowledgeRepository:
    # -- collections -------------------------------------------------------
    def create_collection(
        self, *, owner_id: str, name: str, description: str | None = None, scope: str = "documents"
    ) -> dict[str, Any]:
        collection = KnowledgeCollectionRow(
            id=f"col_{uuid.uuid4().hex}",
            owner_id=owner_id,
            name=name.strip()[:200] or "Coleção",
            description=(description or None),
            scope=scope if scope in {"documents", "studies"} else "documents",
        )
        with self._session() as session:
            session.add(collection)
            session.commit()
            return _collection_dict(collection, chunks=0)

    def list_collections(self, owner_id: str) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.execute(
                select(KnowledgeCollectionRow)
                .where(
                    KnowledgeCollectionRow.owner_id == owner_id,
                    KnowledgeCollectionRow.deleted_at.is_(None),
                )
                .order_by(KnowledgeCollectionRow.created_at)
            ).scalars().all()
            counts = dict(
                session.execute(
                    select(KnowledgeChunkRow.collection_id, func.count())
                    .where(
                        KnowledgeChunkRow.owner_id == owner_id,
                        KnowledgeChunkRow.deleted_at.is_(None),
                    )
                    .group_by(KnowledgeChunkRow.collection_id)
                ).all()
            )
            return [_collection_dict(row, chunks=int(counts.get(row.id, 0))) for row in rows]

    def get_collection(self, owner_id: str, collection_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.execute(
                select(KnowledgeCollectionRow).where(
                    KnowledgeCollectionRow.id == collection_id,
                    KnowledgeCollectionRow.owner_id == owner_id,
                    KnowledgeCollectionRow.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            return _collection_dict(row, chunks=0) if row else None

    def ensure_collection(
        self, *, owner_id: str, collection_id: str | None, name: str | None, scope: str
    ) -> dict[str, Any]:
        if collection_id:
            existing = self.get_collection(owner_id, collection_id)
            if existing is None:
                raise LookupError("Coleção não encontrada.")
            return existing
        with self._session() as session:
            row = session.execute(
                select(KnowledgeCollectionRow).where(
                    KnowledgeCollectionRow.owner_id == owner_id,
                    KnowledgeCollectionRow.name == (name or "").strip(),
                    KnowledgeCollectionRow.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if row is not None:
                return _collection_dict(row, chunks=0)
        return self.create_collection(
            owner_id=owner_id,
            name=name or ("Documentos" if scope == "documents" else "Estudos"),
            scope=scope,
        )

    def delete_collection(self, owner_id: str, collection_id: str) -> bool:
        with self._session() as session:
            row = session.execute(
                select(KnowledgeCollectionRow).where(
                    KnowledgeCollectionRow.id == collection_id,
                    KnowledgeCollectionRow.owner_id == owner_id,
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.deleted_at = _now()
            session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.owner_id == owner_id,
                    KnowledgeChunkRow.collection_id == collection_id,
                )
            )
            session.commit()
            return True

    # -- chunks ------------------------------------------------------------
    def replace_document_chunks(
        self,
        *,
        owner_id: str,
        collection_id: str,
        document_id: str,
        drafts: list[ChunkDraft],
        embedding_model: str | None = None,
    ) -> int:
        with self._session() as session:
            session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.owner_id == owner_id,
                    KnowledgeChunkRow.collection_id == collection_id,
                    KnowledgeChunkRow.document_id == document_id,
                )
            )
            for draft in drafts:
                session.add(
                    KnowledgeChunkRow(
                        chunk_id=draft.chunk_id,
                        owner_id=owner_id,
                        collection_id=collection_id,
                        document_id=document_id,
                        run_id=draft.metadata.get("run_id"),
                        revision_id=draft.metadata.get("revision_id"),
                        page_number=draft.page_number,
                        block_id=draft.block_id,
                        chunk_index=draft.chunk_index,
                        text=draft.text,
                        offset_start=draft.offset_start,
                        offset_end=draft.offset_end,
                        token_count=draft.token_count,
                        chunk_hash=draft.chunk_hash,
                        parser_version=PARSER_VERSION,
                        chunker_version=CHUNKER_VERSION,
                        embedding_model=embedding_model,
                        metadata_json=draft.metadata,
                    )
                )
            session.commit()
            return len(drafts)

    def delete_document(self, owner_id: str, collection_id: str, document_id: str) -> int:
        with self._session() as session:
            result = session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.owner_id == owner_id,
                    KnowledgeChunkRow.collection_id == collection_id,
                    KnowledgeChunkRow.document_id == document_id,
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    def delete_run_chunks(self, owner_id: str, run_id: str) -> int:
        with self._session() as session:
            result = session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.owner_id == owner_id,
                    KnowledgeChunkRow.run_id == run_id,
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    def candidates(self, owner_id: str, collection_ids: list[str] | None) -> list[dict[str, Any]]:
        with self._session() as session:
            query = select(KnowledgeChunkRow).where(
                KnowledgeChunkRow.owner_id == owner_id,
                KnowledgeChunkRow.deleted_at.is_(None),
            )
            if collection_ids:
                query = query.where(KnowledgeChunkRow.collection_id.in_(collection_ids))
            rows = session.execute(query).scalars().all()
            return [_chunk_dict(row) for row in rows]

    def chunk_count(self, owner_id: str) -> int:
        with self._session() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(KnowledgeChunkRow)
                    .where(
                        KnowledgeChunkRow.owner_id == owner_id,
                        KnowledgeChunkRow.deleted_at.is_(None),
                    )
                ).scalar()
                or 0
            )

    # -- index jobs --------------------------------------------------------
    def create_job(
        self, *, owner_id: str, collection_id: str | None, source: dict[str, Any]
    ) -> dict[str, Any]:
        job = KnowledgeIndexJobRow(
            id=f"idx_{uuid.uuid4().hex}",
            owner_id=owner_id,
            collection_id=collection_id,
            status="queued",
            source_json=source,
        )
        with self._session() as session:
            session.add(job)
            session.commit()
            return _job_dict(job)

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        stats: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._session() as session:
            job = session.get(KnowledgeIndexJobRow, job_id)
            if job is None:
                return
            job.status = status
            if stats is not None:
                job.stats_json = stats
            job.error_code = error_code
            job.error_message = error_message
            job.updated_at = _now()
            session.commit()

    def get_job(self, owner_id: str, job_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            job = session.get(KnowledgeIndexJobRow, job_id)
            if job is None or job.owner_id != owner_id:
                return None
            return _job_dict(job)

    def _session(self):
        return get_session_factory()()


def _collection_dict(row: KnowledgeCollectionRow, *, chunks: int) -> dict[str, Any]:
    return {
        "collection_id": row.id,
        "name": row.name,
        "description": row.description,
        "scope": row.scope,
        "chunks": chunks,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _chunk_dict(row: KnowledgeChunkRow) -> dict[str, Any]:
    return {
        "chunk_id": row.chunk_id,
        "owner_id": row.owner_id,
        "collection_id": row.collection_id,
        "document_id": row.document_id,
        "run_id": row.run_id,
        "revision_id": row.revision_id,
        "page_number": row.page_number,
        "block_id": row.block_id,
        "chunk_index": row.chunk_index,
        "text": row.text,
        "offset_start": row.offset_start,
        "offset_end": row.offset_end,
        "token_count": row.token_count,
        "chunk_hash": row.chunk_hash,
        "embedding_model": row.embedding_model,
        "metadata": row.metadata_json or {},
    }


def _job_dict(row: KnowledgeIndexJobRow) -> dict[str, Any]:
    return {
        "job_id": row.id,
        "status": row.status,
        "collection_id": row.collection_id,
        "source": row.source_json or {},
        "stats": row.stats_json or {},
        "error": (
            {"code": row.error_code, "message": row.error_message}
            if row.error_code
            else None
        ),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


__all__ = ["KnowledgeRepository"]
