"""Celery tasks: split -> group(process_chunk x K) -> finalize_job.

    split_job(job_id)
        streams the VCF from S3, writes K chunk objects, then publishes the chord
    chord( group(process_chunk(job, i) for i in range(K)) )( finalize_job(job) )

The chord is built inside `split_job` because K is only known after splitting. Every task
is safe to run more than once (see loader.py for chunks): chunk objects are rewritten under
fixed keys, chunk rows are replaced in one transaction, and finalize recomputes from the
committed chunk rows.
"""

import logging
import random
import time
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import sqlalchemy.exc
from botocore.exceptions import BotoCoreError, ClientError
from celery import Task, chord, group

from variant_etl.config import get_settings
from variant_etl.db.session import get_engine
from variant_etl.pipeline import jobstate
from variant_etl.pipeline.celery_app import app
from variant_etl.pipeline.chunking import VcfFormatError, read_chunk, split_vcf_lines
from variant_etl.pipeline.loader import chunk_is_done, load_chunk, record_chunk_failure
from variant_etl.pipeline.reference_store import open_reference
from variant_etl.pipeline.textio import iter_lines, maybe_gunzip
from variant_etl.pipeline.transform import process_lines
from variant_etl.storage.s3 import ObjectStorage, get_storage, is_transient_client_error

log = logging.getLogger(__name__)

MAX_CHUNK_RETRIES = 5
MAX_SPLIT_RETRIES = 3


def chunk_key(job_id: str, index: int) -> str:
    return f"chunks/{job_id}/{index:06d}.vcf"


def is_transient(exc: BaseException) -> bool:
    """Infrastructure hiccups worth retrying, as opposed to bad data or bugs."""
    if isinstance(exc, ClientError):
        return is_transient_client_error(exc)
    return isinstance(
        exc,
        BotoCoreError
        | psycopg.OperationalError
        | sqlalchemy.exc.OperationalError
        | sqlalchemy.exc.InterfaceError
        | ConnectionError
        | TimeoutError,
    )


def backoff_seconds(retries: int) -> float:
    """Exponential backoff with jitter: ~1s, 2s, 4s, ... capped at 60s."""
    return min(2**retries, 60) * random.uniform(0.5, 1.0)


class JobTask(Task):
    """Marks the job failed if a task that owns the whole job ends in an unhandled error."""

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        job_id = args[-1] if self.name.endswith("finalize_job") else args[0]
        log.error("task %s failed for job %s: %r", self.name, job_id, exc)
        jobstate.mark_failed(get_engine(), uuid.UUID(job_id), f"{self.name} failed: {exc}")


# --- split ---------------------------------------------------------------------------------


def run_split(job_id: str, storage: ObjectStorage | None = None) -> tuple[list[str], str]:
    """Stream the VCF from S3 and write chunk objects.

    Returns (chunk object keys, reference key).
    """
    storage = storage or get_storage()
    engine = get_engine()
    job_uuid = uuid.UUID(job_id)
    jobstate.mark_running(engine, job_uuid)
    vcf_key, reference_key = jobstate.get_inputs(engine, job_uuid)

    reference = open_reference(storage, reference_key)
    started = time.perf_counter()
    lines = iter_lines(maybe_gunzip(storage.iter_blocks(vcf_key)))
    keys: list[str] = []
    records = 0
    for chunk in split_vcf_lines(lines, get_settings().chunk_size):
        key = chunk_key(job_id, chunk.index)
        storage.put_bytes(key, chunk.payload)
        keys.append(key)
        records += chunk.record_count
    jobstate.set_split_result(engine, job_uuid, len(keys), reference.contigs)
    log.info(
        "job %s split: %d records into %d chunks in %.1fs",
        job_id,
        records,
        len(keys),
        time.perf_counter() - started,
    )
    return keys, reference_key


@app.task(bind=True, base=JobTask, name="variant_etl.split_job", max_retries=MAX_SPLIT_RETRIES)
def split_job(self: Task, job_id: str) -> dict[str, Any]:
    try:
        keys, reference_key = run_split(job_id)
    except VcfFormatError as exc:
        jobstate.mark_failed(get_engine(), uuid.UUID(job_id), f"invalid VCF: {exc}")
        return {"job_id": job_id, "chunks": 0, "error": str(exc)}
    except Exception as exc:
        if is_transient(exc) and self.request.retries < MAX_SPLIT_RETRIES:
            raise self.retry(exc=exc, countdown=backoff_seconds(self.request.retries)) from exc
        raise  # JobTask.on_failure marks the job failed

    if not keys:  # a header-only VCF: nothing to fan out, finish right away
        jobstate.finalize(get_engine(), uuid.UUID(job_id))
        return {"job_id": job_id, "chunks": 0}

    header = group(
        [process_chunk.s(job_id, index, key, reference_key) for index, key in enumerate(keys)]
    )
    chord(header)(finalize_job.s(job_id))
    return {"job_id": job_id, "chunks": len(keys)}


# --- chunk ---------------------------------------------------------------------------------


def run_chunk(
    job_id: str,
    chunk_index: int,
    key: str,
    reference_key: str,
    storage: ObjectStorage | None = None,
    before_commit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Parse, validate, normalize and load one chunk. Raises on any failure."""
    storage = storage or get_storage()
    engine = get_engine()
    job_uuid = uuid.UUID(job_id)

    if chunk_is_done(engine, job_uuid, chunk_index):
        # Redelivered after success: the committed rows are complete, nothing to redo.
        return {"chunk_index": chunk_index, "status": "done", "skipped": True}

    reference = open_reference(storage, reference_key)
    t0 = time.perf_counter()
    first_line, body = read_chunk(iter_lines(storage.iter_blocks(key)))
    result = process_lines(
        body, first_line, reference, get_settings().max_rejected_samples_per_chunk
    )
    t1 = time.perf_counter()
    load_chunk(engine, job_uuid, chunk_index, result, before_commit=before_commit)
    t2 = time.perf_counter()
    log.info(
        "job %s chunk %d: %d records (%d rows, %d rejected) parse+normalize %.2fs copy %.2fs",
        job_id,
        chunk_index,
        result.total,
        len(result.rows),
        result.rejected,
        t1 - t0,
        t2 - t1,
    )
    return {
        "chunk_index": chunk_index,
        "status": "done",
        "total": result.total,
        "rows": len(result.rows),
        "process_seconds": round(t1 - t0, 3),
        "load_seconds": round(t2 - t1, 3),
    }


@app.task(bind=True, name="variant_etl.process_chunk", max_retries=MAX_CHUNK_RETRIES)
def process_chunk(
    self: Task, job_id: str, chunk_index: int, key: str, reference_key: str
) -> dict[str, Any]:
    """Process one chunk; never raises for a chunk-level failure.

    A chord body only runs if every header task succeeds, so letting one bad chunk raise
    would leave the job stuck in `running` forever. Instead a chunk that cannot be
    processed (permanent error, or transient errors beyond the retry budget) records a
    `failed` row and returns normally, and `finalize_job` turns that into a failed job while
    the chunks that did succeed stay queryable.
    """
    try:
        return run_chunk(job_id, chunk_index, key, reference_key)
    except Exception as exc:
        if is_transient(exc) and self.request.retries < MAX_CHUNK_RETRIES:
            log.warning("job %s chunk %d transient error, retrying: %r", job_id, chunk_index, exc)
            raise self.retry(exc=exc, countdown=backoff_seconds(self.request.retries)) from exc
        log.exception("job %s chunk %d failed permanently", job_id, chunk_index)
        message = f"{type(exc).__name__}: {exc}"
        try:
            record_chunk_failure(get_engine(), uuid.UUID(job_id), chunk_index, message)
        except Exception:
            log.exception("could not record failure of chunk %d", chunk_index)
        return {"chunk_index": chunk_index, "status": "failed", "error": message}


# --- finalize ------------------------------------------------------------------------------


@app.task(bind=True, base=JobTask, name="variant_etl.finalize_job")
def finalize_job(self: Task, chunk_results: list[dict[str, Any]], job_id: str) -> dict[str, Any]:
    status = jobstate.finalize(get_engine(), uuid.UUID(job_id))
    log.info("job %s finalized: %s", job_id, status)
    return {"job_id": job_id, "status": status}
