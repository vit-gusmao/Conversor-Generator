"""S3 / MinIO storage backend. `boto3` is imported lazily so the service can
run in standalone mode without it installed."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

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

    # -- streaming ---------------------------------------------------------
    def open_read(self, key: str) -> BinaryIO:
        return self._client.get_object(Bucket=self.bucket, Key=key)["Body"]

    def put_stream(
        self,
        key: str,
        stream: BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Multipart upload driven by boto3; bounded memory for large files."""
        digest = hashlib.sha256()
        total = 0

        def _hashed():
            nonlocal total
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    return
                digest.update(chunk)
                total += len(chunk)
                yield chunk

        # boto3 picks multipart automatically for file-like objects.
        self._client.upload_fileobj(
            _hashed(), self.bucket, key, ExtraArgs={"ContentType": content_type}
        )
        return StoredObject(
            key=key, size_bytes=total, sha256=digest.hexdigest(), content_type=content_type
        )

    def compose(
        self,
        part_keys: Iterable[str],
        target_key: str,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Server-side multipart assembly from the confirmed part objects.

        Each source part is streamed as one multipart part, so memory stays
        bounded by the part size rather than the document size.
        """
        response = self._client.create_multipart_upload(
            Bucket=self.bucket, Key=target_key, ContentType=content_type
        )
        upload_id = response["UploadId"]
        digest = hashlib.sha256()
        total = 0
        completed: list[dict[str, object]] = []
        try:
            for index, part_key in enumerate(part_keys, start=1):
                body = self.get_bytes(part_key)
                digest.update(body)
                total += len(body)
                sent = self._client.upload_part(
                    Bucket=self.bucket,
                    Key=target_key,
                    UploadId=upload_id,
                    PartNumber=index,
                    Body=body,
                )
                completed.append({"PartNumber": index, "ETag": sent["ETag"]})
            self._client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=target_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": completed},
            )
        except Exception:
            try:
                self._client.abort_multipart_upload(
                    Bucket=self.bucket, Key=target_key, UploadId=upload_id
                )
            except Exception:  # noqa: BLE001
                pass
            raise
        return StoredObject(
            key=target_key,
            size_bytes=total,
            sha256=digest.hexdigest(),
            content_type=content_type,
        )

    def move(self, source_key: str, target_key: str) -> bool:
        self._client.copy_object(
            Bucket=self.bucket,
            Key=target_key,
            CopySource={"Bucket": self.bucket, "Key": source_key},
        )
        self._client.delete_object(Bucket=self.bucket, Key=source_key)
        return True

    def presigned_url(self, key: str, expires_in: int = 900) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
        )


__all__ = ["S3Storage"]
