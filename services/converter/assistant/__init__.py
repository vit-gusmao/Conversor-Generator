"""Local assistant (Qwen3.5-4B via Ollama)."""

from assistant.ollama_client import OllamaClient, OllamaError
from assistant.service import SYSTEM_PROMPT, AssistantService

__all__ = ["OllamaClient", "OllamaError", "AssistantService", "SYSTEM_PROMPT"]
