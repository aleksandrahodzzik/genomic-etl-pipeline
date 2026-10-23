"""Idempotent bulk write of one chunk with COPY.

Why COPY: it streams rows to the server in one command with no per-row statement parsing or
round trip. For 100k-row chunks that is the difference between hundreds of milliseconds and
tens of seconds compared with row-by-row INSERTs (see docs/BENCHMARKS.md).

Why this is idempotent: Celery with `acks_late` delivers at least once, so the same
(job_id, chunk_index) can run twice (worker killed mid-task, broker redelivery, a task that
timed out but kept running). Every write for a chunk happens in ONE transaction that

  1. takes a transaction-scoped advisory lock on (job_id, chunk_index), so two copies of the
     same chunk serialize instead of interleaving;
  2. deletes whatever an earlier attempt left for that chunk;
  3. COPYs the variants and the sampled rejected lines;
  4. inserts the `job_chunks` marker row with the chunk's counts.

Either all of it commits or none of it does, so a chunk is never half-loaded, and running it
again replaces rather than duplicates. The marker row also lets a redelivered message after
success skip the parse work entirely (`chunk_is_done`).
"""

import json
import uuid
from collections.abc import Callable

from sqlalchemy import Engine, text

from variant_etl.pipeline.transform import ChunkResult

VARIANT_COLUMNS = (
    "job_id, chunk_index, chrom, pos, ref, alt, qual, filter, "
    "variant_type, is_transition, af, was_normalized"
)
REJECTED_COLUMNS = "job_id, chunk_index, line_no, reason, raw_line"
MAX_RAW_LINE_CHARS = 2_000


def _clean(value: str) -> str:
    """PostgreSQL text cannot hold NUL, and COPY fails the whole chunk if it sees one."""
    return value.replace("\x00", "\ufffd")


def chunk_is_done(engine: Engine, job_id: uuid.UUID, chunk_index: int) -> bool:
    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM job_chunks WHERE job_id = :j AND chunk_index = :c"),
            {"j": job_id, "c": chunk_index},
        ).scalar()
    return status == "done"


def load_chunk(
    engine: Engine,
    job_id: uuid.UUID,
    chunk_index: int,
    result: ChunkResult,
    before_commit: Callable[[], None] | None = None,
) -> None:
    """Replace everything stored for (job_id, chunk_index) with `result`, atomically.

    `before_commit` runs after all rows are written and before COMMIT. It exists so tests can
    simulate a crash at the worst possible moment and prove nothing is left behind.
    """
    with engine.begin() as conn:
        params = {"j": job_id, "c": chunk_index}
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(CAST(:j AS text)), :c)"), params)
        conn.execute(text("DELETE FROM variants WHERE job_id = :j AND chunk_index = :c"), params)
        conn.execute(
            text("DELETE FROM rejected_records WHERE job_id = :j AND chunk_index = :c"), params
        )
        conn.execute(text("DELETE FROM job_chunks WHERE job_id = :j AND chunk_index = :c"), params)

        cursor = conn.connection.cursor()  # the psycopg cursor on this same transaction
        with cursor.copy(f"COPY variants ({VARIANT_COLUMNS}) FROM STDIN") as copy:
            for row in result.rows:
                copy.write_row((job_id, chunk_index, *row))
        with cursor.copy(f"COPY rejected_records ({REJECTED_COLUMNS}) FROM STDIN") as copy:
            for rejected in result.rejected_samples:
                copy.write_row(
                    (
                        job_id,
                        chunk_index,
                        rejected.line_no,
                        rejected.reason,
                        _clean(rejected.raw_line[:MAX_RAW_LINE_CHARS]),
                    )
                )

        conn.execute(
            text(
                "INSERT INTO job_chunks (job_id, chunk_index, status, total, accepted, rejected,"
                " normalized, reject_reasons) VALUES (:j, :c, 'done', :total, :accepted,"
                " :rejected, :normalized, CAST(:reasons AS jsonb))"
            ),
            {
                **params,
                "total": result.total,
                "accepted": result.accepted,
                "rejected": result.rejected,
                "normalized": result.normalized,
                "reasons": json.dumps(dict(result.reject_reasons)),
            },
        )
        if before_commit is not None:
            before_commit()


def record_chunk_failure(engine: Engine, job_id: uuid.UUID, chunk_index: int, error: str) -> None:
    """Mark a chunk as permanently failed unless a successful attempt already committed."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO job_chunks (job_id, chunk_index, status, total, accepted, rejected,"
                " normalized, error) VALUES (:j, :c, 'failed', 0, 0, 0, 0, :error)"
                " ON CONFLICT (job_id, chunk_index) DO NOTHING"
            ),
            {"j": job_id, "c": chunk_index, "error": error[:2_000]},
        )
