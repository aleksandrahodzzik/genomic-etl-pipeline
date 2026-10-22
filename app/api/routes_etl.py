"""ETL job endpoints: upload, one-click demo dataset, job status."""

import logging
import os
import re
import tempfile
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DatasetJob, FileType, JobStatus
from app.schemas import JobListResponse, JobResponse
from app.storage import S3StorageClient, get_storage
from app.worker.tasks import process_genomic_dataset_task
from scripts.generate_sample_data import write_vcf_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/etl", tags=["ETL"])

VCF_EXTENSIONS = (".vcf",)
FASTA_EXTENSIONS = (".fasta", ".fa")
SPOOL_MAX_BYTES = 64 * 1024 * 1024


def get_storage_client() -> S3StorageClient:
    """FastAPI dependency (overridable in tests)."""
    return get_storage()


def _safe_filename(filename: str) -> str:
    base = os.path.basename(filename.replace("\\", "/"))
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    return cleaned or "upload"


def _detect_file_type(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(VCF_EXTENSIONS):
        return FileType.VCF
    if lowered.endswith(FASTA_EXTENSIONS):
        return FileType.FASTA
    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail="Unsupported file type. Upload a .vcf, .fasta or .fa file.",
    )


def _create_and_dispatch(
    db: Session, job: DatasetJob
) -> DatasetJob:
    """Persist the job, then hand it to the Celery worker."""
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        process_genomic_dataset_task.delay(str(job.id))
    except Exception as exc:  # broker unreachable
        logger.exception("Failed to dispatch job %s", job.id)
        job.status = JobStatus.FAILED
        job.error_message = f"Could not enqueue background task: {exc}"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task queue unavailable; job was recorded as FAILED.",
        ) from exc
    return job


@router.post(
    "/upload",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a VCF/FASTA file and start asynchronous processing",
)
def upload_dataset(
    file: UploadFile = File(..., description="A .vcf, .fasta or .fa file"),
    db: Session = Depends(get_db),
    storage: S3StorageClient = Depends(get_storage_client),
) -> DatasetJob:
    filename = _safe_filename(file.filename or "")
    file_type = _detect_file_type(filename)

    job_id = uuid.uuid4()
    s3_key = f"uploads/{job_id}/{filename}"
    try:
        storage.upload_fileobj(file.file, s3_key)
    except Exception as exc:
        logger.exception("Upload to object storage failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Object storage unavailable.",
        ) from exc

    job = DatasetJob(
        id=job_id,
        filename=filename,
        file_type=file_type,
        s3_key=s3_key,
        status=JobStatus.PENDING,
    )
    return _create_and_dispatch(db, job)


@router.post(
    "/demo-100k",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="One-click demo: generate a synthetic VCF, store it in MinIO and process it",
)
def create_demo_dataset(
    num_variants: int = Query(
        100_000, ge=1_000, le=500_000, description="Number of VCF data lines to generate"
    ),
    seed: Optional[int] = Query(42, description="Random seed for reproducible data"),
    db: Session = Depends(get_db),
    storage: S3StorageClient = Depends(get_storage_client),
) -> DatasetJob:
    job_id = uuid.uuid4()
    filename = f"demo_{num_variants}_variants.vcf"
    s3_key = f"uploads/{job_id}/{filename}"

    # Spool to memory up to 64 MiB, then to disk, so even 500k variants stay cheap.
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES, mode="w+b") as spool:
        write_vcf_stream(spool, num_variants, seed=seed if seed is not None else 42)
        spool.seek(0)
        try:
            storage.upload_fileobj(spool, s3_key)
        except Exception as exc:
            logger.exception("Demo upload to object storage failed")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Object storage unavailable.",
            ) from exc

    job = DatasetJob(
        id=job_id,
        filename=filename,
        file_type=FileType.VCF,
        s3_key=s3_key,
        status=JobStatus.PENDING,
    )
    return _create_and_dispatch(db, job)


@router.get(
    "/jobs",
    response_model=JobListResponse,
    summary="List ETL jobs (newest first)",
)
def list_jobs(
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        pattern="^(PENDING|PROCESSING|COMPLETED|FAILED)$",
        description="Filter by job status",
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> JobListResponse:
    query = select(DatasetJob)
    count_query = select(func.count()).select_from(DatasetJob)
    if status_filter:
        query = query.where(DatasetJob.status == status_filter)
        count_query = count_query.where(DatasetJob.status == status_filter)
    jobs = db.scalars(
        query.order_by(DatasetJob.created_at.desc()).limit(limit).offset(offset)
    ).all()
    total = db.scalar(count_query) or 0
    return JobListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[JobResponse.model_validate(job) for job in jobs],
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    summary="Job progress, batch counts and throughput metrics",
)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> DatasetJob:
    job = db.get(DatasetJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job
