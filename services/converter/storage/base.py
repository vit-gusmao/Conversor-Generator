"""Storage abstraction: PDFs, generated XLSX and compressed audit artifacts.

The Excel writer and the API never talk to the filesystem directly, so moving
to S3/MinIO is a configuration change, not a code change.
"""

from __future__ import annotations

import abc
import hashlib
import io
from collections.abc import Iterable
from typing import BinaryIO
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

    # -- streaming ---------------------------------------------------------
    def open_read(self, key: str) -> BinaryIO:
        """Readable binary stream. Overridden by backends with real streams."""
        return self.open_stream(key)

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Writes a stream without requiring the whole object in memory.

        The default buffers, which is correct but memory bound; backends that
        can stream (local, S3) override it.
        """
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        stored = self.put_bytes(key, data, content_type)
        return StoredObject(
            key=key, size_bytes=total, sha256=digest.hexdigest(), content_type=content_type
        )

    def compose(
        self,
        part_keys: Iterable[str],
        target_key: str,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Concatenates confirmed parts into one immutable object.

        A resumed upload never rewrites an earlier part, so composition is the
        only step that touches the full object. Implementations stream part by
        part instead of buffering the document.
        """
        digest = hashlib.sha256()
        total = 0
        buffer = bytearray()
        for key in part_keys:
            for chunk in self._iter_chunks(key):
                digest.update(chunk)
                buffer.extend(chunk)
                total += len(chunk)
        stored = self.put_bytes(target_key, bytes(buffer), content_type)
        return StoredObject(
            key=target_key,
            size_bytes=total,
            sha256=digest.hexdigest(),
            content_type=content_type,
        )

    def materialize(self, key: str, destination: Path) -> Path:
        """Ensures the object exists on disk and returns the local path.

        Backends with a local path return it directly (no copy); remote
        backends stream the object to disk.
        """
        local = self.local_path(key)
        if local is not None and local.exists():
            return local
        destination.parent.mkdir(parents=True, exist_ok=True)
        stream = self.open_read(key)
        try:
            with destination.open("wb") as handle:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return destination

    def move(self, source_key: str, target_key: str) -> bool:
        """Moves an object. Default streams it (source is deleted after)."""
        stream = self.open_read(source_key)
        try:
            self.put_stream(target_key, stream, "application/octet-stream")
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        self.delete(source_key)
        return True

    def _iter_chunks(self, key: str, chunk_size: int = 1024 * 1024):
        stream = self.open_read(key)
        try:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()


__all__ = ["Storage", "StoredObject"]
