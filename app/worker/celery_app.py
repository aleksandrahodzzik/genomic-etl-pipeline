"""Celery application (Redis broker + result backend)."""

from celery import Celery
from celery.signals import worker_process_init

from app.config import settings

celery_app = Celery(
    "genomic_etl",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # One long-running ETL job per process slot; never hoard queued jobs.
    worker_prefetch_multiplier=1,
    # Ack after completion so a crashed worker's job is redelivered (the task is idempotent).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    result_expires=24 * 3600,
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)


@worker_process_init.connect
def _reset_db_pool_in_child(**_kwargs: object) -> None:
    """Drop connections inherited from the parent so each prefork child opens its own."""
    from app.database import engine

    engine.dispose()
