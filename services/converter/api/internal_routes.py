"""Internal API consumed by the NestJS gateway.

Everything here is guarded by `x-internal-token`.  `/health` is open so a
readiness probe can run before the token is mounted.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response

from api.capabilities import capabilities_payload
from api.deps import ApiError, internal_guard
from api.knowledge_routes import (
    assistant as assistant_service,
)
from api.knowledge_routes import (
    build_chat_response,
    indexer,
    knowledge_repo,
)
from api.schemas import (
    ChatMessageRequest,
    CollectionCreate,
    ConversationCreate,
    EnqueueRequest,
    ExportRequest,
    IndexRequest,
)
from api.service import ConversionService, _split_cell_id
from config import settings
from engine.ocr.engines import ocr_capability
from jobqueue import get_queue
from repositories.conversion_repository import ConversionRepository
from storage import get_storage
from version import ENGINE_VERSION

router = APIRouter(prefix="/internal/v1", tags=["internal"])
repo = ConversionRepository()


@router.get("/health", include_in_schema=True)
def health() -> dict[str, Any]:
    queue = get_queue()
    capability = ocr_capability()
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "ocr_enabled": settings.converter_ocr_enabled,
        "ocr_engine": capability.get("selected"),
        "queue": queue.name,
        "queue_status": queue.health(),
        "capabilities": {
            "ocr": bool(capability.get("available")),
            "tables": True,
            "audit": True,
            "preview": True,
            "download": True,
            "forms": True,
            "docling": _has("docling"),
            "camelot": _has("camelot"),
        },
    }


def _has(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None


@router.post("/jobs/{run_id}/enqueue", status_code=202, dependencies=[Depends(internal_guard)])
def enqueue(run_id: str, body: EnqueueRequest) -> dict[str, Any]:
    if body.run_id != run_id:
        raise ApiError(400, "RUN_MISMATCH", "O run_id do corpo não corresponde ao da rota.")
    repo.create_run(
        run_id=run_id,
        conversion_id=body.conversion_id,
        engine_version=ENGINE_VERSION,
        config=body.options,
    )
    get_queue().submit(
        run_id,
        {
            "conversion_id": body.conversion_id,
            "storage_key": body.storage_key,
            "filename": body.filename,
            "options": body.options,
        },
    )
    return {"run_id": run_id, "status": "queued", "queue": get_queue().name}


@router.post("/jobs/{run_id}/cancel", dependencies=[Depends(internal_guard)])
def cancel(run_id: str) -> dict[str, Any]:
    ok = get_queue().cancel(run_id) and repo.cancel_run(run_id)
    return {"run_id": run_id, "cancelled": bool(ok)}


@router.get("/jobs/{run_id}", dependencies=[Depends(internal_guard)])
def job(run_id: str) -> dict[str, Any]:
    data = repo.get_run(run_id)
    if data is None:
        raise ApiError(404, "NOT_FOUND", "Execução não encontrada.")
    events = repo.events_since(data["conversion_id"], since=0, limit=100)
    data["events"] = events[-20:]
    return data


@router.get("/jobs/{run_id}/result", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/result", dependencies=[Depends(internal_guard)])
def result(run_id: str) -> dict[str, Any]:
    data = repo.result_summary(run_id)
    if data is None:
        raise ApiError(404, "NOT_FOUND", "Resultado não encontrado.")
    return data


@router.get("/jobs/{run_id}/preview", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/preview", dependencies=[Depends(internal_guard)])
def preview(
    run_id: str,
    sheet: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    data = repo.preview(run_id, sheet, offset, limit)
    if data is None:
        raise ApiError(404, "NOT_FOUND", "Prévia não disponível.")
    return data


@router.get("/jobs/{run_id}/download", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/download", dependencies=[Depends(internal_guard)])
def download(run_id: str) -> Response:
    data = repo.xlsx_bytes(run_id)
    if data is None:
        raise ApiError(409, "NOT_READY", "Arquivo ainda não disponível.")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="conversao-{run_id}.xlsx"'},
    )


@router.get("/jobs/{run_id}/audit", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/audit", dependencies=[Depends(internal_guard)])
def audit(run_id: str) -> dict[str, Any]:
    data = repo.audit_summary(run_id)
    if data is None:
        raise ApiError(404, "NOT_FOUND", "Auditoria não encontrada.")
    return data


@router.get("/jobs/{run_id}/audit/events", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/audit/events", dependencies=[Depends(internal_guard)])
def audit_events(
    run_id: str,
    stage: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    page: int | None = None,
    since_sequence: int | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    return repo.audit_events(
        run_id,
        stage=stage,
        event_type=event_type,
        severity=severity,
        page=page,
        since_sequence=since_sequence,
        limit=limit,
    )


@router.get("/jobs/{run_id}/pages", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/pages", dependencies=[Depends(internal_guard)])
def pages(run_id: str) -> dict[str, Any]:
    return repo.pages(run_id)


@router.get("/jobs/{run_id}/tables", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/tables", dependencies=[Depends(internal_guard)])
def tables(run_id: str) -> dict[str, Any]:
    return repo.tables(run_id)


@router.get("/jobs/{run_id}/cells/{cell_id:path}", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/cells/{cell_id:path}", dependencies=[Depends(internal_guard)])
def cell_evidence(run_id: str, cell_id: str) -> dict[str, Any]:
    sheet, cell = _split_cell_id(cell_id)
    data = repo.cells(run_id, sheet=sheet, cell=cell, limit=1)
    if not data["items"]:
        raise ApiError(404, "NOT_FOUND", f"Célula {cell_id} não encontrada.")
    return data["items"][0]


@router.get("/jobs/{run_id}/audit/pages/{page}", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/audit/pages/{page}", dependencies=[Depends(internal_guard)])
def audit_page(run_id: str, page: int) -> dict[str, Any]:
    data = repo.page_audit(run_id, page)
    if data is None:
        raise ApiError(404, "NOT_FOUND", f"Página {page} não encontrada.")
    return data


@router.get("/jobs/{run_id}/audit/cells", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/audit/cells", dependencies=[Depends(internal_guard)])
def audit_cells(
    run_id: str,
    sheet: str | None = None,
    cell: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    return repo.cells(run_id, sheet=sheet, cell=cell, limit=limit)


@router.get("/jobs/{run_id}/audit/issues", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/audit/issues", dependencies=[Depends(internal_guard)])
def audit_issues(run_id: str, severity: str | None = None) -> dict[str, Any]:
    return repo.issues(run_id, severity)


@router.get("/jobs/{run_id}/audit/export", dependencies=[Depends(internal_guard)])
@router.get("/runs/{run_id}/audit/export", dependencies=[Depends(internal_guard)])
def audit_export(run_id: str, format: str = Query(default="json", pattern="^(json|txt)$")) -> Response:
    data, media, filename = repo.export(run_id, format)
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/conversions/{conversion_id}/run", dependencies=[Depends(internal_guard)])
def run_for_conversion(conversion_id: str) -> dict[str, Any]:
    data = repo.get_run_by_conversion(conversion_id)
    if data is None:
        raise ApiError(404, "NOT_FOUND", "Nenhuma execução para esta conversão.")
    return data


# --------------------------------------------------------------------------- #
# Gateway parity: capabilities, artifacts and text by run id
# --------------------------------------------------------------------------- #

_service = ConversionService()


@router.get("/capabilities", dependencies=[Depends(internal_guard)])
def internal_capabilities() -> dict[str, Any]:
    return capabilities_payload()


@router.get("/runs/{run_id}/exports", dependencies=[Depends(internal_guard)])
def internal_exports(run_id: str) -> dict[str, Any]:
    items = repo.exports(run_id)
    run = repo.get_run(run_id) or {}
    conversion_id = run.get("conversion_id")
    for item in items:
        item["download_url"] = (
            f"/api/v1/conversions/{conversion_id}/exports/{item['export_id']}/download"
            if item.get("publication_status") == "approved" and conversion_id
            else None
        )
    return {"items": items, "total": len(items)}


@router.post("/runs/{run_id}/exports", status_code=201, dependencies=[Depends(internal_guard)])
def internal_create_export(run_id: str, body: ExportRequest) -> dict[str, Any]:
    return _service.create_export_for_run(run_id, body.format, body.options)


@router.get("/runs/{run_id}/exports/{export_id}/download", dependencies=[Depends(internal_guard)])
def internal_download_export(run_id: str, export_id: str) -> Response:
    meta = repo.export_by_id(run_id, export_id)
    if meta is None:
        raise ApiError(404, "NOT_FOUND", "Exportação não encontrada.")
    if meta["publication_status"] != "approved":
        raise ApiError(409, "ARTIFACT_QUARANTINED", "Artefato em quarentena.")
    data = get_storage().get_bytes(meta["storage_key"])
    if meta.get("sha256") and __import__("hashlib").sha256(data).hexdigest() != meta["sha256"]:
        raise ApiError(409, "ARTIFACT_HASH_MISMATCH", "O hash registrado não confere.")
    return Response(
        content=data,
        media_type=meta["mime_type"],
        headers={"Content-Disposition": f'attachment; filename="{meta["filename"]}"'},
    )


@router.get("/runs/{run_id}/text", dependencies=[Depends(internal_guard)])
def internal_text(
    run_id: str,
    page: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50_000, ge=1, le=200_000),
) -> dict[str, Any]:
    return _service.text_for_run(run_id, page=page, offset=offset, limit=limit)


# --------------------------------------------------------------------------- #
# Gateway parity: knowledge base and assistant
# --------------------------------------------------------------------------- #


@router.get("/knowledge/collections", dependencies=[Depends(internal_guard)])
def internal_collections(owner: str = Header(alias="x-owner-id")) -> dict[str, Any]:
    items = knowledge_repo.list_collections(owner)
    return {"items": items, "total": len(items)}


@router.post(
    "/knowledge/collections", status_code=201, dependencies=[Depends(internal_guard)]
)
def internal_create_collection(
    body: CollectionCreate, owner: str = Header(alias="x-owner-id")
) -> dict[str, Any]:
    return knowledge_repo.create_collection(
        owner_id=owner, name=body.name, description=body.description, scope=body.scope
    )


@router.delete(
    "/knowledge/collections/{collection_id}",
    status_code=204,
    dependencies=[Depends(internal_guard)],
)
def internal_delete_collection(
    collection_id: str, owner: str = Header(alias="x-owner-id")
) -> Response:
    if not knowledge_repo.delete_collection(owner, collection_id):
        raise ApiError(404, "NOT_FOUND", "Coleção não encontrada.")
    return Response(status_code=204)


@router.post(
    "/knowledge/index-jobs", status_code=202, dependencies=[Depends(internal_guard)]
)
def internal_create_index_job(
    body: IndexRequest, owner: str = Header(alias="x-owner-id")
) -> dict[str, Any]:
    return indexer.submit(owner_id=owner, request=body.model_dump())


@router.get("/knowledge/index-jobs/{job_id}", dependencies=[Depends(internal_guard)])
def internal_get_index_job(job_id: str, owner: str = Header(alias="x-owner-id")) -> dict[str, Any]:
    job = knowledge_repo.get_job(owner, job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "Job não encontrado.")
    return job


@router.get("/models/status", dependencies=[Depends(internal_guard)])
def internal_model_status() -> dict[str, Any]:
    return assistant_service.status()


@router.get("/chat/conversations", dependencies=[Depends(internal_guard)])
def internal_conversations(owner: str = Header(alias="x-owner-id")) -> dict[str, Any]:
    items = assistant_service.list_conversations(owner)
    return {"items": items, "total": len(items)}


@router.post(
    "/chat/conversations", status_code=201, dependencies=[Depends(internal_guard)]
)
def internal_create_conversation(
    body: ConversationCreate, owner: str = Header(alias="x-owner-id")
) -> dict[str, Any]:
    return assistant_service.create_conversation(
        owner_id=owner,
        title=body.title,
        collection_id=body.collection_id,
        document_id=body.document_id,
        run_id=body.run_id,
    )


@router.get("/chat/conversations/{conversation_id}", dependencies=[Depends(internal_guard)])
def internal_get_conversation(
    conversation_id: str, owner: str = Header(alias="x-owner-id")
) -> dict[str, Any]:
    conversation = assistant_service.get_conversation(owner, conversation_id)
    if conversation is None:
        raise ApiError(404, "NOT_FOUND", "Conversa não encontrada.")
    return conversation


@router.delete(
    "/chat/conversations/{conversation_id}",
    status_code=204,
    dependencies=[Depends(internal_guard)],
)
def internal_delete_conversation(
    conversation_id: str, owner: str = Header(alias="x-owner-id")
) -> Response:
    if not assistant_service.delete_conversation(owner, conversation_id):
        raise ApiError(404, "NOT_FOUND", "Conversa não encontrada.")
    return Response(status_code=204)


@router.post(
    "/chat/conversations/{conversation_id}/messages",
    dependencies=[Depends(internal_guard)],
)
def internal_chat(
    conversation_id: str,
    body: ChatMessageRequest,
    owner: str = Header(alias="x-owner-id"),
):
    return build_chat_response(
        owner_id=owner, conversation_id=conversation_id, body=body
    )


__all__ = ["router"]
