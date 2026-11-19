"""Transactional operations for the sources domain (T2).

No HTTP here. Repositories receive a SQLAlchemy ``Session`` and do the
owner-checked reads/writes the routes and importer build on. Every domain
reference also checks the owner: foreign IDs answer "not found".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sources.models import (
    FileSourceRow,
    SourceFileVersionRow,
    SourceImportItemRow,
    SourceImportRow,
    SourceKnowledgeHeadRow,
    SourceRootRow,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class SourceRepository:
    """Owner-scoped persistence for roots, sources, imports and versions."""

    # ------------------------------------------------------------------ roots
    def create_root(
        self,
        session: Session,
        *,
        owner_id: str,
        runtime_id: str,
        canonical_path: str,
        granted_by: str,
        label: str | None = None,
        grant_channel: str = "local-admin",
    ) -> SourceRootRow:
        root = SourceRootRow(
            id=f"root_{self._hex()}",
            owner_id=owner_id,
            runtime_id=runtime_id,
            canonical_path=canonical_path,
            label=label,
            granted_by=granted_by,
            grant_channel=grant_channel,
        )
        session.add(root)
        session.flush()
        return root

    def get_root(self, session: Session, *, root_id: str, owner_id: str) -> SourceRootRow | None:
        return session.execute(
            select(SourceRootRow).where(
                SourceRootRow.id == root_id,
                SourceRootRow.owner_id == owner_id,
                SourceRootRow.revoked_at.is_(None),
            )
        ).scalar_one_or_none()

    def list_roots(self, session: Session, *, owner_id: str) -> list[SourceRootRow]:
        return list(
            session.execute(
                select(SourceRootRow)
                .where(SourceRootRow.owner_id == owner_id, SourceRootRow.revoked_at.is_(None))
                .order_by(SourceRootRow.created_at)
            ).scalars()
        )

    def revoke_root(self, session: Session, *, root_id: str, owner_id: str) -> bool:
        root = self.get_root(session, root_id=root_id, owner_id=owner_id)
        if root is None:
            return False
        root.revoked_at = utcnow()
        return True

    # ---------------------------------------------------------------- sources
    def create_source(
        self,
        session: Session,
        *,
        owner_id: str,
        root_id: str,
        relative_path: str,
        kind: str,
        name: str,
        recursive: bool = False,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> FileSourceRow:
        source = FileSourceRow(
            id=f"src_{self._hex()}",
            owner_id=owner_id,
            root_id=root_id,
            relative_path=relative_path,
            kind=kind,
            name=name,
            recursive=recursive,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        session.add(source)
        session.flush()
        return source

    def get_source(self, session: Session, *, source_id: str, owner_id: str) -> FileSourceRow | None:
        return session.execute(
            select(FileSourceRow).where(
                FileSourceRow.id == source_id,
                FileSourceRow.owner_id == owner_id,
                FileSourceRow.archived_at.is_(None),
            )
        ).scalar_one_or_none()

    def list_sources(self, session: Session, *, owner_id: str) -> list[FileSourceRow]:
        return list(
            session.execute(
                select(FileSourceRow)
                .where(FileSourceRow.owner_id == owner_id, FileSourceRow.archived_at.is_(None))
                .order_by(FileSourceRow.created_at)
            ).scalars()
        )

    def update_source(
        self,
        session: Session,
        *,
        source_id: str,
        owner_id: str,
        expected_version: int,
        changes: dict[str, Any],
    ) -> FileSourceRow | None:
        """Optimistic edit: stale versions return ``None`` without writing."""
        source = self.get_source(session, source_id=source_id, owner_id=owner_id)
        if source is None or source.version != expected_version:
            return None
        allowed = {
            "relative_path",
            "kind",
            "name",
            "recursive",
            "include_patterns",
            "exclude_patterns",
            "enabled",
        }
        for key, value in changes.items():
            if key in allowed:
                setattr(source, key, value)
        source.version = expected_version + 1
        return source

    def archive_source(self, session: Session, *, source_id: str, owner_id: str) -> bool:
        source = self.get_source(session, source_id=source_id, owner_id=owner_id)
        if source is None:
            return False
        source.archived_at = utcnow()
        source.enabled = False
        return True

    # ---------------------------------------------------------------- imports
    def record_import(
        self,
        session: Session,
        *,
        owner_id: str,
        source_id: str,
        source_version: int,
        idempotency_key: str,
        request_hash: str,
        definition: dict[str, Any],
        options: dict[str, Any],
        force: bool = False,
        collection_id: str | None = None,
        schedule_run_id: str | None = None,
    ) -> SourceImportRow:
        """Idempotent by ``(owner_id, idempotency_key)``; caller commits."""
        existing = session.execute(
            select(SourceImportRow).where(
                SourceImportRow.owner_id == owner_id,
                SourceImportRow.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = SourceImportRow(
            id=f"imp_{self._hex()}",
            owner_id=owner_id,
            source_id=source_id,
            source_version=source_version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            definition_json=definition,
            options_json=options,
            force=force,
            collection_id=collection_id,
            schedule_run_id=schedule_run_id,
        )
        session.add(row)
        return row

    def get_import(self, session: Session, *, import_id: str, owner_id: str) -> SourceImportRow | None:
        return session.execute(
            select(SourceImportRow).where(
                SourceImportRow.id == import_id,
                SourceImportRow.owner_id == owner_id,
            )
        ).scalar_one_or_none()

    def add_import_item(
        self,
        session: Session,
        *,
        import_id: str,
        relative_path: str,
        client_item_id: str,
        size_bytes: int = 0,
        mtime_ns: int = 0,
        sha256: str | None = None,
        snapshot_key: str | None = None,
    ) -> SourceImportItemRow:
        item = SourceImportItemRow(
            import_id=import_id,
            relative_path=relative_path,
            client_item_id=client_item_id,
            size_bytes=size_bytes,
            mtime_ns=mtime_ns,
            sha256=sha256,
            snapshot_key=snapshot_key,
        )
        session.add(item)
        return item

    def get_import_item(
        self, session: Session, *, import_id: str, client_item_id: str
    ) -> SourceImportItemRow | None:
        return session.execute(
            select(SourceImportItemRow).where(
                SourceImportItemRow.import_id == import_id,
                SourceImportItemRow.client_item_id == client_item_id,
            )
        ).scalar_one_or_none()

    # -------------------------------------------------------------- versions
    def upsert_source_file_version(
        self,
        session: Session,
        *,
        owner_id: str,
        source_id: str,
        relative_path: str,
        sha256: str,
        options_hash: str,
        import_id: str,
        conversion_id: str | None,
    ) -> SourceFileVersionRow | None:
        """Idempotent identity row; returns None on concurrent duplicate."""
        existing = session.execute(
            select(SourceFileVersionRow).where(
                SourceFileVersionRow.owner_id == owner_id,
                SourceFileVersionRow.source_id == source_id,
                SourceFileVersionRow.relative_path == relative_path,
                SourceFileVersionRow.sha256 == sha256,
                SourceFileVersionRow.options_hash == options_hash,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.last_success_import_id = import_id
            if conversion_id is not None:
                existing.conversion_id = conversion_id
            return existing
        row = SourceFileVersionRow(
            owner_id=owner_id,
            source_id=source_id,
            relative_path=relative_path,
            sha256=sha256,
            options_hash=options_hash,
            last_success_import_id=import_id,
            conversion_id=conversion_id,
        )
        session.add(row)
        return row

    def latest_processed_sha256(
        self, session: Session, *, owner_id: str, source_id: str, relative_path: str
    ) -> tuple[str | None, str | None]:
        """Returns ``(sha256, options_hash)`` of the last processed version."""
        rows = session.execute(
            select(SourceFileVersionRow)
            .where(
                SourceFileVersionRow.owner_id == owner_id,
                SourceFileVersionRow.source_id == source_id,
                SourceFileVersionRow.relative_path == relative_path,
            )
            .order_by(SourceFileVersionRow.updated_at.desc())
            .limit(1)
        ).scalars().first()
        if rows is None:
            return None, None
        return rows.sha256, rows.options_hash

    # ----------------------------------------------------------------- heads
    def promote_knowledge_head(
        self,
        session: Session,
        *,
        owner_id: str,
        collection_id: str,
        source_id: str,
        relative_path: str,
        document_id: str,
        run_id: str,
        index_generation: int,
        source_missing: bool = False,
    ) -> SourceKnowledgeHeadRow:
        """Atomic promotion of the active generation (caller commits)."""
        head = session.execute(
            select(SourceKnowledgeHeadRow).where(
                SourceKnowledgeHeadRow.owner_id == owner_id,
                SourceKnowledgeHeadRow.collection_id == collection_id,
                SourceKnowledgeHeadRow.source_id == source_id,
                SourceKnowledgeHeadRow.relative_path == relative_path,
            )
        ).scalar_one_or_none()
        if head is None:
            head = SourceKnowledgeHeadRow(
                owner_id=owner_id,
                collection_id=collection_id,
                source_id=source_id,
                relative_path=relative_path,
                active_document_id=document_id,
                active_run_id=run_id,
                index_generation=index_generation,
                source_missing=source_missing,
            )
            session.add(head)
            return head
        head.active_document_id = document_id
        head.active_run_id = run_id
        head.index_generation = index_generation
        head.source_missing = source_missing
        return head

    def mark_source_missing(
        self,
        session: Session,
        *,
        owner_id: str,
        collection_id: str,
        source_id: str,
        relative_path: str,
    ) -> bool:
        head = session.execute(
            select(SourceKnowledgeHeadRow).where(
                SourceKnowledgeHeadRow.owner_id == owner_id,
                SourceKnowledgeHeadRow.collection_id == collection_id,
                SourceKnowledgeHeadRow.source_id == source_id,
                SourceKnowledgeHeadRow.relative_path == relative_path,
            )
        ).scalar_one_or_none()
        if head is None:
            return False
        head.source_missing = True
        return True

    @staticmethod
    def _hex() -> str:
        import uuid

        return uuid.uuid4().hex
