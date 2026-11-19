"""Ollama model tag comparison shared by the assistant, OCR and diagnostics.

Ollama reports every model with a tag (``name:tag``) and treats a bare name as
``name:latest``.  Matching must therefore compare full tags: ``qwen3.5:4b`` is
not installed just because ``qwen3.5:9b`` is.
"""

from __future__ import annotations


def normalize_model_tag(name: str | None) -> str:
    cleaned = (name or "").strip().lower()
    if not cleaned:
        return ""
    last_segment = cleaned.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        cleaned = f"{cleaned}:latest"
    return cleaned


def model_tag_matches(installed: str | None, wanted: str | None) -> bool:
    current = normalize_model_tag(installed)
    return bool(current) and current == normalize_model_tag(wanted)


__all__ = ["model_tag_matches", "normalize_model_tag"]
