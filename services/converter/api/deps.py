"""FastAPI dependencies: session, authentication and error envelope."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy import select

from api.serialization import iso_utc
from config import settings
from models.db import PublicUserRow, get_session_factory


class ApiError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            detail={"error": {"code": code, "message": message, "details": details or {}}},
            headers=headers,
        )


def get_db() -> Iterator[Any]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Local session tokens
# --------------------------------------------------------------------------- #


def create_token(user_id: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_expires_in_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def user_dict(user: PublicUserRow) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_anonymous": bool(user.is_anonymous),
        "created_at": iso_utc(user.created_at),
    }


def resolve_user(
    session, authorization: str | None, device_id: str | None
) -> tuple[dict[str, Any], str | None]:
    """Return (user, generated_device_id)."""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token:
        try:
            payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise ApiError(401, "UNAUTHENTICATED", "Token inválido ou expirado.") from exc
        user = session.get(PublicUserRow, payload.get("sub"))
        if user is None:
            raise ApiError(401, "UNAUTHENTICATED", "Usuário não encontrado.")
        return user_dict(user), None

    if settings.auth_mode == "required":
        raise ApiError(401, "UNAUTHENTICATED", "Autenticação obrigatória.")

    generated = None
    if not device_id:
        device_id = f"dev_{secrets.token_hex(16)}"
        generated = device_id
    # `.limit(1)` instead of `scalar_one_or_none()`: concurrent first requests
    # from the same browser can create the device identity twice, and a lookup
    # must never turn into a 500 because of that.
    user = session.execute(
        select(PublicUserRow)
        .where(PublicUserRow.device_id == device_id)
        .order_by(PublicUserRow.created_at)
        .limit(1)
    ).scalar_one_or_none()
    if user is None:
        user = PublicUserRow(
            id=f"usr_{uuid.uuid4().hex}",
            role="owner",
            is_anonymous=True,
            device_id=device_id,
        )
        session.add(user)
        session.commit()
    return user_dict(user), generated


DEVICE_COOKIE = "pdf2x_device_id"


def session_cookie_kwargs() -> dict[str, Any]:
    """Cookie attributes for the anonymous browser session.

    The SPA and the API share one origin, so a first-party HttpOnly cookie
    with `SameSite=Lax` is sufficient.
    """
    return {"max_age": 31_536_000, "httponly": True, "samesite": "lax", "path": "/"}


def current_user(
    request: Request,
    response: Response,
    session=Depends(get_db),
    authorization: str | None = Header(default=None),
    x_device_id: str | None = Header(default=None),
) -> dict[str, Any]:
    # Cookie first: a browser that cleared localStorage but kept cookies keeps
    # the same local workspace and files.
    cookie_device = request.cookies.get(DEVICE_COOKIE)
    user, generated = resolve_user(session, authorization, x_device_id or cookie_device)
    if generated:
        request.state.generated_device_id = generated
        response.set_cookie(
            DEVICE_COOKIE,
            generated,
            **session_cookie_kwargs(),
        )
    return user


def internal_guard(x_internal_token: str | None = Header(default=None)) -> None:
    token = x_internal_token or ""
    if not token or not secrets.compare_digest(token, settings.converter_internal_token):
        raise ApiError(401, "UNAUTHORIZED_INTERNAL", "Token interno inválido.")


__all__ = [
    "ApiError",
    "DEVICE_COOKIE",
    "get_db",
    "current_user",
    "internal_guard",
    "create_token",
    "user_dict",
    "resolve_user",
    "session_cookie_kwargs",
]
