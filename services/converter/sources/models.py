"""SQLAlchemy models for the sources domain (T2, doc 02 §5).

Registered on the shared ``models.db`` metadata so ``init_db()`` (standalone
SQLite) and the Alembic chain (deployed PostgreSQL) produce the same schema.
The Alembic revision ``sources_and_automation`` mirrors these tables exactly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)

from models.db import Base, utcnow

MAX_ID = 64


class SourceRootRow(Base):
    """A granted disk root: the only way disk access may be obtained."""

    __tablename__ = "source_roots"

    id = Column(String(MAX_ID), primary_key=True)
    owner_id = Column(String(MAX_ID), nullable=False, index=True)
    runtime_id = Column(String(128), nullable=False, default="local")
    canonical_path = Column(String(1024), nullable=False)
    label = Column(String(200), nullable=True)
    granted_by = Column(String(MAX_ID), nullable=False)
    grant_channel = Column(String(32), nullable=False, default="local-admin")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_source_roots_owner_active", "owner_id", "revoked_at"),
    )


class FileSourceRow(Base):
    """A registered file/directory inside a granted root."""

    __tablename__ = "file_sources"

    id = Column(String(MAX_ID), primary_key=True)
    owner_id = Column(String(MAX_ID), nullable=False, index=True)
    root_id = Column(
        String(MAX_ID), ForeignKey("source_roots.id", ondelete="RESTRICT"), nullable=False
    )
    relative_path = Column(String(1024), nullable=False)
    kind = Column(String(16), nullable=False)  # file | directory
    name = Column(String(200), nullable=False)
    recursive = Column(Boolean, nullable=False, default=False)
    include_patterns = Column(JSON, nullable=True)
    exclude_patterns = Column(JSON, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    version = Column(Integer, nullable=False, default=1)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_file_sources_owner_enabled", "owner_id", "enabled"),
        Index("ix_file_sources_root", "root_id"),
    )


class SourceImportRow(Base):
    """One import execution; definition frozen at creation time."""

    __tablename__ = "source_imports"

    id = Column(String(MAX_ID), primary_key=True)
    owner_id = Column(String(MAX_ID), nullable=False, index=True)
    source_id = Column(String(MAX_ID), nullable=False, index=True)
    source_version = Column(Integer, nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    schedule_run_id = Column(String(MAX_ID), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    batch_id = Column(String(MAX_ID), nullable=True)
    definition_json = Column(JSON, nullable=False)
    options_json = Column(JSON, nullable=False, default=dict)
    force = Column(Boolean, nullable=False, default=False)
    collection_id = Column(String(MAX_ID), nullable=True)
    total_files = Column(Integer, nullable=False, default=0)
    imported_files = Column(Integer, nullable=False, default=0)
    skipped_files = Column(Integer, nullable=False, default=0)
    failed_files = Column(Integer, nullable=False, default=0)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    lease_owner = Column(String(128), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    fence = Column(Integer, nullable=False, default=0)
    cancel_requested_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_source_import_idem"),
        Index("ix_source_imports_owner_status", "owner_id", "status"),
        Index("ix_source_imports_source_created", "source_id", "created_at"),
    )


class SourceImportItemRow(Base):
    """Per-file progress inside one import."""

    __tablename__ = "source_import_items"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    import_id = Column(
        String(MAX_ID),
        ForeignKey("source_imports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relative_path = Column(String(1024), nullable=False)
    client_item_id = Column(String(128), nullable=False)
    size_bytes = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, default=0)
    mtime_ns = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, default=0)
    sha256 = Column(String(64), nullable=True)
    snapshot_key = Column(String(512), nullable=True)
    upload_id = Column(String(MAX_ID), nullable=True)
    conversion_id = Column(String(MAX_ID), nullable=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("import_id", "client_item_id", name="uq_source_import_item_client"),
        Index("ix_source_import_items_import_status", "import_id", "status"),
    )


class SourceFileVersionRow(Base):
    """Last successfully processed content version per logical source file."""

    __tablename__ = "source_file_versions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    owner_id = Column(String(MAX_ID), nullable=False)
    source_id = Column(String(MAX_ID), nullable=False)
    relative_path = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=False)
    options_hash = Column(String(64), nullable=False)
    last_success_import_id = Column(String(MAX_ID), nullable=False)
    conversion_id = Column(String(MAX_ID), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "source_id",
            "relative_path",
            "sha256",
            "options_hash",
            name="uq_source_file_version_identity",
        ),
        Index("ix_source_file_versions_lookup", "owner_id", "source_id", "relative_path"),
    )


class SourceKnowledgeHeadRow(Base):
    """Current searchable generation per logical file inside a collection."""

    __tablename__ = "source_knowledge_heads"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    owner_id = Column(String(MAX_ID), nullable=False)
    collection_id = Column(String(MAX_ID), nullable=False)
    source_id = Column(String(MAX_ID), nullable=False)
    relative_path = Column(String(1024), nullable=False)
    active_document_id = Column(String(MAX_ID), nullable=False)
    active_run_id = Column(String(MAX_ID), nullable=False)
    index_generation = Column(Integer, nullable=False, default=1)
    source_missing = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "collection_id",
            "source_id",
            "relative_path",
            name="uq_source_knowledge_head_identity",
        ),
        Index("ix_source_knowledge_heads_collection", "owner_id", "collection_id"),
    )


__all__ = [
    "SourceRootRow",
    "FileSourceRow",
    "SourceImportRow",
    "SourceImportItemRow",
    "SourceFileVersionRow",
    "SourceKnowledgeHeadRow",
    "datetime",
]
