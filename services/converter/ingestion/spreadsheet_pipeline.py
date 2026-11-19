"""Spreadsheet -> XLSX/TXT/CSV conversion pipeline (T3, doc 04).

Each sheet is one logical unit of processing: it becomes exactly one
``TableStructure`` (CSV/TXT export) and one ``SheetPlan`` (normalized XLSX).
Provenance is the real cell address (``sheet!A1`` style element ids); bboxes
are never fabricated. Formulas follow the contract's ``cached_or_text``
policy: cached values pass through, otherwise the formula stays text with an
explicit warning — never recalculated, never invented.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any

from audit.events import (
    AuditRecorder,
    EVT_DOCUMENT_REJECTED,
    EVT_DOCUMENT_VALIDATED,
    EVT_RUN_FAILED,
    ProgressSink,
)
from audit.manifest import build_summary
from engine.pipeline import ConversionError, PipelineOptions, PipelineResult
from engine.validation.candidate_validator import ValidationCheck, quality_from_checks
from engine.validation.publication import publication_report
from exports.base import ExportError, ExportOptions, ExportResult
from exports.csv_writer import export_csv
from exports.text_plan import TextArtifactPlan, build_text_plan
from exports.text_writer import export_text
from ingestion.base import IngestionError
from ingestion.spreadsheet_input import (
    SPREADSHEET_READER_VERSION,
    SpreadsheetCell,
    SpreadsheetDocument,
    SpreadsheetOptions,
    read_spreadsheet,
)
from models.document import (
    BBox,
    CellPlan,
    CoverageRow,
    DocumentIR,
    MergedSpan,
    SheetPlan,
    TableStructure,
    TableCell,
    WorkbookPlan,
)
from models.document import PageResult


class SpreadsheetConversionPipeline:
    def __init__(
        self,
        *,
        spreadsheet_path: Path,
        run_id: str,
        conversion_id: str,
        recorder: AuditRecorder,
        sink: ProgressSink | None = None,
        options: PipelineOptions | None = None,
        cancel_check=None,
    ) -> None:
        self.spreadsheet_path = Path(spreadsheet_path)
        self.run_id = run_id
        self.conversion_id = conversion_id
        self.recorder = recorder
        self.sink = sink
        self.options = options or PipelineOptions()
        self.cancel_check = cancel_check
        self._started = time.perf_counter()

    # ------------------------------------------------------------------ run
    def run(self) -> PipelineResult:
        ir = DocumentIR(
            file_name=self.options.source_name or self.spreadsheet_path.name, sha256="", page_count=0
        )
        plan = WorkbookPlan()
        checks: list[dict] = []
        summary: dict = {}
        exports: list[ExportResult] = []
        xlsx: Any = None
        text_plan: TextArtifactPlan | None = None
        publication = "approved"
        status = "completed"
        quality = "pending"
        error: ConversionError | None = None
        document: SpreadsheetDocument | None = None

        try:
            self._progress("validate", 0.05, "Validando a planilha recebida")
            self.recorder.stage_start("validate", "Validando a planilha")
            spreadsheet_options = SpreadsheetOptions.from_mapping(self.options.spreadsheet)
            try:
                document = read_spreadsheet(self.spreadsheet_path, spreadsheet_options)
            except IngestionError as exc:
                self.recorder.emit(
                    EVT_DOCUMENT_REJECTED,
                    "validate",
                    exc.message,
                    severity="error",
                    payload={"code": exc.code, **exc.details},
                )
                raise ConversionError(exc.code, exc.message, detail=str(exc.details)) from exc

            import hashlib

            data = self.spreadsheet_path.read_bytes()
            ir.sha256 = hashlib.sha256(data).hexdigest()
            ir.page_count = len(document.sheets)
            ir.metadata = {
                "source_type": "spreadsheet",
                "format": document.format,
                "reader_version": document.reader_version,
                "dialect": document.dialect,
                "detected_encoding": document.detected_encoding,
                "date_system": document.date_system,
                "sheet_count": len(document.sheets),
                "header_policy": spreadsheet_options.header_row,
            }
            self.recorder.emit(
                EVT_DOCUMENT_VALIDATED,
                "validate",
                f"Planilha válida: {document.format} com {len(document.sheets)} aba(s)",
                payload=dict(ir.metadata),
            )
            for warning in document.warnings:
                self.recorder.issue(
                    warning.get("code", "SPREADSHEET_WARNING"),
                    warning.get("message", "Aviso na leitura da planilha."),
                    severity="warning",
                    context=warning,
                )
            self.recorder.stage_end("validate", pages=len(document.sheets))

            self.recorder.stage_start("structure", f"Estruturando {len(document.sheets)} aba(s)")
            total_cells = 0
            for index, sheet in enumerate(document.sheets):
                if self.cancel_check and self.cancel_check():
                    raise ConversionError("CANCELLED", "Conversão cancelada pelo usuário.")
                page_number = index + 1
                table_id = f"tbl_s{page_number:02d}"
                header_labels, data_rows, decision = self._split_header(
                    sheet, spreadsheet_options.header_row
                )
                self.recorder.emit(
                    "SHEET_HEADER_DECISION",
                    "structure",
                    f"Aba '{sheet.name}': cabeçalho {'primeira linha' if decision == 'first_row' else 'técnico'}.",
                    page=page_number,
                    payload={"sheet": sheet.name, "decision": decision},
                )

                display_rows = self._display_rows(sheet)
                table_cells = self._table_cells(sheet, table_id)
                merged = self._merged_spans(sheet)

                table = TableStructure(
                    table_id=table_id,
                    page_number=page_number,
                    bbox=BBox.empty(),
                    strategy="spreadsheet",
                    rows=display_rows,
                    headers=header_labels,
                    cells=table_cells,
                    merged=merged,
                    confidence=1.0,
                    title=sheet.name,
                )
                ir.tables.append(table)

                page = PageResult(
                    page_number=page_number,
                    width=0.0,
                    height=0.0,
                    classification="native",
                    text_source="native",
                    status="ok" if sheet.non_empty_rows() else "partial",
                    metrics={"cells": len(table_cells)},
                )
                # The text plan renders page.tables (per page), not ir.tables.
                page.tables = [table]
                page.coverage = CoverageRow(
                    page_number=page_number,
                    text_blocks=0,
                    tokens=len(table_cells),
                    assigned=len(table_cells),
                    preserved=len(table_cells),
                )
                ir.pages.append(page)

                sheet_plan = self._sheet_plan(sheet, sheet_plan_name=sheet.name, table_id=table_id,
                                              header_labels=header_labels, data_rows=data_rows,
                                              page_number=page_number, decision=decision)
                plan.sheets.append(sheet_plan)
                plan.title = ir.file_name
                total_cells += len(table_cells)
                self._progress(
                    "structure", (index + 1) / max(1, len(document.sheets)),
                    f"Aba {page_number}/{len(document.sheets)} estruturada",
                )
            self.recorder.stage_end("structure", pages=len(document.sheets))

            self._progress("generate", 0.55, "Gerando os formatos solicitados")
            text_plan = build_text_plan(ir, include_table_text=self.options.include_table_text)
            targets = self.options.target_formats or ["xlsx", "txt", "csv"]

            if "xlsx" in targets:
                from engine.excel.writer import write_workbook

                try:
                    # Like the PDF pipeline: the workbook artifact travels in
                    # `PipelineResult.xlsx`; `exports` carries txt/csv results.
                    xlsx = write_workbook(
                        plan, run_id=self.run_id, title=plan.title or ir.file_name
                    )
                    self.recorder.emit(
                        "SPREADSHEET_ARTIFACT_GENERATED",
                        "generate",
                        f"XLSX normalizado gerado: {xlsx.sheets} aba(s), {xlsx.cells} célula(s)",
                        payload={"sheets": xlsx.sheets, "cells": xlsx.cells},
                    )
                except ValueError as exc:
                    self.recorder.issue(
                        "EXPORT_XLSX_FAILED",
                        f"A exportação XLSX não pôde ser produzida: {exc}",
                        severity="error",
                    )

            export_options = ExportOptions(
                format="txt",
                newline=self.options.text_newline,
                page_separator=self.options.page_separator,
                include_table_text=self.options.include_table_text,
                csv_profile=self.options.csv_profile,
                csv_delimiter=self.options.csv_delimiter,
                formula_policy=self.options.csv_formula_policy,
                table_ids=self.options.csv_table_ids,
                value_mode=self.options.csv_value_mode,
            )
            if "txt" in targets:
                try:
                    exports.append(export_text(text_plan, export_options))
                except ExportError as exc:
                    self.recorder.issue(
                        f"EXPORT_{exc.code}",
                        f"A exportação TXT não pôde ser produzida: {exc.message}",
                        severity="error",
                    )
            if "csv" in targets:
                csv_options = ExportOptions(
                    format="csv",
                    newline=self.options.text_newline,
                    page_separator=self.options.page_separator,
                    include_table_text=self.options.include_table_text,
                    csv_profile=self.options.csv_profile,
                    csv_delimiter=self.options.csv_delimiter,
                    formula_policy=self.options.csv_formula_policy,
                    table_ids=self.options.csv_table_ids,
                    value_mode=self.options.csv_value_mode,
                )
                try:
                    exports.append(export_csv(ir, csv_options))
                except ExportError as exc:
                    self.recorder.issue(
                        f"EXPORT_{exc.code}",
                        f"A exportação CSV não pôde ser produzida: {exc.message}",
                        severity="error",
                    )

            checks.extend(self._checks(document, plan, total_cells))
            has_content = total_cells > 0
            quality = quality_from_checks(
                [ValidationCheck(**check) for check in checks], has_content=has_content
            )
            publication = publication_report(checks, quality)["status"]
            if publication == "quarantined":
                status = "failed"
            self._progress("audit", 0.95, "Auditoria final")
            summary = build_summary(
                conversion_id=self.conversion_id,
                run_id=self.run_id,
                status=self._status_for(status, quality, publication),
                quality=quality,
                ir=ir,
                plan=plan,
                recorder=self.recorder,
                validations=checks,
                hash_chain_valid=self.recorder.verify_chain(),
            )
            summary["source_type"] = "spreadsheet"
            summary["reader"] = {
                "name": "spreadsheet-reader",
                "version": document.reader_version,
                "format": document.format,
            }
            summary["publication_status"] = publication
            summary["exports"] = [item.summary() for item in exports]
            summary["issues"] = [
                {
                    "issue_id": issue.issue_id,
                    "run_id": issue.run_id,
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "page_number": issue.page_number,
                    "context": issue.context,
                }
                for issue in self.recorder.issues
            ]
            status = self._status_for(status, quality, publication)
            summary["status"] = status
            summary["quality"] = quality
            self._progress("audit", 1.0, "Auditoria final concluída")
        except ConversionError as exc:
            error = exc
            status = "failed"
            quality = "failed"
            self.recorder.emit(
                EVT_RUN_FAILED,
                "deliver",
                exc.message,
                severity="error",
                payload={"code": exc.code, "retryable": exc.retryable, "detail": exc.detail},
            )
        except Exception as exc:  # noqa: BLE001
            error = ConversionError(
                "INTERNAL_ERROR",
                "Falha inesperada durante a conversão da planilha.",
                retryable=True,
                detail=f"{exc}\n{traceback.format_exc(limit=8)}",
            )
            status = "failed"
            quality = "failed"
            self.recorder.emit(
                EVT_RUN_FAILED,
                "deliver",
                error.message,
                severity="critical",
                payload={"code": error.code, "detail": str(exc)},
            )

        if not summary:
            summary = build_summary(
                conversion_id=self.conversion_id,
                run_id=self.run_id,
                status=status,
                quality=quality,
                ir=ir,
                plan=plan,
                recorder=self.recorder,
                validations=checks,
                hash_chain_valid=self.recorder.verify_chain(),
            )
            summary["source_type"] = "spreadsheet"
            summary["publication_status"] = publication
            summary["exports"] = [item.summary() for item in exports]
            summary["issues"] = [
                {
                    "issue_id": issue.issue_id,
                    "run_id": issue.run_id,
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "page_number": issue.page_number,
                    "context": issue.context,
                }
                for issue in self.recorder.issues
            ]

        duration = int((time.perf_counter() - self._started) * 1000)
        written_cells = plan.total_cells()
        stats = {
            "pages_total": ir.page_count,
            "pages_processed": len(ir.pages),
            "pages_failed": sum(1 for page in ir.pages if page.status == "failed"),
            "pages_unrecognized": sum(1 for page in ir.pages if page.status == "partial"),
            "tables": len(ir.tables),
            "fields": 0,
            "records": 0,
            "sheets": len(plan.sheets),
            "cells": written_cells,
            "text_blocks": len(ir.all_blocks()),
            "ocr_regions": 0,
            "ocr_pages": 0,
            "warnings": self.recorder.count("warning"),
            "errors": self.recorder.count("error") + self.recorder.count("critical"),
            "duration_ms": duration,
            "publication_status": publication,
            "exports": [item.format for item in exports],
            "source_type": "spreadsheet",
        }
        summary.setdefault("metrics", {}).update(
            {"duration_ms": duration, "pages_processed": len(ir.pages)}
        )

        return PipelineResult(
            ok=error is None,
            status=status,
            quality=quality,
            ir=ir,
            plan=plan,
            xlsx=xlsx,
            summary=summary,
            checks=checks,
            evidence=[],
            validation={
                "is_pdf": False,
                "is_image": False,
                "spreadsheet": True,
                "format": document.format if document else None,
                "sheet_count": len(document.sheets) if document else 0,
                "reader_version": document.reader_version if document else SPREADSHEET_READER_VERSION,
            },
            error=error,
            stage_timings=self.recorder.stage_timings,
            stats=stats,
            coverage=[
                {
                    "page_number": row.page_number,
                    "text_blocks": row.text_blocks,
                    "tokens": row.tokens,
                    "assigned": row.assigned,
                    "preserved": row.preserved,
                    "header_footer": row.header_footer,
                    "unclassified": row.unclassified,
                    "dropped": row.dropped,
                    "coverage_percent": row.coverage_percent,
                }
                for row in (page.coverage for page in ir.pages)
                if row is not None
            ],
            duration_ms=duration,
            exports=exports,
            text_plan=text_plan,
            publication_status=publication,
        )

    # ------------------------------------------------------------- helpers
    def _split_header(self, sheet, header_row: bool | None):
        """Header decision: explicit option wins; None records a deterministic
        heuristic (first row all-string when it has content)."""
        non_empty_first = [
            cell for cell in (sheet.rows[0] if sheet.rows else []) if cell is not None and not cell.is_empty
        ]
        if header_row is True:
            decision = "first_row"
        elif header_row is False:
            decision = "technical"
        elif non_empty_first and all(cell.value_type in {"text", "formula"} for cell in non_empty_first):
            decision = "first_row"
        else:
            decision = "technical"

        if decision == "first_row":
            labels = [
                self._display_value(cell) if cell is not None and not cell.is_empty else f"col_{i + 1}"
                for i, cell in enumerate(sheet.rows[0] or [])
            ]
            return labels, sheet.rows[1:], decision
        width = sheet.max_column or 1
        return [f"col_{i + 1}" for i in range(width)], sheet.rows, "technical"

    @staticmethod
    def _display_value(cell: SpreadsheetCell | None) -> str:
        if cell is None or cell.is_empty:
            return ""
        if cell.formula_text and cell.value is None:
            return cell.formula_text  # uncached formula stays text
        return cell.original

    def _display_rows(self, sheet) -> list[list[str]]:
        return [[self._display_value(cell) for cell in row] for row in sheet.rows]

    def _table_cells(self, sheet, table_id: str) -> list[TableCell]:
        cells: list[TableCell] = []
        for row_index, row in enumerate(sheet.rows):
            for column_index, cell in enumerate(row):
                if cell is None:
                    continue
                cells.append(
                    TableCell(
                        row=row_index + 1,
                        col=column_index + 1,
                        text=self._display_value(cell),
                        origin="spreadsheet",
                        confidence=1.0,
                    )
                )
        return cells

    @staticmethod
    def _merged_spans(sheet) -> list[MergedSpan]:
        spans: list[MergedSpan] = []
        seen_anchors: dict[str, tuple[int, int]] = {}
        for row_index, row in enumerate(sheet.rows):
            for column_index, cell in enumerate(row):
                if cell is None or not cell.is_merged_anchor:
                    continue
                seen_anchors[cell.cell_address] = (row_index + 1, column_index + 1)
        for address, (row, col) in seen_anchors.items():
            # The reader keeps only anchor/merged flags; spans of 1x1 anchors
            # are recorded so merged ranges survive into the audit trail.
            spans.append(MergedSpan(row=row, col=col, rowspan=1, colspan=1))
        return spans

    def _cell_plan(self, cell: SpreadsheetCell, page_number: int, table_id: str) -> CellPlan | None:
        if cell is None:
            return None
        if cell.is_empty and cell.merged_in_anchor is None:
            return None
        if cell.is_empty and cell.merged_in_anchor is not None:
            return None  # non-anchor merged cells are references, not values

        if cell.value_type == "formula":
            if cell.value is not None:  # cached value passes through
                value, display, value_type = (
                    cell.value,
                    self._display_value(cell),
                    self._cached_type(cell.value),
                )
            else:
                value, display, value_type = (
                    cell.formula_text,
                    cell.formula_text,
                    "text",
                )
        else:
            value, display, value_type = cell.value, self._display_value(cell), cell.value_type

        return CellPlan(
            value=value,
            display=display,
            value_type=value_type,
            header="",
            original=cell.original,
            element_id=f"{table_id}!{cell.cell_address}",
            page_number=page_number,
            bbox=None,  # real evidence is the cell address, never a bbox
            origin="spreadsheet",
            engine=f"spreadsheet-reader/{cell.cell_address}",
            table_id=table_id,
        )

    @staticmethod
    def _cached_type(value: Any) -> str:
        import datetime as _dt

        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, (_dt.datetime, _dt.date)):
            return "date"
        return "text"

    def _sheet_plan(
        self,
        sheet,
        *,
        sheet_plan_name: str,
        table_id: str,
        header_labels: list[str],
        data_rows,
        page_number: int,
        decision: str,
    ) -> SheetPlan:
        grid: list[list[CellPlan | None]] = []
        for row in data_rows:
            grid_row: list[CellPlan | None] = [
                self._cell_plan(cell, page_number, table_id) for cell in row
            ]
            grid.append(grid_row)
        return SheetPlan(
            name=sheet_plan_name,
            kind="table",
            headers=header_labels,
            rows=grid,
            source_pages=[page_number],
            table_id=table_id,
            notes=f"header={decision}",
        )

    def _checks(self, document: SpreadsheetDocument, plan: WorkbookPlan, total_cells: int) -> list[dict]:
        source_cells = sum(
            1
            for sheet in document.sheets
            for row in sheet.rows
            for cell in row
            if cell is not None and not cell.is_empty
        )
        return [
            ValidationCheck(
                check="sheets_accounted",
                status="pass" if len(document.sheets) == len(plan.sheets) else "fail",
                details=f"{len(plan.sheets)} de {len(document.sheets)} aba(s) estruturadas",
                measured=str(len(plan.sheets)),
                expected=str(len(document.sheets)),
            ).to_dict(),
            ValidationCheck(
                check="cells_preserved",
                status="pass" if plan.total_cells() == source_cells else "warn",
                details=(
                    f"{plan.total_cells()} célula(s) publicadas de {source_cells} lidas "
                    "(mescladas não-âncora não duplicam valores)"
                ),
                measured=str(plan.total_cells()),
                expected=str(source_cells),
            ).to_dict(),
            ValidationCheck(
                check="content_present",
                status="pass" if total_cells > 0 else "warn",
                details=(
                    "A planilha possui valores" if total_cells > 0 else "A planilha não possui valores"
                ),
                measured=str(total_cells),
                expected="> 0",
            ).to_dict(),
        ]

    def _status_for(self, status: str, quality: str, publication: str) -> str:
        if publication == "quarantined" or quality == "failed":
            return "failed"
        if quality == "validated":
            return "completed"
        return "completed_with_warnings"

    def _progress(self, stage: str, fraction: float, message: str) -> None:
        if self.sink:
            self.sink.update(
                status={
                    "validate": "validating",
                    "structure": "structuring",
                    "generate": "generating",
                    "audit": "auditing",
                }.get(stage, "processing"),
                stage=stage,
                progress=round(0.05 + 0.95 * max(0.0, min(1.0, fraction)), 4),
                message=message,
            )


__all__ = ["SpreadsheetConversionPipeline"]
