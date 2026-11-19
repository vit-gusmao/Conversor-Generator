"""Submission ports (T5, doc 02 §8).

``StandaloneSubmissionPort`` reuses the validated admission path:
``BatchService`` creates the durable batch and items; the snapshot bytes go
straight to the content-addressed storage key (they are already hashed); the
conversion is created through ``ConversionService.create_from_stored`` with a
stable idempotency key — so a lost HTTP response or a process crash never
duplicates a conversion. ``GatewaySubmissionPort`` (T6) implements the same
contract over the gateway's internal API.
"""

from __future__ import annotations

import uuid
from typing import Any

from models.db import get_session_factory
from storage import get_storage


class SubmissionResult:
    def __init__(self, *, item_id: str, upload_id: str, conversion_id: str) -> None:
        self.item_id = item_id
        self.upload_id = upload_id
        self.conversion_id = conversion_id


class StandaloneSubmissionPort:
    """Submit captured snapshots into the existing standalone services."""

    def ensure_batch(self, owner_id: str, import_id: str) -> str:
        from api.batch_service import BatchService

        batch = BatchService().create_batch(
            owner_id=owner_id,
            client_batch_key=f"source-import:{import_id}",
        )
        return batch["batch_id"]

    def submit_snapshot(
        self,
        owner_id: str,
        batch_id: str,
        *,
        import_id: str,
        import_item_id: int,
        relative_path: str,
        snapshot_key: str | None,
        snapshot_bytes: bytes | None,
        sha256: str,
        size_bytes: int,
        normalized_options: dict[str, Any],
    ) -> SubmissionResult:
        from api.batch_service import BatchService
        from api.service import ConversionService

        service = BatchService()
        client_item_id = f"srcimp:{import_id}:{import_item_id}"
        extension = ".csv" if relative_path.lower().endswith(".csv") else ".xlsx"
        registered = service.register_item(
            owner_id=owner_id,
            batch_id=batch_id,
            client_item_id=client_item_id,
            filename=relative_path.rsplit("/", 1)[-1],
            size_bytes=size_bytes,
            options=normalized_options,
            allow_stamped=True,
        )
        item_id = registered["item"]["item_id"]
        upload_id = registered["upload_id"]

        if registered.get("replayed") and registered["item"].get("conversion_id"):
            # Re-entrega: a conversão já existe para este item.
            return SubmissionResult(
                item_id=item_id,
                upload_id=upload_id,
                conversion_id=registered["item"]["conversion_id"],
            )

        storage = get_storage()
        storage_key = f"documents/{owner_id}/{sha256}{extension}"
        if snapshot_bytes is not None:
            if not storage.exists(storage_key):
                storage.put_bytes(
                    storage_key,
                    snapshot_bytes,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    if extension == ".xlsx"
                    else "text/csv",
                )

        conversion = ConversionService().create_from_stored(
            owner_id=owner_id,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=size_bytes,
            filename=relative_path.rsplit("/", 1)[-1],
            mime_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                if extension == ".xlsx"
                else "text/csv"
            ),
            normalized=normalized_options,
            page_count=None,
            # One stable key per import item: at-least-once submission can
            # never produce two conversions for the same captured version.
            idempotency_key=f"source-import:{import_id}:{import_item_id}",
        )
        conversion_id = conversion["conversion_id"]
        self._bind_item(owner_id, upload_id, item_id, conversion_id)
        return SubmissionResult(item_id=item_id, upload_id=upload_id, conversion_id=conversion_id)

    def _bind_item(self, owner_id: str, upload_id: str, item_id: str, conversion_id: str) -> None:
        """Replicates the post-complete binding: session completed, item queued."""
        from datetime import UTC, datetime

        from models.db import (
            PublicBatchItemRow,
            PublicUploadSessionRow,
        )
        from sqlalchemy import select

        session = get_session_factory()()
        try:
            upload = session.execute(
                select(PublicUploadSessionRow).where(PublicUploadSessionRow.id == upload_id)
            ).scalar_one_or_none()
            if upload is not None and upload.state != "completed":
                upload.state = "completed"
            item = session.execute(
                select(PublicBatchItemRow).where(PublicBatchItemRow.id == item_id)
            ).scalar_one_or_none()
            if item is not None:
                item.conversion_id = conversion_id
                item.status = "queued"
                item.updated_at = datetime.now(UTC)
            session.commit()
        finally:
            session.close()

    def seal_batch(self, owner_id: str, batch_id: str) -> None:
        from api.batch_service import BatchService

        try:
            BatchService().seal(owner_id=owner_id, batch_id=batch_id)
        except Exception:
            # Sealing is a UI affordance; the import record stays authoritative.
            pass

    def reconcile(self, owner_id: str, batch_id: str) -> dict[str, Any]:
        from api.batch_service import BatchService

        batch = BatchService().get_batch(owner_id=owner_id, batch_id=batch_id)
        counts = batch.get("counts", {})
        finished = counts.get("completed", 0) + counts.get("with_warnings", 0) + counts.get("failed", 0) + counts.get("cancelled", 0)
        return {
            "batch_id": batch_id,
            "status": batch.get("status"),
            "finished_items": finished,
            "total_items": counts.get("total_items", 0),
        }

    def cancel(self, owner_id: str, batch_id: str) -> None:
        from api.batch_service import BatchService

        BatchService().cancel_batch(owner_id=owner_id, batch_id=batch_id)


class GatewaySubmissionPort:
    """Full-mode adapter (implemented in T6)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("GatewaySubmissionPort é entregue na tarefa T6.")


__all__ = ["GatewaySubmissionPort", "StandaloneSubmissionPort", "SubmissionResult"]
