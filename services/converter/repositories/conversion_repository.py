"""Persistence layer.

Write side: the pipeline calls `persist()` once, which stores the complete
provenance graph in a single transaction (run, pages, regions, blocks, fields,
tables, sheets, cells, evidence, events, issues, validations, coverage,
engine usage, timings, artifact references).

Read side: every audit screen is served from these tables; nothing is
recomputed on read, so what the user sees is exactly what was recorded.

The same module also owns the standalone application tables (`public_*`)
because in `RUNTIME_MODE=standalone` one process serves everything and only one
service writes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import func, select

from audit.events import AuditEvent, AuditRecorder, canonical_timestamp, payload_hash
from audit.manifest import build_export_text
from config import settings
from engine.pipeline import PipelineResult
from models.db import (
    ArtifactRow,
    AuditEventRow,
    AuditIssueRow,
    CellEvidenceRow,
    CoverageRowModel,
    DecisionRow,
    EngineUsageRow,
    ExportArtifactRow,
    ExtractedFieldRow,
    ExtractedTableRow,
    OutputCellRow,
    OutputSheetRow,
    PageResultRow,
    PublicConversionRow,
    PublicDocumentRow,
    PublicEventRow,
    PublicUserRow,
    RegionRow,
    Run,
    StageTimingRow,
    TextElementRow,
    ValidationResultRow,
    get_session_factory,
)
from storage import get_storage

TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class RunProgressSink:
    """Persists status transitions and emits public SSE events."""

    def __init__(self, repository: "ConversionRepository", run_id: str) -> None:
        self.repo = repository
        self.run_id = run_id
        self._sequence = 0

    def update(
        self,
        *,
        status: str,
        stage: str,
        progress: float,
        indeterminate: bool = False,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.repo.update_run_progress(
            self.run_id,
            status=status,
            stage=stage,
            progress=progress,
            indeterminate=indeterminate,
            message=message,
            payload=payload,
        )


class ConversionRepository:
    # ------------------------------------------------------------------ #
    # Runs
    # ------------------------------------------------------------------ #
    def create_run(
        self,
        *,
        run_id: str,
        conversion_id: str,
        attempt: int = 1,
        engine_version: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        with self._session() as session:
            existing = session.get(Run, run_id)
            if existing is not None:
                return
            session.add(
                Run(
                    id=run_id,
                    conversion_id=conversion_id,
                    attempt=attempt,
                    status="received",
                    stage="receive",
                    progress=0.0,
                    quality="pending",
                    engine_version=engine_version,
                    config_json=config or {},
                    started_at=utcnow(),
                )
            )
            session.commit()

    def progress_sink(self, run_id: str) -> RunProgressSink:
        return RunProgressSink(self, run_id)

    def update_run_progress(
        self,
        run_id: str,
        *,
        status: str,
        stage: str,
        progress: float,
        indeterminate: bool = False,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            run.status = status
            run.stage = stage
            run.progress = round(max(0.0, min(1.0, progress)), 4)
            run.progress_indeterminate = indeterminate
            run.updated_at = utcnow()
            session.commit()
            conversion_id = run.conversion_id

            conversion = session.get(PublicConversionRow, conversion_id)
            if conversion is not None:
                conversion.status = status
                conversion.stage = stage
                conversion.progress = run.progress
                conversion.updated_at = utcnow()
                session.commit()

            self._emit_public_event(
                session,
                conversion_id=conversion_id,
                type_="stage" if stage else "progress",
                status=status,
                stage=stage,
                progress=run.progress,
                indeterminate=indeterminate,
                message=message,
                payload=payload or {},
            )
            session.commit()

    def _emit_public_event(
        self,
        session,
        *,
        conversion_id: str,
        type_: str,
        status: str,
        stage: str,
        progress: float,
        indeterminate: bool,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        if not conversion_id:
            return
        last = session.execute(
            select(func.max(PublicEventRow.sequence)).where(
                PublicEventRow.conversion_id == conversion_id
            )
        ).scalar()
        session.add(
            PublicEventRow(
                conversion_id=conversion_id,
                sequence=int(last or 0) + 1,
                type=type_,
                status=status,
                stage=stage,
                progress=progress,
                progress_indeterminate=indeterminate,
                message=message[:2000],
                payload_json=payload,
            )
        )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        quality: str,
        stats: dict[str, Any],
        validation: dict[str, Any] | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            run.status = status
            run.quality = quality
            run.stage = "done" if status in {"completed", "completed_with_warnings"} else run.stage
            run.progress = 1.0 if status in {"completed", "completed_with_warnings"} else run.progress
            run.stats_json = stats
            run.validation_json = validation
            run.error_code = error_code
            run.error_message = error_message
            run.finished_at = utcnow()
            run.updated_at = utcnow()
            session.commit()
            conversion = session.get(PublicConversionRow, run.conversion_id)
            if conversion is not None:
                conversion.status = status
                conversion.quality = quality
                conversion.stage = run.stage
                conversion.progress = run.progress
                conversion.stats_json = stats
                conversion.validation_json = validation
                conversion.error_code = error_code
                conversion.error_message = error_message
                conversion.updated_at = utcnow()
                session.commit()
            self._emit_public_event(
                session,
                conversion_id=run.conversion_id,
                type_="completed" if status != "failed" else "failed",
                status=status,
                stage="done" if status != "failed" else "failed",
                progress=run.progress,
                indeterminate=False,
                message=error_message or "Conversão finalizada",
                payload={"quality": quality, "stats": stats},
            )
            session.commit()

    def cancel_run(self, run_id: str) -> bool:
        with self._session() as session:
            run = session.get(Run, run_id)
            if run is None or run.status in TERMINAL:
                return False
            run.status = "cancelled"
            run.quality = "failed"
            run.error_code = "CANCELLED"
            run.error_message = "Conversão cancelada."
            run.finished_at = utcnow()
            # The public view and the event stream must agree with the run:
            # otherwise the client keeps polling while the API already says
            # "cancelled".
            conversion = session.execute(
                select(PublicConversionRow).where(PublicConversionRow.active_run_id == run_id)
            ).scalar_one_or_none()
            if conversion is not None:
                conversion.status = "cancelled"
                conversion.quality = "failed"
                conversion.finished_at = utcnow()
                self._emit_public_event(
                    session,
                    conversion_id=conversion.id,
                    type_="status",
                    status="cancelled",
                    stage="done",
                    progress=1.0,
                    indeterminate=False,
                    message="Conversão cancelada pelo usuário.",
                    payload={"quality": "failed", "code": "CANCELLED"},
                )
            session.commit()
            return True

    # ------------------------------------------------------------------ #
    # Persist the whole result
    # ------------------------------------------------------------------ #
    def persist(self, result: PipelineResult, recorder: AuditRecorder) -> None:
        run_id = recorder.run_id
        plan = result.plan
        ir = result.ir
        started = utcnow()

        with self._session() as session:
            self._clear_run(session, run_id)

            for page in ir.pages:
                session.add(
                    PageResultRow(
                        run_id=run_id,
                        page_number=page.page_number,
                        width=page.width,
                        height=page.height,
                        rotation=page.rotation,
                        classification=page.classification,
                        status=page.status,
                        text_source=page.text_source,
                        native_chars=page.native_chars,
                        ocr_chars=page.ocr_chars,
                        metrics_json={k: round(v, 4) for k, v in page.metrics.items()},
                    )
                )
                for region in page.regions:
                    session.add(
                        RegionRow(
                            run_id=run_id,
                            page_number=page.page_number,
                            region_id=region.region_id,
                            kind=region.kind,
                            x0=region.bbox.x0,
                            y0=region.bbox.y0,
                            x1=region.bbox.x1,
                            y1=region.bbox.y1,
                            confidence=region.confidence,
                            engine=region.engine,
                            resolution=region.resolution,
                            text_source=region.text_source,
                        )
                    )
                for block in page.blocks:
                    session.add(
                        TextElementRow(
                            run_id=run_id,
                            element_id=block.element_id,
                            page_number=page.page_number,
                            region_id=None,
                            text=block.text[:20000],
                            x0=block.bbox.x0,
                            y0=block.bbox.y0,
                            x1=block.bbox.x1,
                            y1=block.bbox.y1,
                            origin=block.origin,
                            font=block.font,
                            size=block.size,
                            direction=block.direction,
                            confidence=block.confidence,
                            engine=block.engine,
                            kind=block.kind,
                            destination=block.destination,
                            destination_sheet=block.destination_sheet,
                            destination_cell=block.destination_cell,
                            tokens_count=sum(len(line.words) for line in block.lines),
                        )
                    )

            for record in ir.records:
                for field in record.fields:
                    session.add(
                        ExtractedFieldRow(
                            run_id=run_id,
                            record_id=record.record_id,
                            page_number=field.page_number,
                            label=field.label[:255],
                            value=field.value[:20000],
                            value_type="text",
                            confidence=field.confidence,
                            separator=field.separator,
                            x0=field.bbox.x0,
                            y0=field.bbox.y0,
                            x1=field.bbox.x1,
                            y1=field.bbox.y1,
                            section=(record.section or "")[:255] or None,
                            element_ids_json=field.element_ids,
                        )
                    )

            for table in ir.tables:
                session.add(
                    ExtractedTableRow(
                        run_id=run_id,
                        table_id=table.table_id,
                        page_number=table.page_number,
                        sheet_name=None,
                        strategy=table.strategy,
                        headers_json=table.headers,
                        rows_json=[[cell[:2000] for cell in row] for row in table.rows],
                        n_rows=len(table.rows),
                        n_cols=table.n_cols,
                        confidence=table.confidence,
                        bbox_json=table.bbox.to_list(),
                        merged_json=[
                            {
                                "row": span.row,
                                "col": span.col,
                                "rowspan": span.rowspan,
                                "colspan": span.colspan,
                            }
                            for span in table.merged
                        ],
                        continued_from=table.continued_from,
                        continued_on_page=table.continued_on_page,
                        alternatives_json=[
                            {
                                "strategy": alt.strategy,
                                "confidence": alt.confidence,
                                "accepted": alt.accepted,
                                "reason": alt.reason,
                            }
                            for alt in table.alternatives
                        ],
                        cell_range=None,
                    )
                )

            for sheet in plan.sheets:
                session.add(
                    OutputSheetRow(
                        run_id=run_id,
                        sheet_id=f"sheet_{sheet.order:03d}",
                        name=sheet.name,
                        kind=sheet.kind,
                        order_index=sheet.order,
                        n_rows=sheet.n_rows,
                        n_cols=sheet.n_cols,
                        source_pages_json=sheet.source_pages,
                        table_id=sheet.table_id,
                    )
                )

            for sheet in plan.sheets:
                for row_index, row in enumerate(sheet.rows, start=2):
                    for col_index, cell in enumerate(row, start=1):
                        if cell is None:
                            continue
                        from audit.evidence import column_letter

                        ref = f"{column_letter(col_index)}{row_index}"
                        session.add(
                            OutputCellRow(
                                run_id=run_id,
                                sheet=sheet.name,
                                cell_ref=ref,
                                row_index=row_index,
                                col_index=col_index,
                                header=cell.header[:255],
                                value_text=_stringify(cell.value),
                                original_value=cell.original,
                                value_type=cell.value_type,
                                number_format=cell.number_format,
                                record_id=cell.record_id,
                                field_label=(cell.field_label or "")[:255] or None,
                                table_id=cell.table_id,
                                element_id=cell.element_id,
                                page_number=cell.page_number,
                                bbox_json=cell.bbox.to_list() if cell.bbox else None,
                                origin=cell.origin,
                                engine=cell.engine,
                                confidence=cell.confidence,
                            )
                        )
                        if cell.element_id:
                            session.add(
                                CellEvidenceRow(
                                    run_id=run_id,
                                    sheet=sheet.name,
                                    cell_ref=ref,
                                    element_id=cell.element_id,
                                    relation="source",
                                )
                            )
            session.flush()

            for event in recorder.events:
                session.add(
                    AuditEventRow(
                        run_id=run_id,
                        sequence=event.sequence,
                        event_id=event.event_id,
                        event_type=event.event_type,
                        stage=event.stage,
                        severity=event.severity,
                        page_number=event.page_number,
                        region_id=event.region_id,
                        extraction_method=event.extraction_method,
                        message=event.message[:8000],
                        payload_json=event.payload,
                        previous_hash=event.previous_hash,
                        event_hash=event.event_hash,
                        created_at=event.created_at,
                    )
                )
            for issue in recorder.issues:
                session.add(
                    AuditIssueRow(
                        run_id=run_id,
                        issue_id=issue.issue_id,
                        severity=issue.severity,
                        code=issue.code,
                        message=issue.message[:8000],
                        page_number=issue.page_number,
                        region_id=issue.region_id,
                        context_json=issue.context,
                    )
                )
            for decision in recorder.decisions:
                session.add(
                    DecisionRow(
                        run_id=run_id,
                        decision_id=decision.decision_id,
                        stage=decision.stage,
                        subject=decision.subject[:255],
                        accepted=decision.accepted,
                        algorithm=decision.algorithm,
                        reason_codes_json=decision.reason_codes,
                        alternatives=decision.alternatives,
                        page_number=decision.page_number,
                        payload_json=decision.payload,
                    )
                )
            for check in result.checks:
                session.add(
                    ValidationResultRow(
                        run_id=run_id,
                        check_name=check["check"],
                        status=check["status"],
                        details=str(check.get("details") or "")[:4000],
                        measured=check.get("measured"),
                        expected=check.get("expected"),
                    )
                )
            for row in result.coverage:
                session.add(
                    CoverageRowModel(
                        run_id=run_id,
                        page_number=row["page_number"],
                        text_blocks=row["text_blocks"],
                        tokens=row["tokens"],
                        assigned=row["assigned"],
                        preserved=row["preserved"],
                        header_footer=row["header_footer"],
                        unclassified=row["unclassified"],
                        dropped=row["dropped"],
                    )
                )
            for usage in ir.engines.values():
                session.add(
                    EngineUsageRow(
                        run_id=run_id,
                        engine=usage.engine[:48],
                        kind=usage.kind,
                        regions=usage.regions,
                        pages=usage.pages,
                        duration_ms=usage.duration_ms,
                        accepted=usage.accepted,
                        rejected=usage.rejected,
                    )
                )
            for timing in recorder.stage_timings:
                session.add(
                    StageTimingRow(
                        run_id=run_id,
                        stage=timing["stage"],
                        started_at=_parse_dt(timing.get("started_at")) or started,
                        finished_at=_parse_dt(timing.get("finished_at")),
                        duration_ms=int(timing.get("duration_ms") or 0),
                        pages=int(timing.get("pages") or 0),
                    )
                )
            session.commit()

        # Artifacts (outside the DB transaction: object storage is not
        # transactional, and the audit records the reference right after).
        if result.xlsx is not None:
            key = f"runs/{run_id}/output.xlsx"
            stored = get_storage().put_bytes(
                key,
                result.xlsx.data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            self._artifact(run_id, "xlsx", key, stored.sha256, stored.size_bytes, settings.retention_xlsx_days)

        if result.text_plan is not None:
            # Persisted so a later TXT export with different options (newline,
            # page separator) never re-runs OCR or re-parses the document.
            payload = gzip.compress(
                json.dumps(_text_plan_payload(result.text_plan), ensure_ascii=False).encode("utf-8")
            )
            key = f"runs/{run_id}/text_plan.json.gz"
            stored = get_storage().put_bytes(key, payload, "application/gzip")
            self._artifact(
                run_id, "text_plan", key, stored.sha256, stored.size_bytes, settings.retention_xlsx_days
            )

        for export in result.exports:
            self.store_export(
                run_id, export, publication_status=result.publication_status
            )

        if result.summary:
            key = f"runs/{run_id}/audit.json.gz"
            payload = gzip.compress(
                json.dumps(result.summary, ensure_ascii=False, default=str).encode("utf-8")
            )
            stored = get_storage().put_bytes(key, payload, "application/gzip")
            self._artifact(
                run_id, "audit_manifest", key, stored.sha256, stored.size_bytes, settings.retention_audit_days
            )

    def store_export(
        self,
        run_id: str,
        export,
        *,
        publication_status: str = "approved",
    ) -> dict[str, Any]:
        """Store one artifact and its metadata.

        Publication is decided by the caller (the publication policy), never by
        the exporter.  A quarantined artifact keeps its metadata for diagnosis
        but cannot be downloaded.
        """
        export_id = new_id("exp")
        extension = export.filename.rsplit(".", 1)[-1] if "." in export.filename else "bin"
        key = f"runs/{run_id}/exports/{export_id}.{extension}"
        stored = get_storage().put_bytes(key, export.data, export.mime_type)
        with self._session() as session:
            session.add(
                ExportArtifactRow(
                    run_id=run_id,
                    export_id=export_id,
                    format=export.format,
                    filename=export.filename,
                    mime_type=export.mime_type,
                    storage_key=key,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    options_json=export.options,
                    warnings_json=export.warnings,
                    manifest_json=export.manifest,
                    publication_status=publication_status,
                )
            )
            session.commit()
        return {
            "export_id": export_id,
            "format": export.format,
            "filename": export.filename,
            "mime_type": export.mime_type,
            "sha256": stored.sha256,
            "size_bytes": stored.size_bytes,
            "warnings": export.warnings,
            "options": export.options,
            "manifest": export.manifest,
            "publication_status": publication_status,
        }

    def exports(self, run_id: str) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.execute(
                select(ExportArtifactRow)
                .where(ExportArtifactRow.run_id == run_id)
                .order_by(ExportArtifactRow.id)
            ).scalars().all()
            return [
                {
                    "export_id": row.export_id,
                    "format": row.format,
                    "filename": row.filename,
                    "mime_type": row.mime_type,
                    "sha256": row.sha256,
                    "size_bytes": row.size_bytes,
                    "warnings": row.warnings_json or [],
                    "options": row.options_json or {},
                    "publication_status": row.publication_status,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "download_url": (
                        f"/api/v1/conversions/{{conversion_id}}/exports/{row.export_id}/download"
                        if row.publication_status == "approved"
                        else None
                    ),
                }
                for row in rows
            ]

    def export_by_id(self, run_id: str, export_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.execute(
                select(ExportArtifactRow).where(
                    ExportArtifactRow.run_id == run_id,
                    ExportArtifactRow.export_id == export_id,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "export_id": row.export_id,
                "format": row.format,
                "filename": row.filename,
                "mime_type": row.mime_type,
                "sha256": row.sha256,
                "size_bytes": row.size_bytes,
                "storage_key": row.storage_key,
                "publication_status": row.publication_status,
                "warnings": row.warnings_json or [],
                "options": row.options_json or {},
                "manifest": row.manifest_json or {},
            }

    def text_plan(self, run_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.execute(
                select(ArtifactRow).where(
                    ArtifactRow.run_id == run_id, ArtifactRow.kind == "text_plan"
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            key = row.path
        try:
            raw = get_storage().get_bytes(key)
            return json.loads(gzip.decompress(raw).decode("utf-8"))
        except Exception:  # noqa: BLE001 - a missing plan is reported, not masked
            return None

    def _artifact(self, run_id: str, kind: str, key: str, sha: str, size: int, days: int) -> None:
        with self._session() as session:
            session.add(
                ArtifactRow(
                    run_id=run_id,
                    artifact_id=new_id("art"),
                    kind=kind,
                    path=key,
                    sha256=sha,
                    size_bytes=size,
                    version=1,
                    retention_days=days,
                )
            )
            session.commit()

    def _clear_run(self, session, run_id: str) -> None:
        for model in (
            PageResultRow,
            RegionRow,
            TextElementRow,
            ExtractedFieldRow,
            ExtractedTableRow,
            OutputSheetRow,
            OutputCellRow,
            CellEvidenceRow,
            AuditEventRow,
            AuditIssueRow,
            DecisionRow,
            ValidationResultRow,
            CoverageRowModel,
            EngineUsageRow,
            StageTimingRow,
            ArtifactRow,
            ExportArtifactRow,
        ):
            session.query(model).filter(model.run_id == run_id).delete()
        session.commit()

    # ------------------------------------------------------------------ #
    # Reads — run state
    # ------------------------------------------------------------------ #
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            run = session.get(Run, run_id)
            return _run_dict(run) if run else None

    def get_run_by_conversion(self, conversion_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            run = session.execute(
                select(Run)
                .where(Run.conversion_id == conversion_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            return _run_dict(run) if run else None

    def events_since(self, conversion_id: str, since: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.execute(
                select(PublicEventRow)
                .where(PublicEventRow.conversion_id == conversion_id, PublicEventRow.sequence > since)
                .order_by(PublicEventRow.sequence)
                .limit(limit)
            ).scalars().all()
            return [
                {
                    "sequence": row.sequence,
                    "type": row.type,
                    "status": row.status,
                    "stage": row.stage,
                    "progress": row.progress,
                    "progress_indeterminate": row.progress_indeterminate,
                    "message": row.message,
                    "payload": row.payload_json,
                    "at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    # ------------------------------------------------------------------ #
    # Reads — result
    # ------------------------------------------------------------------ #
    def result_summary(self, run_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return None
            sheets = session.execute(
                select(OutputSheetRow)
                .where(OutputSheetRow.run_id == run_id)
                .order_by(OutputSheetRow.order_index)
            ).scalars().all()
            cells = session.execute(
                select(func.count())
                .select_from(OutputCellRow)
                .where(
                    OutputCellRow.run_id == run_id,
                    OutputCellRow.value_text.is_not(None),
                    OutputCellRow.value_text != "",
                )
            ).scalar() or 0
            table_count = session.execute(
                select(func.count()).select_from(ExtractedTableRow).where(ExtractedTableRow.run_id == run_id)
            ).scalar() or 0
            pages = session.execute(
                select(func.count()).select_from(PageResultRow).where(PageResultRow.run_id == run_id)
            ).scalar() or 0
            warnings = session.execute(
                select(AuditIssueRow)
                .where(AuditIssueRow.run_id == run_id, AuditIssueRow.severity.in_(["warning", "error", "critical"]))
                .order_by(AuditIssueRow.id)
                .limit(20)
            ).scalars().all()
            stats = run.stats_json or {}
            return {
                "conversion_id": run.conversion_id,
                "status": run.status,
                "quality": run.quality,
                "pages": pages,
                "tables": table_count,
                "sheets_count": len(sheets),
                "cells": cells,
                "records": stats.get("records", 0),
                "fields": stats.get("fields", 0),
                "download_url": f"/api/v1/conversions/{run.conversion_id}/download",
                "audit_url": f"/api/v1/conversions/{run.conversion_id}/audit",
                "sheets": [
                    {
                        "name": sheet.name,
                        "kind": sheet.kind,
                        "rows": sheet.n_rows,
                        "columns": sheet.n_cols,
                        "source_pages": sheet.source_pages_json or [],
                        "table_id": sheet.table_id,
                    }
                    for sheet in sheets
                ],
                "warnings": [_issue_dict(issue) for issue in warnings],
            }

    def preview(self, run_id: str, sheet: str | None, offset: int, limit: int) -> dict[str, Any] | None:
        with self._session() as session:
            sheets = session.execute(
                select(OutputSheetRow)
                .where(OutputSheetRow.run_id == run_id)
                .order_by(OutputSheetRow.order_index)
            ).scalars().all()
            if not sheets:
                return None
            names = [s.name for s in sheets]
            # The consolidated tab is a derived view (union of the other tabs),
            # so it is never the implicit preview: with no explicit choice the
            # app opens the first tab that carries extracted data.
            default_sheet = next((s.name for s in sheets if s.kind != "consolidated"), names[0])
            target = sheet if sheet in names else default_sheet
            meta = next(s for s in sheets if s.name == target)
            rows = session.execute(
                select(OutputCellRow)
                .where(OutputCellRow.run_id == run_id, OutputCellRow.sheet == target)
                .order_by(OutputCellRow.row_index, OutputCellRow.col_index)
            ).scalars().all()

            grouped: dict[int, dict[int, OutputCellRow]] = {}
            for row in rows:
                grouped.setdefault(row.row_index, {})[row.col_index] = row
            headers = [""] * (meta.n_cols or 0)
            if grouped:
                first = grouped[min(grouped)]
                for column, cell in first.items():
                    if 1 <= column <= len(headers):
                        headers[column - 1] = cell.header
            headers = [h or f"Coluna {i + 1}" for i, h in enumerate(headers)]

            total = len(grouped)
            ordered = sorted(grouped)
            page_indices = ordered[offset : offset + limit]
            out_rows: list[list[dict[str, Any]]] = []
            for index in page_indices:
                cells = grouped[index]
                out_rows.append(
                    [
                        {
                            "value": _typed_value(cells.get(col)),
                            "type": cells.get(col).value_type if cells.get(col) else "empty",
                            "display": (
                                cells.get(col).original_value
                                if cells.get(col) and cells.get(col).original_value is not None
                                else (cells.get(col).value_text if cells.get(col) else "")
                            ),
                        }
                        for col in range(1, len(headers) + 1)
                    ]
                )
            return {
                "sheet": target,
                "headers": headers,
                "rows": out_rows,
                "total_rows": total,
                "offset": offset,
                "limit": limit,
            }

    # ------------------------------------------------------------------ #
    # Reads — audit
    # ------------------------------------------------------------------ #
    def audit_summary(self, run_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return None
            events = session.execute(
                select(AuditEventRow).where(AuditEventRow.run_id == run_id).order_by(AuditEventRow.sequence)
            ).scalars().all()
            issues = session.execute(
                select(AuditIssueRow).where(AuditIssueRow.run_id == run_id).order_by(AuditIssueRow.id)
            ).scalars().all()
            coverage = session.execute(
                select(CoverageRowModel)
                .where(CoverageRowModel.run_id == run_id)
                .order_by(CoverageRowModel.page_number)
            ).scalars().all()
            engines = session.execute(
                select(EngineUsageRow).where(EngineUsageRow.run_id == run_id)
            ).scalars().all()
            decisions = session.execute(
                select(DecisionRow)
                .where(DecisionRow.run_id == run_id)
                .order_by(DecisionRow.id)
                .limit(500)
            ).scalars().all()
            checks = session.execute(
                select(ValidationResultRow)
                .where(ValidationResultRow.run_id == run_id)
                .order_by(ValidationResultRow.id)
            ).scalars().all()
            timings = session.execute(
                select(StageTimingRow)
                .where(StageTimingRow.run_id == run_id)
                .order_by(StageTimingRow.id)
            ).scalars().all()
            pages_total = session.execute(
                select(func.count()).select_from(PageResultRow).where(PageResultRow.run_id == run_id)
            ).scalar() or 0
            cells = session.execute(
                select(func.count())
                .select_from(OutputCellRow)
                .where(
                    OutputCellRow.run_id == run_id,
                    OutputCellRow.value_text.is_not(None),
                    OutputCellRow.value_text != "",
                )
            ).scalar() or 0
            tables = session.execute(
                select(func.count()).select_from(ExtractedTableRow).where(ExtractedTableRow.run_id == run_id)
            ).scalar() or 0
            fields = session.execute(
                select(func.count()).select_from(ExtractedFieldRow).where(ExtractedFieldRow.run_id == run_id)
            ).scalar() or 0
            blocks = session.execute(
                select(TextElementRow).where(TextElementRow.run_id == run_id)
            ).scalars().all()

            stats = run.stats_json or {}
            coverage_total = sum(row.tokens for row in coverage)
            assigned = sum(row.assigned for row in coverage)
            return {
                "conversion_id": run.conversion_id,
                "run_id": run.id,
                "quality": run.quality,
                "status": run.status,
                "metrics": {
                    "pages_analyzed": pages_total,
                    "pages_total": pages_total,
                    "blocks_total": len(blocks),
                    "blocks_assigned": sum(1 for b in blocks if b.destination in {"field", "table", "metadata"}),
                    "blocks_preserved": sum(1 for b in blocks if b.destination == "text"),
                    "blocks_unclassified": sum(
                        1 for b in blocks if b.destination in {"unclassified", None}
                    ),
                    "blocks_dropped": sum(1 for b in blocks if b.destination == "dropped"),
                    "cells_generated": cells,
                    "tables_detected": tables,
                    "fields_detected": fields,
                    "records_detected": stats.get("records", 0),
                    "ocr_regions": stats.get("ocr_regions", 0),
                    "events_total": len(events),
                    "issues_total": len(issues),
                    "warnings": sum(1 for i in issues if i.severity == "warning"),
                    "errors": sum(1 for i in issues if i.severity in {"error", "critical"}),
                    "coverage_percent": (
                        round(100.0 * (assigned + sum(r.preserved for r in coverage)
                                       + sum(r.unclassified for r in coverage)
                                       + sum(r.header_footer for r in coverage)) / coverage_total, 2)
                        if coverage_total
                        else 100.0
                    ),
                    "assignment_percent": round(100.0 * assigned / coverage_total, 2) if coverage_total else 100.0,
                },
                "coverage": [
                    {
                        "page_number": row.page_number,
                        "text_blocks": row.text_blocks,
                        "tokens": row.tokens,
                        "assigned": row.assigned,
                        "preserved": row.preserved,
                        "header_footer": row.header_footer,
                        "unclassified": row.unclassified,
                        "dropped": row.dropped,
                        "coverage_percent": (
                            round(
                                100.0
                                * (row.assigned + row.preserved + row.unclassified + row.header_footer)
                                / row.tokens,
                                2,
                            )
                            if row.tokens
                            else 100.0
                        ),
                    }
                    for row in coverage
                ],
                "engines": [
                    {
                        "engine": row.engine,
                        "kind": row.kind,
                        "regions": row.regions,
                        "pages": row.pages,
                        "duration_ms": row.duration_ms,
                        "accepted": row.accepted,
                        "rejected": row.rejected,
                    }
                    for row in engines
                ],
                "decisions": [
                    {
                        "decision_id": row.decision_id,
                        "stage": row.stage,
                        "subject": row.subject,
                        "accepted": row.accepted,
                        "algorithm": row.algorithm,
                        "reason_codes": row.reason_codes_json or [],
                        "alternatives": row.alternatives,
                        "page_number": row.page_number,
                        "at": (run.finished_at or run.created_at).isoformat(),
                    }
                    for row in decisions
                ],
                "validations": [
                    {
                        "check": row.check_name,
                        "status": row.status,
                        "details": row.details,
                        "measured": row.measured,
                        "expected": row.expected,
                    }
                    for row in checks
                ],
                "timings": [
                    {
                        "stage": row.stage,
                        "started_at": row.started_at.isoformat() if row.started_at else "",
                        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                        "duration_ms": row.duration_ms,
                        "pages": row.pages,
                    }
                    for row in timings
                ],
                "hash_chain_valid": _verify_chain(events),
                "generated_at": utcnow().isoformat(),
            }

    def audit_events(
        self,
        run_id: str,
        *,
        stage: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        page: int | None = None,
        since_sequence: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        with self._session() as session:
            query = select(AuditEventRow).where(AuditEventRow.run_id == run_id)
            if stage:
                query = query.where(AuditEventRow.stage == stage)
            if event_type:
                query = query.where(AuditEventRow.event_type == event_type)
            if severity:
                query = query.where(AuditEventRow.severity == severity)
            if page is not None:
                query = query.where(AuditEventRow.page_number == page)
            if since_sequence is not None:
                query = query.where(AuditEventRow.sequence > since_sequence)
            rows = session.execute(
                query.order_by(AuditEventRow.sequence).limit(max(1, min(limit, 500)))
            ).scalars().all()
            total = session.execute(
                select(func.count()).select_from(AuditEventRow).where(AuditEventRow.run_id == run_id)
            ).scalar() or 0
            return {"items": [_event_dict(row) for row in rows], "total": total}

    def pages(self, run_id: str) -> dict[str, Any]:
        """Run-level page list (dimensions, classification, regions count)."""
        with self._session() as session:
            rows = session.execute(
                select(PageResultRow)
                .where(PageResultRow.run_id == run_id)
                .order_by(PageResultRow.page_number)
            ).scalars().all()
            region_counts = dict(
                session.execute(
                    select(RegionRow.page_number, func.count())
                    .where(RegionRow.run_id == run_id)
                    .group_by(RegionRow.page_number)
                ).all()
            )
            coverage = {
                row.page_number: round(
                    100.0
                    * (row.assigned + row.preserved + row.unclassified + row.header_footer)
                    / row.tokens,
                    2,
                )
                if row.tokens
                else 100.0
                for row in session.execute(
                    select(CoverageRowModel).where(CoverageRowModel.run_id == run_id)
                ).scalars()
            }
            items = [
                {
                    "page_number": row.page_number,
                    "width": row.width,
                    "height": row.height,
                    "rotation": row.rotation,
                    "classification": row.classification,
                    "status": row.status,
                    "text_source": row.text_source,
                    "native_chars": row.native_chars,
                    "ocr_chars": row.ocr_chars,
                    "coverage_percent": coverage.get(row.page_number),
                    "regions": int(region_counts.get(row.page_number, 0)),
                }
                for row in rows
            ]
            return {"items": items, "total": len(items)}

    def tables(self, run_id: str) -> dict[str, Any]:
        """Run-level list of the reconstructed tables."""
        with self._session() as session:
            rows = session.execute(
                select(ExtractedTableRow)
                .where(ExtractedTableRow.run_id == run_id)
                .order_by(ExtractedTableRow.page_number, ExtractedTableRow.table_id)
            ).scalars().all()
            items = [
                {
                    "table_id": row.table_id,
                    "page_number": row.page_number,
                    "sheet_name": row.sheet_name,
                    "strategy": row.strategy,
                    "headers": row.headers_json or [],
                    "rows": row.rows_json or [],
                    "n_rows": row.n_rows,
                    "n_cols": row.n_cols,
                    "confidence": row.confidence,
                    "bbox": row.bbox_json or [],
                    "merged": row.merged_json or [],
                    "continued_from": row.continued_from,
                    "continued_on_page": row.continued_on_page,
                }
                for row in rows
            ]
            return {"items": items, "total": len(items)}

    def page_audit(self, run_id: str, page_number: int) -> dict[str, Any] | None:
        with self._session() as session:
            page = session.execute(
                select(PageResultRow).where(
                    PageResultRow.run_id == run_id, PageResultRow.page_number == page_number
                )
            ).scalar_one_or_none()
            if page is None:
                return None
            regions = session.execute(
                select(RegionRow).where(RegionRow.run_id == run_id, RegionRow.page_number == page_number)
            ).scalars().all()
            blocks = session.execute(
                select(TextElementRow).where(
                    TextElementRow.run_id == run_id, TextElementRow.page_number == page_number
                )
            ).scalars().all()
            tables = session.execute(
                select(ExtractedTableRow).where(
                    ExtractedTableRow.run_id == run_id, ExtractedTableRow.page_number == page_number
                )
            ).scalars().all()
            issues = session.execute(
                select(AuditIssueRow).where(
                    AuditIssueRow.run_id == run_id, AuditIssueRow.page_number == page_number
                )
            ).scalars().all()
            return {
                "page_number": page.page_number,
                "width": page.width,
                "height": page.height,
                "rotation": page.rotation,
                "classification": page.classification,
                "status": page.status,
                "text_source": page.text_source,
                "native_chars": page.native_chars,
                "ocr_chars": page.ocr_chars,
                "text_blocks": len(blocks),
                "tables": len(tables),
                "fields": session.execute(
                    select(func.count())
                    .select_from(ExtractedFieldRow)
                    .where(
                        ExtractedFieldRow.run_id == run_id,
                        ExtractedFieldRow.page_number == page_number,
                    )
                ).scalar()
                or 0,
                "regions": [
                    {
                        "region_id": region.region_id,
                        "kind": region.kind,
                        "bbox": [region.x0, region.y0, region.x1, region.y1],
                        "confidence": region.confidence,
                        "engine": region.engine,
                        "resolution": region.resolution,
                    }
                    for region in regions
                ],
                "blocks": [
                    {
                        "element_id": block.element_id,
                        "text": block.text,
                        "bbox": [block.x0, block.y0, block.x1, block.y1],
                        "origin": block.origin,
                        "page_number": block.page_number,
                        "font": block.font,
                        "size": block.size,
                        "direction": block.direction,
                        "confidence": block.confidence,
                        "destination": (
                            {
                                "kind": block.destination,
                                "sheet": block.destination_sheet,
                                "cell": block.destination_cell,
                                "record_id": None,
                            }
                            if block.destination
                            else None
                        ),
                    }
                    for block in blocks
                ],
                "tables_detail": [
                    {
                        "table_id": table.table_id,
                        "page_number": table.page_number,
                        "bbox": table.bbox_json or [0, 0, 0, 0],
                        "strategy": table.strategy,
                        "headers": table.headers_json or [],
                        "rows_count": table.n_rows,
                        "columns_count": table.n_cols,
                        "confidence": table.confidence,
                        "merged_cells": table.merged_json or [],
                        "continued_from_table_id": table.continued_from,
                        "continued_on_page": table.continued_on_page,
                        "sheet": table.sheet_name,
                        "cell_range": table.cell_range,
                        "alternatives": table.alternatives_json or [],
                    }
                    for table in tables
                ],
                "issues": [_issue_dict(issue) for issue in issues],
                "metrics": page.metrics_json or {},
            }

    def cells(
        self, run_id: str, *, sheet: str | None = None, cell: str | None = None, limit: int = 200
    ) -> dict[str, Any]:
        with self._session() as session:
            query = select(OutputCellRow).where(OutputCellRow.run_id == run_id)
            if sheet:
                query = query.where(OutputCellRow.sheet == sheet)
            if cell:
                query = query.where(OutputCellRow.cell_ref == cell)
            rows = session.execute(
                query.order_by(OutputCellRow.sheet, OutputCellRow.row_index, OutputCellRow.col_index).limit(
                    max(1, min(limit, 500))
                )
            ).scalars().all()
            total = session.execute(
                select(func.count()).select_from(OutputCellRow).where(OutputCellRow.run_id == run_id)
            ).scalar() or 0
            items = []
            for row in rows:
                items.append(
                    {
                        "sheet": row.sheet,
                        "cell": row.cell_ref,
                        "row": row.row_index,
                        "column": row.col_index,
                        "header": row.header,
                        "value": row.value_text,
                        "original_value": row.original_value,
                        "value_type": row.value_type,
                        "number_format": row.number_format,
                        "source": (
                            {
                                "element_id": row.element_id,
                                "page_number": row.page_number,
                                "bbox": row.bbox_json or [0, 0, 0, 0],
                                "origin": row.origin or "native",
                                "engine": row.engine or "",
                                "confidence": row.confidence,
                            }
                            if row.element_id
                            else None
                        ),
                        "transformations": _derive_transformations(row),
                        "record_id": row.record_id,
                        "field_label": row.field_label,
                        "table_id": row.table_id,
                        "validation": {
                            "source_preserved": row.original_value is not None or row.element_id is not None,
                            "cell_written": True,
                            "value_matches_source": _value_matches_source(row),
                        },
                    }
                )
            return {"items": items, "total": total}

    def issues(self, run_id: str, severity: str | None = None) -> dict[str, Any]:
        with self._session() as session:
            query = select(AuditIssueRow).where(AuditIssueRow.run_id == run_id)
            if severity:
                query = query.where(AuditIssueRow.severity == severity)
            rows = session.execute(query.order_by(AuditIssueRow.id)).scalars().all()
            return {"items": [_issue_dict(row) for row in rows], "total": len(rows)}

    def export(self, run_id: str, fmt: str = "json") -> tuple[bytes, str, str]:
        summary = self.audit_summary(run_id) or {}
        issues = self.issues(run_id)["items"]
        events = self.audit_events(run_id, limit=500)["items"]
        if fmt == "txt":
            summary_with_issues = {**summary, "issues": issues}
            text = build_export_text(summary_with_issues)
            return text.encode("utf-8"), "text/plain; charset=utf-8", f"auditoria-{run_id}.txt"
        payload = json.dumps(
            {"manifest": summary, "issues": issues, "events": events},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        return payload.encode("utf-8"), "application/json", f"auditoria-{run_id}.json"

    def xlsx_bytes(self, run_id: str) -> bytes | None:
        with self._session() as session:
            artifact = session.execute(
                select(ArtifactRow).where(ArtifactRow.run_id == run_id, ArtifactRow.kind == "xlsx")
            ).scalars().first()
        if artifact is None:
            return None
        return get_storage().get_bytes(artifact.path)

    # ------------------------------------------------------------------ #
    # Session helper
    # ------------------------------------------------------------------ #
    def _session(self):
        return _SessionContext()


class _SessionContext:
    def __enter__(self):
        self.session = get_session_factory()()
        return self.session

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None:
                self.session.rollback()
        finally:
            self.session.close()
        return False


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #


def _text_plan_payload(plan) -> dict[str, Any]:
    return {
        "version": 1,
        "encoding": plan.encoding,
        "newline": plan.newline,
        "page_separator": plan.page_separator,
        "source_sha256": plan.source_sha256,
        "source_name": plan.source_name,
        "warnings": plan.warnings,
        "pages": [
            {
                "page_number": page.page_number,
                "status": page.status,
                "warnings": page.warnings,
                "segments": [
                    {
                        "text": segment.text,
                        "kind": segment.kind,
                        "element_id": segment.element_id,
                        "table_id": segment.table_id,
                        "order": segment.order,
                    }
                    for segment in page.segments
                ],
            }
            for page in plan.pages
        ],
    }


def _typed_value(cell: OutputCellRow | None):
    """Preview values keep their Excel type so the UI can align/sort them."""
    if cell is None or cell.value_text is None:
        return None
    raw = cell.value_text
    try:
        if cell.value_type == "integer":
            return int(float(raw))
        if cell.value_type in {"decimal", "money", "percentage"}:
            return float(raw)
        if cell.value_type == "boolean":
            return raw.strip().lower() == "true"
    except (TypeError, ValueError):
        return raw
    return raw


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _run_dict(run: Run) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "conversion_id": run.conversion_id,
        "attempt": run.attempt,
        "status": run.status,
        "stage": run.stage,
        "progress": run.progress,
        "progress_indeterminate": bool(run.progress_indeterminate),
        "quality": run.quality,
        "engine_version": run.engine_version,
        "stats": run.stats_json or {},
        "validation": run.validation_json,
        "error": (
            {"code": run.error_code, "message": run.error_message, "retryable": run.error_code != "CANCELLED"}
            if run.error_code
            else None
        ),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


def _event_dict(row: AuditEventRow) -> dict[str, Any]:
    return {
        "event_id": row.event_id,
        "run_id": row.run_id,
        "sequence": row.sequence,
        "event_type": row.event_type,
        "stage": row.stage,
        "severity": row.severity,
        "page_number": row.page_number,
        "region_id": row.region_id,
        "extraction_method": row.extraction_method,
        "message": row.message,
        "payload": row.payload_json or {},
        "previous_hash": row.previous_hash,
        "event_hash": row.event_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _issue_dict(row: AuditIssueRow) -> dict[str, Any]:
    return {
        "issue_id": row.issue_id,
        "run_id": row.run_id,
        "severity": row.severity,
        "code": row.code,
        "message": row.message,
        "page_number": row.page_number,
        "region_id": row.region_id,
        "context": row.context_json or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _digits(text: str) -> str:
    return "".join(ch for ch in text or "" if ch.isdigit())


def _digit_groups(text: str) -> list[str]:
    return [group for group in re.findall(r"\d+", text or "") if group]


def _derive_transformations(row: OutputCellRow) -> list[dict[str, str]]:
    """Operations provable from the stored original/normalised pair.

    The detailed per-step list lives in the intermediate representation; what
    can be proven from the persisted row is that the original text was
    normalised into the stored value with the recorded type.  Returning the
    pair is factual; returning an empty list while values differ would not be.
    """
    original = (row.original_value or "").strip()
    normalized = (row.value_text or "").strip()
    if not original or original == normalized:
        return []
    return [
        {
            "step": f"normalize:{row.value_type or 'text'}",
            "from": original,
            "to": normalized,
        }
    ]


def _value_matches_source(row: OutputCellRow) -> bool:
    """Whether the exported value is provably derivable from the source text.

    This is a real check, never an assumption: a cell whose pair cannot be
    reconciled is reported as *not confirmed* instead of claiming a validation
    that never happened.
    """
    original = (row.original_value or "").strip()
    normalized = (row.value_text or "").strip()
    if not original:
        # No raw text was recorded: the only evidence left is the provenance
        # link to a source element.
        return bool(row.element_id)
    if not normalized:
        return False
    if original == normalized:
        return True

    value_type = (row.value_type or "text").lower()
    if value_type in {"date", "datetime"}:
        # 10/03/2026 -> 2026-03-10: the components survive, the order changes.
        return sorted(_digit_groups(original)) == sorted(_digit_groups(normalized))
    if value_type in {"integer", "decimal", "money"}:
        digits = _digits(original)
        return bool(digits) and digits == _digits(normalized)
    if value_type == "percent":
        # 12,5% -> 0.125: the magnitude is divided by one hundred.
        return bool(_digits(original)) and _digits(original).lstrip("0") in _digits(normalized).lstrip("0")
    return False


def _verify_chain(events: Iterable[AuditEventRow]) -> bool:
    previous: str | None = None
    for event in events:
        if event.previous_hash != previous:
            return False
        body_v2 = {
            "seq": event.sequence,
            "type": event.event_type,
            "stage": event.stage,
            "page": event.page_number,
            "region": event.region_id,
            "payload": event.payload_json or {},
            "severity": event.severity,
            "message": event.message,
            "extraction_method": event.extraction_method,
            "created_at": canonical_timestamp(event.created_at),
        }
        expected = hashlib.sha256(
            f"{payload_hash(body_v2)}|{previous or ''}".encode()
        ).hexdigest()
        if expected != event.event_hash:
            # Chains persisted before the hashed body was widened still verify.
            body_v1 = {
                "seq": event.sequence,
                "type": event.event_type,
                "stage": event.stage,
                "page": event.page_number,
                "region": event.region_id,
                "payload": event.payload_json or {},
            }
            expected_v1 = hashlib.sha256(
                f"{payload_hash(body_v1)}|{previous or ''}".encode()
            ).hexdigest()
            if expected_v1 != event.event_hash:
                return False
        previous = event.event_hash
    return True


__all__ = ["ConversionRepository", "RunProgressSink", "new_id"]
