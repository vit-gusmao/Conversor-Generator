"""OCR engine adapters.

Each adapter is optional.  The runner asks the registry for the first available
engine and records exactly which one ran, with version, DPI and parameters.
When no engine is installed the pipeline does **not** hallucinate text: it
records an `OCR_UNAVAILABLE` issue and leaves the region unclassified.

The registry order is the preference order: `glm-ocr` (local VLM on GPU via
Ollama) first, then the lightweight deterministic recognizers.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from typing import Any

from config import settings
from engine.ocr.base import OcrEngine, OcrUnavailable, OcrWord
from engine.ocr.glmocr import GlmOcrEngine


class TesseractEngine(OcrEngine):
    name = "tesseract"

    def __init__(self) -> None:
        import pytesseract  # type: ignore[import-not-found]

        self._pt = pytesseract
        try:
            self.version = str(pytesseract.get_tesseract_version()).splitlines()[0]
        except Exception:  # noqa: BLE001
            self.version = "unknown"

    @classmethod
    def available(cls) -> bool:
        if importlib.util.find_spec("pytesseract") is None:
            return False
        if shutil.which("tesseract") is None and os.name == "nt":
            # Windows installs are often present but not on PATH.
            for candidate in (
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ):
                if os.path.exists(candidate):
                    os.environ.setdefault("TESSERACT_CMD", candidate)
                    return True
            return False
        return shutil.which("tesseract") is not None

    def recognize(self, image, *, mode: str = "page", lang: str | None = None) -> list[OcrWord]:
        from PIL import Image

        cmd = os.environ.get("TESSERACT_CMD")
        if cmd:
            self._pt.pytesseract.tesseract_cmd = cmd
        psm = {"page": 6, "region": 6, "line": 7, "cell": 7}.get(mode, 6)
        config = f"--oem 3 --psm {psm}"
        try:
            data = self._pt.image_to_data(
                image if isinstance(image, Image.Image) else image,
                lang=lang or "por+eng",
                config=config,
                output_type=self._pt.Output.DICT,
                timeout=settings.converter_task_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable(f"tesseract failed: {exc}") from exc

        words: list[OcrWord] = []
        for i, text in enumerate(data.get("text", [])):
            text = (text or "").strip()
            if not text:
                continue
            try:
                conf = float(data["conf"][i])
            except (KeyError, ValueError, TypeError):
                conf = -1.0
            if conf < 0:
                continue
            left = float(data["left"][i])
            top = float(data["top"][i])
            width = float(data["width"][i])
            height = float(data["height"][i])
            words.append(
                OcrWord(text=text, bbox_px=(left, top, left + width, top + height), confidence=conf / 100.0)
            )
        return words


class RapidOCREngine(OcrEngine):
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

        self._ocr = RapidOCR()
        try:
            import rapidocr_onnxruntime as pkg  # type: ignore[import-not-found]

            self.version = str(getattr(pkg, "__version__", "unknown"))
        except Exception:  # noqa: BLE001
            self.version = "unknown"

    @classmethod
    def available(cls) -> bool:
        return importlib.util.find_spec("rapidocr_onnxruntime") is not None

    def recognize(self, image, *, mode: str = "page", lang: str | None = None) -> list[OcrWord]:
        import numpy as np

        try:
            result, _elapsed = self._ocr(np.array(image.convert("RGB")), use_det=True, use_cls=False, use_rec=True)
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable(f"rapidocr failed: {exc}") from exc

        words: list[OcrWord] = []
        for item in result or []:
            try:
                box, text, score = item[0], item[1], float(item[2])
            except (IndexError, TypeError, ValueError):
                continue
            text = (text or "").strip()
            if not text:
                continue
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            words.append(
                OcrWord(
                    text=text,
                    bbox_px=(min(xs), min(ys), max(xs), max(ys)),
                    confidence=max(0.0, min(1.0, score)),
                )
            )
        return words


class NullEngine(OcrEngine):
    name = "none"

    @classmethod
    def available(cls) -> bool:
        return True

    def recognize(self, image, *, mode: str = "page", lang: str | None = None) -> list[OcrWord]:
        raise OcrUnavailable("No OCR engine is installed")


_ENGINES: tuple[type[OcrEngine], ...] = (GlmOcrEngine, RapidOCREngine, TesseractEngine)


def available_engines() -> list[str]:
    result = []
    for engine in _ENGINES:
        try:
            if engine.available():
                result.append(engine.name)
        except Exception:  # noqa: BLE001
            continue
    return result


def get_engine(preferred: str | None = None) -> OcrEngine:
    preferred = (preferred or os.getenv("CONVERTER_OCR_ENGINE") or "").lower()
    ordered: list[type[OcrEngine]] = list(_ENGINES)
    if preferred:
        ordered.sort(key=lambda e: 0 if e.name == preferred else 1)
    for engine_cls in ordered:
        try:
            if engine_cls.available():
                return engine_cls()
        except Exception:  # noqa: BLE001 - a broken install must not crash the run
            continue
    return NullEngine()


def ocr_capability() -> dict[str, object]:
    engines = available_engines()
    capability: dict[str, object] = {
        "available": bool(engines),
        "engines": engines,
        "selected": engines[0] if engines else None,
    }
    return capability


def engine_details(engine: OcrEngine) -> dict[str, Any]:
    """Optional audit metadata describing how the engine produces results."""
    describe = getattr(engine, "describe", None)
    if callable(describe):
        try:
            return dict(describe())
        except Exception:  # noqa: BLE001 - audit metadata must never break a run
            return {}
    return {}


__all__ = [
    "OcrWord",
    "OcrEngine",
    "OcrUnavailable",
    "GlmOcrEngine",
    "TesseractEngine",
    "RapidOCREngine",
    "NullEngine",
    "available_engines",
    "engine_details",
    "get_engine",
    "ocr_capability",
]
