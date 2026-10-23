import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from variant_etl.db import queries
from variant_etl.db.models import Job
from variant_etl.db.session import get_db
from variant_etl.pipeline.records import VariantType
from variant_etl.schemas.stats import (
    AfBin,
    AfHistogram,
    ChromosomeStat,
    ChromosomeStats,
    RecordCounts,
    RejectReasonStat,
    SummaryStats,
    VariantOut,
    VariantPage,
)

router = APIRouter(prefix="/api/v1/jobs/{job_id}", tags=["analytics"])
DbSession = Annotated[Session, Depends(get_db)]


def _job(session: Session, job_id: uuid.UUID) -> Job:
    job = queries.get_job(session, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


def _pct(part: int, whole: int, digits: int = 4) -> float:
    return round(100 * part / whole, digits) if whole else 0.0


@router.get("/stats/summary", response_model=SummaryStats, summary="Counts, Ti/Tv, rejections")
def summary(job_id: uuid.UUID, session: DbSession) -> SummaryStats:
    """Variants by type, transition/transversion ratio of SNVs, the share of rows changed by
    normalization, and rejected records broken down by reason."""
    job = _job(session, job_id)
    totals = queries.job_totals(session, job_id)
    by_type: dict[str, int] = {}
    transitions = transversions = normalized = 0
    for row in queries.type_stats(session, job_id):
        by_type[row.variant_type] = row.n
        transitions += row.ti
        transversions += row.tv
        normalized += row.normalized
    variants = sum(by_type.values())
    reasons = queries.reject_reason_counts(session, job_id)
    return SummaryStats(
        job_id=job_id,
        job_status=job.status,
        partial=queries.is_partial(job.status),
        records=RecordCounts(
            total=totals["total"],
            accepted=totals["accepted"],
            rejected=totals["rejected"],
            rejected_percent=_pct(totals["rejected"], totals["total"]),
        ),
        variants=variants,
        by_type=dict(sorted(by_type.items())),
        transitions=transitions,
        transversions=transversions,
        titv_ratio=round(transitions / transversions, 4) if transversions else None,
        normalized=normalized,
        normalized_percent=_pct(normalized, variants, 2),
        rejected_by_reason={
            reason: RejectReasonStat(count=count, percent_of_records=_pct(count, totals["total"]))
            for reason, count in reasons.items()
        },
    )


@router.get("/stats/chromosomes", response_model=ChromosomeStats, summary="Variants per chromosome")
def chromosomes(job_id: uuid.UUID, session: DbSession) -> ChromosomeStats:
    """Variant count and density (variants per megabase) for each contig of the reference."""
    job = _job(session, job_id)
    counts = queries.chromosome_counts(session, job_id)
    lengths = job.contigs or {}
    names = sorted(set(counts) | set(lengths), key=queries.chrom_sort_key)
    return ChromosomeStats(
        job_id=job_id,
        job_status=job.status,
        partial=queries.is_partial(job.status),
        chromosomes=[
            ChromosomeStat(
                chrom=name,
                length=lengths.get(name),
                variants=counts.get(name, 0),
                density_per_mb=(
                    round(counts.get(name, 0) / (lengths[name] / 1_000_000), 2)
                    if lengths.get(name)
                    else None
                ),
            )
            for name in names
        ],
    )


@router.get("/stats/af-histogram", response_model=AfHistogram, summary="Allele-frequency histogram")
def af_histogram(
    job_id: uuid.UUID,
    session: DbSession,
    bins: Annotated[int, Query(ge=1, le=200)] = 20,
) -> AfHistogram:
    """Equal-width bins over [0, 1]; empty bins are included so the shape is explicit."""
    job = _job(session, job_id)
    counts, missing = queries.af_histogram(session, job_id, bins)
    width = 1 / bins
    return AfHistogram(
        job_id=job_id,
        job_status=job.status,
        partial=queries.is_partial(job.status),
        bins=[
            AfBin(
                bin_start=round(i * width, 6),
                bin_end=round((i + 1) * width, 6),
                count=counts.get(i + 1, 0),
            )
            for i in range(bins)
        ],
        missing_af=missing,
    )


@router.get("/variants", response_model=VariantPage, summary="Query variants (keyset pagination)")
def list_variants(
    job_id: uuid.UUID,
    session: DbSession,
    chrom: str | None = None,
    start: Annotated[int | None, Query(ge=1, description="1-based, inclusive")] = None,
    end: Annotated[int | None, Query(ge=1, description="1-based, inclusive")] = None,
    type: Annotated[VariantType | None, Query(description="SNV, MNV, INS, DEL or COMPLEX")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: str | None = None,
) -> VariantPage:
    """Ordered by (chrom, pos, id). Follow `next_cursor` for the next page; there is no
    OFFSET, so deep pages cost the same as the first."""
    job = _job(session, job_id)
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "start must be <= end")
    try:
        decoded = queries.decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise HTTPException(422, "invalid cursor") from exc

    rows = queries.query_variants(
        session,
        job_id,
        chrom=chrom,
        start=start,
        end=end,
        variant_type=type.value if type else None,
        limit=limit + 1,  # one extra row tells us whether another page exists
        cursor=decoded,
    )
    page = rows[:limit]
    next_cursor = (
        queries.encode_cursor(page[-1]["chrom"], page[-1]["pos"], page[-1]["id"])
        if len(rows) > limit
        else None
    )
    return VariantPage(
        job_status=job.status,
        partial=queries.is_partial(job.status),
        items=[VariantOut(**row) for row in page],
        next_cursor=next_cursor,
    )
