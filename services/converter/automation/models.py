"""SQLAlchemy models for the automation domain (T2, docs 02/05).

Occurrence keys are globally unique (``schedule:revision:instant``). Leases
and fences follow the same discipline as the durable conversion queue: every
update carries the fence, and recovery bumps it so stale workers cannot
finalize results.
"""

from __future__ import annotations

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


class AutomationScheduleRow(Base):
    """Definition of one recurring import task."""

    __tablename__ = "automation_schedules"

    id = Column(String(64), primary_key=True)
    owner_id = Column(String(64), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    source_id = Column(String(64), nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=True)
    archived = Column(Boolean, nullable=False, default=False)
    revision = Column(Integer, nullable=False, default=1)
    rule_json = Column(JSON, nullable=False)
    timezone = Column(String(100), nullable=False, default="UTC")
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    anchor_at = Column(DateTime(timezone=True), nullable=True)
    misfire_policy = Column(String(32), nullable=False, default="run_once_latest")
    overlap_policy = Column(String(32), nullable=False, default="skip")
    options_json = Column(JSON, nullable=False, default=dict)
    index_collection_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_automation_schedules_due", "enabled", "next_run_at"),
        Index("ix_automation_schedules_owner", "owner_id", "created_at"),
    )


class AutomationRunRow(Base):
    """One occurrence of a schedule; unique by ``occurrence_key``."""

    __tablename__ = "automation_runs"

    id = Column(String(64), primary_key=True)
    owner_id = Column(String(64), nullable=False)
    schedule_id = Column(String(64), nullable=False)
    schedule_revision = Column(Integer, nullable=False)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    trigger = Column(String(16), nullable=False, default="schedule")
    occurrence_key = Column(String(256), nullable=False)
    snapshot_json = Column(JSON, nullable=False)
    state = Column(String(32), nullable=False, default="queued", index=True)
    phase = Column(String(32), nullable=True)
    import_id = Column(String(64), nullable=True, index=True)
    index_job_id = Column(String(64), nullable=True)
    attempt = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(128), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    fence = Column(Integer, nullable=False, default=0)
    #: Durable cancellation request. The executor cooperates at phase
    #: boundaries; a published result stays in the history.
    cancel_requested_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("occurrence_key", name="uq_automation_run_occurrence"),
        Index("ix_automation_runs_state_lease", "state", "lease_until"),
        Index("ix_automation_runs_owner_schedule", "owner_id", "schedule_id", "created_at"),
    )


class AutomationEventRow(Base):
    """Append-only timeline; SSE replay uses (run_id, sequence)."""

    __tablename__ = "automation_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    run_id = Column(
        String(64),
        ForeignKey("automation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence = Column(Integer, nullable=False)
    type = Column(String(64), nullable=False)
    payload_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_automation_event_sequence"),
    )


__all__ = ["AutomationScheduleRow", "AutomationRunRow", "AutomationEventRow"]
