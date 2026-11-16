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
from models.document import ORIGIN_OCR, BBox, Token, median


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

        # Orientation first.  The projection only says "sideways", never which
        # way round, and it can be wrong on a dense page — so the *original*
        # orientation is always a candidate and the winner is the variant that
        # produces more real words.  The decision comes from the recognizer's own
        # output, never from a file-specific rule.  Pages that are not flagged
        # sideways pay for exactly one recognition.
        hint = detect_orientation_hint(raster.image)
        candidates: list[tuple[int, RasterPage]] = [(0, raster)]
        if hint in (90, 270):
            candidates += [(90, raster.rotated(90)), (270, raster.rotated(270))]
        elif hint == 180:
            candidates.append((180, raster.rotated(180)))

        results: list[tuple[tuple[int, int, int, int, int], int, RasterPage, PreprocessResult, list]] = []
        for angle, candidate in candidates:
            processed = self._preprocess_page(candidate.image, page_number)
            words = self._recognize_with_variants(
                engine, candidate.image, processed.image, page_number
            )
            # A line of text is wide and short — but that only means something
            # when the bands are real ink.  When the ink did not segment 1:1
            # with the recognised lines the boxes were *invented* (evenly split)
            # and always look wide; trusting them picked a sideways candidate
            # whose geometry was fiction.
            exact = 1 if getattr(engine, "boxing_method", "") == "projection_v1" else 0
            landscape = _landscape_bands(getattr(engine, "bands", []) or []) if exact else 0
            score = (
                exact,
                landscape,
                _label_pair_score(words),
                _geometry_score(words),
                _word_score(words),
            )
            results.append((score, angle, candidate, processed, words or []))
            if angle == 0 and exact:
                # The page is already measured: rotating it cannot add anything
                # and each extra candidate costs a full recognition.
                break

        upright = results[0]
        rotated = max(results[1:], key=lambda item: item[0]) if len(results) > 1 else None
        # Rotation is used only when it produces *measured* geometry (ink and
        # recognised lines 1:1).  With synthetic boxes the rotated coordinate
        # space is transposed, which is worse than keeping the page as it is —
        # the recogniser reads sideways text anyway.
        best = rotated if rotated is not None and rotated[0][0] > 0 and rotated[0] > upright[0] else upright

        score, angle, raster, processed, words = best
        if angle:
            self.recorder.issue(
                "OCR_ORIENTATION_CORRECTED",
                f"A página {page_number} foi lida girada em {angle}°: essa orientação produziu "
                "faixas de texto medidas (1:1 com as linhas reconhecidas) e as coordenadas "
                "voltam ao espaço da página original.",
                severity="warning",
                page=page_number,
                context={"angle": angle, "bands": score[1], "exact": score[0]},
            )
        outcome.dpi = raster.dpi
        outcome.operations = list(processed.operations) + (
            [f"rotate_{angle}"] if angle else []
        )
        outcome.engine = engine.name
        outcome.version = engine.version
        outcome.attempted = True

        band_heights = [
            float(band[3]) - float(band[1])
            for band in (getattr(engine, "bands", []) or [])
            if len(band) == 4 and float(band[3]) > float(band[1])
        ]
        if band_heights:
            median_height = median(band_heights)
            if median_height < 6.0:
                # Below ~2 mm at 300 DPI the glyphs are no longer reliably
                # legible; a vision model still answers, but with guesses.  The
                # export stays, and the audit says the source was not legible
                # enough to trust word by word.
                self.recorder.issue(
                    "OCR_LOW_LEGIBILITY",
                    f"A página {page_number} tem glifos de ~{median_height:.1f}px de altura "
                    "(resolução muito baixa): o texto reconhecido pode conter leituras "
                    "incorretas e merece conferência.",
                    severity="warning",
                    page=page_number,
                    context={"median_glyph_px": round(median_height, 2), "dpi": raster.dpi},
                )

        if not words:
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


    def _preprocess_page(self, image, page_number: int) -> PreprocessResult:
        try:
            return preprocess(image)
        except Exception as exc:  # noqa: BLE001 - preprocessing must never kill the page
            # A failure in the enhancement step is not a reason to lose the
            # native text already extracted from this page: fall back to the
            # original raster and record the degradation.
            self.recorder.issue(
                "OCR_PREPROCESS_FAILED",
                f"O pré-processamento falhou na página {page_number}; a imagem original foi usada.",
                severity="warning",
                page=page_number,
                context={"error": str(exc)},
            )
            return PreprocessResult(image=image, operations=[], skipped_reason=str(exc))

    def _recognize_with_variants(self, engine, primary_image, fallback_image, page_number: int):
        """Recognise, retrying only with a *different* input.

        The plan forbids repeating the same request after a failure: the second
        attempt uses the other variant, and the number of attempts is capped by
        CONVERTER_MAX_FALLBACK_ATTEMPTS.  Callers pass the untouched raster as
        the primary image (true geometry) and the enhanced one as fallback.

        The fallback triggers on failure, never on "the text looked small": a
        quality retry was measured on the corpus and made blurry scans worse,
        because the enhanced variant changes the ink layout the geometry depends
        on.
        """
        attempts = max(1, settings.converter_max_fallback_attempts + 1)
        variants = [("original", primary_image), ("enhanced", fallback_image)]
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


def _word_score(words) -> int:
    """Readable text produced by a variant (letters, not symbols)."""
    if not words:
        return 0
    return sum(1 for word in words if sum(ch.isalpha() for ch in word.text) >= 2)


def _landscape_bands(bands) -> int:
    """How many ink bands look like lines of text (wide, not tall)."""
    if not bands:
        return 0
    wide = 0
    for band in bands:
        try:
            x0, y0, x1, y1 = band
        except (TypeError, ValueError):
            continue
        height = max(1.0, float(y1) - float(y0))
        if (float(x1) - float(x0)) / height >= 2.5:
            wide += 1
    return wide


def _label_pair_score(words) -> int:
    """Lines where a label and its value ended up together ("Nome: Ana").

    A form read in the right orientation keeps ``Label:`` and the value on the
    same line; a wrong rotation scatters them across lines.  The count is a
    structural signal about the geometry, independent of the words themselves.
    """
    if not words:
        return 0
    heights = [word.bbox_px[3] - word.bbox_px[1] for word in words if word.bbox_px[3] > word.bbox_px[1]]
    tolerance = max(2.0, (median(heights) if heights else 8.0) * 0.6)
    rows: list[list] = []
    centers: list[float] = []
    for word in words:
        center = (word.bbox_px[1] + word.bbox_px[3]) / 2.0
        for index, value in enumerate(centers):
            if abs(value - center) <= tolerance:
                rows[index].append(word)
                break
        else:
            rows.append([word])
            centers.append(center)
    pairs = 0
    for row in rows:
        labels = [word for word in row if word.text.strip().endswith((":", "="))]
        for label in labels:
            for word in row:
                if word is label or not word.text.strip():
                    continue
                # The value must sit to the *right* of its label on the same
                # line.  In a sideways image the neighbours of a label share its
                # y but belong to other lines, so this also rejects the wrong
                # rotation.
                if word.bbox_px[0] < label.bbox_px[2] - 1:
                    continue
                if word.bbox_px[0] - label.bbox_px[2] > 320:
                    continue
                overlap = min(word.bbox_px[3], label.bbox_px[3]) - max(
                    word.bbox_px[1], label.bbox_px[1]
                )
                height = max(1.0, min(
                    word.bbox_px[3] - word.bbox_px[1],
                    label.bbox_px[3] - label.bbox_px[1],
                ))
                if overlap / height >= 0.6:
                    pairs += 1
                    break
    return pairs


def _geometry_score(words) -> int:
    """How many text lines of the variant look like *lines*.

    A vision model reads the same characters whatever the rotation, so word
    count cannot choose an orientation.  The geometry can: correct orientation
    produces wide, short rows; a wrong one produces tall, narrow columns.  Rows
    with an aspect ratio of at least 2.5 are what a human would call a line of
    text.
    """
    if not words:
        return 0
    heights = [word.bbox_px[3] - word.bbox_px[1] for word in words if word.bbox_px[3] > word.bbox_px[1]]
    tolerance = max(2.0, (median(heights) if heights else 8.0) * 0.6)
    rows: list[list[tuple[float, float, float, float]]] = []
    centers: list[float] = []
    for word in words:
        x0, y0, x1, y1 = word.bbox_px
        center = (y0 + y1) / 2.0
        for index, value in enumerate(centers):
            if abs(value - center) <= tolerance:
                rows[index].append((x0, y0, x1, y1))
                break
        else:
            rows.append([(x0, y0, x1, y1)])
            centers.append(center)
    score = 0
    for row in rows:
        left = min(box[0] for box in row)
        right = max(box[2] for box in row)
        top = min(box[1] for box in row)
        bottom = max(box[3] for box in row)
        height = max(bottom - top, 1.0)
        if (right - left) / height >= 2.5:
            score += 1
    return score


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
