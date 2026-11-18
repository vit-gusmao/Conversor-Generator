"""Public resumable-upload routes (standalone mode)."""

from __future__ import annotations

import tempfile
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request

from api.deps import current_user
from api.schemas import CompleteUploadRequest
from api.upload_service import UploadService

router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])
service = UploadService()

#: Spool threshold: parts up to this stay in RAM, larger ones roll to disk.
SPOOL_BYTES = 1024 * 1024


@router.put("/{upload_id}/parts/{part_number}")
async def put_part(
    upload_id: str,
    part_number: int,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    x_part_sha256: str | None = Header(default=None),
) -> dict[str, Any]:
    """Stores one part. The body is streamed, never buffered whole."""
    content_length = request.headers.get("content-length")
    declared = int(content_length) if content_length and content_length.isdigit() else None

    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
    try:
        async for chunk in request.stream():
            if chunk:
                spool.write(chunk)
        spool.seek(0)
        return service.put_part(
            owner_id=user["id"],
            upload_id=upload_id,
            part_number=part_number,
            stream=spool,
            declared_sha256=x_part_sha256,
            content_length=declared,
        )
    finally:
        spool.close()


@router.get("/{upload_id}")
def get_upload(
    upload_id: str,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    return service.get_session(
        owner_id=user["id"], upload_id=upload_id, cursor=cursor, limit=limit
    )


@router.get("/{upload_id}/parts")
def list_parts(
    upload_id: str,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    return service.get_session(
        owner_id=user["id"], upload_id=upload_id, cursor=cursor, limit=limit
    )


@router.post("/{upload_id}/complete", status_code=202)
def complete_upload(
    upload_id: str,
    body: CompleteUploadRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    return service.complete(
        owner_id=user["id"],
        upload_id=upload_id,
        sha256=body.sha256,
        total_parts=body.total_parts,
    )


@router.delete("/{upload_id}")
def abort_upload(upload_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return service.abort(owner_id=user["id"], upload_id=upload_id)


__all__ = ["router"]
