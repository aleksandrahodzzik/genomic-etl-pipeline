"""The pre-loaded sample dataset.

`python -m variant_etl.demo` (the `seed` service in docker-compose) uploads the bundled sample
files and starts a job for them, so the web UI has real results to show the first time it is
opened. It is idempotent: jobs are deduplicated by content, so running it again is a no-op.
"""

import logging
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from variant_etl.api.service import SubmitError, content_checksum, ensure_capacity, submit_job
from variant_etl.db.models import Job
from variant_etl.storage.s3 import ObjectNotFound, ObjectStorage

SAMPLE_DIR = Path(__file__).parent / "api" / "static" / "sample"
DEMO_PREFIX = "uploads/demo/"
DEMO_VCF_KEY = f"{DEMO_PREFIX}variants.vcf"
DEMO_REFERENCE_KEY = f"{DEMO_PREFIX}ref.fa"
SYNTHETIC_PREFIX = "uploads/synthetic/"

log = logging.getLogger(__name__)


def seed_demo(
    session: Session, storage: ObjectStorage, enqueue: Callable[[str], None]
) -> tuple[Job, bool]:
    """Upload the sample files under fixed keys and submit them. Returns (job, deduplicated)."""
    storage.put_bytes(DEMO_VCF_KEY, (SAMPLE_DIR / "variants.vcf").read_bytes())
    storage.put_bytes(DEMO_REFERENCE_KEY, (SAMPLE_DIR / "ref.fa").read_bytes())
    return submit_job(session, storage, enqueue, DEMO_VCF_KEY, DEMO_REFERENCE_KEY)


def submit_synthetic(
    session: Session,
    storage: ObjectStorage,
    enqueue: Callable[[str], None],
    directory: Path,
    max_active: int = 0,
) -> Job:
    """The public demo's "synthetic records" button: upload a fresh copy of the synthetic
    dataset in `directory` (variants.vcf + ref.fa) and submit it as a new job.

    A run marker after the `##fileformat` line gives every copy its own content checksum, so
    each click is processed for real instead of being deduplicated to the previous one.
    """
    ensure_capacity(session, max_active)  # before uploading, so a busy demo costs nothing
    vcf = (directory / "variants.vcf").read_bytes()
    run = uuid.uuid4().hex
    first_line_end = vcf.index(b"\n") + 1
    marked = vcf[:first_line_end] + f"##demo_run={run}\n".encode() + vcf[first_line_end:]
    prefix = f"{SYNTHETIC_PREFIX}{run}/"
    storage.put_bytes(f"{prefix}variants.vcf", marked)
    storage.put_bytes(f"{prefix}ref.fa", (directory / "ref.fa").read_bytes())
    job, _ = submit_job(
        session, storage, enqueue, f"{prefix}variants.vcf", f"{prefix}ref.fa", max_active
    )
    return job


def find_demo_job(session: Session, storage: ObjectStorage) -> Job | None:
    """The job for the sample content, however it was created.

    Matching by content checksum (not by object key) also finds a job that was created by
    someone clicking "Use sample data" in the browser, which uploads under random keys.
    """
    try:
        checksum = content_checksum(storage, DEMO_VCF_KEY, DEMO_REFERENCE_KEY)
    except (SubmitError, ObjectNotFound):
        return None  # never seeded
    return session.query(Job).filter(Job.checksum == checksum).one_or_none()


def main() -> int:
    from variant_etl.api.deps import enqueue_split
    from variant_etl.db.session import get_session_factory
    from variant_etl.storage.s3 import get_storage

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with get_session_factory()() as session:
        try:
            job, deduplicated = seed_demo(session, get_storage(), enqueue_split)
        except SubmitError as exc:
            log.error("could not seed the sample dataset: %s", exc.detail)
            return 1
    log.info(
        "sample dataset %s: job %s (%s)",
        "already loaded" if deduplicated else "queued",
        job.id,
        job.status,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
