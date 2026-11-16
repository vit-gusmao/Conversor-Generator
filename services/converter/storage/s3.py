"""S3 / MinIO storage backend. `boto3` is imported lazily so the service can
run in standalone mode without it installed."""

from __future__ import annotations

from pathlib import Path

from config import settings

from .base import Storage, StoredObject


class S3Storage(Storage):
    backend = "s3"

    def __init__(self) -> None:
        try:
            import boto3  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "STORAGE_BACKEND=s3 requires the 's3' extra: pip install '.[s3]'"
            ) from exc

        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            use_ssl=settings.s3_secure,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:  # noqa: BLE001 - any failure means "create it"
            try:
                self._client.create_bucket(Bucket=self.bucket)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"cannot create bucket {self.bucket!r}: {exc}") from exc

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return StoredObject(key=key, size_bytes=len(data), sha256=self.sha256(data), content_type=content_type)

    def get_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete(self, key: str) -> bool:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def local_path(self, key: str) -> Path | None:
        return None

    def presigned_url(self, key: str, expires_in: int = 900) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
        )


__all__ = ["S3Storage"]
