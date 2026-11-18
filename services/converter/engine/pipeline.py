"""The conversion pipeline.

Order of operations (mirrors the functional contract in `docs/PDF-to-Excel.txt`):

    validate -> inspect -> for each page: extract(+OCR) / layout / tables
            -> structure (fields, records, regions)
            -> normalize (locale, dedupe, merge) -> reconcile (unified workbook)
            -> generate XLSX -> audit XLSX -> manifest

Two invariants hold at every step:

1. An event is recorded before the artefact it describes is persisted.
2. A failure never produces invented data — it produces a partial result with
   an explicit issue, or a failed run with the reason.
"""

from __future__ import annotations

import hashlib
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from audit.coverage import aggregate_coverage, build_coverage
from audit.events import (
    EVT_CELL_WRITTEN,
    EVT_COVERAGE_COMPUTED,
    EVT_DOCUMENT_REJECTED,
    EVT_DOCUMENT_VALIDATED,
    EVT_EXCEL_GENERATED,
    EVT_NATIVE_TEXT_EXTRACTED,
    EVT_PAGE_ANALYZED,
    EVT_PAGE_FAILED,
    EVT_PAGE_SKIPPED,
    EVT_TEXT_LAYER_ASSESSED,
    EVT_XLSX_AUDIT_FAILED,
    EVT_XLSX_AUDIT_PASSED,
    AuditRecorder,
    ProgressSink,
)
from audit.evidence import build_cell_evidence
from audit.manifest import build_summary
from config import settings
from engine.excel.auditor import XlsxAudit, audit_xlsx
from engine.excel.writer import ExcelResult, write_workbook
from engine.extraction.native_text import NativePage, classify_page, extract_native, needs_ocr
from engine.extraction.visibility import page_invisible_ratios, page_invisible_ratios_path
from engine.fields.field_extractor import extract_fields
from engine.fields.record_grouper import GroupingConfig, group_records
from engine.inspector.pdf_inspector import PDFInspection, inspect_pdf, inspect_pdf_path
from engine.layout.blocks import build_layout
from engine.layout.reading_order import assign_reading_order
from engine.layout.regions import (
    build_regions,
    detect_repeated_marginals,
    preclassify_blocks,
)
from engine.normalization.normalizer import LocaleProfile, detect_locale
from engine.ocr.ocr_runner import OcrRunner, deduplicate_ocr
from engine.reconciliation.merger import reconcile
from engine.streaming.source import DocumentSource, sha256_file
from engine.structure.resolver import build_workbook
from engine.tables.table_builder import detect_tables
from engine.validation.candidate_validator import (
    ValidationCheck,
    quality_from_checks,
    validate_plan,
)
from engine.validation.publication import publication_report
from repositories.checkpoint_repository import (
    STAGE_EXTRACT,
    CheckpointKey,
    CheckpointRepository,
    options_hash as checkpoint_options_hash,
)
from exports.base import ExportError, ExportOptions, ExportResult
from exports.csv_writer import export_csv
from exports.text_plan import TextArtifactPlan, build_text_plan
from exports.text_writer import export_text
from models.document import (
    ORIGIN_OCR,
    REGION_FOOTER,
    REGION_HEADER,
    BBox,
    DocumentIR,
    EngineUsage,
    Field,
    PageResult,
    Record,
    TextBlock,
    median,
    WorkbookPlan,
)

STAGE_ORDER = ["validate", "inspect", "extract", "structure", "normalize", "reconcile", "generate", "audit"]
_STAGE_BOUNDS = {
    "validate": (0.00, 0.05),
    "inspect": (0.05, 0.10),
    "extract": (0.10, 0.40),
    "structure": (0.40, 0.60),
    "normalize": (0.60, 0.70),
    "reconcile": (0.70, 0.78),
    "generate": (0.78, 0.92),
    "audit": (0.92, 1.00),
}
_STATUS_FOR_STAGE = {
    "validate": "validating",
    "inspect": "inspecting",
    "extract": "extracting",
    "structure": "structuring",
    "normalize": "normalizing",
    "reconcile": "reconciling",
    "generate": "generating",
    "audit": "auditing",
}


class ConversionError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail


@dataclass(slots=True)
class PipelineOptions:
    ocr_enabled: bool = True
    max_pages: int | None = None
    dpi: int | None = None
    stop_after_page: int | None = None
    #: ``None`` keeps the legacy behaviour (XLSX only).  A list enables the
    #: extra artifacts on the same run, without repeating OCR.
    target_formats: list[str] | None = None
    source_name: str | None = None
    text_newline: str = "lf"
    page_separator: str = "form_feed"
    include_table_text: bool = True
    csv_profile: str = "interoperable"
    csv_delimiter: str | None = None
    csv_table_ids: list[str] | None = None
    csv_formula_policy: str = "safe"
    csv_value_mode: str = "original"


@dataclass(slots=True)
class PipelineResult:
    ok: bool
    status: str
    quality: str
    ir: DocumentIR
    plan: WorkbookPlan
    xlsx: ExcelResult | None
    summary: dict
    checks: list[dict]
    evidence: list[dict]
    validation: dict
    error: ConversionError | None = None
    stage_timings: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    coverage: list[dict] = field(default_factory=list)
    duration_ms: int = 0
    exports: list[ExportResult] = field(default_factory=list)
    text_plan: TextArtifactPlan | None = None
    publication_status: str = "approved"


@dataclass(slots=True)
class _PageContext:
    page: PageResult
    native: NativePage


def extraction_fingerprint(options: PipelineOptions) -> dict:
    """Options that change extraction output and therefore invalidate pages."""
    return {
        "ocr_enabled": options.ocr_enabled,
        "dpi": options.dpi,
        "include_table_text": options.include_table_text,
        "schema": "extract-v1",
    }


def cached_pages_enabled() -> bool:
    """Checkpoint reuse can be turned off without disabling the pipeline."""
    return bool(getattr(settings, "checkpoint_reuse_enabled", True))


def _engine_version() -> str:
    try:
        from version import ENGINE_VERSION

        return ENGINE_VERSION
    except Exception:  # noqa: BLE001
        return "unknown"


class ConversionPipeline:
    def __init__(
        self,
        *,
        pdf_path: Path,
        run_id: str,
        conversion_id: str,
        recorder: AuditRecorder,
        sink: ProgressSink | None = None,
        options: PipelineOptions | None = None,
        cancel_check=None,
        source: DocumentSource | None = None,
        resume: bool = False,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.run_id = run_id
        self.conversion_id = conversion_id
        self.recorder = recorder
        self.sink = sink
        self.options = options or PipelineOptions()
        self.cancel_check = cancel_check
        # The document is never held in memory: the digest is computed by
        # streaming and every parser opens the file by path.
        #: Only a redelivered unit may adopt checkpoints. A fresh execution
        #: always reprocesses, so two different pipelines that happen to share
        #: a run id can never exchange pages.
        self.resume = resume
        self._sha256 = source.sha256 if source is not None else sha256_file(self.pdf_path)
        self._checkpoints = CheckpointRepository()
        self._total = 0
        self._started = time.perf_counter()

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def run(self) -> PipelineResult:
        ir = DocumentIR(
            file_name=self.options.source_name or self.pdf_path.name,
            sha256=self._sha256,
            page_count=0,
        )
        validation: dict = {}
        plan = WorkbookPlan()
        checks: list[dict] = []
        evidence: list[dict] = []
        summary: dict = {}
        xlsx: ExcelResult | None = None
        error: ConversionError | None = None
        status = "completed"
        quality = "pending"
        exports: list[ExportResult] = []
        text_plan: TextArtifactPlan | None = None
        publication = "approved"

        try:
            inspection = self._validate(ir)
            validation = inspection.to_report()
            ir.page_count = inspection.page_count
            ir.metadata = inspection.metadata
            ir.pdf_version = inspection.pdf_version

            self._progress("inspect", 0.1, "Inspecionando estrutura das páginas")
            contexts = self._extract_and_layout(ir, inspection)
            self._structure(contexts, ir, inspection)
            locale = self._normalize(ir, contexts)
            plan = self._reconcile(ir, locale)
            self._coverage(ir)
            checks = self._validate_plan(plan, ir)
            quality = quality_from_checks(
                [ValidationCheck(**c) for c in checks], has_content=bool(plan.sheets)
            )
            if not plan.sheets:
                raise ConversionError(
                    "NO_CONTENT",
                    "Nenhum conteúdo textual pôde ser recuperado do PDF.",
                    retryable=False,
                )

            xlsx = self._generate(plan, ir)
            xlsx_audit = self._audit_xlsx(plan)
            checks.extend(xlsx_audit.checks)
            # The XLSX audit is part of the evidence: its findings must be able
            # to downgrade the outcome (a formula or a missing cell is a real
            # failure, not a footnote).  Recompute after the audit runs.
            quality = quality_from_checks(
                [ValidationCheck(**c) for c in checks], has_content=bool(plan.sheets)
            )

            # TXT/CSV are produced from the same IR: OCR runs once per page and
            # all artifacts share the run's provenance.
            text_plan = build_text_plan(
                ir, include_table_text=self.options.include_table_text
            )
            exports = self._export_artifacts(ir, text_plan)
            publication = publication_report(checks, quality)["status"]
            if publication == "quarantined":
                status = "failed"
                self.recorder.issue(
                    "ARTIFACT_QUARANTINED",
                    "O artefato foi reprovado por uma falha fatal de integridade e não está "
                    "disponível para download. O diagnóstico permanece acessível.",
                    severity="critical",
                    context={"checks": [
                        check.get("check") for check in checks if check.get("status") == "fail"
                    ]},
                )

            evidence = [item.to_api() for item in build_cell_evidence(plan, self._blocks_index(ir))]
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
            summary["publication_status"] = publication
            summary["exports"] = [item.summary() for item in exports]
            summary["issues"] = [
                {
                    "issue_id": i.issue_id,
                    "run_id": i.run_id,
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "page_number": i.page_number,
                    "region_id": i.region_id,
                    "context": i.context,
                }
                for i in self.recorder.issues
            ]
            status = "failed" if publication == "quarantined" else self._final_status(quality)
            summary["status"] = status
            summary["quality"] = quality

            self._progress("audit", 1.0, "Auditoria final concluída")

        except ConversionError as exc:
            error = exc
            status = "failed"
            quality = "failed"
            self.recorder.emit(
                "RUN_FAILED",
                "deliver",
                exc.message,
                severity="error",
                payload={"code": exc.code, "retryable": exc.retryable, "detail": exc.detail},
            )
        except Exception as exc:  # noqa: BLE001 - never leak a raw traceback to the user
            error = ConversionError(
                "INTERNAL_ERROR",
                "Falha inesperada durante a conversão.",
                retryable=True,
                detail=f"{exc}\n{traceback.format_exc(limit=8)}",
            )
            status = "failed"
            quality = "failed"
            self.recorder.emit(
                "RUN_FAILED",
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
            summary["issues"] = [
                {
                    "issue_id": i.issue_id,
                    "run_id": i.run_id,
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "page_number": i.page_number,
                    "region_id": i.region_id,
                    "context": i.context,
                }
                for i in self.recorder.issues
            ]

        duration = int((time.perf_counter() - self._started) * 1000)
        stats = self._stats(ir, plan, xlsx, duration)
        stats["publication_status"] = publication
        stats["exports"] = [item.format for item in exports]
        summary.setdefault("publication_status", publication)
        summary.setdefault("exports", [item.summary() for item in exports])
        summary.setdefault("metrics", {}).update(
            {"duration_ms": duration, "pages_processed": len(ir.pages)}
        )
        self._cleanup_work_copy()

        return PipelineResult(
            ok=error is None,
            status=status,
            quality=quality,
            ir=ir,
            plan=plan,
            xlsx=xlsx,
            summary=summary,
            checks=checks,
            evidence=evidence,
            validation=validation,
            error=error,
            stage_timings=self.recorder.stage_timings,
            stats=stats,
            exports=exports,
            text_plan=text_plan,
            publication_status=publication,
            coverage=[
                {
                    "page_number": r.page_number,
                    "text_blocks": r.text_blocks,
                    "tokens": r.tokens,
                    "assigned": r.assigned,
                    "preserved": r.preserved,
                    "header_footer": r.header_footer,
                    "unclassified": r.unclassified,
                    "dropped": r.dropped,
                    "coverage_percent": r.coverage_percent,
                }
                for r in (p.coverage for p in ir.pages)
                if r is not None
            ],
            duration_ms=duration,
        )

    # ------------------------------------------------------------------ #
    # Stages
    # ------------------------------------------------------------------ #
    def _options_fingerprint(self) -> dict:
        return extraction_fingerprint(self.options)

    def _validate(self, ir: DocumentIR) -> PDFInspection:
        self._progress("validate", 0.0, "Validando o arquivo recebido")
        self.recorder.stage_start("validate", "Validando assinatura, estrutura e permissões")
        inspection = inspect_pdf_path(self.pdf_path)
        if not inspection.is_pdf:
            self.recorder.emit(
                EVT_DOCUMENT_REJECTED,
                "validate",
                "O arquivo enviado não é um PDF válido.",
                severity="error",
                payload=inspection.to_report(),
            )
            raise ConversionError("INVALID_FILE", "O arquivo enviado não é um PDF válido.")
        if inspection.errors:
            self.recorder.emit(
                EVT_DOCUMENT_REJECTED,
                "validate",
                inspection.errors[0],
                severity="error",
                payload=inspection.to_report(),
            )
            if inspection.needs_password or inspection.encrypted:
                code = "ENCRYPTED_PDF"
            elif inspection.page_count <= 0:
                code = "INVALID_FILE"
            else:
                code = "INVALID_FILE"
            raise ConversionError(code, inspection.errors[0])
        self.recorder.emit(
            EVT_DOCUMENT_VALIDATED,
            "validate",
            f"PDF válido com {inspection.page_count} página(s)",
            payload=inspection.to_report(),
        )
        self.recorder.stage_end("validate", pages=inspection.page_count)
        self._progress("validate", 1.0, f"PDF validado ({inspection.page_count} páginas)")
        return inspection

    def _extract_and_layout(self, ir: DocumentIR, inspection: PDFInspection) -> list[_PageContext]:
        self.recorder.stage_start("inspect", "Abrindo o documento e inspecionando páginas")
        contexts: list[_PageContext] = []
        work_path = self._work_copy(inspection)
        ocr = OcrRunner(work_path, self.recorder)
        capability = ocr.capability()
        self.recorder.emit(
            "PAGE_ANALYZED",
            "inspect",
            f"OCR disponível: {capability.get('selected') or 'nenhum motor instalado'}",
            payload=capability,
        )

        # No page ceiling: every page of the document has a persisted outcome.
        # `max_pages` is intentionally ignored here (see Settings.max_pages),
        # so a stale environment value can no longer truncate a document.
        try:
            pdf = pdfplumber.open(str(work_path))
        except Exception as exc:  # noqa: BLE001
            raise ConversionError("PDF_UNREADABLE", f"Não foi possível abrir o PDF: {exc}") from exc

        with pdf:
            pages = pdf.pages
            total = len(pages)
            if total == 0:
                raise ConversionError("INVALID_FILE", "O PDF não contém páginas.")
            self._total = total
            self.recorder.stage_end("inspect", pages=total)
            self.recorder.stage_start("extract", f"Extraindo texto de {total} página(s)")

            invisible_ratios = page_invisible_ratios_path(work_path)
            if invisible_ratios and any((ratio or 0) >= 0.9 for ratio in invisible_ratios[:total]):
                hidden = sum(1 for ratio in invisible_ratios[:total] if (ratio or 0) >= 0.9)
                self.recorder.emit(
                    "INVISIBLE_TEXT_LAYER_DETECTED",
                    "inspect",
                    f"{hidden} página(s) possuem texto com modo de renderização invisível "
                    "(não aparece na página); a camada será tratada como não confiável.",
                    payload={"pages": hidden},
                )

            # Checkpoints make a crash-resumed run cheap: pages extracted by a
            # previous attempt of the SAME run are reloaded instead of redoing
            # OCR. A user-requested retry creates a new run, so it never reuses
            # these.
            checkpoint_key = CheckpointKey(
                source_sha256=ir.sha256,
                engine_version=_engine_version(),
                options_hash=checkpoint_options_hash(self._options_fingerprint()),
            )
            cached_pages = (
                self._checkpoints.existing_pages(
                    run_id=self.run_id, key=checkpoint_key, stage=STAGE_EXTRACT
                )
                if (self.resume and cached_pages_enabled())
                else {}
            )
            reused = 0

            for index in range(total):
                if self.cancel_check and self.cancel_check():
                    raise ConversionError("CANCELLED", "Conversão cancelada pelo usuário.")
                page_number = index + 1

                if page_number in cached_pages:
                    restored = self._checkpoints.load_page(
                        run_id=self.run_id,
                        page_number=page_number,
                        key=checkpoint_key,
                        stage=STAGE_EXTRACT,
                    )
                    if restored is not None:
                        ir.pages.append(restored)
                        contexts.append(
                            _PageContext(
                                page=restored,
                                native=NativePage(page_number, 0, 0, 0),
                            )
                        )
                        ir.ocr_regions += len([r for r in restored.regions if r.text_source == "ocr"])
                        reused += 1
                        self._progress(
                            "extract",
                            (index + 1) / max(1, total),
                            f"Página {page_number}/{total} retomada do checkpoint",
                        )
                        continue

                try:
                    ratio = invisible_ratios[index] if index < len(invisible_ratios) else None
                    context = self._process_page(
                        index, page_number, pages[index], ocr, ir, invisible_ratio=ratio
                    )
                except Exception as exc:  # noqa: BLE001
                    page = PageResult(
                        page_number=page_number,
                        width=0,
                        height=0,
                        classification="empty",
                        status="failed",
                    )
                    self.recorder.emit(
                        EVT_PAGE_FAILED,
                        "extract",
                        f"Falha ao processar a página {page_number}: {exc}",
                        severity="error",
                        page=page_number,
                    )
                    self.recorder.issue(
                        "PAGE_FAILED",
                        f"A página {page_number} não pôde ser processada e foi preservada como falha.",
                        severity="error",
                        page=page_number,
                        context={"error": str(exc)},
                    )
                    contexts.append(_PageContext(page=page, native=NativePage(page_number, 0, 0, 0)))
                    # A failed page is still part of the document: it must be
                    # counted in the IR, in the coverage and in the manifest,
                    # otherwise `pages_failed` would always read zero and the
                    # page would vanish from the audit.
                    ir.pages.append(page)
                    continue
                contexts.append(context)
                ir.pages.append(context.page)
                try:
                    self._checkpoints.save_page(
                        run_id=self.run_id,
                        page=context.page,
                        key=checkpoint_key,
                        stage=STAGE_EXTRACT,
                    )
                except Exception:  # noqa: BLE001 - a checkpoint is an optimisation
                    self.recorder.emit(
                        "CHECKPOINT_FAILED",
                        "extract",
                        f"O checkpoint da página {page_number} não pôde ser gravado.",
                        severity="warning",
                        page=page_number,
                    )
                fraction = (index + 1) / max(1, total)
                self._progress(
                    "extract",
                    fraction,
                    f"Página {page_number}/{total} extraída ({context.page.text_source})",
                )

        ir.pages.sort(key=lambda p: p.page_number)
        if reused:
            self.recorder.emit(
                "PAGES_RESTORED",
                "extract",
                f"{reused} página(s) foram retomadas de checkpoints sem reprocessar OCR.",
                payload={"pages": reused, "total": total},
            )
        self.recorder.stage_end("extract", pages=len(ir.pages))
        return contexts

    def _work_copy(self, inspection: PDFInspection) -> Path:
        """Bake ``/Rotate`` into the content once, before any extraction.

        pypdf's ``transfer_rotation_to_content`` rewrites the content stream so
        the page is upright with ``/Rotate 0``.  Both pdfplumber and the OCR
        rasteriser then see a normal page, which removes an entire class of
        coordinate bugs.
        """
        if not inspection.rotation_pages:
            return self.pdf_path
        cached = getattr(self, "_work_path", None)
        if cached is not None and Path(cached).exists():
            return Path(cached)
        try:
            from pypdf import PdfWriter

            target = settings.data_dir / "work" / f"{self.run_id}.derotated.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            writer = PdfWriter(clone_from=str(self.pdf_path))
            changed = 0
            for page in writer.pages:
                if page.get("/Rotate"):
                    page.transfer_rotation_to_content()
                    changed += 1
            with open(target, "wb") as handle:
                writer.write(handle)
            self._work_path = target
            self.recorder.emit(
                "ROTATION_NORMALIZED",
                "extract",
                f"Rotação de {changed} página(s) aplicada ao conteúdo antes da extração",
                payload={"pages": inspection.rotation_pages, "changed": changed},
            )
            return target
        except Exception as exc:  # noqa: BLE001
            self.recorder.issue(
                "ROTATION_NORMALIZATION_FAILED",
                f"Não foi possível normalizar a rotação das páginas: {exc}",
                severity="warning",
            )
            return self.pdf_path

    def _cleanup_work_copy(self) -> None:
        """Delete the de-rotated working copy.

        It is a full duplicate of the client document written to disk only to
        normalise ``/Rotate``; leaving it behind would defeat the retention
        policy and keep personal data beyond the conversion.
        """
        path = getattr(self, "_work_path", None)
        if path is None:
            return
        try:
            Path(path).unlink(missing_ok=True)
            self._work_path = None
        except OSError as exc:
            self.recorder.issue(
                "WORK_COPY_CLEANUP_FAILED",
                "A cópia temporária de trabalho não pôde ser removida do disco.",
                severity="warning",
                context={"path": str(path), "error": str(exc)},
            )

    def _process_page(
        self,
        index: int,
        page_number: int,
        page,
        ocr: OcrRunner,
        ir: DocumentIR,
        *,
        invisible_ratio: float | None = None,
    ) -> _PageContext:
        native = extract_native(page, page_number, invisible_ratio=invisible_ratio)
        classification = classify_page(native)
        page_result = PageResult(
            page_number=page_number,
            width=native.width,
            height=native.height,
            rotation=native.rotation,
            classification=classification,
            native_chars=native.char_count,
            native_words=len(native.tokens),
        )

        tokens = list(native.tokens)
        rotated = list(native.rotated_tokens)
        text_source = "native"
        ocr_recovered = False

        if needs_ocr(native) and self.options.ocr_enabled:
            reason = (
                "camada textual inexistente"
                if not native.has_text_layer
                else f"qualidade da camada textual baixa ({native.quality.get('score', 0):.2f})"
            )
            if native.text_is_invisible:
                reason = (
                    "texto da camada invisível (não aparece na página); "
                    "o conteúdo visual precisa ser lido"
                )
            outcome = ocr.run_page(index, page_number, reason=reason)
            if outcome.tokens:
                ocr_recovered = True
                if native.text_is_invisible:
                    # The hidden layer cannot be verified against the page: the
                    # characters OCR reads from the image are what a human sees,
                    # so they replace the overlay instead of merging with it.
                    tokens = outcome.tokens
                    text_source = "ocr"
                elif native.has_text_layer and native.quality.get("score", 0) >= 0.3:
                    kept_native = [t for t in tokens if not _is_suspicious(t)]
                    tokens = kept_native + deduplicate_ocr(kept_native, outcome.tokens)
                    text_source = "mixed"
                else:
                    tokens = outcome.tokens
                    text_source = "ocr"
                page_result.ocr_chars = outcome.char_count
                ir.ocr_regions += len(outcome.tokens)
                rotated = [] if text_source == "ocr" else rotated
        else:
            if native.has_text_layer:
                ocr.skip(page_number, "camada textual nativa utilizável")
            elif native.is_blank:
                ocr.skip(page_number, "página sem conteúdo")

        if native.text_is_invisible:
            self.recorder.issue(
                "INVISIBLE_TEXT_LAYER",
                (
                    f"O texto da página {page_number} está em modo de renderização invisível "
                    f"({int(native.invisible_render_ratio * 100)}% dos caracteres). "
                    + (
                        "O conteúdo visual foi lido por OCR e prevaleceu sobre a camada oculta."
                        if ocr_recovered
                        else "Sem conteúdo visual para conferir: a camada oculta foi preservada "
                        "e marcada como não verificável."
                    )
                ),
                severity="warning",
                page=page_number,
                context={
                    "invisible_render_ratio": round(native.invisible_render_ratio, 4),
                    "ocr_used": ocr_recovered,
                    "image_coverage": round(native.image_coverage, 3),
                },
            )

        if native.image_count and native.image_coverage >= 0.12 and not ocr_recovered:
            context = {
                "image_count": native.image_count,
                "coverage": round(native.image_coverage, 3),
            }
            healthy_text = native.has_text_layer and native.quality.get("score", 0) >= 0.55
            if native.image_coverage >= 0.9 and healthy_text:
                # Full-page background behind a readable text layer: the text was
                # recovered, but the image itself was not read — say exactly that
                # instead of implying that text is missing.
                message = (
                    f"A imagem de fundo da página {page_number} (100% da página) não foi lida; "
                    "o texto foi recuperado da camada textual."
                )
            else:
                message = (
                    f"A página {page_number} contém uma região de imagem "
                    f"({int(native.image_coverage * 100)}% da página) que não foi reconhecida. "
                    "O conteúdo dessa imagem não foi exportado."
                )
            self.recorder.issue(
                "IMAGE_REGION_NOT_RECOGNIZED",
                message,
                severity="warning",
                page=page_number,
                context=context,
            )

        tokens.sort(key=lambda t: (round(t.bbox.y_center, 1), t.bbox.x0))
        tokens.extend(rotated)
        if not tokens:
            # Nothing was recovered from this page: it must be visible as a
            # partial page, never silently counted as a successful empty page.
            # A genuinely blank page is not a loss of content, so it is only
            # annotated (no issue, no quality degradation).
            text_source = "none"
            if native.is_blank:
                self.recorder.emit(
                    EVT_PAGE_SKIPPED,
                    "extract",
                    f"Página {page_number} está em branco (sem texto e sem imagens).",
                    page=page_number,
                    payload={"classification": classification},
                )
            else:
                page_result.status = "partial"
                self.recorder.issue(
                    "PAGE_WITHOUT_TEXT",
                    f"A página {page_number} não produziu nenhum texto. Ela permanece sem "
                    "representação no workbook e está registrada como não recuperada.",
                    severity="warning",
                    page=page_number,
                    context={
                        "classification": classification,
                        "images": native.image_count,
                        "image_coverage": round(native.image_coverage, 3),
                    },
                )
        page_result.tokens = tokens
        page_result.text_source = text_source
        page_result.metrics = native.to_metrics()

        layout = build_layout(tokens, page_number, native.width, native.height)
        blocks = assign_reading_order(layout.blocks, native.width, native.height)
        page_result.blocks = blocks

        tables = detect_tables(
            page,
            page_number,
            # Rotated words are a separate text layer on the page (a vertical
            # stamp, a rotated title).  Feeding them to the table detector made
            # them land inside cell boxes ("ODA\n994.50"): they belong to the
            # text column, not to the grid.
            [token for token in tokens if token.direction in {"ltr", "rtl"}],
            blocks,
            rotation=native.rotation,
            page_width=native.width,
            recorder=self.recorder,
        )
        page_result.tables = tables

        self.recorder.emit(
            EVT_NATIVE_TEXT_EXTRACTED,
            "extract",
            f"Página {page_number}: {len(tokens)} palavra(s), {len(blocks)} bloco(s), "
            f"{len(tables)} tabela(s)",
            page=page_number,
            extraction_method=text_source,
            payload={
                "classification": classification,
                "text_source": text_source,
                "words": len(tokens),
                "blocks": len(blocks),
                "tables": len(tables),
                "quality": {k: round(v, 4) for k, v in native.quality.items()},
            },
        )
        if native.has_text_layer and native.quality.get("score", 1.0) < 0.55:
            self.recorder.emit(
                EVT_TEXT_LAYER_ASSESSED,
                "extract",
                f"Camada textual da página {page_number} considerada defeituosa "
                f"(score {native.quality.get('score', 0):.2f})",
                severity="warning",
                page=page_number,
                payload={k: round(v, 4) for k, v in native.quality.items()},
            )
        self.recorder.emit(
            EVT_PAGE_ANALYZED,
            "inspect",
            f"Página {page_number} classificada como {classification}",
            page=page_number,
            payload={
                "classification": classification,
                "rotation": native.rotation,
                "size": [native.width, native.height],
                "images": native.image_count,
                "image_coverage": round(native.image_coverage, 4),
            },
        )
        return _PageContext(page=page_result, native=native)

    def _structure(self, contexts: list[_PageContext], ir: DocumentIR, inspection: PDFInspection) -> None:
        self.recorder.stage_start("structure", "Identificando campos, registros e regiões")
        pages = [c.page for c in contexts]
        repeated = detect_repeated_marginals(pages)

        for index, context in enumerate(contexts):
            page = context.page
            if page.status == "failed":
                continue
            preclassify_blocks(page, page.blocks, repeated)

            table_boxes = [t.bbox for t in page.tables]
            candidates = [
                block
                for block in page.blocks
                if block.kind not in {REGION_HEADER, REGION_FOOTER}
                # A block inside a table is table content: it must not be read
                # as a label/value pair nor become the section of the next field.
                and not any(block.bbox.overlap_ratio(box) >= 0.55 for box in table_boxes)
            ]
            extraction = extract_fields(candidates, page_number=page.page_number, recorder=self.recorder)
            page.fields = extraction.fields
            page.records = group_records(
                extraction.fields,
                page_number=page.page_number,
                blocks=page.blocks,
                config=_grouping_config(extraction.fields),
            )
            ir.records.extend(page.records)
            build_regions(page, page.blocks, page.tables, page.fields, repeated)

            if extraction.unresolved_labels:
                self.recorder.issue(
                    "FIELD_WITHOUT_VALUE",
                    f"{len(extraction.unresolved_labels)} rótulo(s) sem valor associado na página "
                    f"{page.page_number}: {', '.join(extraction.unresolved_labels[:5])}",
                    severity="warning",
                    page=page.page_number,
                )
            self._progress(
                "structure",
                (index + 1) / max(1, len(contexts)),
                f"Estruturas da página {page.page_number} resolvidas "
                f"({len(page.fields)} campos, {len(page.records)} registros)",
            )
        ir.tables.extend(t for p in pages for t in p.tables)
        ir.engines = _engine_usage(ir)
        self.recorder.stage_end("structure", pages=len(pages))

        if inspection.has_forms:
            self.recorder.emit(
                "FORM_FIELDS_DETECTED",
                "structure",
                f"{len(inspection.form_fields)} campo(s) de formulário interativo encontrados",
                payload={
                    "fields": [
                        {"name": f.name, "page": f.page_number, "value_present": bool(f.value)}
                        for f in inspection.form_fields[:100]
                    ]
                },
            )
            merged = self._merge_form_fields(pages, ir, inspection)
            if merged:
                ir.engines = _engine_usage(ir)
                self.recorder.emit(
                    "FORM_FIELDS_MERGED",
                    "structure",
                    f"{merged} valor(es) de formulário interativo incorporados ao workbook",
                    payload={"merged": merged},
                )

    def _merge_form_fields(self, pages, ir: DocumentIR, inspection: PDFInspection) -> int:
        """Turn AcroForm widget values into workbook fields.

        A fillable PDF stores its data in the widget, not in the text layer:
        the page shows only the labels and the drawn values are invisible to
        text extraction.  Without this step a perfectly filled form converted
        to an empty workbook.

        The label is recovered from the page (the text immediately left of, or
        above, the widget); when geometry is unavailable the internal field name
        is cleaned (`Nome_001230` -> `Nome`).  Values already extracted from the
        text layer are never duplicated.
        """
        if not inspection.form_fields:
            return 0
        by_page: dict[int, list[Field]] = {}
        for item in inspection.form_fields:
            value = str(item.value).strip() if item.value is not None else ""
            if not value:
                continue
            page_number = item.page_number or 1
            page = next((p for p in pages if p.page_number == page_number), None)
            label = _form_field_label(page, item)
            if not label:
                continue
            box = BBox(*item.bbox) if item.bbox else (page.bbox if page is not None else BBox.empty())
            if box.is_empty:
                continue
            existing = [
                field
                for record in ir.records
                if record.page_number == page_number
                for field in record.fields
            ]
            if any(
                field.label.strip().lower() == label.lower()
                and field.value.strip().lower() == value.lower()
                for field in existing
            ):
                continue
            by_page.setdefault(page_number, []).append(
                Field(
                    label=label,
                    value=value,
                    page_number=page_number,
                    label_bbox=box,
                    value_bbox=box,
                    separator="form",
                    confidence=0.99,
                    reason_codes=["acroform_widget"],
                    section=None,
                )
            )
        merged = 0
        for page_number, fields in sorted(by_page.items()):
            if not fields:
                continue
            # One entity per page: widget values belong to the record already
            # read from the text layer, not to a second row/sheet.
            page_records = [r for r in ir.records if r.page_number == page_number]
            if len(page_records) == 1:
                target = page_records[0]
                target.fields.extend(fields)
                target.finalize()
                merged += len(fields)
                continue
            record = Record(
                record_id=f"rec_form_{page_number:03d}",
                page_number=page_number,
                fields=fields,
                section=fields[0].section,
                origin="acroform",
                order=len(ir.records),
            )
            ir.records.append(record.finalize())
            merged += len(fields)
        return merged

    def _normalize(self, ir: DocumentIR, contexts: list[_PageContext]) -> LocaleProfile:
        self.recorder.stage_start("normalize", "Detectando locale, deduplicando e unificando tabelas")
        texts = [block.text for block in ir.all_blocks()]
        locale = detect_locale(texts)
        self.recorder.emit(
            "LOCALE_DETECTED",
            "normalize",
            f"Formato numérico inferido: decimal '{locale.decimal_sep}', "
            f"milhar '{locale.thousand_sep}', datas {locale.date_order}",
            payload={
                "decimal_sep": locale.decimal_sep,
                "thousand_sep": locale.thousand_sep,
                "date_order": locale.date_order,
                "confidence": locale.confidence,
                "samples": locale.samples,
            },
        )

        reconciled = reconcile(ir.tables, ir.records, recorder=self.recorder)
        ir.tables = reconciled.tables
        ir.records = reconciled.records
        if reconciled.duplicates_removed:
            self.recorder.issue(
                "DUPLICATE_RECORDS_REMOVED",
                f"{reconciled.duplicates_removed} registro(s) duplicado(s) removido(s) por "
                "sobreposição de região e conteúdo idêntico.",
                severity="info",
            )
        self._progress("normalize", 1.0, "Valores e estruturas normalizados")
        self.recorder.stage_end("normalize", pages=len(ir.pages))
        return locale

    def _reconcile(self, ir: DocumentIR, locale: LocaleProfile) -> WorkbookPlan:
        self.recorder.stage_start("reconcile", "Montando a estrutura unificada do workbook")
        result = build_workbook(ir, locale=locale, recorder=self.recorder)
        for warning in result.warnings:
            self.recorder.issue("WORKBOOK_WARNING", warning, severity="warning")
        self._progress("reconcile", 1.0, f"{len(result.plan.sheets)} aba(s) planejada(s)")
        self.recorder.stage_end("reconcile", pages=len(ir.pages))
        return result.plan

    def _coverage(self, ir: DocumentIR) -> None:
        rows = build_coverage(ir.pages)
        aggregate = aggregate_coverage(rows)
        self.recorder.emit(
            EVT_COVERAGE_COMPUTED,
            "reconcile",
            f"Cobertura textual: {aggregate['coverage_percent']}% "
            f"({int(aggregate['assigned'])} de {int(aggregate['tokens'])} tokens atribuídos)",
            payload=aggregate,
        )

    def _validate_plan(self, plan: WorkbookPlan, ir: DocumentIR) -> list[dict]:
        checks = validate_plan(plan, ir)
        for check in checks:
            if check.status in {"fail", "warn"}:
                severity = "error" if check.status == "fail" else "warning"
                self.recorder.issue(
                    f"VALIDATION_{check.check.upper()}",
                    check.details,
                    severity=severity,
                    context={"measured": check.measured, "expected": check.expected},
                )
        return [check.to_dict() for check in checks]

    def _generate(self, plan: WorkbookPlan, ir: DocumentIR) -> ExcelResult:
        self.recorder.stage_start("generate", "Gerando o arquivo Excel")
        self._progress("generate", 0.2, "Escrevendo células tipadas")
        result = write_workbook(plan, run_id=self.run_id, title=plan.title or ir.file_name)
        for sheet in plan.sheets:
            self.recorder.emit(
                EVT_CELL_WRITTEN,
                "generate",
                f"Aba '{sheet.name}': {sheet.n_rows} linha(s), {sheet.n_cols} coluna(s)",
                payload={
                    "sheet": sheet.name,
                    "rows": sheet.n_rows,
                    "columns": sheet.n_cols,
                    "kind": sheet.kind,
                },
            )
        self.recorder.emit(
            EVT_EXCEL_GENERATED,
            "generate",
            f"XLSX gerado: {result.cells} célula(s), {result.size_bytes} bytes",
            payload={
                "sheets": result.sheets,
                "cells": result.cells,
                "size_bytes": result.size_bytes,
                "truncated_cells": result.truncated_cells,
            },
        )
        if result.sanitized_chars:
            # Excel cannot store control characters (XML 1.0 forbids them).
            # They are replaced by U+FFFD instead of aborting the conversion,
            # and the affected cells are named so nothing is lost silently.
            self.recorder.issue(
                "ILLEGAL_CHARACTERS_REPLACED",
                f"{result.sanitized_chars} caractere(s) de controle — que o formato XLSX "
                "não aceita — foram substituídos por U+FFFD nas células indicadas.",
                severity="warning",
                context={
                    "replacement": "\ufffd",
                    "cells": result.sanitized_refs[:20],
                    "total": result.sanitized_chars,
                },
            )
        if result.truncated_cells:
            # XLSX caps a cell at 32,767 characters: the overflow is not lost
            # silently — it is reported with the affected references so the
            # reader knows exactly which cells are incomplete.
            self.recorder.issue(
                "CELL_TEXT_TRUNCATED",
                f"{result.truncated_cells} célula(s) excederam o limite de 32.767 caracteres "
                "do formato XLSX e foram truncadas.",
                severity="warning",
                context={
                    "limit": 32_767,
                    "cells": result.truncated_refs[:20],
                    "total": result.truncated_cells,
                },
            )
        self._progress("generate", 1.0, "Arquivo Excel gerado")
        self.recorder.stage_end("generate", pages=len(ir.pages))
        self._last_xlsx = result
        return result

    def _audit_xlsx(self, plan: WorkbookPlan) -> XlsxAudit:
        self.recorder.stage_start("audit", "Auditando o arquivo gerado")
        if self._last_xlsx is None:
            raise ConversionError("XLSX_MISSING", "O arquivo Excel não foi gerado.")
        audit = audit_xlsx(self._last_xlsx.data, plan)
        for check in audit.checks:
            if check["status"] == "fail":
                self.recorder.issue(
                    f"XLSX_{check['check'].upper()}",
                    str(check["details"]),
                    severity="error",
                    context={"measured": check.get("measured"), "expected": check.get("expected")},
                )
        event = EVT_XLSX_AUDIT_PASSED if audit.ok else EVT_XLSX_AUDIT_FAILED
        self.recorder.emit(
            event,
            "audit",
            "Auditoria do XLSX aprovada" if audit.ok else "Auditoria do XLSX encontrou falhas",
            severity="info" if audit.ok else "error",
            payload=audit.to_dict(),
        )
        self.recorder.stage_end("audit", pages=len(plan.sheets))
        return audit

    def _export_artifacts(
        self, ir: DocumentIR, text_plan: TextArtifactPlan
    ) -> list[ExportResult]:
        """Produce the requested non-XLSX artifacts from the shared IR."""
        formats = self.options.target_formats
        if not formats:
            return []
        results: list[ExportResult] = []
        if "txt" in formats:
            try:
                results.append(
                    export_text(
                        text_plan,
                        ExportOptions(
                            format="txt",
                            newline=self.options.text_newline,
                            page_separator=self.options.page_separator,
                            include_table_text=self.options.include_table_text,
                        ),
                    )
                )
            except ExportError as exc:
                self.recorder.issue(
                    f"EXPORT_{exc.code}",
                    f"A exportação TXT não pôde ser produzida: {exc.message}",
                    severity="error",
                )
        if "csv" in formats:
            try:
                results.append(
                    export_csv(
                        ir,
                        ExportOptions(
                            format="csv",
                            csv_profile=self.options.csv_profile,
                            csv_delimiter=self.options.csv_delimiter,
                            formula_policy=self.options.csv_formula_policy,
                            table_ids=self.options.csv_table_ids,
                            value_mode=self.options.csv_value_mode,
                        ),
                    )
                )
            except ExportError as exc:
                self.recorder.issue(
                    f"EXPORT_{exc.code}",
                    f"A exportação CSV não foi gerada: {exc.message}",
                    severity="warning",
                    context={"code": exc.code, **exc.details},
                )
        return results

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    _last_xlsx: ExcelResult | None = None

    def _progress(self, stage: str, fraction: float, message: str) -> None:
        low, high = _STAGE_BOUNDS[stage]
        progress = round(low + (high - low) * max(0.0, min(1.0, fraction)), 4)
        if self.sink:
            self.sink.update(
                status=_STATUS_FOR_STAGE[stage],
                stage=stage,
                progress=progress,
                message=message,
            )

    def _blocks_index(self, ir: DocumentIR) -> dict[str, TextBlock]:
        return {block.element_id: block for block in ir.all_blocks()}

    def _final_status(self, quality: str) -> str:
        if quality == "validated":
            return "completed"
        if quality == "partial":
            return "completed_with_warnings"
        # "failed" (and any unknown state) must never be reported as a success
        # with warnings: that is how an entire page can go missing while the
        # application claims the conversion finished.
        return "failed"

    def _stats(
        self,
        ir: DocumentIR,
        plan: WorkbookPlan,
        xlsx: ExcelResult | None,
        duration_ms: int,
    ) -> dict:
        pages = len(ir.pages)
        tables = len(ir.tables)
        blocks = ir.all_blocks()
        assigned = sum(1 for b in blocks if b.destination in {"field", "table", "metadata"})
        preserved = sum(1 for b in blocks if b.destination == "text")
        unclassified = sum(1 for b in blocks if b.destination in {"unclassified", None})
        coverage = aggregate_coverage([p.coverage for p in ir.pages if p.coverage is not None])
        record_rows = sum(s.n_rows for s in plan.sheets if s.kind == "records")
        sheet_cells = sum(1 for s in plan.sheets for r in s.rows for c in r if c is not None)
        return {
            "pages_total": ir.page_count,
            "pages_processed": pages,
            "pages_failed": sum(1 for p in ir.pages if p.status == "failed"),
            "pages_unrecognized": sum(1 for p in ir.pages if p.status == "partial"),
            "tables": tables,
            "fields": sheet_cells,
            "records": record_rows,
            "sheets": len(plan.sheets),
            "cells": xlsx.cells if xlsx else 0,
            "text_blocks": len(blocks),
            "blocks_assigned": assigned,
            "blocks_preserved": preserved,
            "blocks_unclassified": unclassified,
            "ocr_regions": ir.ocr_regions,
            "ocr_pages": sum(1 for p in ir.pages if p.text_source == "ocr"),
            "warnings": self.recorder.count("warning"),
            "errors": self.recorder.count("error") + self.recorder.count("critical"),
            "duration_ms": duration_ms,
            "coverage_percent": coverage["coverage_percent"],
        }


def _grouping_config(fields: list[Field]) -> GroupingConfig:
    """Record-break threshold tuned to the page's own line pitch.

    A record break must be clearly larger than the rhythm of the page.  On
    native text the default ratio already expresses that; on OCR pages the word
    boxes are tight inside a wide line pitch, and the same ratio cuts a form
    into one record per line.  The rule only ever relaxes the default.
    """
    config = GroupingConfig()
    if len(fields) < 3:
        return config
    tops = sorted(field.label_bbox.y0 for field in fields)
    deltas = [end - start for start, end in zip(tops, tops[1:]) if end - start > 1.0]
    if not deltas:
        return config
    # The *larger* half of the gaps is the line pitch: OCR geometry often packs
    # the first lines tightly, and a median over all gaps would then be the
    # intra-line distance, which is not the rhythm of the page.
    deltas.sort()
    pitch = deltas[int(len(deltas) * 0.75)]
    heights = [f.label_bbox.height for f in fields if f.label_bbox.height > 0]
    line_height = median(heights) if heights else 0.0
    if pitch <= 0 or line_height <= 0:
        return config
    config.max_vertical_gap_ratio = max(config.max_vertical_gap_ratio, (pitch * 1.6) / line_height)
    return config


def _form_field_label(page, item) -> str | None:
    """Visual label of an AcroForm widget, or a cleaned internal name.

    Priority: text ending just left of the widget (same line), then text right
    above it, then the declared `/TU` tooltip, then the internal name without
    its numeric suffix.  `TextField12` is never shown to the user as a header.
    """
    box = BBox(*item.bbox) if item.bbox else None
    if page is not None and box is not None and not box.is_empty:
        left: tuple[float, str] | None = None
        above: tuple[float, str] | None = None
        for block in page.blocks:
            for line in block.lines:
                text = line.text.strip().rstrip(":").strip()
                if not text or len(text.split()) > 5:
                    continue
                vertical_overlap = min(line.bbox.y1, box.y1) - max(line.bbox.y0, box.y0)
                if vertical_overlap > 0 and line.bbox.x1 <= box.x0 + 6:
                    distance = box.x0 - line.bbox.x1
                    if distance <= 160 and (left is None or distance < left[0]):
                        left = (distance, text)
                    continue
                horizontal_overlap = min(line.bbox.x1, box.x1) - max(line.bbox.x0, box.x0)
                gap = box.y0 - line.bbox.y1
                if horizontal_overlap > 0 and 0 <= gap <= 24:
                    if above is None or gap < above[0]:
                        above = (gap, text)
        if left is not None:
            return left[1]
        if above is not None:
            return above[1]
    tooltip = getattr(item, "label_guess", None)
    if tooltip and str(tooltip).strip():
        return " ".join(str(tooltip).split())[:60]
    name = re.sub(r"[_\-\s]*\d{4,}$", "", str(item.name or "")).replace("_", " ").strip()
    name = " ".join(part.capitalize() if part.islower() else part for part in name.split())
    return name or None


def _engine_usage(ir: DocumentIR) -> dict[str, EngineUsage]:
    """Aggregate which engines actually contributed, for the audit manifest."""
    usage: dict[str, EngineUsage] = {}

    def bump(name: str, kind: str, *, regions: int = 0, pages: int = 0, accepted: int = 0, rejected: int = 0) -> None:
        item = usage.setdefault(name, EngineUsage(engine=name, kind=kind))
        item.regions += regions
        item.pages += pages
        item.accepted += accepted
        item.rejected += rejected

    bump("pdfplumber", "native", regions=len(ir.all_blocks()), pages=len(ir.pages))
    bump("layout-analyzer", "layout", regions=sum(len(p.regions) for p in ir.pages))
    for table in ir.tables:
        bump(
            table.strategy if table.strategy != "transposed" else "transposed-detector",
            "table",
            regions=1,
            pages=1,
            accepted=1,
            rejected=max(0, len(table.alternatives) - 1),
        )
    field_count = sum(len(r.fields) for r in ir.records)
    if field_count:
        bump("field-resolver-v1", "semantic", regions=field_count, pages=len(ir.pages))
    if ir.ocr_regions:
        # Record the recognizer that actually produced the tokens (e.g.
        # "glm-ocr"), not a generic kind label: the audit must say which engine
        # ran, with which model, per the forensic requirements.
        ocr_engines = sorted(
            {
                token.engine
                for page in ir.pages
                for token in page.tokens
                if token.origin == ORIGIN_OCR and getattr(token, "engine", None)
            }
        ) or ["ocr"]
        for engine_name in ocr_engines:
            bump(
                engine_name,
                "ocr",
                regions=ir.ocr_regions,
                pages=sum(1 for p in ir.pages if p.ocr_chars),
            )
    return usage


def _is_suspicious(token) -> bool:
    text = token.text
    if "\ufffd" in text:
        return True
    if token.size is not None and token.size < 3.5:
        return True
    if len(text) > 60:
        return True
    return False


__all__ = ["ConversionPipeline", "PipelineOptions", "PipelineResult", "ConversionError"]
