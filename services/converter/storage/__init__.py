"""Storage factory."""

from __future__ import annotations

from config import settings

from .base import Storage, StoredObject
from .local import LocalStorage

_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        if settings.storage_backend == "s3":
            from .s3 import S3Storage

            _storage = S3Storage()
        else:
            _storage = LocalStorage()
    return _storage


def set_storage(storage: Storage | None) -> None:
    """Test hook."""
    global _storage
    _storage = storage


__all__ = ["get_storage", "set_storage", "Storage", "StoredObject", "LocalStorage"]
