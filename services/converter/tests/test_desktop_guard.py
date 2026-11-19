"""Desktop host guard, desktop-mode app wiring, runtime activity and entry secrets.

Covers the plan rule "loopback alone is not authentication": every HTTP request
in desktop mode carries the per-start bootstrap secret and a same-origin
Host/Origin, and the entry point refuses to serve without supplied secrets
(AU1-23: a late-generated secret must never be silently ignored).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.desktop_guard import DesktopHostGuard
from config import settings

SERVICE_DIR = Path(__file__).resolve().parents[1]
SECRET = "s" * 43
PORT = 41234


async def _inner(scope, receive, send):  # noqa: ANN001
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(
    *,
    method: str = "GET",
    path: str = "/api/v1/sources",
    headers: list[tuple[str, str]] | None = None,
    port: int | None = PORT,
    scope_type: str = "http",
) -> dict:
    return {
        "type": scope_type,
        "method": method,
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
        "server": ("127.0.0.1", port) if port is not None else None,
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "http_version": "1.1",
    }


def _run(app, scope) -> list[dict]:  # noqa: ANN001
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return messages


def _status(app, scope) -> int:  # noqa: ANN001
    messages = _run(app, scope)
    return messages[0]["status"] if messages and messages[0]["type"] == "http.response.start" else -1


def _good_headers(**overrides: str) -> list[tuple[str, str]]:
    headers = {
        "host": f"127.0.0.1:{PORT}",
        "x-desktop-host": SECRET,
        "accept": "application/json",
    }
    headers.update(overrides)
    return [(k, v) for k, v in headers.items() if v is not None]


@pytest.fixture
def guard() -> DesktopHostGuard:
    return DesktopHostGuard(_inner, secret=SECRET, enabled=True)


def test_request_with_the_bootstrap_secret_passes(guard):
    assert _status(guard, _scope(headers=_good_headers())) == 200


def test_missing_secret_is_forbidden(guard):
    headers = [(k, v) for k, v in _good_headers() if k != "x-desktop-host"]
    assert _status(guard, _scope(headers=headers)) == 403


def test_wrong_secret_is_forbidden(guard):
    assert _status(guard, _scope(headers=_good_headers(**{"x-desktop-host": "x" * 43}))) == 403


def test_duplicated_secret_header_is_forbidden(guard):
    headers = _good_headers() + [("x-desktop-host", SECRET)]
    assert _status(guard, _scope(headers=headers)) == 403


def test_forbidden_body_is_a_structured_error(guard):
    messages = _run(guard, _scope(headers=[("host", f"127.0.0.1:{PORT}")]))
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == "DESKTOP_HOST_FORBIDDEN"


def test_health_is_the_only_exempt_path(guard):
    assert _status(guard, _scope(path="/health", headers=[])) == 200
    # Same path with another method is not exempt.
    assert _status(guard, _scope(method="POST", path="/health", headers=[])) == 403
    assert _status(guard, _scope(path="/healthz", headers=[])) == 403


def test_static_and_sse_requests_need_the_secret_too(guard):
    assert _status(guard, _scope(path="/assets/app.js", headers=[])) == 403
    assert _status(guard, _scope(path="/api/v1/conversions/x/events", headers=[])) == 403
    assert _status(guard, _scope(path="/", headers=_good_headers())) == 200


def test_host_must_be_the_own_loopback_origin(guard):
    assert _status(guard, _scope(headers=_good_headers(host=f"localhost:{PORT}"))) == 200
    assert _status(guard, _scope(headers=_good_headers(host=f"evil.test:{PORT}"))) == 403
    assert _status(guard, _scope(headers=_good_headers(host="127.0.0.1:9999"))) == 403
    assert _status(guard, _scope(headers=_good_headers(host=f"127.0.0.1:{PORT}.evil.test"))) == 403


def test_foreign_or_null_origin_is_forbidden(guard):
    assert _status(guard, _scope(headers=_good_headers(origin=f"http://127.0.0.1:{PORT}"))) == 200
    assert _status(guard, _scope(headers=_good_headers(origin="http://evil.test"))) == 403
    assert _status(guard, _scope(headers=_good_headers(origin="null"))) == 403
    assert _status(guard, _scope(headers=_good_headers(origin=f"http://localhost:{PORT}"))) == 403


def test_unknown_server_port_fails_closed(guard):
    assert _status(guard, _scope(headers=_good_headers(), port=None)) == 403


def test_websocket_is_denied(guard):
    messages = _run(guard, _scope(scope_type="websocket", headers=_good_headers()))
    assert messages == [{"type": "websocket.close", "code": 1008}]


def test_inactive_without_enabled_or_secret(monkeypatch):
    passthrough = DesktopHostGuard(_inner, secret=SECRET, enabled=False)
    assert _status(passthrough, _scope(headers=[])) == 200
    no_secret = DesktopHostGuard(_inner, secret="", enabled=True)
    assert _status(no_secret, _scope(headers=[])) == 200


def test_lifespan_always_passes_through(guard):
    # Lifespan has no headers: it must reach the inner app, not get a 403.
    messages = _run(guard, {"type": "lifespan"})
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 200


def test_desktop_mode_app_has_guard_and_no_cors(monkeypatch):
    from starlette.middleware.cors import CORSMiddleware

    from api.desktop_guard import DesktopHostGuard as Guard
    from api.main import create_app

    monkeypatch.setattr(settings, "desktop_host_enabled", True)
    monkeypatch.setattr(settings, "desktop_bootstrap_secret", SECRET)
    app = create_app()
    classes = [middleware.cls for middleware in app.user_middleware]
    assert Guard in classes
    assert CORSMiddleware not in classes
    # Starlette prepends each add_middleware call: the guard (added last) is outermost.
    assert classes[0] is Guard


def test_desktop_mode_http_end_to_end(monkeypatch):
    from api.main import create_app

    monkeypatch.setattr(settings, "desktop_host_enabled", True)
    monkeypatch.setattr(settings, "desktop_bootstrap_secret", SECRET)
    client = TestClient(create_app())

    # TestClient binds the 'testserver' origin on port 80.
    own_host = {"Host": "127.0.0.1:80"}
    assert client.get("/health").status_code == 200
    denied = client.get("/api/v1/sources", headers=own_host)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "DESKTOP_HOST_FORBIDDEN"
    allowed = client.get("/api/v1/sources", headers={**own_host, "x-desktop-host": SECRET})
    assert allowed.status_code != 403
    foreign = client.get(
        "/api/v1/sources",
        headers={**own_host, "x-desktop-host": SECRET, "Origin": "http://evil.test"},
    )
    assert foreign.status_code == 403


def test_standalone_app_keeps_cors_and_no_guard(monkeypatch):
    from starlette.middleware.cors import CORSMiddleware

    from api.main import create_app

    monkeypatch.setattr(settings, "desktop_host_enabled", False)
    monkeypatch.setattr(settings, "desktop_bootstrap_secret", None)
    classes = [middleware.cls for middleware in create_app().user_middleware]
    assert CORSMiddleware in classes
    assert DesktopHostGuard not in classes


# --------------------------------------------------------------------------- #
# Runtime activity (drain before apply)
# --------------------------------------------------------------------------- #


def test_runtime_activity_reports_every_counter_from_existing_tables(monkeypatch):
    from api.main import app

    monkeypatch.setattr(settings, "converter_internal_token", "activity-token-0123456789")
    client = TestClient(app)
    response = client.get(
        "/internal/v1/runtime/activity",
        headers={"x-internal-token": "activity-token-0123456789"},
    )
    assert response.status_code == 200
    body = response.json()
    for key in ("queued", "running", "waiting_children", "active_imports", "indexing"):
        assert isinstance(body[key], int) and body[key] >= 0
    assert body["total"] == sum(v for k, v in body.items() if k != "total")


def test_runtime_activity_requires_the_internal_token():
    from api.main import app

    response = TestClient(app).get("/internal/v1/runtime/activity")
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Entry point secrets (AU1-23)
# --------------------------------------------------------------------------- #


def test_required_secrets_reject_missing_short_and_default_values():
    from desktop_entry import DesktopSecretError, required_desktop_secrets

    good = {
        "CONVERTER_INTERNAL_TOKEN": "i" * 43,
        "DESKTOP_BOOTSTRAP_SECRET": "b" * 43,
        "JWT_SECRET": "j" * 96,
    }
    assert set(required_desktop_secrets(good)) == set(good)
    for name in good:
        missing = {k: v for k, v in good.items() if k != name}
        with pytest.raises(DesktopSecretError):
            required_desktop_secrets(missing)
    with pytest.raises(DesktopSecretError):
        required_desktop_secrets({**good, "CONVERTER_INTERNAL_TOKEN": "dev-internal-token-change-me"})
    with pytest.raises(DesktopSecretError):
        required_desktop_secrets({**good, "JWT_SECRET": "dev-jwt-secret-change-me"})
    with pytest.raises(DesktopSecretError):
        required_desktop_secrets({**good, "DESKTOP_BOOTSTRAP_SECRET": "short"})


def test_settings_mismatch_is_detected():
    from types import SimpleNamespace

    from desktop_entry import DesktopSecretError, verify_settings_secrets

    supplied = {
        "CONVERTER_INTERNAL_TOKEN": "i" * 43,
        "DESKTOP_BOOTSTRAP_SECRET": "b" * 43,
        "JWT_SECRET": "j" * 96,
    }
    stale = SimpleNamespace(
        converter_internal_token="dev-internal-token-change-me",
        desktop_bootstrap_secret="b" * 43,
        jwt_secret="j" * 96,
    )
    with pytest.raises(DesktopSecretError):
        verify_settings_secrets(stale, supplied)
    matching = SimpleNamespace(
        converter_internal_token="i" * 43,
        desktop_bootstrap_secret="b" * 43,
        jwt_secret="j" * 96,
    )
    verify_settings_secrets(matching, supplied)


def _entry_env(tmp_path: Path, *, with_secrets: bool) -> dict[str, str]:
    app_data = tmp_path / "Conversor Generator" / "userData"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CONVERTER_INTERNAL_TOKEN", "DESKTOP_BOOTSTRAP_SECRET", "JWT_SECRET"}
    }
    env.update(
        {
            "APP_DATA_DIR": str(app_data),
            "WORK_DIR": str(app_data / "work"),
            "LOG_DIR": str(app_data / "logs"),
            "STORAGE_LOCAL_ROOT": str(app_data / "storage"),
            "WEB_DIST_DIR": str(tmp_path / "dist"),
            "CONVERTER_DATABASE_URL": f"sqlite:///{(app_data / 'converter.db').as_posix()}",
            "SERVICE_DIR": str(SERVICE_DIR),
            "CONVERTER_OCR_ENABLED": "false",
            "CONVERTER_QUEUE": "inline",
            "DESKTOP_HOST_ENABLED": "false",
        }
    )
    if with_secrets:
        env.update(
            {
                "CONVERTER_INTERNAL_TOKEN": "i" * 43,
                "DESKTOP_BOOTSTRAP_SECRET": "b" * 43,
                "JWT_SECRET": "j" * 96,
            }
        )
    return env


_ENTRY_SCRIPT = textwrap.dedent(
    """
    import os
    import sys
    sys.path.insert(0, os.environ["SERVICE_DIR"])
    from desktop_entry import main
    code = main(["--no-serve"])
    if code == 0:
        import config
        s = config.settings
        print("SETTINGS", s.converter_internal_token, s.desktop_bootstrap_secret, s.jwt_secret)
    sys.exit(code)
    """
)


def test_entry_refuses_to_start_without_supplied_secrets(tmp_path: Path):
    """Without the supervisor's secrets the process exits before any runtime setup."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", _ENTRY_SCRIPT],
        cwd=str(tmp_path),
        env=_entry_env(tmp_path, with_secrets=False),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 78, result.stderr[-1500:]
    assert "CONVERTER_REFUSED" in result.stderr
    assert "CONVERTER_READY" not in result.stdout
    assert not (tmp_path / "Conversor Generator").exists()


def test_settings_singleton_carries_the_supplied_secrets(tmp_path: Path):
    """AU1-23 regression: the singleton is created with the supervisor's values."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", _ENTRY_SCRIPT],
        cwd=str(tmp_path),
        env=_entry_env(tmp_path, with_secrets=True),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    line = next(item for item in result.stdout.splitlines() if item.startswith("SETTINGS "))
    token, bootstrap, jwt_secret = line.split()[1:]
    assert token == "i" * 43
    assert bootstrap == "b" * 43
    assert jwt_secret == "j" * 96
    assert "dev-internal-token-change-me" not in result.stdout
