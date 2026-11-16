"""Local filesystem storage backend (default for standalone mode)."""

from __future__ import annotations

import os
import shutil
import tempfile
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

    def copy_to(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._resolve(key), destination)
        return destination


__all__ = ["LocalStorage"]
