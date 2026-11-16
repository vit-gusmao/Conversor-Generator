"""Knowledge base, assistant and model-status routes.

All routes require an authenticated session and apply ownership before any
query.  The chat stream is SSE with typed events (`status|delta|citation|done|error`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse

from api.deps import ApiError, current_user
from api.schemas import (
    ChatMessageRequest,
    CollectionCreate,
    ConversationCreate,
    IndexRequest,
)
from assistant.service import AssistantService
from config import settings
from knowledge.indexer import IndexingError, KnowledgeIndexer
from knowledge.repository import KnowledgeRepository
from knowledge.retrieval import retrieve

router = APIRouter(prefix="/api/v1", tags=["knowledge"])
knowledge_repo = KnowledgeRepository()
indexer = KnowledgeIndexer(knowledge_repo)
assistant = AssistantService()
_chat_semaphore = asyncio.Semaphore(max(1, settings.assistant_max_concurrent))


# --------------------------------------------------------------------------- #
# Collections and indexing
# --------------------------------------------------------------------------- #


@router.get("/knowledge/collections")
def list_collections(user: dict[str, Any] = Depends(current_user)):
    items = knowledge_repo.list_collections(user["id"])
    return {"items": items, "total": len(items)}


@router.post("/knowledge/collections", status_code=201)
def create_collection(body: CollectionCreate, user: dict[str, Any] = Depends(current_user)):
    return knowledge_repo.create_collection(
        owner_id=user["id"],
        name=body.name,
        description=body.description,
        scope=body.scope,
    )


@router.get("/knowledge/collections/{collection_id}")
def get_collection(collection_id: str, user: dict[str, Any] = Depends(current_user)):
    collection = knowledge_repo.get_collection(user["id"], collection_id)
    if collection is None:
        raise ApiError(404, "NOT_FOUND", "Coleção não encontrada.")
    return collection


@router.delete("/knowledge/collections/{collection_id}", status_code=204)
def delete_collection(collection_id: str, user: dict[str, Any] = Depends(current_user)):
    if not knowledge_repo.delete_collection(user["id"], collection_id):
        raise ApiError(404, "NOT_FOUND", "Coleção não encontrada.")
    return Response(status_code=204)


@router.post("/knowledge/index-jobs", status_code=202)
def create_index_job(body: IndexRequest, user: dict[str, Any] = Depends(current_user)):
    try:
        job = indexer.submit(owner_id=user["id"], request=body.model_dump())
    except IndexingError as exc:
        raise ApiError(400, exc.code, exc.message) from exc
    except LookupError as exc:
        raise ApiError(404, "NOT_FOUND", str(exc)) from exc
    return job


@router.get("/knowledge/index-jobs/{job_id}")
def get_index_job(job_id: str, user: dict[str, Any] = Depends(current_user)):
    job = knowledge_repo.get_job(user["id"], job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "Job de indexação não encontrado.")
    return job


@router.post("/knowledge/search")
def search_knowledge(
    body: dict[str, Any],
    user: dict[str, Any] = Depends(current_user),
):
    query = str(body.get("query") or "").strip()
    if not query:
        raise ApiError(400, "INVALID_QUERY", "Informe o texto da consulta.")
    collection_id = body.get("collection_id")
    top_k = int(body.get("top_k") or 8)
    citations = retrieve(
        owner_id=user["id"],
        query=query,
        collection_ids=[collection_id] if collection_id else None,
        top_k=max(1, min(top_k, 20)),
        repository=knowledge_repo,
    )
    return {
        "query": query,
        "items": [citation.to_dict(index) for index, citation in enumerate(citations, start=1)],
        "total": len(citations),
    }


# --------------------------------------------------------------------------- #
# Assistant
# --------------------------------------------------------------------------- #


@router.get("/models/status")
def model_status() -> dict[str, Any]:
    return assistant.status()


@router.get("/chat/conversations")
def list_conversations(user: dict[str, Any] = Depends(current_user)):
    items = assistant.list_conversations(user["id"])
    return {"items": items, "total": len(items)}


@router.post("/chat/conversations", status_code=201)
def create_conversation(body: ConversationCreate, user: dict[str, Any] = Depends(current_user)):
    return assistant.create_conversation(
        owner_id=user["id"],
        title=body.title,
        collection_id=body.collection_id,
        document_id=body.document_id,
        run_id=body.run_id,
    )


@router.get("/chat/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user: dict[str, Any] = Depends(current_user)):
    conversation = assistant.get_conversation(user["id"], conversation_id)
    if conversation is None:
        raise ApiError(404, "NOT_FOUND", "Conversa não encontrada.")
    return conversation


@router.delete("/chat/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, user: dict[str, Any] = Depends(current_user)):
    if not assistant.delete_conversation(user["id"], conversation_id):
        raise ApiError(404, "NOT_FOUND", "Conversa não encontrada.")
    return Response(status_code=204)


@router.post("/chat/conversations/{conversation_id}/messages")
async def chat_stream(
    conversation_id: str,
    body: ChatMessageRequest,
    user: dict[str, Any] = Depends(current_user),
):
    return build_chat_response(
        owner_id=user["id"], conversation_id=conversation_id, body=body
    )


def build_chat_response(
    *,
    owner_id: str,
    conversation_id: str,
    body: ChatMessageRequest,
) -> StreamingResponse:
    async def events() -> AsyncIterator[bytes]:
        if _chat_semaphore.locked():
            yield _sse(
                {
                    "type": "status",
                    "state": "queued",
                    "message": "Aguardando recurso do modelo local.",
                }
            )
        async with _chat_semaphore:
            try:
                async for item in assistant.stream_reply(
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    content=body.content,
                    request_id=body.request_id,
                    collection_id=body.collection_id,
                    top_k=body.top_k,
                ):
                    yield _sse(item)
            except asyncio.CancelledError:
                raise

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(payload: dict[str, Any]) -> bytes:
    kind = payload.get("type") or "message"
    return (
        f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
    ).encode()


__all__ = ["router"]
