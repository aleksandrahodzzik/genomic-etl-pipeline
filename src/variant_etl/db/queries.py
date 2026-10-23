"""Read-side SQL for the API. Plain SQL via `text()` because these are aggregates the ORM
adds nothing to; every query is scoped by job_id first, which is the leading column of both
indexes."""

import base64
import json
import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from variant_etl.db.models import Job, JobStatus


def get_job(session: Session, job_id: uuid.UUID) -> Job | None:
    return session.get(Job, job_id)


def job_totals(session: Session, job_id: uuid.UUID) -> dict[str, Any]:
    """Live counts from committed chunk rows (equal to the job row once finalized)."""
    row = (
        session.execute(
            text(
                "SELECT"
                " count(*) FILTER (WHERE status = 'done') AS chunks_done,"
                " count(*) FILTER (WHERE status = 'failed') AS chunks_failed,"
                " coalesce(sum(total) FILTER (WHERE status = 'done'), 0) AS total,"
                " coalesce(sum(accepted) FILTER (WHERE status = 'done'), 0) AS accepted,"
                " coalesce(sum(rejected) FILTER (WHERE status = 'done'), 0) AS rejected,"
                " coalesce(sum(normalized) FILTER (WHERE status = 'done'), 0) AS normalized"
                " FROM job_chunks WHERE job_id = :j"
            ),
            {"j": job_id},
        )
        .mappings()
        .one()
    )
    return dict(row)


def reject_reason_counts(session: Session, job_id: uuid.UUID) -> dict[str, int]:
    rows = session.execute(
        text(
            "SELECT r.key, sum(r.value::int) AS n"
            " FROM job_chunks c, jsonb_each_text(c.reject_reasons) r"
            " WHERE c.job_id = :j AND c.status = 'done' GROUP BY r.key ORDER BY n DESC, r.key"
        ),
        {"j": job_id},
    ).all()
    return {key: int(n) for key, n in rows}


def failed_chunks(session: Session, job_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT chunk_index, error FROM job_chunks"
            " WHERE job_id = :j AND status = 'failed' ORDER BY chunk_index LIMIT 20"
        ),
        {"j": job_id},
    ).all()
    return [{"chunk_index": i, "error": e} for i, e in rows]


def is_partial(status: str) -> bool:
    return status != JobStatus.COMPLETED


# --- stats -----------------------------------------------------------------------------------


def type_stats(session: Session, job_id: uuid.UUID) -> list[Any]:
    # Ti/Tv counts SNVs only, and skips SNVs touching N (an unknown base is neither).
    return list(
        session.execute(
            text(
                "SELECT variant_type, count(*) AS n,"
                " count(*) FILTER (WHERE is_transition) AS ti,"
                " count(*) FILTER (WHERE variant_type = 'SNV' AND NOT is_transition"
                "                  AND ref <> 'N' AND alt <> 'N') AS tv,"
                " count(*) FILTER (WHERE was_normalized) AS normalized"
                " FROM variants WHERE job_id = :j GROUP BY variant_type"
            ),
            {"j": job_id},
        ).all()
    )


def chromosome_counts(session: Session, job_id: uuid.UUID) -> dict[str, int]:
    rows = session.execute(
        text("SELECT chrom, count(*) FROM variants WHERE job_id = :j GROUP BY chrom"),
        {"j": job_id},
    ).all()
    return {chrom: int(n) for chrom, n in rows}


def af_histogram(session: Session, job_id: uuid.UUID, bins: int) -> tuple[dict[int, int], int]:
    """Counts per equal-width bin of [0, 1]; AF == 1.0 is folded into the last bin."""
    rows = session.execute(
        text(
            "SELECT least(width_bucket(af, 0, 1, :bins), :bins) AS bucket, count(*)"
            " FROM variants WHERE job_id = :j AND af IS NOT NULL"
            " GROUP BY bucket"
        ),
        {"j": job_id, "bins": bins},
    ).all()
    missing = session.execute(
        text("SELECT count(*) FROM variants WHERE job_id = :j AND af IS NULL"), {"j": job_id}
    ).scalar_one()
    # width_bucket gives 0 to values below 0 (AF is not range-checked on input): count those
    # in the first bin rather than dropping them.
    return {max(int(b), 1): int(n) for b, n in rows}, int(missing)


# --- variants (keyset pagination) ------------------------------------------------------------


def encode_cursor(chrom: str, pos: int, row_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([chrom, pos, row_id]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, int, int]:
    try:
        chrom, pos, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return str(chrom), int(pos), int(row_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


def query_variants(
    session: Session,
    job_id: uuid.UUID,
    *,
    chrom: str | None,
    start: int | None,
    end: int | None,
    variant_type: str | None,
    limit: int,
    cursor: tuple[str, int, int] | None,
) -> list[Any]:
    """Page through a job's variants in (chrom, pos, id) order.

    Keyset pagination: the cursor carries the last row's sort key and the next page asks for
    rows strictly after it, so cost per page does not grow with page number (OFFSET would
    re-read and discard every earlier row) and rows inserted meanwhile cannot shift pages.
    """
    clauses = ["job_id = :j"]
    params: dict[str, Any] = {"j": job_id, "limit": limit}
    if chrom is not None:
        clauses.append("chrom = :chrom")
        params["chrom"] = chrom
    if start is not None:
        clauses.append("pos >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("pos <= :end")
        params["end"] = end
    if variant_type is not None:
        clauses.append("variant_type = :vtype")
        params["vtype"] = variant_type
    if cursor is not None:
        clauses.append("(chrom, pos, id) > (:c_chrom, :c_pos, :c_id)")
        params.update(c_chrom=cursor[0], c_pos=cursor[1], c_id=cursor[2])
    sql = (
        "SELECT id, chrom, pos, ref, alt, qual, filter, variant_type, is_transition, af,"
        " was_normalized FROM variants WHERE " + " AND ".join(clauses) + " ORDER BY chrom, pos, id"
        " LIMIT :limit"
    )
    return list(session.execute(text(sql), params).mappings().all())


def rejected_page(
    session: Session, job_id: uuid.UUID, after_line: int, limit: int
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT line_no, chunk_index, reason, raw_line FROM rejected_records"
            " WHERE job_id = :j AND line_no > :after ORDER BY line_no LIMIT :limit"
        ),
        {"j": job_id, "after": after_line, "limit": limit},
    ).mappings()
    return [dict(r) for r in rows]


_CHROM_ORDER = {"X": 23, "Y": 24, "M": 25, "MT": 25}


def chrom_sort_key(name: str) -> tuple[int, int, str]:
    """chr1 < chr2 < ... < chr10 < chrX < chrY < chrM < others (natural, not lexical)."""
    bare = re.sub(r"^chr", "", name, flags=re.IGNORECASE)
    if bare.isdigit():
        return (0, int(bare), "")
    if bare.upper() in _CHROM_ORDER:
        return (1, _CHROM_ORDER[bare.upper()], "")
    return (2, 0, name)
