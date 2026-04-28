from __future__ import annotations

from celery import Celery

from src.config import get_settings


settings = get_settings()
celery_app = Celery(
    "building_change",
    broker=settings.celery_broker_url or settings.redis_url,
    backend=settings.celery_result_backend or settings.redis_url,
    include=["src.jobs.tasks"],
)

celery_app.conf.update(
    task_default_queue=settings.celery_task_default_queue,
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
    task_acks_late=settings.celery_task_acks_late,
    task_reject_on_worker_lost=settings.celery_task_reject_on_worker_lost,
)
