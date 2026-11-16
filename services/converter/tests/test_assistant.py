"""Assistant: streaming, citations, abstention and disabled mode."""

from __future__ import annotations

import pytest

from assistant.ollama_client import OllamaError
from assistant.service import AssistantService
from config import settings
from exports.text_plan import TextArtifactPlan, TextPage, TextSegment
from knowledge.chunker import chunk_text_plan
from knowledge.repository import KnowledgeRepository
from models.db import init_db


@pytest.fixture(autouse=True)
def _db():
    init_db()


class FakeClient:
    def __init__(self, chunks: list[str] | None = None):
        self.chunks = chunks if chunks is not None else ["Resposta baseada na fonte [1]."]
        self.calls: list[list[dict[str, str]]] = []

    def model_status(self, model=None):
        return {
            "available": True,
            "server": "test",
            "model": "qwen3.5:4b",
            "digest": "sha256:test",
            "installed_models": ["qwen3.5:4b"],
        }

    async def chat_stream(self, messages, *, model=None, options=None):
        self.calls.append(messages)
        for piece in self.chunks:
            yield {"message": {"content": piece}, "done": False}
        yield {"message": {"content": ""}, "done": True}


class FailingClient(FakeClient):
    async def chat_stream(self, messages, *, model=None, options=None):
        raise OllamaError("MODEL_TIMEOUT", "tempo esgotado")
        yield  # pragma: no cover


def _seed(owner_id: str, text: str) -> str:
    repo = KnowledgeRepository()
    collection = repo.create_collection(owner_id=owner_id, name="Docs")
    plan = TextArtifactPlan(
        pages=[TextPage(page_number=1, status="ok", segments=[TextSegment(text=text, page_number=1, kind="text")])]
    )
    drafts = chunk_text_plan(
        plan,
        owner_id=owner_id,
        collection_id=collection["collection_id"],
        document_id="doc_1",
        run_id="run_1",
    )
    repo.replace_document_chunks(
        owner_id=owner_id,
        collection_id=collection["collection_id"],
        document_id="doc_1",
        drafts=drafts,
    )
    return collection["collection_id"]


async def _collect(service, **kwargs):
    return [item async for item in service.stream_reply(**kwargs)]


@pytest.mark.asyncio
async def test_answer_has_valid_citation(monkeypatch):
    monkeypatch.setattr(settings, "assistant_enabled", True)
    owner = "assistant_owner_1"
    collection_id = _seed(owner, "O valor total do contrato é 42 reais.")
    client = FakeClient()
    service = AssistantService(client)  # type: ignore[arg-type]
    conversation = service.create_conversation(owner_id=owner, collection_id=collection_id)
    events = await _collect(
        service,
        owner_id=owner,
        conversation_id=conversation["conversation_id"],
        content="Qual é o valor total?",
        request_id="req_1",
    )
    kinds = [item["type"] for item in events]
    assert "citation" in kinds and "done" in kinds
    done = next(item for item in events if item["type"] == "done")
    assert "42 reais" in done["content"] or "Resposta" in done["content"]
    assert done["citations"] and done["citations"][0]["chunk_id"]

    stored = service.get_conversation(owner, conversation["conversation_id"])
    assert stored is not None
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]
    assert stored["messages"][1]["citations"]


@pytest.mark.asyncio
async def test_abstains_when_no_source(monkeypatch):
    monkeypatch.setattr(settings, "assistant_enabled", True)
    owner = "assistant_owner_empty"
    service = AssistantService(FakeClient())  # type: ignore[arg-type]
    conversation = service.create_conversation(owner_id=owner)
    events = await _collect(
        service,
        owner_id=owner,
        conversation_id=conversation["conversation_id"],
        content="Pergunta sem documento",
        request_id="req_empty",
    )
    done = next(item for item in events if item["type"] == "done")
    assert done["abstained"] is True
    assert done["citations"] == []


@pytest.mark.asyncio
async def test_disabled_assistant_returns_explicit_error(monkeypatch):
    monkeypatch.setattr(settings, "assistant_enabled", False)
    owner = "assistant_owner_off"
    service = AssistantService(FakeClient())  # type: ignore[arg-type]
    conversation = service.create_conversation(owner_id=owner)
    events = await _collect(
        service,
        owner_id=owner,
        conversation_id=conversation["conversation_id"],
        content="Olá",
        request_id="req_off",
    )
    error = next(item for item in events if item["type"] == "error")
    assert error["code"] == "MODEL_DISABLED"


@pytest.mark.asyncio
async def test_generation_failure_is_recoverable(monkeypatch):
    monkeypatch.setattr(settings, "assistant_enabled", True)
    owner = "assistant_owner_fail"
    collection_id = _seed(owner, "Fonte suficiente para responder.")
    service = AssistantService(FailingClient())  # type: ignore[arg-type]
    conversation = service.create_conversation(owner_id=owner, collection_id=collection_id)
    events = await _collect(
        service,
        owner_id=owner,
        conversation_id=conversation["conversation_id"],
        content="Fonte?",
        request_id="req_fail",
    )
    error = next(item for item in events if item["type"] == "error")
    assert error["code"] == "MODEL_TIMEOUT"
