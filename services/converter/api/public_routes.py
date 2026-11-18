"""Public API (`/api/v1`) — available in standalone mode.

In `full` mode the NestJS gateway serves this contract and forwards audit reads
to the internal router.  Keeping both implementations behind the same service
guarantees the contract cannot drift.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from api.deps import (
    DEVICE_COOKIE,
    ApiError,
    create_token,
    current_user,
    get_db,
    resolve_user,
    session_cookie_kwargs,
    user_dict,
)
from api.schemas import ExportRequest
from api.service import ConversionService
from config import settings
from engine.ocr.engines import ocr_capability
from jobqueue import get_queue
from models.db import PublicUserRow
from storage import get_storage
from version import ENGINE_VERSION

router = APIRouter(prefix="/api/v1", tags=["public"])
service = ConversionService()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def _session_payload(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": create_token(user["id"]),
        "token_type": "Bearer",
        "expires_in": settings.jwt_expires_in_seconds,
        "user": user,
    }


@router.post("/auth/anonymous", status_code=201)
def anonymous(
    request: Request,
    session=Depends(get_db),
    x_device_id: str | None = Header(default=None),
):
    """Local, account-free session.

    The server keeps one owner per browser (device cookie / header), so files
    and conversations stay private without registration or login.
    """
    user, generated = resolve_user(session, None, x_device_id)
    if generated:
        request.state.generated_device_id = generated
    user_row = session.get(PublicUserRow, user["id"])
    user = user_dict(user_row) if user_row else user
    response = JSONResponse(content=_session_payload(user), status_code=201)
    device_id = x_device_id or generated
    if device_id:
        response.headers["X-Device-Id"] = device_id
        response.headers["Access-Control-Expose-Headers"] = "X-Device-Id"
        response.set_cookie(
            DEVICE_COOKIE,
            device_id,
            **session_cookie_kwargs(),
        )
    return response


@router.get("/auth/me")
def me(user: dict[str, Any] = Depends(current_user)):
    return user


# --------------------------------------------------------------------------- #
# Conversions
# --------------------------------------------------------------------------- #


@router.post("/conversions", status_code=202)
def create_conversion(
    request: Request,
    file: UploadFile = File(...),
    options: str | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict[str, Any] = Depends(current_user),
):
    data = file.file.read(settings.max_bytes() + 1)
    parsed: dict[str, Any] | None = None
    if options:
        try:
            parsed = json.loads(options)
        except ValueError as exc:
            raise ApiError(400, "INVALID_OPTION", "O campo options não é um JSON válido.") from exc
        if not isinstance(parsed, dict):
            raise ApiError(400, "INVALID_OPTION", "options deve ser um objeto JSON.")
    result = service.create(
        data=data,
        filename=file.filename or "documento",
        owner_id=user["id"],
        idempotency_key=idempotency_key,
        options=parsed,
    )
    return _accepted(request, result)


@router.get("/conversions")
def list_conversions(
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict[str, Any] = Depends(current_user),
):
    return service.list(owner_id=user["id"], status=status, q=q, page=page, page_size=page_size)


@router.get("/conversions/{conversion_id}")
def get_conversion(
    conversion_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
):
    return _with_device(request, service.get(owner_id=user["id"], conversion_id=conversion_id))


@router.post("/conversions/{conversion_id}/retry", status_code=202)
def retry_conversion(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    return service.retry(owner_id=user["id"], conversion_id=conversion_id)


@router.post("/conversions/{conversion_id}/cancel", status_code=202)
def cancel_conversion(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    return service.cancel(owner_id=user["id"], conversion_id=conversion_id)


@router.delete("/conversions/{conversion_id}", status_code=204)
def delete_conversion(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    service.delete(owner_id=user["id"], conversion_id=conversion_id)
    return Response(status_code=204)


@router.get("/conversions/{conversion_id}/result")
def conversion_result(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    return service.result(owner_id=user["id"], conversion_id=conversion_id)


@router.get("/conversions/{conversion_id}/result-summary")
def conversion_result_overview(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    """Compact result payload (size independent of the cell count)."""
    return service.result_overview(owner_id=user["id"], conversion_id=conversion_id)


@router.get("/conversions/{conversion_id}/sheets")
def conversion_sheets(
    conversion_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    q: str | None = Query(default=None, max_length=120),
    user: dict[str, Any] = Depends(current_user),
):
    return service.sheets(
        owner_id=user["id"], conversion_id=conversion_id, cursor=cursor, limit=limit, q=q
    )


@router.get("/conversions/{conversion_id}/sheets/{sheet_id}/pages")
def conversion_sheet_pages(
    conversion_id: str,
    sheet_id: str,
    cursor: int | None = Query(default=None, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    user: dict[str, Any] = Depends(current_user),
):
    return service.sheet_pages(
        owner_id=user["id"],
        conversion_id=conversion_id,
        sheet_id=sheet_id,
        cursor=cursor,
        limit=limit,
    )


@router.get("/conversions/{conversion_id}/preview")
def conversion_preview(
    conversion_id: str,
    sheet: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    user: dict[str, Any] = Depends(current_user),
):
    return service.preview(
        owner_id=user["id"], conversion_id=conversion_id, sheet=sheet, offset=offset, limit=limit
    )


@router.get("/conversions/{conversion_id}/pages")
def conversion_pages(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    """Páginas processadas, com classificação, origem do texto e nº de regiões."""
    return service.pages(owner_id=user["id"], conversion_id=conversion_id)


@router.get("/conversions/{conversion_id}/tables")
def conversion_tables(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    """Tabelas reconstruídas (cabeçalhos, linhas, estratégia e mesclagens)."""
    return service.tables(owner_id=user["id"], conversion_id=conversion_id)


@router.get("/conversions/{conversion_id}/cells/{cell_id}")
def conversion_cell(conversion_id: str, cell_id: str, user: dict[str, Any] = Depends(current_user)):
    """Rastreabilidade de uma célula, endereçada como 'Aba!C2' ou 'C2'."""
    return service.cell_evidence(owner_id=user["id"], conversion_id=conversion_id, cell_id=cell_id)


@router.get("/conversions/{conversion_id}/download")
def conversion_download(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    """Streams the workbook from storage instead of loading it into memory."""
    stream, filename, media = service.download_stream(
        owner_id=user["id"], conversion_id=conversion_id
    )
    return StreamingResponse(
        iter(lambda: stream.read(1024 * 1024), b""),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# --------------------------------------------------------------------------- #
# Capabilities and extra artifacts (TXT/CSV)
# --------------------------------------------------------------------------- #


@router.get("/capabilities")
def capabilities() -> dict[str, Any]:
    from api.capabilities import capabilities_payload

    return capabilities_payload()


@router.get("/conversions/{conversion_id}/exports")
def conversion_exports(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    return service.exports(owner_id=user["id"], conversion_id=conversion_id)


@router.post("/conversions/{conversion_id}/exports", status_code=201)
def create_conversion_export(
    conversion_id: str,
    body: ExportRequest,
    user: dict[str, Any] = Depends(current_user),
):
    return service.create_export(
        owner_id=user["id"],
        conversion_id=conversion_id,
        fmt=body.format,
        options=body.options,
    )


@router.get("/conversions/{conversion_id}/exports/{export_id}/download")
def download_conversion_export(
    conversion_id: str, export_id: str, user: dict[str, Any] = Depends(current_user)
):
    data, filename, media = service.download_export(
        owner_id=user["id"], conversion_id=conversion_id, export_id=export_id
    )
    return Response(
        content=data,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/conversions/{conversion_id}/text")
def conversion_text(
    conversion_id: str,
    page: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50_000, ge=1, le=200_000),
    user: dict[str, Any] = Depends(current_user),
):
    return service.text_page(
        owner_id=user["id"],
        conversion_id=conversion_id,
        page=page,
        offset=offset,
        limit=limit,
    )


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #

TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}


@router.get("/conversions/{conversion_id}/events")
async def conversion_events(
    conversion_id: str,
    request: Request,
    session=Depends(get_db),
    authorization: str | None = Header(default=None),
    x_device_id: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None),
    last_event_id_query: int | None = Query(default=None, alias="last_event_id"),
):
    auth = authorization or (f"Bearer {access_token}" if access_token else None)
    user, _generated = resolve_user(session, auth, x_device_id)
    owner_id = user["id"]

    cursor = 0
    for candidate in (last_event_id, last_event_id_query):
        if candidate in (None, ""):
            continue
        try:
            cursor = max(cursor, int(candidate))
        except (TypeError, ValueError):
            continue

    async def stream() -> AsyncIterator[bytes]:
        nonlocal cursor
        yield b": connected\n\n"
        deadline = time.monotonic() + 1800
        idle = 0
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                break
            try:
                items = await asyncio.to_thread(
                    service.events, owner_id=owner_id, conversion_id=conversion_id, since=cursor
                )
                status = await asyncio.to_thread(
                    service.status_of, owner_id=owner_id, conversion_id=conversion_id
                )
            except ApiError:
                yield b"event: error\ndata: {\"message\":\"conversao indisponivel\"}\n\n"
                break
            for item in items:
                cursor = int(item["sequence"])
                yield (
                    f"id: {cursor}\nevent: {item['type']}\n"
                    f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                ).encode()
            if status in TERMINAL and not items:
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


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


@router.get("/conversions/{conversion_id}/audit")
def audit_summary(conversion_id: str, user: dict[str, Any] = Depends(current_user)):
    return service.audit(owner_id=user["id"], conversion_id=conversion_id)


@router.get("/conversions/{conversion_id}/audit/events")
def audit_events(
    conversion_id: str,
    stage: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    page: int | None = None,
    since_sequence: int | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    user: dict[str, Any] = Depends(current_user),
):
    return service.audit_events(
        owner_id=user["id"],
        conversion_id=conversion_id,
        stage=stage,
        event_type=event_type,
        severity=severity,
        page=page,
        since_sequence=since_sequence,
        limit=limit,
    )


@router.get("/conversions/{conversion_id}/audit/pages/{page}")
def audit_page(conversion_id: str, page: int, user: dict[str, Any] = Depends(current_user)):
    return service.audit_page(owner_id=user["id"], conversion_id=conversion_id, page=page)


@router.get("/conversions/{conversion_id}/audit/cells")
def audit_cells(
    conversion_id: str,
    sheet: str | None = None,
    cell: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    user: dict[str, Any] = Depends(current_user),
):
    return service.audit_cells(
        owner_id=user["id"], conversion_id=conversion_id, sheet=sheet, cell=cell, limit=limit
    )


@router.get("/conversions/{conversion_id}/audit/issues")
def audit_issues(
    conversion_id: str,
    severity: str | None = None,
    user: dict[str, Any] = Depends(current_user),
):
    return service.audit_issues(owner_id=user["id"], conversion_id=conversion_id, severity=severity)


@router.get("/conversions/{conversion_id}/audit/export")
def audit_export(
    conversion_id: str,
    format: str = Query(default="json", pattern="^(json|txt)$"),
    user: dict[str, Any] = Depends(current_user),
):
    data, media, filename = service.audit_export(
        owner_id=user["id"], conversion_id=conversion_id, fmt=format
    )
    return Response(
        content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #


@router.get("/settings")
def platform_settings() -> dict[str, Any]:
    capability = ocr_capability()
    return {
        "auth_mode": settings.auth_mode,
        "max_upload_bytes": settings.max_bytes(),
        "max_pages": settings.max_pages(),
        "runtime_mode": settings.runtime_mode,
        "ocr_enabled": settings.converter_ocr_enabled,
        "ocr_engine": capability.get("selected"),
        "storage_backend": settings.storage_backend,
        "retention": {
            "pdf_days": settings.retention_pdf_days,
            "xlsx_days": settings.retention_xlsx_days,
            "audit_days": settings.retention_audit_days,
        },
        "engine_version": ENGINE_VERSION,
        "capabilities": {
            "ocr": bool(capability.get("available")),
            "tables": True,
            "audit": True,
            "preview": True,
            "download": True,
            "forms": True,
            "sse": True,
            "push_events": True,
            "batch": settings.batch_upload_enabled,
            "resumable_upload": settings.batch_upload_enabled,
            "full_document": settings.large_document_pipeline_enabled,
            "result_compact": True,
        },
        "upload": {
            "resumable": settings.batch_upload_enabled,
            "accepts_multiple": settings.batch_upload_enabled,
            "part_size_bytes": settings.upload_part_bytes,
            "max_part_bytes": settings.upload_part_bytes,
            "max_file_bytes": None,
            "max_upload_bytes": settings.max_bytes(),
            "max_files_per_batch": settings.max_files_per_batch,
        },
    }


@router.get("/health")
def health() -> dict[str, Any]:
    from sqlalchemy import text

    from models.db import get_engine

    services: dict[str, str] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        services["db"] = "up"
    except Exception:  # noqa: BLE001
        services["db"] = "down"
    try:
        services["queue"] = get_queue().health()
    except Exception:  # noqa: BLE001
        services["queue"] = "down"
    try:
        storage = get_storage()
        storage.exists("__healthcheck__")
        services["storage"] = "up"
    except Exception:  # noqa: BLE001
        services["storage"] = "down"
    services["converter"] = "up"
    overall = "ok" if all(v == "up" for v in services.values()) else "degraded"
    return {"status": overall, "services": services, "version": ENGINE_VERSION}


@router.get("/health/ready")
def readiness() -> dict[str, Any]:
    """Whether the host can accept new conversions right now.

    Deliberately free of hosts, paths and versions: the SPA uses it to explain
    "server offline" instead of guessing from a failed upload, and the operator
    keeps the detailed diagnostics in the local logs.
    """
    accepting = True
    try:
        from sqlalchemy import text

        from models.db import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        accepting = False
    try:
        if get_queue().health() != "up":
            accepting = False
    except Exception:  # noqa: BLE001
        accepting = False
    try:
        get_storage().exists("__healthcheck__")
    except Exception:  # noqa: BLE001
        accepting = False

    pending = 0
    try:
        backlog = getattr(get_queue(), "backlog", None)
        if callable(backlog):
            pending = int(backlog().get("queued", 0))
    except Exception:  # noqa: BLE001
        pending = 0
    try:
        ocr_available = bool(ocr_capability().get("available"))
    except Exception:  # noqa: BLE001
        ocr_available = False

    return {
        "status": "ok" if accepting else "degraded",
        "accepting_uploads": accepting,
        "ocr_available": ocr_available,
        "queue_pending": pending,
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _accepted(request: Request, payload: dict[str, Any]) -> JSONResponse:
    response = JSONResponse(content=payload, status_code=202)
    _apply_device_header(request, response)
    return response


def _with_device(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def _apply_device_header(request: Request, response: Response) -> None:
    generated = getattr(request.state, "generated_device_id", None)
    if generated:
        response.headers["X-Device-Id"] = generated
        response.headers["Access-Control-Expose-Headers"] = "X-Device-Id"


__all__ = ["router"]
