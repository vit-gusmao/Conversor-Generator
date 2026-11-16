"""OCR execution, coordinate mapping and reconciliation with native text.

Rules enforced here:

* OCR is a *source of text*, never a source of images.  The raster is dropped
  as soon as tokens are produced.
* A low-confidence token is not silently kept: it is counted as rejected and
  reported in the audit.
* Native text that is already valid is never replaced by OCR.  When both exist,
  the native token wins unless the native layer was classified as defective.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from audit.events import (
    EVT_OCR_COMPLETED,
    EVT_OCR_SKIPPED,
    EVT_OCR_STARTED,
    EVT_OCR_UNAVAILABLE,
    AuditRecorder,
)
from config import settings
from engine.ocr.engines import OcrEngine, OcrUnavailable, engine_details, get_engine, ocr_capability
from engine.ocr.preprocess import PreprocessResult, detect_orientation_hint, preprocess
from engine.ocr.rasterize import RasterPage, render_page, render_region
from models.document import ORIGIN_OCR, BBox, Token


@dataclass(slots=True)
class OcrOutcome:
    page_number: int
    tokens: list[Token] = field(default_factory=list)
    engine: str = "none"
    version: str = "unknown"
    dpi: int = 0
    operations: list[str] = field(default_factory=list)
    mean_confidence: float = 0.0
    accepted: int = 0
    rejected: int = 0
    duration_ms: int = 0
    error: str | None = None
    attempted: bool = False

    @property
    def char_count(self) -> int:
        return sum(len(t.text) for t in self.tokens)


class OcrRunner:
    def __init__(self, source: bytes | str | Path, recorder: AuditRecorder) -> None:
        self._source = source
        self.recorder = recorder
        self._engine: OcrEngine | None = None
        self._engine_broken = False
        self._counter = 0

    # -- engine ------------------------------------------------------------
    @property
    def engine(self) -> OcrEngine | None:
        if self._engine is None and not self._engine_broken:
            self._engine = get_engine()
            if getattr(self._engine, "name", "none") == "none":
                self._engine_broken = True
        return None if self._engine_broken else self._engine

    def capability(self) -> dict[str, object]:
        return ocr_capability()

    # -- page --------------------------------------------------------------
    def run_page(self, page_index: int, page_number: int, *, reason: str = "") -> OcrOutcome:
        started = time.perf_counter()
        outcome = OcrOutcome(page_number=page_number)
        engine = self.engine
        if engine is None:
            self.recorder.emit(
                EVT_OCR_UNAVAILABLE,
                "extract",
                "Nenhum motor de OCR instalado; a região permanecerá sem reconhecimento.",
                severity="warning",
                page=page_number,
                extraction_method="ocr",
                payload={"reason": reason, "available": ocr_capability()},
            )
            self.recorder.issue(
                "OCR_UNAVAILABLE",
                "Nenhum motor de OCR está instalado. Texto de páginas digitalizadas não pôde ser recuperado.",
                severity="warning",
                page=page_number,
                context={
                    "install": (
                        "ollama pull glm-ocr:latest (recomendado, GPU), "
                        "pip install rapidocr-onnxruntime, ou tesseract + pytesseract"
                    )
                },
            )
            outcome.error = "ocr_unavailable"
            outcome.duration_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        self.recorder.emit(
            EVT_OCR_STARTED,
            "extract",
            f"OCR iniciado na página {page_number} ({engine.name})",
            page=page_number,
            extraction_method="ocr",
            payload={
                "engine": engine.name,
                "reason": reason,
                # Requested DPI, not the effective one (downscaling may apply);
                # OCR_COMPLETED records the DPI actually used.
                "dpi_requested": settings.converter_render_dpi,
                **engine_details(engine),
            },
        )

        try:
            raster = render_page(self._source, page_index)
        except Exception as exc:  # noqa: BLE001
            outcome.error = f"rasterize_failed: {exc}"
            self.recorder.issue(
                "OCR_RASTERIZE_FAILED",
                f"Falha ao rasterizar a página {page_number}: {exc}",
                severity="error",
                page=page_number,
            )
            outcome.duration_ms = int((time.perf_counter() - started) * 1000)
            return outcome
        return self._recognize_raster(raster, page_number, engine, started)

    def run_image(self, page_number: int, image, *, reason: str = "", dpi: int = 96) -> OcrOutcome:
        """OCR an already decoded image (image ingestion path).

        The image is used at its native resolution; coordinates stay in pixels
        (``scale = 1.0``), which is the canonical space for image documents.
        """
        started = time.perf_counter()
        outcome = OcrOutcome(page_number=page_number)
        engine = self.engine
        if engine is None:
            self.recorder.emit(
                EVT_OCR_UNAVAILABLE,
                "extract",
                "Nenhum motor de OCR instalado; a imagem permanecerá sem reconhecimento.",
                severity="warning",
                page=page_number,
                extraction_method="ocr",
                payload={"reason": reason, "available": ocr_capability()},
            )
            self.recorder.issue(
                "OCR_UNAVAILABLE",
                "Nenhum motor de OCR está instalado; o texto da imagem não pôde ser recuperado.",
                severity="warning",
                page=page_number,
                context={
                    "install": (
                        "ollama pull glm-ocr:latest (recomendado, GPU), "
                        "pip install rapidocr-onnxruntime, ou tesseract + pytesseract"
                    )
                },
            )
            outcome.error = "ocr_unavailable"
            outcome.duration_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        self.recorder.emit(
            EVT_OCR_STARTED,
            "extract",
            f"OCR iniciado no quadro {page_number} ({engine.name})",
            page=page_number,
            extraction_method="ocr",
            payload={
                "engine": engine.name,
                "reason": reason,
                "dpi_requested": dpi,
                **engine_details(engine),
            },
        )
        raster = RasterPage(
            page_number=page_number,
            width_pt=float(image.width),
            height_pt=float(image.height),
            scale=1.0,
            image=image,
            dpi=dpi,
        )
        return self._recognize_raster(raster, page_number, engine, started)

    def _recognize_raster(
        self, raster: RasterPage, page_number: int, engine: OcrEngine, started: float
    ) -> OcrOutcome:
        outcome = OcrOutcome(page_number=page_number)
        try:
            processed = preprocess(raster.image)
        except Exception as exc:  # noqa: BLE001 - preprocessing must never kill the page
            # A failure in the enhancement step is not a reason to lose the
            # native text already extracted from this page: fall back to the
            # original raster and record the degradation.
            processed = PreprocessResult(image=raster.image, operations=[], skipped_reason=str(exc))
            self.recorder.issue(
                "OCR_PREPROCESS_FAILED",
                f"O pré-processamento falhou na página {page_number}; a imagem original foi usada.",
                severity="warning",
                page=page_number,
                context={"error": str(exc)},
            )
        hint = detect_orientation_hint(raster.image)
        if hint not in (None, 0):
            # Page declared upright but the ink says otherwise: rotate the
            # variant handed to the recognizer (the original raster is kept).
            processed = PreprocessResult(
                image=processed.image.rotate(-hint, expand=True),
                operations=list(processed.operations) + [f"rotate_{hint}"],
                parameters=dict(processed.parameters),
                skipped_reason=processed.skipped_reason,
            )
            self.recorder.issue(
                "OCR_ORIENTATION_CORRECTED",
                f"A página {page_number} parecia girada em {hint}°; a imagem foi corrigida para o OCR.",
                severity="warning",
                page=page_number,
                context={"angle": hint},
            )
        outcome.dpi = raster.dpi
        outcome.operations = processed.operations
        outcome.engine = engine.name
        outcome.version = engine.version
        outcome.attempted = True

        words = self._recognize_with_variants(engine, processed.image, raster.image, page_number)
        if words is None:
            outcome.error = "ocr_failed"
            outcome.duration_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        tokens, accepted, rejected = self._to_tokens(words, raster, page_number, offset=(0, 0))
        outcome.tokens = tokens
        outcome.accepted = accepted
        outcome.rejected = rejected
        outcome.mean_confidence = (
            round(sum(t.confidence or 0 for t in tokens) / len(tokens), 3) if tokens else 0.0
        )
        outcome.duration_ms = int((time.perf_counter() - started) * 1000)

        self.recorder.emit(
            EVT_OCR_COMPLETED,
            "extract",
            f"OCR concluído na página {page_number}: {len(tokens)} palavras, "
            f"confiança média {outcome.mean_confidence}",
            page=page_number,
            extraction_method="ocr",
            payload={
                "engine": engine.name,
                "version": engine.version,
                "dpi": outcome.dpi,
                "operations": outcome.operations,
                "accepted": accepted,
                "rejected": rejected,
                "mean_confidence": outcome.mean_confidence,
                "duration_ms": outcome.duration_ms,
                "char_count": outcome.char_count,
                **engine_details(engine),
            },
        )
        if outcome.rejected:
            self.recorder.issue(
                "OCR_LOW_CONFIDENCE",
                f"{outcome.rejected} palavra(s) descartada(s) por confiança abaixo de "
                f"{settings.converter_ocr_min_confidence:.2f} na página {page_number}.",
                severity="warning",
                page=page_number,
            )
        return outcome


    def _recognize_with_variants(self, engine, processed_image, original_image, page_number: int):
        """Recognise, retrying only with a *different* input.

        The plan forbids repeating the same request after a failure: the second
        attempt uses the untouched raster, and the number of attempts is capped
        by CONVERTER_MAX_FALLBACK_ATTEMPTS.
        """
        attempts = max(1, settings.converter_max_fallback_attempts + 1)
        variants = [("enhanced", processed_image), ("original", original_image)]
        last_error: str | None = None
        for index, (label, image) in enumerate(variants[:attempts]):
            try:
                words = engine.recognize(image, mode="page")
            except OcrUnavailable as exc:
                last_error = str(exc)
                if index == 0 and attempts > 1:
                    self.recorder.emit(
                        EVT_OCR_UNAVAILABLE,
                        "extract",
                        f"Primeira passagem do OCR falhou na página {page_number}; "
                        "reprocessando com a imagem original.",
                        severity="warning",
                        page=page_number,
                        extraction_method="ocr",
                        payload={"variant": label, "error": last_error},
                    )
                continue
            if words:
                if index > 0:
                    self.recorder.issue(
                        "OCR_RETRIED_WITH_VARIANT",
                        f"A página {page_number} só foi reconhecida na variante '{label}'.",
                        severity="warning",
                        page=page_number,
                        context={"variant": label},
                    )
                return words
        self.recorder.emit(
            EVT_OCR_UNAVAILABLE,
            "extract",
            f"Falha do motor de OCR em todas as variantes: {last_error}",
            severity="warning",
            page=page_number,
            extraction_method="ocr",
        )
        return None

    # -- region ------------------------------------------------------------
    def run_region(
        self,
        raster: RasterPage,
        bbox: BBox,
        page_number: int,
        *,
        mode: str = "region",
        reason: str = "",
    ) -> OcrOutcome:
        started = time.perf_counter()
        outcome = OcrOutcome(page_number=page_number, dpi=raster.dpi)
        engine = self.engine
        if engine is None:
            outcome.error = "ocr_unavailable"
            outcome.duration_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        crop, (ox, oy) = render_region(raster, bbox)
        processed = preprocess(crop, aggressive=(mode in {"cell", "line"}))
        outcome.operations = processed.operations
        outcome.engine = engine.name
        outcome.version = engine.version
        outcome.attempted = True
        try:
            words = engine.recognize(processed.image, mode=mode)
        except OcrUnavailable as exc:
            outcome.error = str(exc)
            outcome.duration_ms = int((time.perf_counter() - started) * 1000)
            return outcome

        tokens, accepted, rejected = self._to_tokens(words, raster, page_number, offset=(ox, oy))
        # Region OCR returns coordinates relative to the crop; shift back.
        for token in tokens:
            token.bbox = BBox(
                token.bbox.x0 + ox / raster.scale,
                token.bbox.y0 + oy / raster.scale,
                token.bbox.x1 + ox / raster.scale,
                token.bbox.y1 + oy / raster.scale,
            )
        outcome.tokens = tokens
        outcome.accepted = accepted
        outcome.rejected = rejected
        outcome.mean_confidence = (
            round(sum(t.confidence or 0 for t in tokens) / len(tokens), 3) if tokens else 0.0
        )
        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        self.recorder.emit(
            EVT_OCR_COMPLETED,
            "extract",
            f"OCR de região na página {page_number}: {len(tokens)} palavras ({mode})",
            page=page_number,
            extraction_method="ocr",
            payload={
                "engine": engine.name,
                "mode": mode,
                "reason": reason,
                "bbox": bbox.to_list(),
                "operations": outcome.operations,
                "mean_confidence": outcome.mean_confidence,
                "duration_ms": outcome.duration_ms,
            },
        )
        return outcome

    def skip(self, page_number: int, reason: str) -> None:
        self.recorder.emit(
            EVT_OCR_SKIPPED,
            "extract",
            f"OCR não necessário na página {page_number}: {reason}",
            page=page_number,
            extraction_method="ocr",
            payload={"reason": reason},
        )

    # -- helpers -----------------------------------------------------------
    def _to_tokens(
        self,
        words,
        raster: RasterPage,
        page_number: int,
        *,
        offset: tuple[int, int],
    ) -> tuple[list[Token], int, int]:
        tokens: list[Token] = []
        accepted = 0
        rejected = 0
        threshold = settings.converter_ocr_min_confidence
        for word in words:
            self._counter += 1
            if word.confidence < threshold:
                rejected += 1
                continue
            box = raster.to_points(word.bbox_px)
            tokens.append(
                Token(
                    element_id=f"ocr_{page_number:03d}_{self._counter:05d}",
                    text=word.text,
                    bbox=box,
                    page_number=page_number,
                    origin=ORIGIN_OCR,
                    font=None,
                    size=None,
                    direction="ltr",
                    confidence=round(word.confidence, 4),
                    index=self._counter,
                    engine=self.engine.name if self.engine else "ocr",
                )
            )
            accepted += 1
        return tokens, accepted, rejected


def deduplicate_ocr(native_tokens: list[Token], ocr_tokens: list[Token]) -> list[Token]:
    """Drop OCR words that duplicate a native word in the same place."""
    if not native_tokens or not ocr_tokens:
        return ocr_tokens
    kept: list[Token] = []
    for ocr in ocr_tokens:
        duplicate = False
        for native in native_tokens:
            if native.bbox.overlap_ratio(ocr.bbox) > 0.5 and native.text.strip() == ocr.text.strip():
                duplicate = True
                break
        if not duplicate:
            kept.append(ocr)
    return kept


__all__ = ["OcrRunner", "OcrOutcome", "deduplicate_ocr"]
