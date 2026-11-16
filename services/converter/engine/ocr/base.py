"""Shared OCR primitives (contracts used by every engine adapter).

Extracted from `engines.py` so that engine modules (e.g. `glmocr.py`) can
subclass the contract without circular imports.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(slots=True)
class OcrWord:
    text: str
    bbox_px: tuple[float, float, float, float]
    confidence: float


class OcrUnavailable(RuntimeError):
    pass


class OcrEngine(abc.ABC):
    name: str = "abstract"
    version: str = "unknown"
    kind: str = "ocr"

    @classmethod
    @abc.abstractmethod
    def available(cls) -> bool: ...

    @abc.abstractmethod
    def recognize(self, image, *, mode: str = "page", lang: str | None = None) -> list[OcrWord]: ...


__all__ = ["OcrWord", "OcrUnavailable", "OcrEngine"]
