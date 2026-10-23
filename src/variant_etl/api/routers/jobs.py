import hashlib
import logging
import re
import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from variant_etl.api.deps import enqueue_dependency, storage_dependency
from variant_etl.config import get_settings
from variant_etl.db import queries
from variant_etl.db.models import Job, JobStatus
from variant_etl.db.session import get_db
from variant_etl.schemas.jobs import (
    FailedChunk,
    JobCounts,
    JobCreate,
    JobOut,
    JobProgress,
    RejectedPage,
    RejectedRecordOut,
    UploadRequest,
    UploadResponse,
)
from variant_etl.storage.s3 import ObjectNotFound, ObjectStorage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["jobs"])

UPLOAD_PREFIX = "uploads/"
DbSession = Annotated[Session, Depends(get_db)]
Storage = Annotated[ObjectStorage, Depends(storage_dependency)]
Enqueue = Annotated[Callable[[str], None], Depends(enqueue_dependency)]


def safe_filename(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name.replace("\\", "/").rsplit("/", 1)[-1])
    return base.strip("._")[:100] or "upload"


def job_out(session: Session, job: Job, deduplicated: bool = False) -> JobOut:
    totals = queries.job_totals(session, job.id)
    chunks_total = job.chunk_count
    if chunks_total:
        fraction = min(totals["chunks_done"] / chunks_total, 1.0)
    else:
        fraction = 1.0 if job.status == JobStatus.COMPLETED else 0.0
    duration = (
        (job.finished_at - job.started_at).total_seconds()
        if job.finished_at and job.started_at
        else None
    )
    return JobOut(
        id=job.id,
        status=job.status,  # type: ignore[arg-type]
        vcf_key=job.vcf_key,
        reference_key=job.reference_key,
        counts=JobCounts(
            total=totals["total"],
            accepted=totals["accepted"],
            rejected=totals["rejected"],
            normalized=totals["normalized"],
        ),
        progress=JobProgress(
            chunks_total=chunks_total,
            chunks_done=totals["chunks_done"],
            chunks_failed=totals["chunks_failed"],
            fraction=round(fraction, 4),
        ),
        rejected_by_reason=queries.reject_reason_counts(session, job.id),
        failed_chunks=[FailedChunk(**c) for c in queries.failed_chunks(session, job.id)],
        error=job.error,
        partial=queries.is_partial(job.status),
        deduplicated=deduplicated,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_seconds=duration,
    )


@router.post("/uploads", response_model=UploadResponse, summary="Get a presigned upload URL")
def create_upload(body: UploadRequest, storage: Storage) -> UploadResponse:
    """The file never passes through this service: the client PUTs it straight to object
    storage with the returned URL, then references the `key` in `POST /jobs`."""
    key = f"{UPLOAD_PREFIX}{uuid.uuid4().hex}/{safe_filename(body.filename)}"
    return UploadResponse(
        key=key,
        upload_url=storage.presign_put(key),
        expires_in=get_settings().presign_expires_seconds,
    )


@router.post(
    "/jobs",
    response_model=JobOut,
    status_code=202,
    summary="Start an ETL job",
    responses={200: {"model": JobOut, "description": "An identical job already exists."}},
)
def create_job(
    body: JobCreate, response: Response, session: DbSession, storage: Storage, enqueue: Enqueue
) -> JobOut:
    for key in (body.vcf_key, body.reference_key):
        if not key.startswith(UPLOAD_PREFIX):
            raise HTTPException(422, f"key must start with '{UPLOAD_PREFIX}': {key}")
    try:
        vcf, reference = storage.head(body.vcf_key), storage.head(body.reference_key)
    except ObjectNotFound as exc:
        raise HTTPException(422, f"{exc}; upload the file first") from exc

    # Identity of a job is the content of its two inputs. The ETag is the object's MD5 for
    # single-part uploads, so the same bytes uploaded twice under different keys dedupe.
    checksum = hashlib.sha256(
        f"{vcf.etag}:{vcf.size}|{reference.etag}:{reference.size}".encode()
    ).hexdigest()

    existing = session.query(Job).filter(Job.checksum == checksum).one_or_none()
    if existing is not None and existing.status != JobStatus.FAILED:
        response.status_code = 200
        return job_out(session, existing, deduplicated=True)
    if existing is not None:
        # Re-submitting after a failure means "try again": drop the failed attempt (its
        # variants cascade) so the checksum is free for a clean run.
        session.execute(delete(Job).where(Job.id == existing.id))
        session.commit()
        storage.delete_prefix(f"chunks/{existing.id}/")

    job = Job(
        id=uuid.uuid4(),
        status=JobStatus.QUEUED,
        vcf_key=body.vcf_key,
        reference_key=body.reference_key,
        checksum=checksum,
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request with the same inputs won the race on the UNIQUE checksum.
        session.rollback()
        winner = session.query(Job).filter(Job.checksum == checksum).one()
        response.status_code = 200
        return job_out(session, winner, deduplicated=True)

    try:
        enqueue(str(job.id))
    except Exception as exc:
        # Without a queued task the job would sit in `queued` forever, and its checksum
        # would block a retry. Remove it and tell the client to try again.
        log.exception("could not enqueue job %s", job.id)
        session.execute(delete(Job).where(Job.id == job.id))
        session.commit()
        raise HTTPException(503, "task queue unavailable, try again shortly") from exc
    session.refresh(job)
    return job_out(session, job)


def _require_job(session: Session, job_id: uuid.UUID) -> Job:
    job = queries.get_job(session, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@router.get("/jobs/{job_id}", response_model=JobOut, summary="Job status, progress and counts")
def get_job(job_id: uuid.UUID, session: DbSession) -> JobOut:
    return job_out(session, _require_job(session, job_id))


@router.get(
    "/jobs/{job_id}/rejected",
    response_model=RejectedPage,
    summary="Sample of rejected lines with reasons",
)
def list_rejected(
    job_id: uuid.UUID,
    session: DbSession,
    after_line: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> RejectedPage:
    _require_job(session, job_id)
    rows = queries.rejected_page(session, job_id, after_line, limit + 1)
    more = len(rows) > limit
    items = [RejectedRecordOut(**r) for r in rows[:limit]]
    return RejectedPage(items=items, next_after_line=items[-1].line_no if more else None)
