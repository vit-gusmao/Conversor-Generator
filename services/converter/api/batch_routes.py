"""Public batch routes (standalone mode).

In `full` mode the NestJS API owns these endpoints; this module gives the
single-process product the same contract.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from api.batch_service import BatchService
from api.deps import DEVICE_COOKIE, ApiError, current_user
from api.schemas import BatchRetryRequest, CreateBatchRequest, RegisterBatchItemRequest
from api.sse import resolve_stream_user, resume_cursor

router = APIRouter(prefix="/api/v1/batches", tags=["batches"])
service = BatchService()

TERMINAL_BATCH = {"completed", "completed_with_errors", "cancelled"}


@router.post("", status_code=201)
def create_batch(
    body: CreateBatchRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    return service.create_batch(owner_id=user["id"], client_batch_key=body.client_batch_key)


@router.get("/{batch_id}")
def get_batch(batch_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return service.get_batch(owner_id=user["id"], batch_id=batch_id)


@router.post("/{batch_id}/items", status_code=201)
def register_item(
    batch_id: str,
    body: RegisterBatchItemRequest,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    return service.register_item(
        owner_id=user["id"],
        batch_id=batch_id,
        client_item_id=body.client_item_id,
        filename=body.filename,
        size_bytes=body.size_bytes,
        options=body.options,
    )


@router.get("/{batch_id}/items")
def list_items(
    batch_id: str,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
    status: str | None = Query(default=None),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    return service.list_items(
        owner_id=user["id"],
        batch_id=batch_id,
        cursor=cursor,
        limit=limit,
        status=status,
    )


@router.post("/{batch_id}/seal")
def seal_batch(batch_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return service.seal(owner_id=user["id"], batch_id=batch_id)


@router.post("/{batch_id}/cancel")
def cancel_batch(batch_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return service.cancel_batch(owner_id=user["id"], batch_id=batch_id)


@router.post("/{batch_id}/items/{item_id}/cancel")
def cancel_item(
    batch_id: str, item_id: str, user: dict[str, Any] = Depends(current_user)
) -> dict[str, Any]:
    return service.cancel_item(owner_id=user["id"], batch_id=batch_id, item_id=item_id)


@router.post("/{batch_id}/items/{item_id}/retry")
def retry_item(
    batch_id: str,
    item_id: str,
    body: BatchRetryRequest = Body(default=BatchRetryRequest()),  # noqa: B008
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    return service.retry_item(
        owner_id=user["id"], batch_id=batch_id, item_id=item_id, scope=body.scope
    )


@router.get("/{batch_id}/events")
async def batch_events(
    batch_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_device_id: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None),
    last_event_id_query: int | None = Query(default=None, alias="last_event_id"),
):
    auth = authorization or (f"Bearer {access_token}" if access_token else None)
    device_id = x_device_id or request.cookies.get(DEVICE_COOKIE)
    # Resolved with a short-lived session: the stream below can run for minutes.
    user = await asyncio.to_thread(resolve_stream_user, auth, device_id)
    owner_id = user["id"]
    cursor = resume_cursor(last_event_id, last_event_id_query)

    # Verify ownership before opening the stream.
    summary = await asyncio.to_thread(service.get_batch, owner_id=owner_id, batch_id=batch_id)

    async def stream() -> AsyncIterator[bytes]:
        nonlocal cursor, summary
        yield b": connected\n\n"
        deadline = time.monotonic() + 1800
        idle = 0
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                break
            try:
                items = await asyncio.to_thread(
                    service.events, owner_id=owner_id, batch_id=batch_id, after=cursor
                )
                summary = await asyncio.to_thread(
                    service.get_batch, owner_id=owner_id, batch_id=batch_id
                )
            except ApiError:
                yield b'event: error\ndata: {"message":"lote indisponivel"}\n\n'
                break
            for item in items:
                cursor = int(item["sequence"])
                yield (
                    f"id: {cursor}\nevent: {item['type']}\n"
                    f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                ).encode()
            if summary["status"] in TERMINAL_BATCH and not items:
                break
            idle += 1
            if idle % 30 == 0:
                yield b": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router"]
