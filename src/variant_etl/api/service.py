"""Creating a job from two uploaded objects. Shared by `POST /jobs` and the demo seeder, so
both go through the same validation, deduplication and enqueue-or-roll-back logic."""

import hashlib
import logging
import uuid
from collections.abc import Callable

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from variant_etl.db.models import Job, JobStatus
from variant_etl.storage.s3 import ObjectNotFound, ObjectStorage

log = logging.getLogger(__name__)

UPLOAD_PREFIX = "uploads/"


class SubmitError(Exception):
    """A request that cannot become a job; carries the HTTP status the API should answer with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def content_checksum(storage: ObjectStorage, vcf_key: str, reference_key: str) -> str:
    """Identity of a job is the content of its two inputs. The ETag is the object's MD5 for
    single-part uploads, so the same bytes uploaded twice under different keys dedupe."""
    for key in (vcf_key, reference_key):
        if not key.startswith(UPLOAD_PREFIX):
            raise SubmitError(422, f"key must start with '{UPLOAD_PREFIX}': {key}")
    try:
        vcf, reference = storage.head(vcf_key), storage.head(reference_key)
    except ObjectNotFound as exc:
        raise SubmitError(422, f"{exc}; upload the file first") from exc
    return hashlib.sha256(
        f"{vcf.etag}:{vcf.size}|{reference.etag}:{reference.size}".encode()
    ).hexdigest()


def submit_job(
    session: Session,
    storage: ObjectStorage,
    enqueue: Callable[[str], None],
    vcf_key: str,
    reference_key: str,
) -> tuple[Job, bool]:
    """Return (job, deduplicated). Raises SubmitError for bad input or an unavailable queue."""
    checksum = content_checksum(storage, vcf_key, reference_key)

    existing = session.query(Job).filter(Job.checksum == checksum).one_or_none()
    if existing is not None and existing.status != JobStatus.FAILED:
        return existing, True
    if existing is not None:
        # Re-submitting after a failure means "try again": drop the failed attempt (its
        # variants cascade) so the checksum is free for a clean run.
        session.execute(delete(Job).where(Job.id == existing.id))
        session.commit()
        storage.delete_prefix(f"chunks/{existing.id}/")

    job = Job(
        id=uuid.uuid4(),
        status=JobStatus.QUEUED,
        vcf_key=vcf_key,
        reference_key=reference_key,
        checksum=checksum,
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent request with the same inputs won the race on the UNIQUE checksum.
        session.rollback()
        return session.query(Job).filter(Job.checksum == checksum).one(), True

    try:
        enqueue(str(job.id))
    except Exception as exc:
        # Without a queued task the job would sit in `queued` forever, and its checksum
        # would block a retry. Remove it and tell the client to try again.
        log.exception("could not enqueue job %s", job.id)
        session.execute(delete(Job).where(Job.id == job.id))
        session.commit()
        raise SubmitError(503, "task queue unavailable, try again shortly") from exc
    session.refresh(job)
    return job, False
