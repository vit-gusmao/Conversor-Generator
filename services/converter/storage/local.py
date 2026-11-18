"""Local filesystem storage backend (default for standalone mode)."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterable
from typing import BinaryIO
from pathlib import Path

from config import settings

from .base import Storage, StoredObject


class LocalStorage(Storage):
    backend = "local"

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else settings.storage_dir
        self.root.mkdir(parents=True, exist_ok=True)

    # -- path safety -------------------------------------------------------
    def _resolve(self, key: str) -> Path:
        if not key or key.startswith(("/", "\\")) or ".." in key.split("/"):
            raise ValueError(f"unsafe storage key: {key!r}")
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if root not in candidate.parents and candidate != root:
            raise ValueError(f"storage key escapes root: {key!r}")
        return candidate

    # -- API ---------------------------------------------------------------
    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return StoredObject(key=key, size_bytes=len(data), sha256=self.sha256(data), content_type=content_type)

    def get_bytes(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> bool:
        try:
            self._resolve(key).unlink()
            return True
        except FileNotFoundError:
            return False

    def local_path(self, key: str) -> Path | None:
        return self._resolve(key)

    def open_read(self, key: str) -> BinaryIO:
        return self._resolve(key).open("rb")

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Streams to a temporary file, hashing incrementally.

        The object only becomes visible after `os.replace`, so a crash leaves
        no half-written part under its final key.
        """
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
                    total += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return StoredObject(
            key=key, size_bytes=total, sha256=digest.hexdigest(), content_type=content_type
        )

    def compose(
        self,
        part_keys: Iterable[str],
        target_key: str,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Copies each confirmed part into the final object with bounded memory."""
        path = self._resolve(target_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".compose")
        try:
            with os.fdopen(fd, "wb") as handle:
                for part_key in part_keys:
                    with self._resolve(part_key).open("rb") as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                            handle.write(chunk)
                            total += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return StoredObject(
            key=target_key,
            size_bytes=total,
            sha256=digest.hexdigest(),
            content_type=content_type,
        )

    def move(self, source_key: str, target_key: str) -> bool:
        source = self._resolve(source_key)
        target = self._resolve(target_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        return True

    def copy_to(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._resolve(key), destination)
        return destination


__all__ = ["LocalStorage"]
