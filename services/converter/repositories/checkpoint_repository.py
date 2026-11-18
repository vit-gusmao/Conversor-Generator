"""Checkpoint persistence: one artifact per (run, page, stage).

Artifacts live in object storage; the row holds only metadata and hashes. A
checkpoint is reusable when the source digest, the engine version, the options
hash and the schema version all match. Anything else means "different work",
and the page is reprocessed instead of silently reusing a stale result.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from engine.streaming.checkpoint import SCHEMA_VERSION, decode_artifact, encode_artifact
from models.db import PageCheckpointRow, RunCheckpointRow, get_session_factory, utcnow
from models.document import PageResult
from storage import get_storage

STAGE_EXTRACT = "extract"


@dataclass(slots=True)
class CheckpointRef:
    run_id: str
    page_number: int
    stage: str
    artifact_key: str
    artifact_sha256: str
    status: str


@dataclass(slots=True)
class CheckpointKey:
    """Everything that must match for a checkpoint to be reusable."""

    source_sha256: str
    engine_version: str
    options_hash: str
    schema_version: int = SCHEMA_VERSION

    def matches(self, row: PageCheckpointRow) -> bool:
        return (
            row.source_sha256 == self.source_sha256
            and row.engine_version == self.engine_version
            and row.options_hash == self.options_hash
            and int(row.schema_version) == self.schema_version
            and row.status == "ok"
        )


def options_hash(options: dict[str, Any] | None) -> str:
    import json

    payload = json.dumps(options or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CheckpointRepository:
    def save_page(
        self,
        *,
        run_id: str,
        page: PageResult,
        key: CheckpointKey,
        stage: str = STAGE_EXTRACT,
    ) -> CheckpointRef:
        data = encode_artifact(page)
        digest = hashlib.sha256(data).hexdigest()
        artifact_key = f"runs/{run_id}/pages/{stage}/{page.page_number:06d}.json.gz"
        get_storage().put_bytes(artifact_key, data, "application/gzip")

        session = get_session_factory()()
        try:
            row = session.execute(
                select(PageCheckpointRow).where(
                    PageCheckpointRow.run_id == run_id,
                    PageCheckpointRow.page_number == page.page_number,
                    PageCheckpointRow.stage == stage,
                )
            ).scalar_one_or_none()
            if row is None:
                row = PageCheckpointRow(
                    run_id=run_id,
                    page_number=page.page_number,
                    stage=stage,
                )
                session.add(row)
            row.schema_version = key.schema_version
            row.source_sha256 = key.source_sha256
            row.engine_version = key.engine_version
            row.options_hash = key.options_hash
            row.artifact_key = artifact_key
            row.artifact_sha256 = digest
            row.status = "ok" if page.status != "failed" else "failed"
            row.error_json = (
                {"status": page.status, "metrics": page.metrics} if page.status == "failed" else None
            )
            try:
                session.commit()
            except IntegrityError:
                # Two workers finished the same page: keep the first artifact,
                # the second is byte-identical or the page is retried.
                session.rollback()
                row = session.execute(
                    select(PageCheckpointRow).where(
                        PageCheckpointRow.run_id == run_id,
                        PageCheckpointRow.page_number == page.page_number,
                        PageCheckpointRow.stage == stage,
                    )
                ).scalar_one()
            return CheckpointRef(
                run_id=run_id,
                page_number=page.page_number,
                stage=stage,
                artifact_key=row.artifact_key,
                artifact_sha256=row.artifact_sha256,
                status=row.status,
            )
        finally:
            session.close()

    def load_page(
        self,
        *,
        run_id: str,
        page_number: int,
        key: CheckpointKey,
        stage: str = STAGE_EXTRACT,
    ) -> PageResult | None:
        """Returns the cached page, or None when it must be reprocessed."""
        session = get_session_factory()()
        try:
            row = session.execute(
                select(PageCheckpointRow).where(
                    PageCheckpointRow.run_id == run_id,
                    PageCheckpointRow.page_number == page_number,
                    PageCheckpointRow.stage == stage,
                )
            ).scalar_one_or_none()
            if row is None or not key.matches(row):
                return None
            artifact_key = row.artifact_key
            expected_sha = row.artifact_sha256
            status = row.status
        finally:
            session.close()

        if status != "ok":
            # A failed page is only reused when the failure is deterministic;
            # simplest and safest is to retry it.
            return None

        try:
            data = get_storage().get_bytes(artifact_key)
        except Exception:  # noqa: BLE001
            return None
        if hashlib.sha256(data).hexdigest() != expected_sha:
            # Corrupted artifact: reprocess only this unit.
            return None
        try:
            return decode_artifact(data)
        except Exception:  # noqa: BLE001
            return None

    def existing_pages(
        self,
        *,
        run_id: str,
        key: CheckpointKey,
        stage: str = STAGE_EXTRACT,
    ) -> dict[int, PageCheckpointRow]:
        session = get_session_factory()()
        try:
            rows = session.execute(
                select(PageCheckpointRow).where(
                    PageCheckpointRow.run_id == run_id,
                    PageCheckpointRow.stage == stage,
                    PageCheckpointRow.status == "ok",
                    PageCheckpointRow.source_sha256 == key.source_sha256,
                    PageCheckpointRow.engine_version == key.engine_version,
                    PageCheckpointRow.options_hash == key.options_hash,
                    PageCheckpointRow.schema_version == key.schema_version,
                )
            ).scalars().all()
            return {int(row.page_number): row for row in rows}
        finally:
            session.close()

    def delete_run(self, run_id: str) -> None:
        session = get_session_factory()()
        try:
            session.query(PageCheckpointRow).filter(PageCheckpointRow.run_id == run_id).delete()
            session.query(RunCheckpointRow).filter(RunCheckpointRow.run_id == run_id).delete()
            session.commit()
        finally:
            session.close()

    def set_phase_cursor(
        self,
        *,
        run_id: str,
        phase: str,
        cursor: str | None,
        event_sequence: int = 0,
    ) -> None:
        session = get_session_factory()()
        try:
            row = session.execute(
                select(RunCheckpointRow).where(
                    RunCheckpointRow.run_id == run_id, RunCheckpointRow.phase == phase
                )
            ).scalar_one_or_none()
            if row is None:
                row = RunCheckpointRow(run_id=run_id, phase=phase)
                session.add(row)
            row.cursor = cursor
            row.last_event_sequence = max(int(row.last_event_sequence or 0), int(event_sequence))
            row.updated_at = utcnow()
            session.commit()
        finally:
            session.close()

    def get_phase_cursor(self, *, run_id: str, phase: str) -> RunCheckpointRow | None:
        session = get_session_factory()()
        try:
            return session.execute(
                select(RunCheckpointRow).where(
                    RunCheckpointRow.run_id == run_id, RunCheckpointRow.phase == phase
                )
            ).scalar_one_or_none()
        finally:
            session.close()


__all__ = [
    "CheckpointRepository",
    "CheckpointKey",
    "CheckpointRef",
    "STAGE_EXTRACT",
    "options_hash",
]
