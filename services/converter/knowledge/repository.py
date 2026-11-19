"""Persistence and ACL for the knowledge base.

Every query starts from ``owner_id``; there is no code path that filters by
owner after ranking.  A collection and its chunks are always scoped to the
session owner.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from knowledge.chunker import CHUNKER_VERSION, PARSER_VERSION, ChunkDraft
from models.db import (
    KnowledgeChunkRow,
    KnowledgeCollectionRow,
    KnowledgeIndexJobRow,
    get_session_factory,
)
from sources.models import SourceImportItemRow, SourceImportRow, SourceKnowledgeHeadRow
from sources.repository import SourceRepository


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
        # Same resolved name for lookup and create (create stores name.strip()[:200]),
        # so repeated unnamed submits reuse one collection per owner.
        resolved = (name or "").strip()[:200] or ("Documentos" if scope == "documents" else "Estudos")
        with self._session() as session:
            row = session.execute(
                select(KnowledgeCollectionRow).where(
                    KnowledgeCollectionRow.owner_id == owner_id,
                    KnowledgeCollectionRow.name == resolved,
                    KnowledgeCollectionRow.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if row is not None:
                return _collection_dict(row, chunks=0)
        return self.create_collection(owner_id=owner_id, name=resolved, scope=scope)

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

    # -- source-backed generations (source_knowledge_heads) -----------------
    def source_origin(self, owner_id: str, conversion_id: str) -> dict[str, str] | None:
        """Logical source file that produced ``conversion_id``, or None for manual uploads."""
        with self._session() as session:
            row = session.execute(
                select(SourceImportItemRow.relative_path, SourceImportRow.source_id)
                .join(SourceImportRow, SourceImportRow.id == SourceImportItemRow.import_id)
                .where(
                    SourceImportItemRow.conversion_id == conversion_id,
                    SourceImportRow.owner_id == owner_id,
                )
                .order_by(SourceImportItemRow.id.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return {"source_id": row.source_id, "relative_path": row.relative_path}

    def head_generation(
        self, owner_id: str, collection_id: str, origin: dict[str, str]
    ) -> int | None:
        """Generation currently promoted for the logical file, or None if none yet."""
        with self._session() as session:
            return session.execute(
                select(SourceKnowledgeHeadRow.index_generation).where(
                    SourceKnowledgeHeadRow.owner_id == owner_id,
                    SourceKnowledgeHeadRow.collection_id == collection_id,
                    SourceKnowledgeHeadRow.source_id == origin["source_id"],
                    SourceKnowledgeHeadRow.relative_path == origin["relative_path"],
                )
            ).scalar_one_or_none()

    def promote_source_head(
        self,
        *,
        owner_id: str,
        collection_id: str,
        origin: dict[str, str],
        document_id: str,
        run_id: str,
        expected_generation: int | None,
    ) -> bool:
        """Make ``document_id`` the active generation, only if nothing moved meanwhile.

        ``expected_generation`` is the value observed before the new chunks were
        written. ``None`` means no head existed then, so only a create can win.
        The update is a compare-and-set on ``index_generation``: a stale writer
        matches zero rows and returns False, leaving the promoted head untouched.
        """
        with self._session() as session:
            try:
                if expected_generation is None:
                    # Create-only: an existing head means another writer moved first.
                    # promote_knowledge_head would overwrite it, so check here.
                    existing = session.execute(
                        select(SourceKnowledgeHeadRow.id).where(
                            SourceKnowledgeHeadRow.owner_id == owner_id,
                            SourceKnowledgeHeadRow.collection_id == collection_id,
                            SourceKnowledgeHeadRow.source_id == origin["source_id"],
                            SourceKnowledgeHeadRow.relative_path == origin["relative_path"],
                        )
                    ).first()
                    if existing is not None:
                        return False
                    SourceRepository().promote_knowledge_head(
                        session,
                        owner_id=owner_id,
                        collection_id=collection_id,
                        source_id=origin["source_id"],
                        relative_path=origin["relative_path"],
                        document_id=document_id,
                        run_id=run_id,
                        index_generation=1,
                    )
                    session.commit()
                    return True
                result = session.execute(
                    update(SourceKnowledgeHeadRow)
                    .where(
                        SourceKnowledgeHeadRow.owner_id == owner_id,
                        SourceKnowledgeHeadRow.collection_id == collection_id,
                        SourceKnowledgeHeadRow.source_id == origin["source_id"],
                        SourceKnowledgeHeadRow.relative_path == origin["relative_path"],
                        SourceKnowledgeHeadRow.index_generation == expected_generation,
                    )
                    .values(
                        active_document_id=document_id,
                        active_run_id=run_id,
                        index_generation=expected_generation + 1,
                        source_missing=False,
                    )
                    .execution_options(synchronize_session=False)
                )
                session.commit()
                return result.rowcount == 1
            except IntegrityError:
                # Lost a create race: another writer inserted the head first.
                session.rollback()
                return False

    def active_heads(
        self, owner_id: str, collection_ids: list[str] | None
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        """Head per ``(collection_id, source_id, relative_path)`` for this owner."""
        with self._session() as session:
            query = select(SourceKnowledgeHeadRow).where(
                SourceKnowledgeHeadRow.owner_id == owner_id
            )
            if collection_ids:
                query = query.where(SourceKnowledgeHeadRow.collection_id.in_(collection_ids))
            rows = session.execute(query).scalars().all()
            return {
                (row.collection_id, row.source_id, row.relative_path): {
                    "active_document_id": row.active_document_id,
                    "active_run_id": row.active_run_id,
                    "index_generation": int(row.index_generation),
                    "source_missing": bool(row.source_missing),
                }
                for row in rows
            }

    def mark_source_missing(
        self,
        owner_id: str,
        source_id: str,
        relative_paths: list[str],
        *,
        session: Any | None = None,
    ) -> int:
        """Flag heads whose origin file vanished from the source. Returns rows flagged.

        The head keeps pointing at the last good generation, so search still
        returns its chunks; citations carry ``source_missing``.
        """
        paths = sorted({path for path in relative_paths if path})
        if not paths:
            return 0
        statement = (
            update(SourceKnowledgeHeadRow)
            .where(
                SourceKnowledgeHeadRow.owner_id == owner_id,
                SourceKnowledgeHeadRow.source_id == source_id,
                SourceKnowledgeHeadRow.relative_path.in_(paths),
            )
            .values(source_missing=True)
            .execution_options(synchronize_session=False)
        )
        if session is not None:
            return int(session.execute(statement).rowcount or 0)
        with self._session() as own:
            count = int(own.execute(statement).rowcount or 0)
            own.commit()
            return count

    # -- index jobs --------------------------------------------------------
    def create_job(
        self,
        *,
        owner_id: str,
        collection_id: str | None,
        source: dict[str, Any],
        job_id: str | None = None,
    ) -> dict[str, Any]:
        job = KnowledgeIndexJobRow(
            id=job_id or f"idx_{uuid.uuid4().hex}",
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
