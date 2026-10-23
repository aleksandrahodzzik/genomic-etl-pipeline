"""Job lifecycle transitions: queued -> running -> completed | failed."""

import json
import uuid

from sqlalchemy import Engine, text

from variant_etl.db.models import JobStatus


def get_inputs(engine: Engine, job_id: uuid.UUID) -> tuple[str, str]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT vcf_key, reference_key FROM jobs WHERE id = :id"), {"id": job_id}
        ).one()
    return row.vcf_key, row.reference_key


def mark_running(engine: Engine, job_id: uuid.UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status = :running, started_at = COALESCE(started_at, now()),"
                " error = NULL WHERE id = :id AND status IN (:queued, :running)"
            ),
            {"id": job_id, "queued": JobStatus.QUEUED, "running": JobStatus.RUNNING},
        )


def set_split_result(
    engine: Engine, job_id: uuid.UUID, chunk_count: int, contigs: dict[str, int]
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET chunk_count = :n, contigs = CAST(:contigs AS jsonb) WHERE id = :id"
            ),
            {"id": job_id, "n": chunk_count, "contigs": json.dumps(contigs)},
        )


def mark_failed(engine: Engine, job_id: uuid.UUID, error: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status = :failed, finished_at = now(), error = :error"
                " WHERE id = :id"
            ),
            {"id": job_id, "failed": JobStatus.FAILED, "error": error[:2_000]},
        )


def finalize(engine: Engine, job_id: uuid.UUID) -> str:
    """Aggregate chunk results into the job row and set the final status.

    The totals are recomputed from `job_chunks` (committed facts), not from the values the
    chord passed along, so running finalize twice, or after a redelivered chunk, gives the
    same answer. A chunk that never reported counts as failed: a job is `completed` only if
    every chunk it was split into committed.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT j.chunk_count,"
                " count(c.chunk_index) FILTER (WHERE c.status = 'done') AS done,"
                " coalesce(sum(c.total) FILTER (WHERE c.status = 'done'), 0) AS total,"
                " coalesce(sum(c.accepted) FILTER (WHERE c.status = 'done'), 0) AS accepted,"
                " coalesce(sum(c.rejected) FILTER (WHERE c.status = 'done'), 0) AS rejected,"
                " coalesce(sum(c.normalized) FILTER (WHERE c.status = 'done'), 0) AS normalized"
                " FROM jobs j LEFT JOIN job_chunks c ON c.job_id = j.id"
                " WHERE j.id = :id GROUP BY j.chunk_count"
            ),
            {"id": job_id},
        ).one()
        failures = conn.execute(
            text(
                "SELECT chunk_index, error FROM job_chunks"
                " WHERE job_id = :id AND status = 'failed' ORDER BY chunk_index LIMIT 5"
            ),
            {"id": job_id},
        ).all()

        expected = row.chunk_count or 0
        missing = expected - row.done
        error = None
        if missing > 0:
            detail = "; ".join(f"chunk {r.chunk_index}: {r.error}" for r in failures)
            error = f"{missing} of {expected} chunks did not complete" + (
                f" ({detail})" if detail else ""
            )
        status = JobStatus.FAILED if error else JobStatus.COMPLETED
        conn.execute(
            text(
                "UPDATE jobs SET status = :status, finished_at = now(), error = :error,"
                " total = :total, accepted = :accepted, rejected = :rejected,"
                " normalized = :normalized WHERE id = :id"
            ),
            {
                "id": job_id,
                "status": status,
                "error": error,
                "total": row.total,
                "accepted": row.accepted,
                "rejected": row.rejected,
                "normalized": row.normalized,
            },
        )
    return status
