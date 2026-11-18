"""Seekable document source.

A run no longer holds the whole PDF in memory: it works from a path on disk and
computes the digest by streaming. That is what lets a 10,000-page document go
through the same code path as a one-page one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_CHUNK = 1024 * 1024


@dataclass(slots=True, frozen=True)
class DocumentSource:
    """A materialized document plus its identity."""

    path: Path
    sha256: str
    size_bytes: int

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    def open(self):
        return self.path.open("rb")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def head_bytes(path: Path, size: int = 1024) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def source_from_path(path: Path, *, expected_sha256: str | None = None) -> DocumentSource:
    """Builds the source, trusting a stored digest only when given one.

    The digest is the cache key for every checkpoint, so it is recomputed when
    absent; a corrupted file must not silently reuse another document's pages.
    """
    stat = path.stat()
    digest = expected_sha256 or sha256_file(path)
    return DocumentSource(path=path, sha256=digest, size_bytes=int(stat.st_size))


__all__ = ["DocumentSource", "sha256_file", "head_bytes", "source_from_path"]
