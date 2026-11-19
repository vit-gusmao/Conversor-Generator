"""Model diagnostics for the desktop configuration screen (T13, doc 03 §6).

Two independent probes: OCR (vision) and the assistant LLM. Listing a model
does not prove it works, so `test_model_profile` performs a real short call
per role and reports latency/error. Neither probe downloads anything nor
switches the running profile.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx


def _base(host: str, port: int) -> str:
    return f"http://{host}:{int(port)}".rstrip("/")


def _ping(base_url: str, timeout: float = 5.0) -> tuple[bool, str | None]:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{base_url}/api/version")
            response.raise_for_status()
            return True, str(response.json().get("version") or "unknown")
    except Exception:  # noqa: BLE001
        return False, None


def _tags(base_url: str, timeout: float = 5.0) -> list[str]:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{base_url}/api/tags")
            response.raise_for_status()
            return [str(item.get("name")) for item in response.json().get("models") or []]
    except Exception:  # noqa: BLE001
        return []


#: 1x1 PNG used to prove the OCR model accepts images.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/wD/9n0AAAAASUVORK5CYII="
)


def test_ocr(base_url: str, model: str | None, *, timeout: float = 30.0) -> dict[str, Any]:
    if not model:
        return {"available": False, "error": "Informe a tag do modelo de OCR.", "latency_ms": None}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "Descreva a imagem em uma frase curta.",
                    "images": [base64.b64encode(_TINY_PNG).decode("ascii")],
                    "stream": False,
                    "options": {"num_predict": 16},
                },
            )
            response.raise_for_status()
            latency = int((time.perf_counter() - started) * 1000)
            return {"available": True, "error": None, "latency_ms": latency}
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200]
        return {
            "available": False,
            "error": f"O modelo não respondeu como modelo de visão ({exc.response.status_code}): {body}",
            "latency_ms": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"Falha ao testar o OCR: {exc}", "latency_ms": None}


def test_llm(base_url: str, model: str | None, *, timeout: float = 60.0) -> dict[str, Any]:
    if not model:
        return {"available": False, "error": "Informe a tag do LLM.", "latency_ms": None}
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "Responda apenas: ok",
                    "stream": False,
                    # Qwen3.5 thinking can starve short answers.
                    "think": False,
                    "options": {"num_predict": 8},
                },
            )
            response.raise_for_status()
            latency = int((time.perf_counter() - started) * 1000)
            return {"available": True, "error": None, "latency_ms": latency}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"Falha ao testar o LLM: {exc}", "latency_ms": None}


def test_model_profile(draft: dict[str, Any]) -> dict[str, Any]:
    """Probe both roles independently; never mutates the running profile."""
    ocr = draft.get("ocr") or {}
    assistant = draft.get("assistant") or {}
    ocr_host = str(ocr.get("endpoint_host") or "127.0.0.1")
    ocr_port = int(ocr.get("endpoint_port") or 11434)
    llm_host = str(assistant.get("endpoint_host") or ocr_host)
    llm_port = int(assistant.get("endpoint_port") or ocr_port)

    ocr_base = _base(ocr_host, ocr_port)
    llm_base = _base(llm_host, llm_port)

    runtime_ok, version = _ping(ocr_base)
    installed = _tags(ocr_base) if runtime_ok else []
    notes: list[str] = []
    if not runtime_ok:
        notes.append(
            "Ollama não respondeu no endpoint informado. Inicie o serviço ou escolha outro modo."
        )
    elif not installed:
        notes.append("O runtime respondeu, mas não há modelos instalados.")

    ocr_result = (
        test_ocr(ocr_base, ocr.get("model")) if runtime_ok and ocr.get("enabled", True) else
        {"available": False, "error": None, "latency_ms": None}
    )
    llm_result = (
        test_llm(llm_base, assistant.get("model"))
        if runtime_ok and assistant.get("enabled", False)
        else {"available": False, "error": None, "latency_ms": None}
    )

    ocr_name = str(ocr.get("model") or "")
    llm_name = str(assistant.get("model") or "")
    return {
        "runtime_detected": runtime_ok,
        "runtime_version": version,
        "ocr_model_available": ocr_name in installed if ocr_name else False,
        "assistant_model_available": llm_name in installed if llm_name else False,
        "ocr_latency_ms": ocr_result.get("latency_ms"),
        "assistant_latency_ms": llm_result.get("latency_ms"),
        "ocr_error": ocr_result.get("error"),
        "assistant_error": llm_result.get("error"),
        "installed_models": installed,
        "notes": notes,
    }


__all__ = ["test_llm", "test_model_profile", "test_ocr"]
