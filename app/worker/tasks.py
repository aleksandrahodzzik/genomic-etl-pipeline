"""Background ETL task: stream from MinIO -> parse -> validate -> normalize -> bulk load."""

import logging
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.etl.loader import bulk_insert_contigs, bulk_insert_variants, transform_records
from app.etl.parser import FastaContigRecord, parse_fasta_stream, parse_vcf_stream
from app.models import DatasetJob, FastaContig, FileType, GenomicVariant, JobStatus
from app.storage import get_storage
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 8000


class JobNotFoundError(LookupError):
    """Raised when the requested job id does not exist."""


def _as_uuid(job_id: "str | uuid.UUID") -> uuid.UUID:
    return job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))


def _process_vcf(session: Session, job: DatasetJob, storage: Any, started: float) -> None:
    batch_size = settings.ETL_BATCH_SIZE
    lines = storage.stream_lines(job.s3_key)
    stage_start = time.perf_counter()
    for batch in parse_vcf_stream(lines, batch_size=batch_size):
        parsed_at = time.perf_counter()
        transformed = transform_records(batch, job.id)
        transformed_at = time.perf_counter()
        bulk_insert_variants(session, transformed.rows)

        # Progress counters commit atomically with the batch's rows.
        job.total_records += transformed.total
        job.valid_records += transformed.valid
        job.rejected_records += transformed.rejected
        job.batches_processed += 1
        session.commit()
        loaded_at = time.perf_counter()

        logger.info(
            "job=%s batch=%d records=%d valid=%d rejected=%d "
            "stream+parse=%.2fs validate+normalize=%.2fs load=%.2fs elapsed=%.2fs",
            job.id,
            job.batches_processed,
            transformed.total,
            transformed.valid,
            transformed.rejected,
            parsed_at - stage_start,
            transformed_at - parsed_at,
            loaded_at - transformed_at,
            loaded_at - started,
        )
        for line_number, reason in transformed.rejection_samples[:3]:
            logger.debug("job=%s rejected line %d: %s", job.id, line_number, reason)
        stage_start = time.perf_counter()


def _process_fasta(session: Session, job: DatasetJob, storage: Any, started: float) -> None:
    lines = storage.stream_lines(job.s3_key)
    pending: list[FastaContigRecord] = []
    flush_size = 1000

    def flush() -> None:
        if not pending:
            return
        bulk_insert_contigs(session, job.id, pending)
        job.batches_processed += 1
        session.commit()
        pending.clear()

    for contig in parse_fasta_stream(lines):
        job.total_records += 1
        if not contig.contig_id or contig.sequence_length == 0:
            job.rejected_records += 1
            continue
        job.valid_records += 1
        pending.append(contig)
        if len(pending) >= flush_size:
            flush()
    flush()
    if job.total_records == 0:
        raise ValueError("no FASTA records found (expected '>' header lines)")
    session.commit()


def run_etl_pipeline_sync(
    job_id: "str | uuid.UUID",
    session_factory: Optional[Callable[[], Session]] = None,
    storage: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute the full ETL pipeline for one job in the calling process.

    This is the exact code path used by the Celery task; it is exposed separately so
    tests and fallback mode need no broker. The run is idempotent: rows from an earlier
    (crashed / redelivered) attempt are removed before loading starts.
    """
    factory = session_factory or SessionLocal
    session = factory()
    started = time.perf_counter()
    try:
        job = session.get(DatasetJob, _as_uuid(job_id))
        if job is None:
            raise JobNotFoundError(f"job {job_id} not found")

        job.status = JobStatus.PROCESSING
        job.total_records = 0
        job.valid_records = 0
        job.rejected_records = 0
        job.batches_processed = 0
        job.processing_time_ms = None
        job.throughput_records_per_sec = None
        job.error_message = None
        job.completed_at = None
        session.execute(delete(GenomicVariant).where(GenomicVariant.job_id == job.id))
        session.execute(delete(FastaContig).where(FastaContig.job_id == job.id))
        session.commit()

        storage_client = storage or get_storage()
        try:
            if job.file_type == FileType.VCF:
                _process_vcf(session, job, storage_client, started)
            elif job.file_type == FileType.FASTA:
                _process_fasta(session, job, storage_client, started)
            else:
                raise ValueError(f"unsupported file type {job.file_type!r}")

            elapsed = time.perf_counter() - started
            job.processing_time_ms = round(elapsed * 1000.0, 3)
            job.throughput_records_per_sec = (
                round(job.total_records / elapsed, 2) if elapsed > 0 else None
            )
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("ETL job %s failed", job_id)
            error_text = traceback.format_exc()[-MAX_ERROR_LENGTH:]
            # Remove rows from batches committed before the failure: a FAILED job must
            # never expose a partially loaded dataset to the analytics endpoints.
            session.execute(
                delete(GenomicVariant).where(GenomicVariant.job_id == _as_uuid(job_id))
            )
            session.execute(
                delete(FastaContig).where(FastaContig.job_id == _as_uuid(job_id))
            )
            job = session.get(DatasetJob, _as_uuid(job_id))
            if job is not None:
                job.status = JobStatus.FAILED
                job.error_message = error_text
                job.processing_time_ms = round(
                    (time.perf_counter() - started) * 1000.0, 3
                )
                job.completed_at = datetime.now(timezone.utc)
                session.commit()

        return {
            "job_id": str(job.id) if job is not None else str(job_id),
            "status": job.status if job is not None else JobStatus.FAILED,
            "total_records": job.total_records if job is not None else 0,
            "valid_records": job.valid_records if job is not None else 0,
            "rejected_records": job.rejected_records if job is not None else 0,
            "batches_processed": job.batches_processed if job is not None else 0,
            "processing_time_ms": job.processing_time_ms if job is not None else None,
            "throughput_records_per_sec": (
                job.throughput_records_per_sec if job is not None else None
            ),
        }
    finally:
        session.close()


@celery_app.task(name="process_genomic_dataset_task")
def process_genomic_dataset_task(job_id: str) -> Dict[str, Any]:
    """Celery entry point: process one uploaded dataset."""
    return run_etl_pipeline_sync(job_id)
