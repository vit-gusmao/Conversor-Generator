"""Queue factory."""

from __future__ import annotations

from config import settings

from .base import JobQueue
from .inline import InlineQueue

_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        if settings.converter_queue.lower() == "celery":
            from .celery_queue import CeleryQueue

            _queue = CeleryQueue()
        else:
            _queue = InlineQueue()
    return _queue


def set_queue(queue: JobQueue | None) -> None:
    global _queue
    if _queue is not None:
        _queue.shutdown()
    _queue = queue


__all__ = ["get_queue", "set_queue", "JobQueue", "InlineQueue"]
