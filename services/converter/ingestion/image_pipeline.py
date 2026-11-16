"""Image -> text conversion pipeline.

Images have no native text layer: OCR is the only text source.  The pipeline
therefore fails explicitly when no recogniser is available instead of
publishing an empty "successful" file.

Reused components: preprocessing, OCR engine adapters, layout/reading order,
the textual IR, the TXT exporter and the audit recorder.  XLSX/CSV are not
produced for image inputs (see the conversion matrix).
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from audit.events import (
    EVT_DOCUMENT_REJECTED,
    EVT_DOCUMENT_VALIDATED,
    EVT_NATIVE_TEXT_EXTRACTED,
    EVT_PAGE_ANALYZED,
    EVT_RUN_FAILED,
    AuditRecorder,
    ProgressSink,
)
from audit.manifest import build_summary
from config import settings
from engine.layout.blocks import build_layout
from engine.layout.reading_order import assign_reading_order
from engine.ocr.engines import ocr_capability
from engine.ocr.ocr_runner import OcrRunner
from engine.pipeline import ConversionError, PipelineOptions, PipelineResult
from engine.validation.candidate_validator import ValidationCheck, quality_from_checks
from engine.validation.publication import publication_report
from exports.base import ExportError, ExportOptions, ExportResult
from exports.text_plan import TextArtifactPlan, build_text_plan
from exports.text_writer import export_text
from ingestion.base import IngestedDocument, IngestionError
from ingestion.image_input import decode_image
from models.document import (
    DEST_TEXT,
    CoverageRow,
    DocumentIR,
    PageResult,
    WorkbookPlan,
)


@dataclass(slots=True)
class ImagePipelineResult(PipelineResult):
    ingested: IngestedDocument | None = None


class ImageConversionPipeline:
    def __init__(
        self,
        *,
        image_path: Path,
        run_id: str,
        conversion_id: str,
        recorder: AuditRecorder,
        sink: ProgressSink | None = None,
        options: PipelineOptions | None = None,
        cancel_check=None,
    ) -> None:
        self.image_path = Path(image_path)
        self.run_id = run_id
        self.conversion_id = conversion_id
        self.recorder = recorder
        self.sink = sink
        self.options = options or PipelineOptions()
        self.cancel_check = cancel_check
        self._started = time.perf_counter()

    def run(self) -> PipelineResult:
        ir = DocumentIR(
            file_name=self.options.source_name or self.image_path.name, sha256="", page_count=0
        )
        plan = WorkbookPlan()
        checks: list[dict] = []
        summary: dict = {}
        exports: list[ExportResult] = []
        text_plan: TextArtifactPlan | None = None
        publication = "approved"
        status = "completed"
        quality = "pending"
        error: ConversionError | None = None
        ingested: IngestedDocument | None = None

        try:
            data = self.image_path.read_bytes()
            self._progress("validate", 0.05, "Validando e decodificando a imagem")
            self.recorder.stage_start("validate", "Validando a imagem recebida")
            try:
                ingested = decode_image(data)
            except IngestionError as exc:
                self.recorder.emit(
                    EVT_DOCUMENT_REJECTED,
                    "validate",
                    exc.message,
                    severity="error",
                    payload={"code": exc.code, **exc.details},
                )
                raise ConversionError(exc.code, exc.message, detail=str(exc.details)) from exc

            ir.sha256 = __import__("hashlib").sha256(data).hexdigest()
            ir.page_count = ingested.page_count
            ir.metadata = {
                "source_type": "image",
                "mime_type": ingested.mime_type,
                "decoder": ingested.decoder,
                "decoder_version": ingested.decoder_version,
                **ingested.metadata,
            }
            self.recorder.emit(
                EVT_DOCUMENT_VALIDATED,
                "validate",
                f"Imagem válida: {ingested.metadata.get('format')} com "
                f"{ingested.page_count} quadro(s)",
                payload={
                    "mime_type": ingested.mime_type,
                    "decoder": ingested.decoder,
                    "decoder_version": ingested.decoder_version,
                    "pages": ingested.page_count,
                    "metadata": ingested.metadata,
                    "warnings": ingested.warnings,
                },
            )
            for warning in ingested.warnings:
                self.recorder.issue(
                    warning.get("code", "IMAGE_WARNING"),
                    warning.get("message", "Aviso ao decodificar a imagem."),
                    severity="warning",
                    page=warning.get("frame_index", 0) + 1
                    if warning.get("frame_index") is not None
                    else None,
                    context=warning,
                )
            self.recorder.stage_end("validate", pages=ingested.page_count)

            capability = ocr_capability()
            if not capability.get("available"):
                raise ConversionError(
                    "OCR_UNAVAILABLE",
                    "Nenhum motor de OCR está instalado. Uma imagem não pode ser convertida "
                    "em texto sem reconhecimento.",
                    retryable=False,
                    detail=(
                        "Instale glm-ocr via Ollama, rapidocr-onnxruntime ou tesseract."
                    ),
                )

            self.recorder.stage_start("extract", f"Reconhecendo texto em {ingested.page_count} quadro(s)")
            ocr = OcrRunner(b"", self.recorder)
            page_results: list[PageResult] = []
            for index, page in enumerate(ingested.pages):
                if self.cancel_check and self.cancel_check():
                    raise ConversionError("CANCELLED", "Conversão cancelada pelo usuário.")
                if page.page_number > (self.options.max_pages or settings.max_pages()):
                    break
                page_result = PageResult(
                    page_number=page.page_number,
                    width=float(page.width),
                    height=float(page.height),
                    classification="image",
                    text_source="ocr",
                    metrics={"images": 1, "image_coverage": 1.0},
                )
                outcome = ocr.run_image(
                    page.page_number, page.image, reason="imagem convertida para texto"
                )
                tokens = sorted(
                    outcome.tokens, key=lambda token: (round(token.bbox.y_center, 1), token.bbox.x0)
                )
                page_result.tokens = tokens
                page_result.ocr_chars = outcome.char_count
                ir.ocr_regions += len(tokens)
                if not tokens:
                    page_result.status = "partial"
                    page_result.text_source = "none"
                    self.recorder.issue(
                        "PAGE_WITHOUT_TEXT",
                        f"O quadro {page.page_number} não produziu texto. Ele permanece "
                        "registrado como não recuperado.",
                        severity="warning",
                        page=page.page_number,
                        context={"error": outcome.error, "engine": outcome.engine},
                    )
                else:
                    layout = build_layout(tokens, page.page_number, page.width, page.height)
                    blocks = assign_reading_order(layout.blocks, page.width, page.height)
                    for block in blocks:
                        block.destination = DEST_TEXT
                    page_result.blocks = blocks
                    page_result.coverage = CoverageRow(
                        page_number=page.page_number,
                        text_blocks=len(blocks),
                        tokens=len(tokens),
                        preserved=len(tokens),
                    )
                page_results.append(page_result)
                ir.pages.append(page_result)
                self.recorder.emit(
                    EVT_NATIVE_TEXT_EXTRACTED,
                    "extract",
                    f"Quadro {page.page_number}: {len(tokens)} palavra(s), "
                    f"{len(page_result.blocks)} bloco(s)",
                    page=page.page_number,
                    extraction_method="ocr",
                    payload={
                        "text_source": page_result.text_source,
                        "engine": outcome.engine,
                        "mean_confidence": outcome.mean_confidence,
                        "accepted": outcome.accepted,
                        "rejected": outcome.rejected,
                        "transformations": page.transformations,
                    },
                )
                self.recorder.emit(
                    EVT_PAGE_ANALYZED,
                    "inspect",
                    f"Quadro {page.page_number} processado",
                    page=page.page_number,
                    payload={
                        "classification": "image",
                        "size": [page.width, page.height],
                        "frame_index": page.frame_index,
                        "transformations": page.transformations,
                    },
                )
                fraction = (index + 1) / max(1, ingested.page_count)
                self._progress(
                    "extract", fraction, f"Quadro {page.page_number}/{ingested.page_count} reconhecido"
                )
            self.recorder.stage_end("extract", pages=len(page_results))

            self._progress("structure", 0.6, "Montando o plano textual")
            text_plan = build_text_plan(ir)
            recovered = any(page.has_text for page in text_plan.pages)
            if not recovered:
                raise ConversionError(
                    "NO_TEXT_RECOVERED",
                    "Nenhum texto pôde ser recuperado da imagem.",
                    retryable=False,
                    detail="O OCR não retornou palavras acima do limite de confiança.",
                )

            checks.extend(self._checks(ir, recovered))
            quality = quality_from_checks(
                [ValidationCheck(**check) for check in checks], has_content=recovered
            )
            self._progress("generate", 0.8, "Gerando o arquivo de texto")
            try:
                exports.append(
                    export_text(
                        text_plan,
                        ExportOptions(
                            format="txt",
                            newline=self.options.text_newline,
                            page_separator=self.options.page_separator,
                        ),
                    )
                )
            except ExportError as exc:
                self.recorder.issue(
                    f"EXPORT_{exc.code}",
                    f"A exportação TXT não pôde ser produzida: {exc.message}",
                    severity="error",
                )
            publication = publication_report(checks, quality)["status"]
            if publication == "quarantined":
                status = "failed"

            self.recorder.emit(
                "TEXT_ARTIFACT_GENERATED",
                "generate",
                f"TXT gerado: {exports[0].size_bytes if exports else 0} byte(s)",
                payload={"exports": [item.summary() for item in exports]},
            )
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
            summary["source_type"] = "image"
            summary["decoder"] = {
                "name": ingested.decoder,
                "version": ingested.decoder_version,
                "mime_type": ingested.mime_type,
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
                "Falha inesperada durante a conversão da imagem.",
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
            summary["source_type"] = "image"
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
        stats = {
            "pages_total": ir.page_count,
            "pages_processed": len(ir.pages),
            "pages_failed": sum(1 for page in ir.pages if page.status == "failed"),
            "pages_unrecognized": sum(1 for page in ir.pages if page.status == "partial"),
            "tables": 0,
            "fields": 0,
            "records": 0,
            "sheets": 0,
            "cells": 0,
            "text_blocks": len(ir.all_blocks()),
            "ocr_regions": ir.ocr_regions,
            "ocr_pages": sum(1 for page in ir.pages if page.ocr_chars),
            "warnings": self.recorder.count("warning"),
            "errors": self.recorder.count("error") + self.recorder.count("critical"),
            "duration_ms": duration,
            "publication_status": publication,
            "exports": [item.format for item in exports],
            "source_type": "image",
        }
        summary.setdefault("metrics", {}).update(
            {"duration_ms": duration, "pages_processed": len(ir.pages)}
        )

        return ImagePipelineResult(
            ok=error is None,
            status=status,
            quality=quality,
            ir=ir,
            plan=plan,
            xlsx=None,
            summary=summary,
            checks=checks,
            evidence=[],
            validation={
                "is_pdf": False,
                "is_image": True,
                "page_count": ir.page_count,
                "mime_type": ingested.mime_type if ingested else None,
                "decoder": ingested.decoder if ingested else None,
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
            ingested=ingested,
        )

    def _checks(self, ir: DocumentIR, recovered: bool) -> list[dict]:
        failed = [page for page in ir.pages if page.status == "partial"]
        return [
            ValidationCheck(
                check="text_recovered",
                status="pass" if recovered else "fail",
                details="Texto recuperado por OCR" if recovered else "Nenhum texto recuperado",
                measured=str(sum(len(page.tokens) for page in ir.pages)),
                expected="> 0",
            ).to_dict(),
            ValidationCheck(
                check="pages_accounted",
                status="pass" if len(ir.pages) >= ir.page_count else "fail",
                details=f"{len(ir.pages)} de {ir.page_count} quadro(s) processados",
                measured=str(len(ir.pages)),
                expected=str(ir.page_count),
            ).to_dict(),
            ValidationCheck(
                check="unrecognized_regions",
                status="pass" if not failed else "warn",
                details=(
                    "Todos os quadros produziram texto"
                    if not failed
                    else f"{len(failed)} quadro(s) sem texto: {[p.page_number for p in failed]}"
                ),
                measured=str(len(failed)),
                expected="0",
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
                    "extract": "extracting",
                    "structure": "structuring",
                    "generate": "generating",
                    "audit": "auditing",
                }.get(stage, "processing"),
                stage=stage,
                progress=round(0.05 + 0.95 * max(0.0, min(1.0, fraction)), 4),
                message=message,
            )


__all__ = ["ImageConversionPipeline", "ImagePipelineResult"]
