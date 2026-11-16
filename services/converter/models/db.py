"""SQLAlchemy schema.

Ownership model (see docs/ARCHITECTURE.md):

* The converter service owns every table prefixed with nothing here: runs,
  page results, evidence, audit events, output cells, artifacts.
* In `standalone` mode the same database also carries the *application* tables
  (`public_*`) because a single FastAPI process serves the public API.  In
  `full` mode those tables live in the NestJS database and are unused here.

No table is ever written by two services: `full` mode keeps the two databases
separate and Node never touches converter tables.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


class Run(Base):
    __tablename__ = "runs"

    id = Column(String(64), primary_key=True)
    conversion_id = Column(String(64), nullable=False, index=True)
    attempt = Column(Integer, nullable=False, default=1)
    status = Column(String(32), nullable=False, default="received", index=True)
    stage = Column(String(32), nullable=False, default="receive")
    progress = Column(Float, nullable=False, default=0.0)
    progress_indeterminate = Column(Boolean, nullable=False, default=False)
    quality = Column(String(16), nullable=False, default="pending")
    engine_version = Column(String(32), nullable=False, default="")
    config_json = Column(JSON, nullable=False, default=dict)
    stats_json = Column(JSON, nullable=True)
    validation_json = Column(JSON, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- #
# Per page / region / element
# --------------------------------------------------------------------------- #


class PageResultRow(Base):
    __tablename__ = "page_results"
    __table_args__ = (UniqueConstraint("run_id", "page_number", name="uq_page_run"),)

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number = Column(Integer, nullable=False)
    width = Column(Float, nullable=False, default=0)
    height = Column(Float, nullable=False, default=0)
    rotation = Column(Integer, nullable=False, default=0)
    classification = Column(String(16), nullable=False, default="native")
    status = Column(String(16), nullable=False, default="ok")
    text_source = Column(String(16), nullable=False, default="native")
    native_chars = Column(Integer, nullable=False, default=0)
    ocr_chars = Column(Integer, nullable=False, default=0)
    metrics_json = Column(JSON, nullable=False, default=dict)


class RegionRow(Base):
    __tablename__ = "regions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number = Column(Integer, nullable=False)
    region_id = Column(String(64), nullable=False)
    kind = Column(String(24), nullable=False)
    x0 = Column(Float, nullable=False)
    y0 = Column(Float, nullable=False)
    x1 = Column(Float, nullable=False)
    y1 = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False, default=0)
    engine = Column(String(32), nullable=False, default="")
    resolution = Column(String(16), nullable=False, default="region")
    text_source = Column(String(16), nullable=False, default="native")

    __table_args__ = (Index("ix_regions_run_page", "run_id", "page_number"),)


class TextElementRow(Base):
    __tablename__ = "text_elements"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    element_id = Column(String(64), nullable=False)
    page_number = Column(Integer, nullable=False)
    region_id = Column(String(64), nullable=True)
    text = Column(Text, nullable=False)
    x0 = Column(Float, nullable=False, default=0)
    y0 = Column(Float, nullable=False, default=0)
    x1 = Column(Float, nullable=False, default=0)
    y1 = Column(Float, nullable=False, default=0)
    origin = Column(String(16), nullable=False, default="native")
    font = Column(String(128), nullable=True)
    size = Column(Float, nullable=True)
    direction = Column(String(16), nullable=True)
    confidence = Column(Float, nullable=True)
    engine = Column(String(32), nullable=False, default="")
    kind = Column(String(24), nullable=False, default="block")
    destination = Column(String(24), nullable=True)
    destination_sheet = Column(String(64), nullable=True)
    destination_cell = Column(String(32), nullable=True)
    tokens_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_text_elements_run_page", "run_id", "page_number"),
        Index("ix_text_elements_run_element", "run_id", "element_id"),
    )


class ExtractionCandidateRow(Base):
    __tablename__ = "extraction_candidates"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_id = Column(String(64), nullable=False)
    page_number = Column(Integer, nullable=False)
    region_id = Column(String(64), nullable=True)
    engine = Column(String(48), nullable=False)
    kind = Column(String(24), nullable=False)
    payload_json = Column(JSON, nullable=False, default=dict)
    score = Column(Float, nullable=False, default=0)
    accepted = Column(Boolean, nullable=False, default=False)
    reason = Column(String(255), nullable=True)


class DecisionRow(Base):
    __tablename__ = "decisions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    decision_id = Column(String(64), nullable=False)
    stage = Column(String(32), nullable=False)
    subject = Column(String(255), nullable=False)
    accepted = Column(Boolean, nullable=False, default=False)
    algorithm = Column(String(64), nullable=False)
    reason_codes_json = Column(JSON, nullable=False, default=list)
    alternatives = Column(Integer, nullable=False, default=0)
    page_number = Column(Integer, nullable=True)
    payload_json = Column(JSON, nullable=False, default=dict)


# --------------------------------------------------------------------------- #
# Semantic results
# --------------------------------------------------------------------------- #


class ExtractedFieldRow(Base):
    __tablename__ = "extracted_fields"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    record_id = Column(String(64), nullable=False)
    page_number = Column(Integer, nullable=False)
    label = Column(String(255), nullable=False)
    value = Column(Text, nullable=False)
    value_type = Column(String(24), nullable=False, default="text")
    confidence = Column(Float, nullable=False, default=0)
    separator = Column(String(16), nullable=False, default="colon")
    x0 = Column(Float, nullable=False, default=0)
    y0 = Column(Float, nullable=False, default=0)
    x1 = Column(Float, nullable=False, default=0)
    y1 = Column(Float, nullable=False, default=0)
    section = Column(String(255), nullable=True)
    element_ids_json = Column(JSON, nullable=False, default=list)


class ExtractedTableRow(Base):
    __tablename__ = "extracted_tables"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    table_id = Column(String(64), nullable=False)
    page_number = Column(Integer, nullable=False)
    sheet_name = Column(String(64), nullable=True)
    strategy = Column(String(24), nullable=False)
    headers_json = Column(JSON, nullable=False, default=list)
    rows_json = Column(JSON, nullable=False, default=list)
    n_rows = Column(Integer, nullable=False, default=0)
    n_cols = Column(Integer, nullable=False, default=0)
    confidence = Column(Float, nullable=False, default=0)
    bbox_json = Column(JSON, nullable=False, default=list)
    merged_json = Column(JSON, nullable=False, default=list)
    continued_from = Column(String(64), nullable=True)
    continued_on_page = Column(Integer, nullable=True)
    alternatives_json = Column(JSON, nullable=False, default=list)
    cell_range = Column(String(32), nullable=True)


class OutputSheetRow(Base):
    __tablename__ = "output_sheets"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sheet_id = Column(String(64), nullable=False)
    name = Column(String(64), nullable=False)
    kind = Column(String(24), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    n_rows = Column(Integer, nullable=False, default=0)
    n_cols = Column(Integer, nullable=False, default=0)
    source_pages_json = Column(JSON, nullable=False, default=list)
    table_id = Column(String(64), nullable=True)


class OutputCellRow(Base):
    __tablename__ = "output_cells"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sheet = Column(String(64), nullable=False)
    cell_ref = Column(String(16), nullable=False)
    row_index = Column(Integer, nullable=False)
    col_index = Column(Integer, nullable=False)
    header = Column(String(255), nullable=False, default="")
    value_text = Column(Text, nullable=True)
    original_value = Column(Text, nullable=True)
    value_type = Column(String(24), nullable=False, default="text")
    number_format = Column(String(64), nullable=True)
    record_id = Column(String(64), nullable=True)
    field_label = Column(String(255), nullable=True)
    table_id = Column(String(64), nullable=True)
    element_id = Column(String(64), nullable=True)
    page_number = Column(Integer, nullable=True)
    bbox_json = Column(JSON, nullable=True)
    origin = Column(String(16), nullable=True)
    engine = Column(String(32), nullable=True)
    confidence = Column(Float, nullable=True)

    __table_args__ = (
        Index("ix_output_cells_run_sheet", "run_id", "sheet"),
        UniqueConstraint("run_id", "sheet", "cell_ref", name="uq_cell_run_sheet_ref"),
    )


class CellEvidenceRow(Base):
    __tablename__ = "cell_evidence"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    sheet = Column(String(64), nullable=False)
    cell_ref = Column(String(16), nullable=False)
    element_id = Column(String(64), nullable=False)
    relation = Column(String(32), nullable=False, default="source")


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_event_seq"),
        Index("ix_audit_events_stage", "run_id", "stage", "sequence"),
        Index("ix_audit_events_type", "run_id", "event_type"),
        Index("ix_audit_events_page", "run_id", "page_number"),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)
    event_id = Column(String(64), nullable=False)
    event_type = Column(String(64), nullable=False)
    stage = Column(String(32), nullable=False)
    severity = Column(String(16), nullable=False, default="info")
    page_number = Column(Integer, nullable=True)
    region_id = Column(String(64), nullable=True)
    extraction_method = Column(String(32), nullable=True)
    message = Column(Text, nullable=False, default="")
    payload_json = Column(JSON, nullable=False, default=dict)
    previous_hash = Column(String(64), nullable=True)
    event_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class AuditIssueRow(Base):
    __tablename__ = "audit_issues"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    issue_id = Column(String(64), nullable=False)
    severity = Column(String(16), nullable=False)
    code = Column(String(64), nullable=False)
    message = Column(Text, nullable=False)
    page_number = Column(Integer, nullable=True)
    region_id = Column(String(64), nullable=True)
    context_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (Index("ix_issues_run_sev", "run_id", "severity"),)


class ValidationResultRow(Base):
    __tablename__ = "validation_results"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    check_name = Column(String(64), nullable=False)
    status = Column(String(8), nullable=False)
    details = Column(Text, nullable=False, default="")
    measured = Column(Text, nullable=True)
    expected = Column(Text, nullable=True)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_id = Column(String(64), nullable=False)
    kind = Column(String(32), nullable=False)
    path = Column(String(512), nullable=False)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    retention_days = Column(Integer, nullable=False, default=30)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class ExportArtifactRow(Base):
    """A downloadable artifact produced from a run's IR.

    ``publication_status`` is checked before any download; a quarantined file
    has metadata but no public link.
    """

    __tablename__ = "export_artifacts"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    export_id = Column(String(64), nullable=False, unique=True)
    format = Column(String(16), nullable=False)
    filename = Column(String(255), nullable=False)
    mime_type = Column(String(128), nullable=False)
    storage_key = Column(String(512), nullable=False)
    sha256 = Column(String(64), nullable=False)
    size_bytes = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, default=0)
    options_json = Column(JSON, nullable=False, default=dict)
    warnings_json = Column(JSON, nullable=False, default=list)
    manifest_json = Column(JSON, nullable=False, default=dict)
    publication_status = Column(String(16), nullable=False, default="approved", index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


# --------------------------------------------------------------------------- #
# Knowledge base (RAG) and assistant
# --------------------------------------------------------------------------- #


class KnowledgeCollectionRow(Base):
    __tablename__ = "knowledge_collections"

    id = Column(String(64), primary_key=True)
    owner_id = Column(String(64), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    scope = Column(String(24), nullable=False, default="documents")  # documents | studies
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class KnowledgeChunkRow(Base):
    __tablename__ = "knowledge_chunks"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    chunk_id = Column(String(96), nullable=False, unique=True, index=True)
    owner_id = Column(String(64), nullable=False, index=True)
    collection_id = Column(String(64), nullable=False, index=True)
    document_id = Column(String(64), nullable=False)
    run_id = Column(String(64), nullable=True)
    revision_id = Column(String(64), nullable=True)
    page_number = Column(Integer, nullable=True)
    block_id = Column(String(96), nullable=True)
    chunk_index = Column(Integer, nullable=False, default=0)
    text = Column(Text, nullable=False)
    offset_start = Column(Integer, nullable=False, default=0)
    offset_end = Column(Integer, nullable=False, default=0)
    token_count = Column(Integer, nullable=False, default=0)
    chunk_hash = Column(String(64), nullable=False)
    parser_version = Column(String(32), nullable=False, default="ir-v1")
    chunker_version = Column(String(32), nullable=False, default="chunker-v1")
    embedding_model = Column(String(96), nullable=True)
    embedding_json = Column(JSON, nullable=True)
    metadata_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "collection_id", "chunk_id", name="uq_chunk_scope"),
    )


class KnowledgeIndexJobRow(Base):
    __tablename__ = "knowledge_index_jobs"

    id = Column(String(64), primary_key=True)
    owner_id = Column(String(64), nullable=False, index=True)
    collection_id = Column(String(64), nullable=True)
    status = Column(String(24), nullable=False, default="queued", index=True)
    source_json = Column(JSON, nullable=False, default=dict)
    stats_json = Column(JSON, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ConversationRow(Base):
    __tablename__ = "conversations"

    id = Column(String(64), primary_key=True)
    owner_id = Column(String(64), nullable=False, index=True)
    title = Column(String(200), nullable=False, default="Nova conversa")
    collection_id = Column(String(64), nullable=True)
    document_id = Column(String(64), nullable=True)
    run_id = Column(String(64), nullable=True)
    model = Column(String(96), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"

    id = Column(String(64), primary_key=True)
    conversation_id = Column(
        String(64), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id = Column(String(64), nullable=True, index=True)
    role = Column(String(16), nullable=False)  # user | assistant | system
    content = Column(Text, nullable=False, default="")
    citations_json = Column(JSON, nullable=False, default=list)
    model = Column(String(96), nullable=True)
    status = Column(String(24), nullable=False, default="completed")
    error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class CoverageRowModel(Base):
    __tablename__ = "coverage_rows"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    page_number = Column(Integer, nullable=False)
    text_blocks = Column(Integer, nullable=False, default=0)
    tokens = Column(Integer, nullable=False, default=0)
    assigned = Column(Integer, nullable=False, default=0)
    preserved = Column(Integer, nullable=False, default=0)
    header_footer = Column(Integer, nullable=False, default=0)
    unclassified = Column(Integer, nullable=False, default=0)
    dropped = Column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("run_id", "page_number", name="uq_coverage_page"),)


class EngineUsageRow(Base):
    __tablename__ = "engine_usage"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    engine = Column(String(48), nullable=False)
    kind = Column(String(24), nullable=False)
    regions = Column(Integer, nullable=False, default=0)
    pages = Column(Integer, nullable=False, default=0)
    duration_ms = Column(Integer, nullable=False, default=0)
    accepted = Column(Integer, nullable=False, default=0)
    rejected = Column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("run_id", "engine", "kind", name="uq_engine_run"),)


class StageTimingRow(Base):
    __tablename__ = "stage_timings"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    stage = Column(String(32), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=False, default=0)
    pages = Column(Integer, nullable=False, default=0)


# --------------------------------------------------------------------------- #
# Standalone-mode application tables
# --------------------------------------------------------------------------- #


class PublicUserRow(Base):
    __tablename__ = "public_users"

    id = Column(String(64), primary_key=True)
    email = Column(String(320), nullable=True, unique=True)
    password_hash = Column(String(255), nullable=True)
    name = Column(String(255), nullable=True)
    role = Column(String(16), nullable=False, default="owner")
    is_anonymous = Column(Boolean, nullable=False, default=False)
    device_id = Column(String(128), nullable=True, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class PublicDocumentRow(Base):
    __tablename__ = "public_documents"

    id = Column(String(64), primary_key=True)
    filename = Column(String(512), nullable=False)
    mime_type = Column(String(128), nullable=False, default="application/pdf")
    size_bytes = Column(BigInteger().with_variant(Integer, "sqlite"), nullable=False, default=0)
    sha256 = Column(String(64), nullable=False, index=True)
    storage_key = Column(String(512), nullable=False)
    page_count = Column(Integer, nullable=True)
    owner_id = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class PublicConversionRow(Base):
    __tablename__ = "public_conversions"

    id = Column(String(64), primary_key=True)
    document_id = Column(String(64), ForeignKey("public_documents.id"), nullable=False)
    owner_id = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="received", index=True)
    quality = Column(String(16), nullable=False, default="pending")
    progress = Column(Float, nullable=False, default=0.0)
    stage = Column(String(32), nullable=False, default="receive")
    active_run_id = Column(String(64), nullable=True)
    options_json = Column(JSON, nullable=False, default=dict)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    stats_json = Column(JSON, nullable=True)
    validation_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class PublicEventRow(Base):
    __tablename__ = "public_events"
    __table_args__ = (UniqueConstraint("conversion_id", "sequence", name="uq_pub_event"),)

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    conversion_id = Column(String(64), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    type = Column(String(24), nullable=False)
    status = Column(String(32), nullable=False)
    stage = Column(String(32), nullable=False)
    progress = Column(Float, nullable=False, default=0)
    progress_indeterminate = Column(Boolean, nullable=False, default=False)
    message = Column(Text, nullable=False, default="")
    payload_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class IdempotencyKeyRow(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("owner_id", "key", name="uq_idem_owner_key"),)

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    owner_id = Column(String(64), nullable=False)
    key = Column(String(128), nullable=False)
    conversion_id = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


# --------------------------------------------------------------------------- #
# Engine / session factory
# --------------------------------------------------------------------------- #

_engine = None
_SessionFactory = None


def _engine_kwargs() -> dict:
    if settings.converter_database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}, "pool_pre_ping": True}
    return {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.converter_database_url, future=True, **_engine_kwargs())
    return _engine


def get_session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionFactory


def init_db() -> None:
    """Create tables when they do not exist and apply additive upgrades.

    Alembic migrations are shipped for real deployments; this keeps the
    standalone mode zero-setup.  ``create_all`` never alters an existing table,
    so a new additive column is applied explicitly and only when missing.
    """
    from sqlalchemy import text

    engine = get_engine()
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()
    Base.metadata.create_all(engine)
    _ensure_additive_columns(engine)


def _ensure_additive_columns(engine) -> None:
    """Apply additive columns introduced after the first release.

    These are safe by construction: nullable/defaulted columns that old readers
    ignore.  Destructive migrations remain the responsibility of Alembic.
    """
    from sqlalchemy import text

    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            columns = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(public_conversions)")
            }
            if columns and "options_json" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE public_conversions ADD COLUMN options_json JSON DEFAULT '{}'"
                )
                conn.commit()
        return
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            conn.execute(
                text(
                    "ALTER TABLE public_conversions "
                    "ADD COLUMN IF NOT EXISTS options_json JSON DEFAULT '{}'::json"
                )
            )
            conn.commit()


def reset_engine() -> None:
    """Test helper: drop the cached engine so a new URL is honoured."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


__all__ = [
    "Base",
    "Run",
    "PageResultRow",
    "RegionRow",
    "TextElementRow",
    "ExtractionCandidateRow",
    "DecisionRow",
    "ExtractedFieldRow",
    "ExtractedTableRow",
    "OutputSheetRow",
    "OutputCellRow",
    "CellEvidenceRow",
    "AuditEventRow",
    "AuditIssueRow",
    "ValidationResultRow",
    "ArtifactRow",
    "CoverageRowModel",
    "EngineUsageRow",
    "StageTimingRow",
    "PublicUserRow",
    "PublicDocumentRow",
    "PublicConversionRow",
    "PublicEventRow",
    "IdempotencyKeyRow",
    "get_engine",
    "get_session_factory",
    "init_db",
    "reset_engine",
]
