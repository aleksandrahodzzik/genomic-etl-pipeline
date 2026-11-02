"""Deletes old jobs and files on the public demo, which anyone can write to.

`python -m variant_etl.janitor` runs next to the API in the Hugging Face Space. Every few
minutes it deletes jobs created more than DEMO_RETENTION_MINUTES ago (their variants, chunk
rows and rejected lines cascade), and every uploaded file and chunk object older than that.
The pre-loaded sample job and its files are kept, so the page always has a result to show.
"""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from variant_etl.db.models import Job
from variant_etl.demo import DEMO_PREFIX, find_demo_job
from variant_etl.storage.s3 import ObjectStorage

INTERVAL_SECONDS = 120

log = logging.getLogger(__name__)


def delete_expired_jobs(session: Session, cutoff: datetime, keep_job_ids: set[uuid.UUID]) -> int:
    """Delete jobs created before `cutoff`, except `keep_job_ids`. The caller commits."""
    expired = select(Job.id).where(Job.created_at < cutoff)
    if keep_job_ids:
        expired = expired.where(Job.id.not_in(keep_job_ids))
    job_ids = list(session.scalars(expired))
    if job_ids:
        session.execute(delete(Job).where(Job.id.in_(job_ids)))
    return len(job_ids)


def purge_expired(
    session: Session, storage: ObjectStorage, cutoff: datetime, keep_job_ids: set[uuid.UUID]
) -> dict[str, int]:
    """Delete jobs created before `cutoff`, then uploads and chunk objects older than it."""
    jobs = delete_expired_jobs(session, cutoff, keep_job_ids)
    session.commit()
    keep = (DEMO_PREFIX, *(f"chunks/{job_id}/" for job_id in keep_job_ids))
    files = storage.delete_older_than(("uploads/", "chunks/"), cutoff, keep)
    return {"jobs": jobs, "files": files}


def purge_once(session: Session, storage: ObjectStorage, retention_minutes: int) -> dict[str, int]:
    sample = find_demo_job(session, storage)
    cutoff = datetime.now(UTC) - timedelta(minutes=retention_minutes)
    return purge_expired(session, storage, cutoff, {sample.id} if sample else set())


def main() -> None:
    from variant_etl.config import get_settings
    from variant_etl.db.session import get_session_factory
    from variant_etl.storage.s3 import get_storage

    logging.basicConfig(level=logging.INFO, format="janitor: %(message)s")
    retention = get_settings().demo_retention_minutes
    log.info("deleting jobs and files older than %d minutes", retention)
    while True:
        try:
            with get_session_factory()() as session:
                deleted = purge_once(session, get_storage(), retention)
            if any(deleted.values()):
                log.info("deleted %(jobs)d jobs and %(files)d files", deleted)
        except Exception:
            log.exception("purge failed; trying again next round")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
