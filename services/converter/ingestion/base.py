"""Ingestion contracts: document -> canonical pages.

An ingestor never trusts the file name or the client MIME type.  It detects the
real content, decodes it with a named decoder and returns canonical pages with
the transformations that were applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class IngestionError(Exception):
    """Stable ingestion failure (``UNSUPPORTED_FORMAT``, ``CODEC_UNAVAILABLE``...)."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(slots=True)
class IngestedPage:
    page_number: int
    image: Any  # PIL.Image.Image, RGB, upright
    width: int
    height: int
    frame_index: int = 0
    transformations: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IngestedDocument:
    source_type: str  # pdf | image
    mime_type: str
    decoder: str
    decoder_version: str
    pages: list[IngestedPage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)


__all__ = ["IngestionError", "IngestedPage", "IngestedDocument"]
