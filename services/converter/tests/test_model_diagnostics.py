"""T13 — local model diagnostics: independent probes and honest failures."""

from __future__ import annotations

import json

import httpx
import pytest

from assistant import diagnostics

INTERNAL = {"x-internal-token": "test-internal-token"}


@pytest.fixture
def fake_ollama(monkeypatch):
    """Mock Transport for the Ollama API used by the diagnostics module."""

    state = {
        "version": True,
        "models": ["glm-ocr:latest", "qwen3.5:4b"],
        "ocr_ok": True,
        "llm_ok": True,
        "ocr_hang": False,
        "calls": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append((request.method, request.url.path))
        if request.url.path == "/api/version":
            if not state["version"]:
                return httpx.Response(503, text="indisponível")
            return httpx.Response(200, json={"version": "0.5.1"})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": name, "digest": f"sha256:{name}"} for name in state["models"]]},
            )
        if request.url.path == "/api/generate":
            body = json.loads(request.content)
            if body.get("images") and not state["ocr_ok"]:
                return httpx.Response(400, text="model does not support images")
            if body.get("images") and state["ocr_hang"]:
                raise httpx.ReadTimeout("timeout", request=request)
            return httpx.Response(200, json={"response": "ok"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    class PatchedClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(diagnostics.httpx, "Client", PatchedClient)
    return state


def _draft(ocr_model="glm-ocr:latest", llm_model="qwen3.5:4b", ocr_enabled=True, llm_enabled=True):
    return {
        "ocr": {
            "enabled": ocr_enabled,
            "model": ocr_model,
            "endpoint_host": "127.0.0.1",
            "endpoint_port": 11434,
        },
        "assistant": {
            "enabled": llm_enabled,
            "model": llm_model,
            "endpoint_host": "127.0.0.1",
            "endpoint_port": 11434,
        },
    }


def test_runtime_and_both_models_available(fake_ollama):
    result = diagnostics.test_model_profile(_draft())
    assert result["runtime_detected"] is True
    assert result["runtime_version"] == "0.5.1"
    assert result["ocr_model_available"] is True
    assert result["assistant_model_available"] is True
    assert result["ocr_error"] is None
    assert result["assistant_error"] is None
    assert result["ocr_latency_ms"] is not None


def test_unknown_tag_is_reported_not_guessed(fake_ollama):
    result = diagnostics.test_model_profile(_draft(ocr_model="glm-ocr:inexistente"))
    assert result["ocr_model_available"] is False
    # The probe still runs: listing a tag never proves compatibility, and a
    # missing tag surfaces as a real model error.
    assert result["installed_models"] == ["glm-ocr:latest", "qwen3.5:4b"]


def test_model_without_vision_is_explained(fake_ollama):
    fake_ollama["ocr_ok"] = False
    result = diagnostics.test_model_profile(_draft())
    assert result["ocr_error"] is not None
    assert "visão" in result["ocr_error"]
    assert result["assistant_error"] is None  # the LLM is judged separately


def test_timeout_on_ocr_does_not_break_the_llm_probe(fake_ollama):
    fake_ollama["ocr_hang"] = True
    result = diagnostics.test_model_profile(_draft())
    assert result["ocr_error"] is not None
    assert result["assistant_error"] is None


def test_runtime_down_reports_actionable_note(fake_ollama):
    fake_ollama["version"] = False
    result = diagnostics.test_model_profile(_draft())
    assert result["runtime_detected"] is False
    assert any("não respondeu" in note for note in result["notes"])
    # No OCR/LLM call is attempted against a dead runtime.
    paths = [path for _, path in fake_ollama["calls"]]
    assert paths.count("/api/generate") == 0


def test_disabled_roles_are_not_probed(fake_ollama):
    result = diagnostics.test_model_profile(_draft(ocr_enabled=False, llm_enabled=False))
    assert result["ocr_latency_ms"] is None
    assert result["assistant_latency_ms"] is None
    assert [p for _, p in fake_ollama["calls"]].count("/api/generate") == 0


def test_endpoint_requires_internal_token(api_client):
    response = api_client.post("/internal/v1/models/diagnostics", json={"draft": {}})
    assert response.status_code in (401, 403)


def test_diagnostics_endpoint_validates_draft(api_client):
    response = api_client.post(
        "/internal/v1/models/diagnostics", json={"draft": "texto"}, headers=INTERNAL
    )
    assert response.status_code == 400


def test_diagnostics_endpoint_returns_full_report(api_client, fake_ollama):
    response = api_client.post(
        "/internal/v1/models/diagnostics", json={"draft": _draft()}, headers=INTERNAL
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["runtime_detected"] is True
    assert "installed_models" in payload


def test_automation_status_and_pause_routes(api_client):
    status = api_client.get("/internal/v1/automation/status", headers=INTERNAL)
    assert status.status_code == 200
    body = status.json()
    assert set(body) >= {"executor_enabled", "runtime_mode"}
