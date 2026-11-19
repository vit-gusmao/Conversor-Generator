"""Retention and cleanup for import snapshots and automation history (T8).

Rules from doc 05 §11:
- snapshots are removed only after a confirmed upload and outside any active
  lease;
- failed occurrences are kept ``AUTOMATION_FAILURE_RETENTION_DAYS`` (7);
- detailed history is kept ``AUTOMATION_HISTORY_RETENTION_DAYS`` (90);
- originals and conversion artifacts are NEVER touched here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from automation.models import AutomationEventRow, AutomationRunRow
from config import settings
from models.db import SourceImportItemRow, SourceImportRow, get_session_factory

logger = logging.getLogger("automation.cleanup")

TERMINAL_RUN_STATES = ("succeeded", "partial", "no_changes", "failed", "cancelled", "skipped")
UPLOADED_ITEM_STATES = ("submitted", "completed", "failed", "skipped")


def cleanup_snapshots(*, now: datetime | None = None) -> dict[str, int]:
    """Delete private snapshot copies no longer needed by any active import."""
    now = now or datetime.now(UTC)
    staging = Path(settings.data_dir) / "sources" / "staging"
    deleted = 0
    kept = 0
    session = get_session_factory()()
    try:
        active_imports = {
            row.id
            for row in session.execute(
                select(SourceImportRow).where(
                    SourceImportRow.status.in_(
                        ("pending", "scanning", "submitting", "converting", "indexing")
                    )
                )
            ).scalars()
        }
        items = list(
            session.execute(
                select(SourceImportItemRow).where(SourceImportItemRow.snapshot_key.is_not(None))
            ).scalars()
        )
        for item in items:
            if item.import_id in active_imports:
                kept += 1
                continue
            if item.status not in UPLOADED_ITEM_STATES:
                kept += 1
                continue
            path = staging / item.snapshot_key
            try:
                if path.exists():
                    path.unlink()
                    deleted += 1
                item.snapshot_key = None
            except OSError:
                kept += 1
        session.commit()
    finally:
        session.close()
    return {"snapshots_deleted": deleted, "snapshots_kept": kept}


def cleanup_history(*, now: datetime | None = None) -> dict[str, int]:
    """Delete automation history past its retention window (never artifacts)."""
    now = now or datetime.now(UTC)
    history_days = int(getattr(settings, "automation_history_retention_days", 90))
    failure_days = int(getattr(settings, "automation_failure_retention_days", 7))
    history_cutoff = now - timedelta(days=history_days)
    failure_cutoff = now - timedelta(days=failure_days)

    removed_runs = 0
    session = get_session_factory()()
    try:
        rows = list(
            session.execute(
                select(AutomationRunRow).where(AutomationRunRow.state.in_(TERMINAL_RUN_STATES))
            ).scalars()
        )
        for run in rows:
            finished = run.finished_at or run.updated_at or run.created_at
            if finished is None:
                continue
            cutoff = failure_cutoff if run.state == "failed" else history_cutoff
            if finished >= cutoff:
                continue
            session.execute(
                AutomationEventRow.__table__.delete().where(AutomationEventRow.run_id == run.id)
            )
            session.delete(run)
            removed_runs += 1
        session.commit()
    finally:
        session.close()
    if removed_runs:
        logger.info("automation cleanup removed %s occurrence(s)", removed_runs)
    return {"runs_removed": removed_runs}


def run_cleanup(*, now: datetime | None = None) -> dict[str, int]:
    """Idempotent: running twice with no new data changes nothing."""
    result = cleanup_snapshots(now=now)
    result.update(cleanup_history(now=now))
    return result


__all__ = ["cleanup_history", "cleanup_snapshots", "run_cleanup"]
