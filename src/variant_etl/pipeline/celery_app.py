from celery import Celery

from variant_etl.config import get_settings

settings = get_settings()

app = Celery(
    "variant_etl",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["variant_etl.pipeline.tasks"],
)

app.conf.update(
    task_default_queue="variant_etl",
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Acknowledge only after the task finished. If a worker dies mid-chunk the broker
    # redelivers the message, which is safe because chunk writes are idempotent. The price is
    # at-least-once (not exactly-once) delivery.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # One chunk at a time per worker process: chunks are long (seconds) and uneven, and
    # prefetching would let one busy worker hoard chunks that an idle one could run.
    worker_prefetch_multiplier=1,
    # With acks_late, Redis redelivers anything not acked within this window. It must exceed
    # the slowest chunk, or healthy work is run twice.
    broker_transport_options={"visibility_timeout": 3600},
    broker_connection_retry_on_startup=True,
    result_expires=24 * 3600,
    task_track_started=False,
)
