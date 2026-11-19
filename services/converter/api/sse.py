"""Helpers shared by the server-sent-event routes.

A stream stays open for minutes. A request-scoped database session would be
pinned for that whole time, one connection per open tab, so the streams resolve
the caller with a short-lived session and open a fresh one for every poll.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from api.deps import resolve_user
from models.db import get_session_factory


@contextmanager
def short_session() -> Iterator[Any]:
    """A session that lives only for the block: commit on success, else roll back."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def resolve_stream_user(authorization: str | None, device_id: str | None) -> dict[str, Any]:
    """The owner of a streaming request, resolved without holding a session."""
    with short_session() as session:
        user, _generated = resolve_user(session, authorization, device_id)
    return user


def resume_cursor(*candidates: str | int | None) -> int:
    """Highest valid ``Last-Event-ID`` among the candidates, or 0."""
    cursor = 0
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            cursor = max(cursor, int(candidate))
        except (TypeError, ValueError):
            continue
    return cursor


__all__ = ["short_session", "resolve_stream_user", "resume_cursor"]
