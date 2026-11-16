"""Storage abstraction: PDFs, generated XLSX and compressed audit artifacts.

The Excel writer and the API never talk to the filesystem directly, so moving
to S3/MinIO is a configuration change, not a code change.
"""

from __future__ import annotations

import abc
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str
    content_type: str


class Storage(abc.ABC):
    """Minimal object-store contract used by the converter."""

    backend: str = "abstract"

    @abc.abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject: ...

    @abc.abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def delete(self, key: str) -> bool: ...

    @abc.abstractmethod
    def local_path(self, key: str) -> Path | None:
        """Return a local path when the backend is filesystem based."""

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> StoredObject:
        return self.put_bytes(key, path.read_bytes(), content_type)

    def size(self, key: str) -> int:
        return len(self.get_bytes(key))

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def open_stream(self, key: str) -> io.BytesIO:
        return io.BytesIO(self.get_bytes(key))


__all__ = ["Storage", "StoredObject"]
