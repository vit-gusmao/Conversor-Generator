"""Local Ollama adapter for Qwen3.5-4B.

The client is deliberately small: status, streaming chat and embeddings.  It
never downloads a model as a side effect of opening a page, and it never
claims GPU usage from configuration — only measured responses are reported.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from config import settings


class OllamaError(Exception):
    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        model: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.assistant_base_url).rstrip("/")
        self.timeout = timeout or settings.assistant_timeout_seconds
        self.model = model or settings.assistant_model

    # -- status ------------------------------------------------------------
    def server_version(self) -> str | None:
        try:
            with httpx.Client(timeout=3.0) as client:
                response = client.get(f"{self.base_url}/api/version")
                response.raise_for_status()
                return str(response.json().get("version") or "unknown")
        except Exception:  # noqa: BLE001
            return None

    def list_models(self) -> list[dict[str, Any]]:
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                return list(response.json().get("models") or [])
        except Exception:  # noqa: BLE001
            return []

    def model_status(self, model: str | None = None) -> dict[str, Any]:
        target = model or self.model
        models = self.list_models()
        match = next(
            (
                item
                for item in models
                if item.get("name") == target
                or item.get("model") == target
                or str(item.get("name", "")).split(":")[0] == target.split(":")[0]
            ),
            None,
        )
        return {
            "available": match is not None,
            "server": self.server_version(),
            "model": target,
            "digest": (match or {}).get("digest"),
            "size_bytes": (match or {}).get("size"),
            "modified_at": (match or {}).get("modified_at"),
            "installed_models": [item.get("name") for item in models][:20],
        }

    # -- chat --------------------------------------------------------------
    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": True,
            # Qwen3.5 emits a long `thinking` stream before the answer; with a
            # small output budget the answer content would be empty.
            "think": settings.assistant_thinking,
            "options": {
                "num_ctx": settings.assistant_context_tokens,
                "num_predict": settings.assistant_max_output_tokens,
                **(options or {}),
            },
        }
        timeout = httpx.Timeout(self.timeout, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/api/chat", json=payload
                ) as response:
                    if response.status_code == 404:
                        raise OllamaError(
                            "MODEL_UNAVAILABLE",
                            f"O modelo {payload['model']} não está instalado no Ollama.",
                        )
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="ignore")[:400]
                        raise OllamaError(
                            "MODEL_ERROR",
                            "O servidor local recusou a geração.",
                            detail=body,
                        )
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        yield event
                        if event.get("done"):
                            break
        except httpx.TimeoutException as exc:
            raise OllamaError("MODEL_TIMEOUT", "A geração excedeu o tempo limite.") from exc
        except httpx.ConnectError as exc:
            raise OllamaError(
                "MODEL_UNAVAILABLE",
                "O servidor local do modelo não está acessível.",
                detail=str(exc),
            ) from exc

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        payload = {"model": model or settings.embedding_model, "input": texts}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
            response = await client.post(f"{self.base_url}/api/embed", json=payload)
            if response.status_code == 404:
                raise OllamaError(
                    "EMBEDDING_UNAVAILABLE",
                    f"O modelo de embeddings {payload['model']} não está instalado.",
                )
            response.raise_for_status()
            return list(response.json().get("embeddings") or [])


__all__ = ["OllamaClient", "OllamaError"]
