"""Resumable uploads: parts in, one immutable document out.

A document is transferred as independent parts so no single HTTP body has to
carry the whole file. The part size is a transport parameter; there is no
product ceiling on document size or page count.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.batch_service import TERMINAL_ITEM_STATUSES, emit_event, now
from api.deps import ApiError
from api.service import ConversionService, _normalize_options, _safe_filename
from config import settings
from ingestion.registry import probe_input
from models.db import (
    PublicBatchItemRow,
    PublicBatchRow,
    PublicUploadPartRow,
    PublicUploadSessionRow,
    get_session_factory,
)
from storage import get_storage

_EXTENSION_FOR_KIND = {"pdf": ".pdf"}


def _ttl() -> timedelta:
    return timedelta(hours=settings.upload_session_ttl_hours)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def expected_parts(expected_bytes: int, part_size: int) -> int:
    if expected_bytes <= 0:
        return 0
    return (expected_bytes + part_size - 1) // part_size


def _extension_for_probe(probe) -> str:
    if probe.kind != "image":
        return ".pdf"
    fmt = (probe.format or "img").lower()
    return {"jpeg": ".jpg", "jpeg2000": ".jp2", "tiff": ".tif"}.get(fmt, f".{fmt}")


class UploadService:
    """Part-based transport for batch items."""

    def _load(
        self, session: Session, owner_id: str, upload_id: str, *, lock: bool = False
    ) -> PublicUploadSessionRow:
        upload = session.get(PublicUploadSessionRow, upload_id, with_for_update=lock)
        if upload is None:
            raise ApiError(404, "UPLOAD_NOT_FOUND", "Sessão de upload não encontrada.")
        if upload.owner_id != owner_id:
            raise ApiError(403, "FORBIDDEN", "Você não tem permissão para acessar este upload.")
        return upload

    # -- session read ------------------------------------------------------
    def session_payload(self, upload: PublicUploadSessionRow, *, with_parts: bool = False,
                        parts: list[PublicUploadPartRow] | None = None,
                        next_cursor: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "upload_id": upload.id,
            "item_id": upload.item_id,
            "filename": upload.filename,
            "state": upload.state,
            "expected_bytes": int(upload.expected_bytes or 0),
            "received_bytes": int(upload.received_bytes or 0),
            "sha256": upload.sha256,
            "part_size": int(upload.part_size or settings.upload_part_bytes),
            # Filled in by `get_session`/`put_part`; 0 here is a safe default.
            "parts_received": 0,
            "next_part": 0,
            "expires_at": _iso(upload.expires_at),
            "created_at": _iso(upload.created_at),
            "updated_at": _iso(upload.updated_at),
        }
        return payload

    def get_session(
        self,
        *,
        owner_id: str,
        upload_id: str,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        size = min(max(int(limit or 100), 1), 1000)
        session = get_session_factory()()
        try:
            upload = self._load(session, owner_id, upload_id)
            payload = self.session_payload(upload)

            query = (
                select(PublicUploadPartRow)
                .where(PublicUploadPartRow.upload_id == upload.id)
                .order_by(PublicUploadPartRow.part_number)
                .limit(size + 1)
            )
            if cursor:
                try:
                    query = query.where(PublicUploadPartRow.part_number > int(cursor))
                except ValueError:
                    pass
            rows = session.execute(query).scalars().all()
            has_more = len(rows) > size
            rows = rows[:size]

            highest = session.execute(
                select(func.max(PublicUploadPartRow.part_number)).where(
                    PublicUploadPartRow.upload_id == upload.id
                )
            ).scalar()
            count = session.execute(
                select(func.count()).where(PublicUploadPartRow.upload_id == upload.id)
            ).scalar()

            payload["parts"] = [
                {
                    "part_number": int(row.part_number),
                    "size": int(row.size),
                    "sha256": row.sha256,
                }
                for row in rows
            ]
            payload["next_cursor"] = (
                str(rows[-1].part_number) if has_more and rows else None
            )
            payload["parts_received"] = int(count or 0)
            payload["next_part"] = int(highest) + 1 if highest is not None else 0
            return payload
        finally:
            session.close()

    # -- parts -------------------------------------------------------------
    def put_part(
        self,
        *,
        owner_id: str,
        upload_id: str,
        part_number: int,
        stream: BinaryIO,
        declared_sha256: str | None,
        content_length: int | None = None,
    ) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            upload = self._load(session, owner_id, upload_id, lock=True)
            if upload.state in {"completed", "aborted"}:
                raise ApiError(
                    409,
                    "UPLOAD_STATE_CONFLICT",
                    "Esta sessão de upload já foi encerrada.",
                )
            if upload.expires_at and upload.expires_at.replace(tzinfo=UTC) < now():
                raise ApiError(410, "UPLOAD_EXPIRED", "A sessão de upload expirou.")

            part_size = int(upload.part_size or settings.upload_part_bytes)
            max_parts = expected_parts(int(upload.expected_bytes or 0), part_size)
            if part_number < 0 or (max_parts and part_number >= max_parts):
                raise ApiError(
                    400,
                    "UPLOAD_PART_MISMATCH",
                    f"part_number deve estar entre 0 e {max(0, max_parts - 1)}.",
                    details={"max_parts": max_parts},
                )
            if content_length is not None and content_length > part_size:
                raise ApiError(
                    413,
                    "FILE_TOO_LARGE",
                    f"Cada parte pode ter no máximo {part_size} bytes.",
                    details={"part_size": part_size},
                )

            storage = get_storage()
            part_key = f"uploads/{upload.id}/{part_number:05d}.part"
            # The incoming body lands under a unique key first. Writing straight
            # to the canonical key would let a divergent retransmission destroy
            # an already-confirmed part.
            incoming_key = f"uploads/{upload.id}/incoming/{part_number:05d}-{uuid.uuid4().hex}.part"

            existing = session.execute(
                select(PublicUploadPartRow).where(
                    PublicUploadPartRow.upload_id == upload.id,
                    PublicUploadPartRow.part_number == part_number,
                )
            ).scalar_one_or_none()

            stored = storage.put_stream(incoming_key, stream, "application/octet-stream")
            if stored.size_bytes > part_size:
                storage.delete(incoming_key)
                raise ApiError(
                    413,
                    "FILE_TOO_LARGE",
                    f"Cada parte pode ter no máximo {part_size} bytes.",
                    details={"part_size": part_size, "received": stored.size_bytes},
                )

            if declared_sha256 and declared_sha256.lower() != stored.sha256:
                storage.delete(incoming_key)
                raise ApiError(
                    422,
                    "UPLOAD_CHECKSUM_MISMATCH",
                    "O hash da parte não confere com o conteúdo recebido.",
                    details={
                        "part_number": part_number,
                        "expected": declared_sha256.lower(),
                        "received": stored.sha256,
                    },
                )

            if existing is not None:
                # Identical retransmission: no new object, same receipt.
                if existing.sha256 == stored.sha256 and int(existing.size) == stored.size_bytes:
                    storage.delete(incoming_key)
                    return self._receipt(session, upload, existing, replayed=True)
                # Divergent retransmission of a confirmed part is a conflict.
                storage.delete(incoming_key)
                raise ApiError(
                    409,
                    "UPLOAD_PART_MISMATCH",
                    "A parte já foi confirmada com outro conteúdo.",
                    details={"part_number": part_number},
                )

            storage.move(incoming_key, part_key)

            part = PublicUploadPartRow(
                upload_id=upload.id,
                part_number=part_number,
                size=stored.size_bytes,
                sha256=stored.sha256,
                storage_key=part_key,
            )
            session.add(part)
            upload.received_bytes = int(upload.received_bytes or 0) + stored.size_bytes
            upload.state = "uploading"
            upload.updated_at = now()
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                storage.delete(part_key)
                existing = session.execute(
                    select(PublicUploadPartRow).where(
                        PublicUploadPartRow.upload_id == upload.id,
                        PublicUploadPartRow.part_number == part_number,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return self._receipt(session, upload, existing, replayed=True)

            session.refresh(part)
            return self._receipt(session, upload, part, replayed=False)
        finally:
            session.close()

    def _receipt(
        self,
        session: Session,
        upload: PublicUploadSessionRow,
        part: PublicUploadPartRow,
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        count = session.execute(
            select(func.count()).where(PublicUploadPartRow.upload_id == upload.id)
        ).scalar()
        highest = session.execute(
            select(func.max(PublicUploadPartRow.part_number)).where(
                PublicUploadPartRow.upload_id == upload.id
            )
        ).scalar()
        return {
            "upload_id": upload.id,
            "part_number": int(part.part_number),
            "size": int(part.size),
            "sha256": part.sha256,
            "received_bytes": int(upload.received_bytes or 0),
            "parts_received": int(count or 0),
            "next_part": int(highest) + 1 if highest is not None else 0,
            "replayed": replayed,
        }

    # -- complete ----------------------------------------------------------
    def complete(
        self,
        *,
        owner_id: str,
        upload_id: str,
        sha256: str,
        total_parts: int,
    ) -> dict[str, Any]:
        expected_sha = (sha256 or "").strip().lower()
        if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
            raise ApiError(422, "UPLOAD_CHECKSUM_MISMATCH", "sha256 inválido.")

        session = get_session_factory()()
        try:
            upload = self._load(session, owner_id, upload_id, lock=True)
            item = session.get(PublicBatchItemRow, upload.item_id)
            if item is None:
                raise ApiError(404, "NOT_FOUND", "Item do upload não encontrado.")

            # The item is the authority on "already converted": a crash between
            # creating the conversion and marking the session complete must not
            # let a retry create a second one.
            if item.conversion_id:
                return {
                    "upload_id": upload.id,
                    "item_id": item.id,
                    "conversion_id": item.conversion_id,
                    "status": "queued",
                    "message": "Upload já concluído anteriormente.",
                    "replayed": True,
                }

            if upload.state in {"aborted"}:
                raise ApiError(409, "UPLOAD_STATE_CONFLICT", "O upload foi abortado.")

            part_size = int(upload.part_size or settings.upload_part_bytes)
            expected_count = expected_parts(int(upload.expected_bytes or 0), part_size)
            if int(total_parts) != expected_count:
                raise ApiError(
                    409,
                    "UPLOAD_INCOMPLETE",
                    f"Esperadas {expected_count} partes para {upload.expected_bytes} bytes.",
                    details={"expected_parts": expected_count, "received_parts": int(total_parts)},
                )

            parts = session.execute(
                select(PublicUploadPartRow)
                .where(PublicUploadPartRow.upload_id == upload.id)
                .order_by(PublicUploadPartRow.part_number)
            ).scalars().all()
            numbers = [int(part.part_number) for part in parts]
            if numbers != list(range(expected_count)):
                missing = sorted(set(range(expected_count)) - set(numbers))
                raise ApiError(
                    409,
                    "UPLOAD_INCOMPLETE",
                    "Há partes faltando ou fora de ordem.",
                    details={"missing_parts": missing[:50], "received_parts": len(numbers)},
                )

            total_bytes = sum(int(part.size) for part in parts)
            if total_bytes != int(upload.expected_bytes):
                raise ApiError(
                    409,
                    "UPLOAD_INCOMPLETE",
                    f"Recebidos {total_bytes} bytes, esperados {upload.expected_bytes}.",
                    details={"received_bytes": total_bytes, "expected_bytes": upload.expected_bytes},
                )

            storage = get_storage()
            staged_key = f"uploads/{upload.id}/assembled.bin"
            assembled = storage.compose(
                [part.storage_key for part in parts], staged_key, "application/octet-stream"
            )
            if assembled.sha256 != expected_sha:
                storage.delete(staged_key)
                raise ApiError(
                    422,
                    "UPLOAD_CHECKSUM_MISMATCH",
                    "O hash do documento não confere com o conteúdo recebido.",
                    details={"expected": expected_sha, "received": assembled.sha256},
                )

            # Detect the real content: a `.pdf` extension never decides the kind.
            with storage.open_read(staged_key) as probe_stream:
                head = probe_stream.read(1024)
            probe = probe_input(head)
            if probe.kind == "unknown":
                storage.delete(staged_key)
                raise ApiError(
                    400,
                    "INVALID_FILE",
                    "O arquivo enviado não é um PDF nem uma imagem válida.",
                )
            if probe.kind == "image" and not probe.supported:
                storage.delete(staged_key)
                raise ApiError(
                    415,
                    "CODEC_UNAVAILABLE",
                    f"O codec para {probe.format} não está instalado nesta máquina.",
                    details={"format": probe.format},
                )

            normalized = _normalize_options(item.options_json or {}, probe.kind)
            filename = _safe_filename(item.filename, default_ext=probe.format)
            extension = _extension_for_probe(probe)
            final_key = f"documents/{owner_id}/{expected_sha}{extension}"
            if storage.exists(final_key):
                storage.delete(staged_key)
            else:
                storage.move(staged_key, final_key)

            page_count = None
            if probe.kind == "pdf":
                local = storage.local_path(final_key)
                if local is not None:
                    page_count = _count_pages(local)

            result = ConversionService().create_from_stored(
                owner_id=owner_id,
                storage_key=final_key,
                sha256=expected_sha,
                size_bytes=assembled.size_bytes,
                filename=filename,
                mime_type=probe.mime_type or "application/octet-stream",
                normalized=normalized,
                page_count=page_count,
                # A stable key makes "one upload = one conversion" a database
                # constraint, not only an application check.
                idempotency_key=f"upload:{upload.id}",
            )

            upload.state = "completed"
            upload.sha256 = expected_sha
            upload.storage_key = final_key
            upload.received_bytes = total_bytes
            upload.updated_at = now()

            item.conversion_id = result["conversion_id"]
            item.status = "queued"
            item.phase = "conversion"
            item.kind = probe.kind
            item.options_json = normalized
            if page_count:
                item.pages_total = int(page_count)
                item.pages_attempted = 0
                item.pages_completed = 0
                item.pages_failed = 0

            batch = session.get(PublicBatchRow, item.batch_id)
            if batch is not None:
                batch.version = int(batch.version or 1) + 1
                emit_event(
                    session,
                    batch,
                    type_="item",
                    item_id=item.id,
                    item_status="queued",
                    message=f"Upload concluído: {filename}.",
                    payload={
                        "conversion_id": result["conversion_id"],
                        "size_bytes": assembled.size_bytes,
                        "sha256": expected_sha,
                    },
                )
            session.commit()

            return {
                "upload_id": upload.id,
                "item_id": item.id,
                "conversion_id": result["conversion_id"],
                "status": "queued",
                "message": "Upload concluído e conversão enfileirada.",
                "replayed": False,
            }
        finally:
            session.close()

    # -- abort -------------------------------------------------------------
    def abort(self, *, owner_id: str, upload_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            upload = self._load(session, owner_id, upload_id, lock=True)
            if upload.state == "completed":
                raise ApiError(
                    409,
                    "UPLOAD_STATE_CONFLICT",
                    "O upload já foi concluído e não pode ser abortado.",
                )
            storage = get_storage()
            parts = session.execute(
                select(PublicUploadPartRow).where(PublicUploadPartRow.upload_id == upload.id)
            ).scalars().all()
            for part in parts:
                storage.delete(part.storage_key)
                session.delete(part)

            item = session.get(PublicBatchItemRow, upload.item_id)
            if item is not None and item.status not in TERMINAL_ITEM_STATUSES:
                item.status = "upload_failed"
                item.phase = "upload"
                item.error_json = {
                    "code": "UPLOAD_ABORTED",
                    "message": "O envio deste arquivo foi interrompido.",
                }
                if item.upload_id == upload.id:
                    item.upload_id = None

            upload.state = "aborted"
            upload.received_bytes = 0
            upload.updated_at = now()

            batch = session.get(PublicBatchRow, item.batch_id) if item is not None else None
            if batch is not None:
                batch.version = int(batch.version or 1) + 1
                emit_event(
                    session,
                    batch,
                    type_="item",
                    item_id=item.id if item else None,
                    item_status="upload_failed",
                    message="Upload abortado.",
                )
            session.commit()
            return {
                "upload_id": upload.id,
                "state": upload.state,
                "deleted_parts": len(parts),
            }
        finally:
            session.close()


def _count_pages(path) -> int | None:
    """Exact page count from a local file, without loading it into memory."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path), strict=False).pages) or None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["UploadService", "expected_parts"]
