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


@pytest.mark.asyncio
async def test_replay_after_failed_then_completed_attempt_does_not_crash(monkeypatch):
    # AU2-13: a failed attempt followed by a completed one leaves two assistant
    # rows for the same request; the replay lookup must pick the completed one.
    monkeypatch.setattr(settings, "assistant_enabled", True)
    owner = "assistant_owner_replay"
    collection_id = _seed(owner, "Fonte suficiente para responder.")
    service = AssistantService(FakeClient())  # type: ignore[arg-type]
    conversation = service.create_conversation(owner_id=owner, collection_id=collection_id)
    cid = conversation["conversation_id"]
    kwargs = {"owner_id": owner, "conversation_id": cid, "content": "Fonte?", "request_id": "req_retry"}

    await _collect(AssistantService(FailingClient()), **kwargs)  # type: ignore[arg-type]
    await _collect(AssistantService(FakeClient(["Resposta final [1]."])), **kwargs)  # type: ignore[arg-type]
    events = await _collect(AssistantService(FakeClient()), **kwargs)  # type: ignore[arg-type]

    done = next(item for item in events if item["type"] == "done")
    assert done["replayed"] is True
    assert "Resposta final" in done["content"]


@pytest.mark.asyncio
async def test_client_disconnect_persists_reply_as_cancelled(monkeypatch):
    # AU2-14: closing the stream while it is suspended at a yield must still
    # persist the assistant row (status cancelled) instead of leaving it pending.
    monkeypatch.setattr(settings, "assistant_enabled", True)
    owner = "assistant_owner_disconnect"
    collection_id = _seed(owner, "Fonte suficiente para responder.")
    service = AssistantService(FakeClient(["parte um ", "parte dois [1]."]))  # type: ignore[arg-type]
    conversation = service.create_conversation(owner_id=owner, collection_id=collection_id)
    cid = conversation["conversation_id"]

    stream = service.stream_reply(
        owner_id=owner, conversation_id=cid, content="Fonte?", request_id="req_disconnect"
    )
    async for item in stream:
        if item["type"] == "delta":
            break
    await stream.aclose()

    stored = service.get_conversation(owner, cid)
    assistant_rows = [message for message in stored["messages"] if message["role"] == "assistant"]
    assert assistant_rows
    assert assistant_rows[-1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_transport_error_maps_to_model_unavailable(monkeypatch):
    # AU2-15: any httpx transport failure is a MODEL_UNAVAILABLE, not a crash.
    import httpx

    from assistant import ollama_client as module

    def handler(request):
        raise httpx.RemoteProtocolError("connection dropped by server")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    client = module.OllamaClient(base_url="http://ollama.test", model="qwen3.5:4b")
    with pytest.raises(OllamaError) as info:
        async for _ in client.chat_stream([{"role": "user", "content": "oi"}]):
            pass
    assert info.value.code == "MODEL_UNAVAILABLE"


def test_model_status_requires_exact_tag_match(monkeypatch):
    # AU2-16: configured qwen3.5:4b is not available when only qwen3.5:9b is installed.
    from assistant.ollama_client import OllamaClient

    client = OllamaClient(base_url="http://ollama.test", model="qwen3.5:4b")
    monkeypatch.setattr(client, "server_version", lambda: "0.5.1")
    monkeypatch.setattr(client, "list_models", lambda: [{"name": "qwen3.5:9b"}])
    assert client.model_status()["available"] is False

    monkeypatch.setattr(client, "list_models", lambda: [{"name": "qwen3.5:4b"}])
    assert client.model_status()["available"] is True

    untagged = OllamaClient(base_url="http://ollama.test", model="qwen3.5")
    monkeypatch.setattr(untagged, "server_version", lambda: "0.5.1")
    monkeypatch.setattr(untagged, "list_models", lambda: [{"name": "qwen3.5:latest"}])
    assert untagged.model_status()["available"] is True
