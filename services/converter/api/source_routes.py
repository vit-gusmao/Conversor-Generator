"""Public source routes (T4) + internal grant administration.

Public: a session owner may list/use roots granted to them and manage their
own sources. Creating a root grant is NOT a public route — it lives under
``/internal/v1`` with the service token (the desktop adds its own grants via
the host credential in T12).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Header

from api.deps import ApiError, current_user, get_db, internal_guard
from sources.service import SourceService

router = APIRouter(prefix="/api/v1", tags=["sources"])
internal_router = APIRouter(prefix="/internal/v1", tags=["internal"])


@router.get("/source-roots")
def list_source_roots(
    user: dict[str, Any] = Depends(current_user), session=Depends(get_db)
) -> dict[str, Any]:
    return {"items": SourceService().list_roots(session, owner_id=user["id"])}


@router.post("/sources", status_code=201)
def create_source(
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    return SourceService().create_source(session, owner_id=user["id"], payload=payload)


@router.get("/sources")
def list_sources(
    user: dict[str, Any] = Depends(current_user), session=Depends(get_db)
) -> dict[str, Any]:
    return {"items": SourceService().list_sources(session, owner_id=user["id"])}


@router.get("/sources/{source_id}")
def get_source(
    source_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    payload = SourceService().get_source(session, source_id=source_id, owner_id=user["id"])
    if payload is None:
        raise ApiError(404, "SOURCE_NOT_FOUND", "Fonte inexistente ou de outro dono.")
    return payload


@router.patch("/sources/{source_id}")
def update_source(
    source_id: str,
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    version = payload.pop("version", None)
    if not isinstance(version, int):
        raise ApiError(400, "INVALID_PAYLOAD", "Edição exige o campo 'version' (controle otimista).")
    updated = SourceService().update_source(
        session,
        source_id=source_id,
        owner_id=user["id"],
        version=version,
        changes=payload,
    )
    if updated is None:
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            "Versão da fonte divergente: releia e reaplique.",
            details={"source_id": source_id},
        )
    return updated


@router.delete("/sources/{source_id}")
def archive_source(
    source_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    archived = SourceService().archive_source(session, source_id=source_id, owner_id=user["id"])
    if not archived:
        raise ApiError(404, "SOURCE_NOT_FOUND", "Fonte inexistente ou de outro dono.")
    return {"archived": True}


@router.post("/sources/{source_id}/validate")
def validate_source(
    source_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    validation = SourceService().validate_source(
        session, source_id=source_id, owner_id=user["id"]
    )
    return {
        "source_id": validation.source_id,
        "ok": validation.ok,
        "available": validation.available,
        "code": validation.code,
        "message": validation.message,
        "sample": [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "mtime_ns": item.mtime_ns,
            }
            for item in validation.sample
        ],
        "estimated_files": validation.estimated_files,
        "estimated_bytes": validation.estimated_bytes,
        "warnings": validation.warnings,
    }


@router.post("/sources/{source_id}/imports", status_code=202)
def start_import(
    source_id: str,
    payload: dict[str, Any] = Body(default={}),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if not idempotency_key or len(idempotency_key) > 128:
        raise ApiError(
            400,
            "INVALID_PAYLOAD",
            "O header Idempotency-Key é obrigatório (1–128 caracteres).",
        )
    from sources.importer import ImportService

    service = ImportService()
    record = service.start(
        owner_id=user["id"],
        source_id=source_id,
        request=payload,
        idempotency_key=idempotency_key,
    )
    service.run_async(record["id"], user["id"])
    return record


@router.get("/imports/{import_id}")
def get_import(
    import_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    from sources.importer import ImportService

    return ImportService().get(import_id, user["id"])


@router.get("/imports/{import_id}/items")
def list_import_items(
    import_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    from sources.importer import ImportService

    items = ImportService().items(import_id, user["id"])
    return {"items": items, "next_cursor": None}


@router.post("/imports/{import_id}/cancel", status_code=202)
def cancel_import(
    import_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    from sources.importer import ImportService

    return ImportService().cancel(import_id, user["id"])


# --------------------------------------------------------------------------
# Internal: root grants (operator token / desktop host credential only)
# --------------------------------------------------------------------------


@internal_router.post("/source-roots", status_code=201)
def grant_root(
    payload: dict[str, Any] = Body(default={}),
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    owner_id = str(payload.get("owner_id") or x_owner_id or "").strip()
    canonical_path = str(payload.get("canonical_path") or "").strip()
    if not owner_id or not canonical_path:
        raise ApiError(400, "INVALID_PAYLOAD", "owner_id e canonical_path são obrigatórios.")
    return SourceService().grant_root(
        session,
        owner_id=owner_id,
        canonical_path=canonical_path,
        granted_by=str(payload.get("granted_by") or "local-admin"),
        runtime_id=str(payload.get("runtime_id") or "local"),
        label=payload.get("label"),
        grant_channel=str(payload.get("grant_channel") or "internal"),
    )


@internal_router.delete("/source-roots/{root_id}")
def revoke_root(
    root_id: str,
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    if not x_owner_id:
        raise ApiError(400, "INVALID_PAYLOAD", "x-owner-id é obrigatório para revogar.")
    revoked = SourceService().revoke_root(session, root_id=root_id, owner_id=x_owner_id)
    if not revoked:
        raise ApiError(404, "ROOT_NOT_FOUND", "Raiz inexistente para este dono.")
    return {"revoked": True}


# --------------------------------------------------------------------------
# Internal: source/schedule management proxy (full-mode gateway passthrough)
# The gateway authenticates the browser session and forwards the owner id.
# --------------------------------------------------------------------------


def _require_owner(x_owner_id: str | None) -> str:
    if not x_owner_id:
        raise ApiError(400, "INVALID_PAYLOAD", "x-owner-id é obrigatório.")
    return x_owner_id


@internal_router.get("/source-roots")
def internal_list_roots(
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    return {"items": SourceService().list_roots(session, owner_id=_require_owner(x_owner_id))}


@internal_router.get("/sources")
def internal_list_sources(
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    return {"items": SourceService().list_sources(session, owner_id=_require_owner(x_owner_id))}


@internal_router.post("/sources", status_code=201)
def internal_create_source(
    payload: dict[str, Any] = Body(default={}),
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    payload.pop("owner_id", None)
    payload.pop("ownerId", None)
    return SourceService().create_source(
        session, owner_id=_require_owner(x_owner_id), payload=payload
    )


@internal_router.get("/sources/{source_id}")
def internal_get_source(
    source_id: str,
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    payload = SourceService().get_source(
        session, source_id=source_id, owner_id=_require_owner(x_owner_id)
    )
    if payload is None:
        raise ApiError(404, "SOURCE_NOT_FOUND", "Fonte inexistente ou de outro dono.")
    return payload


@internal_router.patch("/sources/{source_id}")
@internal_router.put("/sources/{source_id}")
def internal_update_source(
    source_id: str,
    payload: dict[str, Any] = Body(default={}),
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    payload.pop("owner_id", None)
    payload.pop("ownerId", None)
    version = payload.pop("version", None)
    if not isinstance(version, int):
        raise ApiError(400, "INVALID_PAYLOAD", "Edição exige o campo 'version' (controle otimista).")
    updated = SourceService().update_source(
        session,
        source_id=source_id,
        owner_id=_require_owner(x_owner_id),
        version=version,
        changes=payload,
    )
    if updated is None:
        raise ApiError(
            409,
            "VERSION_CONFLICT",
            "Versão da fonte divergente: releia e reaplique.",
            details={"source_id": source_id},
        )
    return updated


@internal_router.delete("/sources/{source_id}")
def internal_archive_source(
    source_id: str,
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    archived = SourceService().archive_source(
        session, source_id=source_id, owner_id=_require_owner(x_owner_id)
    )
    if not archived:
        raise ApiError(404, "SOURCE_NOT_FOUND", "Fonte inexistente ou de outro dono.")
    return {"archived": True}


@internal_router.post("/sources/{source_id}/validate")
def internal_validate_source(
    source_id: str,
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    validation = SourceService().validate_source(
        session, source_id=source_id, owner_id=_require_owner(x_owner_id)
    )
    return {
        "source_id": validation.source_id,
        "ok": validation.ok,
        "available": validation.available,
        "code": validation.code,
        "message": validation.message,
        "sample": [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "mtime_ns": item.mtime_ns,
            }
            for item in validation.sample
        ],
        "estimated_files": validation.estimated_files,
        "estimated_bytes": validation.estimated_bytes,
        "warnings": validation.warnings,
    }


@internal_router.post("/sources/{source_id}/imports", status_code=202)
def internal_start_import(
    source_id: str,
    payload: dict[str, Any] = Body(default={}),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_owner_id: str | None = Header(default=None),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    owner_id = _require_owner(x_owner_id)
    if not idempotency_key or len(idempotency_key) > 128:
        raise ApiError(400, "INVALID_PAYLOAD", "O header Idempotency-Key é obrigatório.")
    from sources.importer import ImportService

    service = ImportService()
    record = service.start(
        owner_id=owner_id,
        source_id=source_id,
        request=payload,
        idempotency_key=idempotency_key,
    )
    service.run_async(record["id"], owner_id)
    return record


@internal_router.get("/imports/{import_id}")
def internal_get_import(
    import_id: str,
    x_owner_id: str | None = Header(default=None),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    from sources.importer import ImportService

    return ImportService().get(import_id, _require_owner(x_owner_id))


@internal_router.get("/imports/{import_id}/items")
def internal_list_import_items(
    import_id: str,
    x_owner_id: str | None = Header(default=None),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    from sources.importer import ImportService

    items = ImportService().items(import_id, _require_owner(x_owner_id))
    return {"items": items, "next_cursor": None}


@internal_router.post("/imports/{import_id}/cancel", status_code=202)
def internal_cancel_import(
    import_id: str,
    x_owner_id: str | None = Header(default=None),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    from sources.importer import ImportService

    return ImportService().cancel(import_id, _require_owner(x_owner_id))


__all__ = ["router", "internal_router"]
