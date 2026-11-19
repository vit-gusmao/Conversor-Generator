"""Desktop host guard (ASGI middleware).

Loopback binding is not authentication: any local process or browser tab can
reach 127.0.0.1. In desktop mode every HTTP request therefore has to prove it
comes from the Electron host that started this backend:

* header `x-desktop-host` equals the per-start bootstrap secret (constant time);
* `Host` is `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding defence);
* an `Origin` header, when present, is exactly this server's own origin.

`GET /health` is the only exemption (readiness probes). The middleware is
inert unless it is constructed with `enabled=True` and a non-empty secret.
The Electron main process injects the header through `webRequest` for the
backend origin only, so the page itself never sees the secret.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

HOST_HEADER_NAME = b"x-desktop-host"
PUBLIC_GET_PATHS = frozenset({"/health"})


class DesktopHostGuard:
    def __init__(self, app: Any, *, secret: str | None, enabled: bool = True) -> None:
        self.app = app
        self._secret = (secret or "").encode("utf-8")
        self.enabled = bool(enabled) and bool(self._secret)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # No websocket surface exists; anything that appears must be denied.
            await send({"type": "websocket.close", "code": 1008})
            return

        if scope["type"] == "http":
            if scope.get("method", "GET").upper() == "GET" and scope.get("path") in PUBLIC_GET_PATHS:
                await self.app(scope, receive, send)
                return
            if not self.allowed(scope):
                await _reject_http(send)
                return

        await self.app(scope, receive, send)

    # ------------------------------------------------------------------ checks
    def allowed(self, scope: Scope) -> bool:
        port = _server_port(scope)
        if port is None:
            return False
        headers = _header_values(scope)

        secrets_presented = headers.get(HOST_HEADER_NAME, [])
        if len(secrets_presented) != 1:
            return False
        if not secrets.compare_digest(secrets_presented[0], self._secret):
            return False

        hosts = headers.get(b"host", [])
        if len(hosts) != 1:
            return False
        allowed_hosts = {f"127.0.0.1:{port}".encode(), f"localhost:{port}".encode()}
        if hosts[0].lower() not in allowed_hosts:
            return False

        origins = headers.get(b"origin", [])
        if len(origins) > 1:
            return False
        if origins and origins[0] != f"http://127.0.0.1:{port}".encode():
            return False
        return True


def _header_values(scope: Scope) -> dict[bytes, list[bytes]]:
    table: dict[bytes, list[bytes]] = {}
    for raw_name, raw_value in scope.get("headers") or []:
        table.setdefault(bytes(raw_name).lower(), []).append(bytes(raw_value))
    return table


def _server_port(scope: Scope) -> int | None:
    server = scope.get("server")
    if not server or len(server) < 2 or not server[1]:
        return None
    return int(server[1])


async def _reject_http(send: Send) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "DESKTOP_HOST_FORBIDDEN",
                "message": "Acesso restrito ao aplicativo desktop.",
                "details": {},
            }
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["DesktopHostGuard", "HOST_HEADER_NAME", "PUBLIC_GET_PATHS"]
