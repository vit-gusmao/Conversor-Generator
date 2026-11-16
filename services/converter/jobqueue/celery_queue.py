"""Celery-backed queue (full mode).

Celery is imported lazily so the service installs and runs without it.
"""

from __future__ import annotations

import logging

from config import settings

from .base import JobQueue

logger = logging.getLogger(__name__)


class CeleryQueue(JobQueue):
    name = "celery"

    def __init__(self) -> None:
        try:
            from worker.celery_app import celery_app
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "CONVERTER_QUEUE=celery requires the 'queue' extra: pip install '.[queue]'"
            ) from exc
        self._app = celery_app
        self._redis = None
        try:
            import redis

            self._redis = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        except Exception:  # noqa: BLE001
            self._redis = None

    def submit(self, run_id: str, payload: dict) -> None:
        self._app.send_task(
            "converter.execute_run",
            kwargs={
                "run_id": run_id,
                "conversion_id": payload.get("conversion_id", ""),
                "storage_key": payload.get("storage_key", ""),
                "filename": payload.get("filename", "documento.pdf"),
                "options": payload.get("options") or {},
            },
            task_id=run_id,
        )

    def cancel(self, run_id: str) -> bool:
        try:
            self._app.control.revoke(run_id, terminate=True, signal="SIGTERM")
            return True
        except Exception:  # noqa: BLE001
            logger.warning("revoke failed for %s", run_id)
            return False

    def is_cancelled(self, run_id: str) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(self._redis.exists(f"pdf2x:cancel:{run_id}"))
        except Exception:  # noqa: BLE001
            return False

    def health(self) -> str:
        if self._redis is None:
            return "degraded"
        try:
            self._redis.ping()
            return "up"
        except Exception:  # noqa: BLE001
            return "down"

    def shutdown(self) -> None:
        return None


__all__ = ["CeleryQueue"]
