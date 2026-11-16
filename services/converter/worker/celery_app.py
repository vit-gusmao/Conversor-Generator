"""Celery application for the `full` runtime mode."""

from __future__ import annotations

from celery import Celery

from config import settings

celery_app = Celery(
    "pdf2x",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["worker.celery_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=settings.converter_task_timeout_seconds,
    task_soft_time_limit=max(30, settings.converter_task_timeout_seconds - 30),
    task_default_queue="pdf-native",
    task_routes={
        "converter.execute_run": {"queue": "pdf-native"},
    },
)

__all__ = ["celery_app"]
