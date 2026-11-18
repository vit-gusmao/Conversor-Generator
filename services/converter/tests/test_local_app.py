"""Local application behaviour: the only mode the converter runs in.

Covers the HTTP surface the local SPA and API rely on: docs and OpenAPI, the
internal router, the session cookie attributes and readiness.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.deps import session_cookie_kwargs


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


def test_app_exposes_docs_openapi_and_internal_routes() -> None:
    from api.main import create_app

    app = create_app()

    assert app.docs_url == "/docs"
    assert app.openapi_url == "/openapi.json"

    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # Mounted but guarded: without the internal token the answer is 401, not 404.
    assert client.post("/internal/v1/jobs/x/enqueue").status_code == 401


# --------------------------------------------------------------------------- #
# Cookies
# --------------------------------------------------------------------------- #


def test_session_cookie_is_httponly_lax_and_not_cross_site() -> None:
    kwargs = session_cookie_kwargs()

    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs.get("secure") is not True
    assert kwargs["samesite"] != "none"
    assert kwargs["path"] == "/"


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #


def test_readiness_does_not_leak_hosts(api_client) -> None:
    response = api_client.get("/api/v1/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "status",
        "accepting_uploads",
        "ocr_available",
        "queue_pending",
    }
    assert isinstance(payload["accepting_uploads"], bool)
    assert "127.0.0.1" not in response.text
