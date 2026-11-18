"""Batches: durable containers for an unbounded number of documents.

Ownership rules mirror the rest of the public API: an owner resolved from the
credential, never from the request body. Item status is derived from the
conversion it points at, so there is a single source of truth for progress.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.deps import ApiError
from api.service import _normalize_options
from config import settings
from models.db import (
    PublicBatchEventRow,
    PublicBatchItemRow,
    PublicBatchRow,
    PublicConversionRow,
    PublicUploadSessionRow,
    get_session_factory,
)

TERMINAL_ITEM_STATUSES = frozenset(
    {"completed", "completed_with_warnings", "failed", "cancelled"}
)
#: Statuses that belong to the upload phase and are not derived from a conversion.
UPLOAD_PHASE_STATUSES = frozenset(
    {"pending_upload", "uploading", "upload_failed"}
)
ACTIVE_STATUSES = frozenset(
    {
        "received",
        "validating",
        "queued",
        "inspecting",
        "extracting",
        "structuring",
        "normalizing",
        "reconciling",
        "generating",
        "auditing",
    }
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_IMAGE_SUFFIXES = {
    "png",
    "jpg",
    "jpeg",
    "bmp",
    "webp",
    "tif",
    "tiff",
    "gif",
    "ico",
    "jp2",
    "pnm",
    "avif",
    "heic",
    "heif",
}


def now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


def clamp_limit(limit: int | None) -> int:
    if not limit or limit <= 0:
        return DEFAULT_LIMIT
    return min(int(limit), MAX_LIMIT)


def declared_kind(filename: str) -> str:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "pdf":
        return "pdf"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    return "unknown"


def item_fingerprint(options: dict[str, Any], filename: str, size_bytes: int) -> str:
    canonical = json.dumps(
        {"options": options, "filename": filename, "size": int(size_bytes)},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def effective_item_status(item: PublicBatchItemRow, conversion_status: str | None) -> str:
    """Upload-phase status wins until a conversion exists."""
    if item.status in UPLOAD_PHASE_STATUSES or item.status == "cancelled":
        return item.status
    if not conversion_status:
        return "queued" if item.conversion_id else item.status
    if conversion_status in TERMINAL_ITEM_STATUSES:
        return conversion_status
    if conversion_status == "queued":
        return "queued"
    return "processing"


def _batch_status(counts: dict[str, int], sealed: bool, cancel_requested: bool) -> str:
    total = int(counts.get("total_items", 0))
    if cancel_requested and counts.get("cancelled", 0) == total and total > 0:
        return "cancelled"
    if not sealed:
        return "accepting"
    terminal = sum(counts.get(status, 0) for status in TERMINAL_ITEM_STATUSES)
    if terminal >= total:
        problems = (
            counts.get("failed", 0)
            + counts.get("cancelled", 0)
            + counts.get("with_warnings", 0)
        )
        return "completed_with_errors" if problems > 0 else "completed"
    return "processing"


def _counts(session: Session, batch_id: str) -> dict[str, int]:
    """Counts per item status, merging upload-phase and conversion statuses.

    Two grouped queries keep this O(distinct statuses) instead of loading every
    item, which matters for a batch with thousands of documents.
    """
    stored = dict(
        session.execute(
            select(PublicBatchItemRow.status, func.count())
            .where(PublicBatchItemRow.batch_id == batch_id)
            .group_by(PublicBatchItemRow.status)
        ).all()
    )
    # Items already counted as `cancelled`/upload-phase must not be counted
    # again through their conversion, or the totals double-count them.
    joined = dict(
        session.execute(
            select(PublicConversionRow.status, func.count())
            .join(PublicBatchItemRow, PublicBatchItemRow.conversion_id == PublicConversionRow.id)
            .where(
                PublicBatchItemRow.batch_id == batch_id,
                PublicBatchItemRow.status.notin_(["cancelled", "upload_failed"]),
            )
            .group_by(PublicConversionRow.status)
        ).all()
    )

    counts = {
        "total_items": sum(stored.values()),
        "pending_upload": 0,
        "uploading": 0,
        "queued": 0,
        "processing": 0,
        "completed": 0,
        "with_warnings": 0,
        "failed": 0,
        "cancelled": 0,
    }

    # Items still in the upload phase keep their stored status.
    upload_phase_total = 0
    for status in UPLOAD_PHASE_STATUSES:
        value = int(stored.get(status, 0))
        counts[status] = value
        upload_phase_total += value
    counts["cancelled"] = int(stored.get("cancelled", 0))

    # Everything else is described by its conversion.
    for status, value in joined.items():
        value = int(value)
        if status == "completed":
            counts["completed"] += value
        elif status == "completed_with_warnings":
            counts["with_warnings"] += value
        elif status == "failed":
            counts["failed"] += value
        elif status == "cancelled":
            counts["cancelled"] += value
        elif status == "queued":
            counts["queued"] += value
        elif status in ACTIVE_STATUSES:
            counts["processing"] += value

    # Items whose stored status is terminal but that have no conversion yet.
    orphan_terminal = max(
        0,
        counts["total_items"] - upload_phase_total - counts["cancelled"] - sum(joined.values()),
    )
    if orphan_terminal:
        counts["queued"] += orphan_terminal

    return counts


def _progress(session: Session, batch_id: str) -> dict[str, int]:
    row = session.execute(
        select(
            func.coalesce(func.sum(PublicBatchItemRow.pages_total), 0),
            func.coalesce(func.sum(PublicBatchItemRow.pages_attempted), 0),
            func.coalesce(func.sum(PublicBatchItemRow.pages_completed), 0),
            func.coalesce(func.sum(PublicBatchItemRow.pages_failed), 0),
        ).where(PublicBatchItemRow.batch_id == batch_id)
    ).one()
    counts = _counts(session, batch_id)
    finished = (
        counts["completed"]
        + counts["with_warnings"]
        + counts["failed"]
        + counts["cancelled"]
    )
    return {
        "finished_items": finished,
        "pages_total": int(row[0]),
        "pages_attempted": int(row[1]),
        "pages_completed": int(row[2]),
        "pages_failed": int(row[3]),
    }


def _links(batch_id: str) -> dict[str, str]:
    base = f"/api/v1/batches/{batch_id}"
    return {
        "self": base,
        "items": f"{base}/items",
        "events": f"{base}/events",
        "seal": f"{base}/seal",
        "cancel": f"{base}/cancel",
    }


def _item_links(batch_id: str, item_id: str, conversion_id: str | None) -> dict[str, str]:
    base = f"/api/v1/batches/{batch_id}/items/{item_id}"
    links = {"cancel": f"{base}/cancel", "retry": f"{base}/retry"}
    if conversion_id:
        links["conversion"] = f"/api/v1/conversions/{conversion_id}"
        links["result"] = f"/api/v1/conversions/{conversion_id}/result"
    return links


def item_payload(
    item: PublicBatchItemRow,
    *,
    conversion_status: str | None = None,
) -> dict[str, Any]:
    status = effective_item_status(item, conversion_status)
    options = item.options_json if isinstance(item.options_json, dict) else {}
    kind = item.kind if item.kind in {"pdf", "image"} else "unknown"
    available: list[str] = ["txt"] if kind == "image" else ["xlsx", "txt", "csv"]
    if isinstance(options.get("target_formats"), list) and options["target_formats"]:
        available = [str(value) for value in options["target_formats"]]
    return {
        "item_id": item.id,
        "batch_id": item.batch_id,
        "client_item_id": item.client_item_id,
        "ordinal": int(item.ordinal or 0),
        "filename": item.filename,
        "size_bytes": int(item.size_bytes or 0),
        "kind": kind,
        "status": status,
        "options": options,
        "available_formats": available,
        "upload_id": item.upload_id,
        "conversion_id": item.conversion_id,
        "pages_total": item.pages_total,
        "pages_attempted": item.pages_attempted,
        "pages_completed": item.pages_completed,
        "pages_failed": item.pages_failed,
        "phase": item.phase,
        "error": item.error_json or None,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "links": _item_links(item.batch_id, item.id, item.conversion_id),
    }


def batch_payload(
    session: Session,
    batch: PublicBatchRow,
    counts: dict[str, int] | None = None,
    progress: dict[str, int] | None = None,
) -> dict[str, Any]:
    resolved_counts = counts if counts is not None else _counts(session, batch.id)
    resolved_progress = progress if progress is not None else _progress(session, batch.id)
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "client_batch_key": batch.client_batch_key,
        "counts": resolved_counts,
        "progress": resolved_progress,
        "created_at": _iso(batch.created_at),
        "sealed_at": _iso(batch.sealed_at),
        "cancel_requested_at": _iso(batch.cancel_requested_at),
        "version": int(batch.version or 1),
        "links": _links(batch.id),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def emit_event(
    session: Session,
    batch: PublicBatchRow,
    *,
    type_: str = "batch",
    item_id: str | None = None,
    status: str | None = None,
    item_status: str | None = None,
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> PublicBatchEventRow:
    """Appends one aggregated batch event with a monotonic per-batch sequence."""
    current = session.execute(
        select(func.max(PublicBatchEventRow.sequence)).where(
            PublicBatchEventRow.batch_id == batch.id
        )
    ).scalar()
    event = PublicBatchEventRow(
        batch_id=batch.id,
        sequence=int(current or 0) + 1,
        type=type_,
        item_id=item_id,
        status=status or batch.status,
        item_status=item_status,
        message=message[:500],
        payload_json=payload or {},
    )
    session.add(event)
    return event


class BatchService:
    """Persistent batch lifecycle. Every method takes the resolved owner."""

    # -- create / read up to seal -----------------------------------------
    def create_batch(self, *, owner_id: str, client_batch_key: str) -> dict[str, Any]:
        key = (client_batch_key or "").strip()
        if not key or len(key) > 128:
            raise ApiError(
                400,
                "INVALID_OPTION",
                "client_batch_key deve ter entre 1 e 128 caracteres.",
            )
        if not settings.batch_upload_enabled:
            raise ApiError(503, "BATCH_DISABLED", "Envio em lote está desativado.")

        session = get_session_factory()()
        try:
            existing = session.execute(
                select(PublicBatchRow).where(
                    PublicBatchRow.owner_id == owner_id,
                    PublicBatchRow.client_batch_key == key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return batch_payload(session, existing)

            batch = PublicBatchRow(
                id=new_id("batch"),
                owner_id=owner_id,
                client_batch_key=key,
                status="accepting",
                version=1,
            )
            session.add(batch)
            try:
                session.commit()
            except IntegrityError:
                # Two concurrent creates with the same key: replay the winner.
                session.rollback()
                existing = session.execute(
                    select(PublicBatchRow).where(
                        PublicBatchRow.owner_id == owner_id,
                        PublicBatchRow.client_batch_key == key,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return batch_payload(session, existing)

            emit_event(session, batch, message="Lote criado.")
            session.commit()
            return batch_payload(session, batch)
        finally:
            session.close()

    def _load(self, session: Session, owner_id: str, batch_id: str, *, lock: bool = False) -> PublicBatchRow:
        batch = session.get(PublicBatchRow, batch_id, with_for_update=lock)
        if batch is None:
            raise ApiError(404, "NOT_FOUND", "Lote não encontrado.")
        if batch.owner_id != owner_id:
            raise ApiError(403, "FORBIDDEN", "Você não tem permissão para acessar este lote.")
        return batch

    def get_batch(self, *, owner_id: str, batch_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id)
            self._refresh_status(session, batch)
            return batch_payload(session, batch)
        finally:
            session.close()

    def _refresh_status(self, session: Session, batch: PublicBatchRow) -> None:
        counts = _counts(session, batch.id)
        status = _batch_status(
            counts,
            sealed=batch.sealed_at is not None,
            cancel_requested=batch.cancel_requested_at is not None,
        )
        if status != batch.status:
            batch.status = status
            batch.version = int(batch.version or 1) + 1
            session.commit()

    def register_item(
        self,
        *,
        owner_id: str,
        batch_id: str,
        client_item_id: str,
        filename: str,
        size_bytes: int,
        options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        client_key = (client_item_id or "").strip()
        if not client_key or len(client_key) > 128:
            raise ApiError(
                400, "INVALID_OPTION", "client_item_id deve ter entre 1 e 128 caracteres."
            )
        safe_name = (filename or "").replace("\\", "/").split("/")[-1].strip()[:512]
        if not safe_name:
            raise ApiError(400, "INVALID_FILE", "O nome do arquivo é obrigatório.")
        size = int(size_bytes or 0)
        if size <= 0:
            raise ApiError(400, "INVALID_FILE", "O arquivo está vazio.")
        max_file = settings.upload_max_bytes if settings.upload_max_bytes else None
        if max_file and size > max_file:
            raise ApiError(
                400,
                "FILE_TOO_LARGE",
                f"O arquivo excede o limite de {max_file // (1024 * 1024)} MiB.",
            )

        kind = declared_kind(safe_name)
        if kind == "unknown":
            raise ApiError(
                400,
                "INVALID_FILE",
                "Envie um PDF ou uma imagem (PNG, JPEG, TIFF, GIF, BMP, WebP…).",
            )
        normalized = _normalize_options(options, kind)
        fingerprint = item_fingerprint(normalized, safe_name, size)

        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id, lock=True)
            if batch.sealed_at is not None:
                raise ApiError(409, "BATCH_NOT_OPEN", "Este lote não aceita mais novos itens.")
            if batch.cancel_requested_at is not None:
                raise ApiError(409, "BATCH_CANCELLED", "O lote foi cancelado.")

            existing = session.execute(
                select(PublicBatchItemRow).where(
                    PublicBatchItemRow.batch_id == batch.id,
                    PublicBatchItemRow.client_item_id == client_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise ApiError(
                        409,
                        "IDEMPOTENT_REPLAY",
                        "O item já foi registrado com outros dados ou opções.",
                        details={"item_id": existing.id},
                    )
                upload = (
                    session.get(PublicUploadSessionRow, existing.upload_id)
                    if existing.upload_id
                    else None
                )
                return {
                    "item": item_payload(existing),
                    "upload_id": existing.upload_id,
                    "part_size": settings.upload_part_bytes,
                    "next_part": _next_part(session, existing.upload_id),
                    "replayed": True,
                    "expires_at": _iso(upload.expires_at) if upload else None,
                }

            ordinal = (
                session.execute(
                    select(func.max(PublicBatchItemRow.ordinal)).where(
                        PublicBatchItemRow.batch_id == batch.id
                    )
                ).scalar()
                or 0
            ) + 1

            item = PublicBatchItemRow(
                id=new_id("item"),
                batch_id=batch.id,
                client_item_id=client_key,
                ordinal=int(ordinal),
                filename=safe_name,
                size_bytes=size,
                kind=kind,
                options_json=normalized,
                request_fingerprint=fingerprint,
                status="pending_upload",
            )
            session.add(item)
            session.flush()

            upload = PublicUploadSessionRow(
                id=new_id("upl"),
                owner_id=owner_id,
                item_id=item.id,
                filename=safe_name,
                expected_bytes=size,
                received_bytes=0,
                part_size=settings.upload_part_bytes,
                state="pending",
                expires_at=now() + _ttl(),
            )
            session.add(upload)
            session.flush()
            item.upload_id = upload.id
            batch.version = int(batch.version or 1) + 1

            emit_event(
                session,
                batch,
                type_="item",
                item_id=item.id,
                item_status="pending_upload",
                message=f"Item registrado: {safe_name}.",
                payload={"filename": safe_name, "size_bytes": size, "kind": kind},
            )
            try:
                session.commit()
            except IntegrityError:
                # Concurrent registration of the same client_item_id.
                session.rollback()
                existing = session.execute(
                    select(PublicBatchItemRow).where(
                        PublicBatchItemRow.batch_id == batch_id,
                        PublicBatchItemRow.client_item_id == client_key,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return {
                    "item": item_payload(existing),
                    "upload_id": existing.upload_id,
                    "part_size": settings.upload_part_bytes,
                    "next_part": _next_part(session, existing.upload_id),
                    "replayed": True,
                    "expires_at": None,
                }

            session.refresh(item)
            return {
                "item": item_payload(item),
                "upload_id": upload.id,
                "part_size": settings.upload_part_bytes,
                "next_part": 0,
                "replayed": False,
                "expires_at": _iso(upload.expires_at),
            }
        finally:
            session.close()

    def list_items(
        self,
        *,
        owner_id: str,
        batch_id: str,
        cursor: str | None = None,
        limit: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        size = clamp_limit(limit)
        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id)
            query = (
                select(PublicBatchItemRow, PublicConversionRow.status)
                .outerjoin(
                    PublicConversionRow,
                    PublicConversionRow.id == PublicBatchItemRow.conversion_id,
                )
                .where(PublicBatchItemRow.batch_id == batch.id)
                .order_by(PublicBatchItemRow.ordinal, PublicBatchItemRow.id)
                .limit(size + 1)
            )
            decoded = decode_cursor(cursor)
            if decoded:
                ordinal = int(decoded.get("ordinal", 0))
                last_id = str(decoded.get("id", ""))
                query = query.where(
                    (PublicBatchItemRow.ordinal > ordinal)
                    | (
                        (PublicBatchItemRow.ordinal == ordinal)
                        & (PublicBatchItemRow.id > last_id)
                    )
                )

            rows = session.execute(query).all()
            has_more = len(rows) > size
            rows = rows[:size]

            items = [
                item_payload(item, conversion_status=conversion_status)
                for item, conversion_status in rows
            ]
            if status:
                items = [payload for payload in items if payload["status"] == status]

            next_cursor = None
            if has_more and rows:
                last_item = rows[-1][0]
                next_cursor = encode_cursor(
                    {"ordinal": int(last_item.ordinal or 0), "id": last_item.id}
                )
            return {"items": items, "next_cursor": next_cursor}
        finally:
            session.close()

    # -- seal / cancel / retry --------------------------------------------
    def seal(self, *, owner_id: str, batch_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id, lock=True)
            if batch.sealed_at is not None:
                return {
                    "batch_id": batch.id,
                    "status": batch.status,
                    "sealed_at": _iso(batch.sealed_at),
                    "message": "O lote já estava selado.",
                }

            # `upload_failed` counts as a terminal outcome: the item was
            # received and the user finished the attempt, so it must not block
            # sealing (otherwise a failed upload traps the whole batch).
            pending = session.execute(
                select(func.count()).where(
                    PublicBatchItemRow.batch_id == batch.id,
                    PublicBatchItemRow.status.in_(["pending_upload", "uploading"]),
                )
            ).scalar() or 0
            if pending:
                raise ApiError(
                    409,
                    "BATCH_HAS_PENDING_UPLOADS",
                    f"O lote ainda possui {int(pending)} upload(s) pendente(s).",
                    details={"pending_uploads": int(pending)},
                )

            batch.sealed_at = now()
            batch.version = int(batch.version or 1) + 1
            counts = _counts(session, batch.id)
            batch.status = _batch_status(counts, sealed=True, cancel_requested=False)
            emit_event(
                session,
                batch,
                type_="batch",
                message="Lote selado; novas inclusões estão bloqueadas.",
                payload={"counts": counts},
            )
            session.commit()
            return {
                "batch_id": batch.id,
                "status": batch.status,
                "sealed_at": _iso(batch.sealed_at),
                "message": "Lote selado.",
            }
        finally:
            session.close()

    def cancel_batch(self, *, owner_id: str, batch_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id, lock=True)
            if batch.cancel_requested_at is None:
                batch.cancel_requested_at = now()
                batch.version = int(batch.version or 1) + 1

            # Active items are cancelled; published artifacts are never undone.
            rows = session.execute(
                select(PublicBatchItemRow).where(
                    PublicBatchItemRow.batch_id == batch.id,
                    PublicBatchItemRow.status.notin_(list(TERMINAL_ITEM_STATUSES)),
                )
            ).scalars()
            from api.service import ConversionService

            service = ConversionService()
            for item in rows:
                if item.conversion_id:
                    try:
                        service.cancel(owner_id=owner_id, conversion_id=item.conversion_id)
                    except ApiError:
                        pass
                item.status = "cancelled"
                item.phase = "cancelled"
            counts = _counts(session, batch.id)
            batch.status = _batch_status(counts, sealed=batch.sealed_at is not None, cancel_requested=True)
            emit_event(
                session,
                batch,
                type_="batch",
                message="Cancelamento do lote solicitado.",
                payload={"counts": counts},
            )
            session.commit()
            return batch_payload(session, batch)
        finally:
            session.close()

    def cancel_item(self, *, owner_id: str, batch_id: str, item_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id)
            item = self._load_item(session, batch.id, item_id)
            if item.status in TERMINAL_ITEM_STATUSES:
                return item_payload(item)

            if item.conversion_id:
                from api.service import ConversionService

                try:
                    ConversionService().cancel(owner_id=owner_id, conversion_id=item.conversion_id)
                except ApiError:
                    pass
            item.status = "cancelled"
            item.phase = "cancelled"
            batch.version = int(batch.version or 1) + 1
            emit_event(
                session,
                batch,
                type_="item",
                item_id=item.id,
                item_status="cancelled",
                message="Item cancelado.",
            )
            session.commit()
            session.refresh(item)
            return item_payload(item)
        finally:
            session.close()

    def retry_item(
        self, *, owner_id: str, batch_id: str, item_id: str, scope: str
    ) -> dict[str, Any]:
        if scope not in {"upload", "conversion"}:
            raise ApiError(400, "INVALID_OPTION", "scope deve ser 'upload' ou 'conversion'.")

        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id)
            item = self._load_item(session, batch.id, item_id)

            if scope == "upload":
                # Keeps the same identity: the client resumes into the same
                # session with the same client_item_id.
                upload = (
                    session.get(PublicUploadSessionRow, item.upload_id)
                    if item.upload_id
                    else None
                )
                if upload is None:
                    raise ApiError(409, "UPLOAD_NOT_FOUND", "Não há sessão de upload para retomar.")
                upload.state = "uploading"
                upload.expires_at = now() + _ttl()
                item.status = "uploading"
                item.phase = "upload"
                session.commit()
                return {
                    "item": item_payload(item),
                    "upload_id": upload.id,
                    "next_part": _next_part(session, upload.id),
                    "part_size": upload.part_size,
                }

            if not item.conversion_id:
                raise ApiError(
                    409,
                    "NOT_READY",
                    "Este item ainda não possui uma conversão para repetir.",
                )
            if item.status not in TERMINAL_ITEM_STATUSES:
                raise ApiError(409, "CONVERSION_BUSY", "A conversão ainda está em andamento.")

            from api.service import ConversionService

            result = ConversionService().retry(
                owner_id=owner_id, conversion_id=item.conversion_id
            )
            item.status = "queued"
            item.phase = "conversion"
            session.commit()
            return {"item": item_payload(item), "conversion": result}
        finally:
            session.close()

    # -- events ------------------------------------------------------------
    def events(
        self, *, owner_id: str, batch_id: str, after: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id)
            rows = session.execute(
                select(PublicBatchEventRow)
                .where(
                    PublicBatchEventRow.batch_id == batch.id,
                    PublicBatchEventRow.sequence > int(after or 0),
                )
                .order_by(PublicBatchEventRow.sequence)
                .limit(min(int(limit or 200), 1000))
            ).scalars()
            return [
                {
                    "batch_id": row.batch_id,
                    "sequence": int(row.sequence),
                    "type": row.type,
                    "item_id": row.item_id,
                    "status": row.status,
                    "item_status": row.item_status,
                    "message": row.message,
                    "payload": row.payload_json or {},
                    "at": _iso(row.created_at),
                }
                for row in rows
            ]
        finally:
            session.close()

    def max_sequence(self, *, owner_id: str, batch_id: str) -> int:
        session = get_session_factory()()
        try:
            batch = self._load(session, owner_id, batch_id)
            value = session.execute(
                select(func.max(PublicBatchEventRow.sequence)).where(
                    PublicBatchEventRow.batch_id == batch.id
                )
            ).scalar()
            return int(value or 0)
        finally:
            session.close()

    # -- helpers -----------------------------------------------------------
    def _load_item(self, session: Session, batch_id: str, item_id: str) -> PublicBatchItemRow:
        item = session.get(PublicBatchItemRow, item_id)
        if item is None or item.batch_id != batch_id:
            raise ApiError(404, "NOT_FOUND", "Item não encontrado neste lote.")
        return item


def _ttl():
    from datetime import timedelta

    return timedelta(hours=settings.upload_session_ttl_hours)


def _next_part(session: Session, upload_id: str | None) -> int:
    if not upload_id:
        return 0
    from models.db import PublicUploadPartRow

    highest = session.execute(
        select(func.max(PublicUploadPartRow.part_number)).where(
            PublicUploadPartRow.upload_id == upload_id
        )
    ).scalar()
    return int(highest) + 1 if highest is not None else 0


__all__ = [
    "BatchService",
    "emit_event",
    "item_payload",
    "batch_payload",
    "declared_kind",
    "encode_cursor",
    "decode_cursor",
    "TERMINAL_ITEM_STATUSES",
]
