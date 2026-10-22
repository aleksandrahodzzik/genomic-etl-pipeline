"""Analytics endpoints backed by the indexed `genomic_variants` table."""

import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.etl.normalizer import VARIANT_TYPES, normalize_chrom
from app.models import DatasetJob, FastaContig, FileType, GenomicVariant
from app.schemas import (
    AlleleFrequencySpectrum,
    ChromosomeDensity,
    FastaContigOut,
    FastaSummaryResponse,
    SummaryResponse,
    VariantOut,
    VariantQueryResponse,
)

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])

GV = GenomicVariant


def _chrom_sort_key(chrom: str) -> tuple:
    suffix = chrom[3:] if chrom.startswith("chr") else chrom
    if suffix.isdigit():
        return (0, int(suffix), "")
    return (1, {"X": 0, "Y": 1, "M": 2}.get(suffix, 3), suffix)


def _resolve_chrom(chrom: Optional[str]) -> Optional[str]:
    if chrom is None:
        return None
    canonical = normalize_chrom(chrom)
    if canonical is None:
        raise HTTPException(status_code=422, detail=f"Unknown chromosome '{chrom}'")
    return canonical


def _conditional_count(condition):
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


@router.get(
    "/variants",
    response_model=VariantQueryResponse,
    summary="Query variants through the B-Tree indexes (region, class, AF, depth, quality)",
)
def query_variants(
    job_id: Optional[uuid.UUID] = Query(None, description="Restrict to one ETL job"),
    chrom: Optional[str] = Query(None, description="e.g. chr1, 1, X, MT"),
    start_pos: Optional[int] = Query(None, ge=1, description="Inclusive, 1-based"),
    end_pos: Optional[int] = Query(None, ge=1, description="Inclusive, 1-based"),
    variant_type: Optional[str] = Query(
        None, description="SNP | INSERTION | DELETION | MNP | COMPLEX"
    ),
    filter_status: Optional[str] = Query(None, description="e.g. PASS, LowQual"),
    min_af: Optional[float] = Query(None, ge=0.0, le=1.0),
    max_af: Optional[float] = Query(None, ge=0.0, le=1.0),
    min_dp: Optional[int] = Query(None, ge=0),
    min_qual: Optional[float] = Query(None, ge=0.0),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> VariantQueryResponse:
    conditions = []

    canonical_chrom = _resolve_chrom(chrom)
    if canonical_chrom is not None:
        conditions.append(GV.chrom == canonical_chrom)
    if start_pos is not None and end_pos is not None and start_pos > end_pos:
        raise HTTPException(status_code=422, detail="start_pos must be <= end_pos")
    if start_pos is not None:
        conditions.append(GV.pos >= start_pos)
    if end_pos is not None:
        conditions.append(GV.pos <= end_pos)
    if job_id is not None:
        conditions.append(GV.job_id == job_id)
    if variant_type is not None:
        normalized_type = variant_type.strip().upper()
        if normalized_type not in VARIANT_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"variant_type must be one of {', '.join(VARIANT_TYPES)}",
            )
        conditions.append(GV.variant_type == normalized_type)
    if filter_status is not None:
        conditions.append(GV.filter_status == filter_status)
    if min_af is not None:
        conditions.append(GV.allele_frequency >= min_af)
    if max_af is not None:
        conditions.append(GV.allele_frequency <= max_af)
    if min_dp is not None:
        conditions.append(GV.read_depth >= min_dp)
    if min_qual is not None:
        conditions.append(GV.quality >= min_qual)

    started = time.perf_counter()
    rows = db.scalars(
        select(GV)
        .where(*conditions)
        .order_by(GV.chrom, GV.pos, GV.id)
        .limit(limit)
        .offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(GV).where(*conditions)) or 0
    query_time_ms = round((time.perf_counter() - started) * 1000.0, 3)

    return VariantQueryResponse(
        query_time_ms=query_time_ms,
        total=total,
        limit=limit,
        offset=offset,
        items=[VariantOut.model_validate(row) for row in rows],
    )


@router.get(
    "/summary",
    response_model=SummaryResponse,
    summary="Real-time variant statistics: Ti/Tv, depth, AF spectrum, density per chromosome",
)
def variant_summary(
    job_id: Optional[uuid.UUID] = Query(None),
    chrom: Optional[str] = Query(None, description="e.g. chr1, 1, X, MT"),
    db: Session = Depends(get_db),
) -> SummaryResponse:
    conditions = []
    canonical_chrom = _resolve_chrom(chrom)
    if canonical_chrom is not None:
        conditions.append(GV.chrom == canonical_chrom)
    if job_id is not None:
        conditions.append(GV.job_id == job_id)

    started = time.perf_counter()

    totals = db.execute(
        select(
            func.count().label("total"),
            func.avg(GV.read_depth).label("avg_dp"),
            func.avg(GV.allele_frequency).label("avg_af"),
            func.avg(GV.quality).label("avg_qual"),
            _conditional_count(GV.is_transition.is_(True)).label("transitions"),
            _conditional_count(GV.variant_type == "SNP").label("snps"),
            _conditional_count(GV.variant_type == "INSERTION").label("insertions"),
            _conditional_count(GV.variant_type == "DELETION").label("deletions"),
            _conditional_count(GV.variant_type == "MNP").label("mnps"),
            _conditional_count(GV.variant_type == "COMPLEX").label("complex"),
            _conditional_count(GV.allele_frequency < 0.01).label("af_rare"),
            _conditional_count(
                (GV.allele_frequency >= 0.01) & (GV.allele_frequency <= 0.05)
            ).label("af_low"),
            _conditional_count(GV.allele_frequency > 0.05).label("af_common"),
            _conditional_count(GV.allele_frequency.is_(None)).label("af_unknown"),
        ).where(*conditions)
    ).one()

    density_rows = db.execute(
        select(
            GV.chrom,
            func.count().label("count"),
            func.min(GV.pos).label("min_pos"),
            func.max(GV.pos).label("max_pos"),
            func.avg(GV.allele_frequency).label("mean_af"),
        )
        .where(*conditions)
        .group_by(GV.chrom)
    ).all()

    query_time_ms = round((time.perf_counter() - started) * 1000.0, 3)

    snps = int(totals.snps)
    transitions = int(totals.transitions)
    transversions = snps - transitions
    ti_tv = round(transitions / transversions, 4) if transversions > 0 else None

    density: List[ChromosomeDensity] = [
        ChromosomeDensity(
            chrom=row.chrom,
            count=int(row.count),
            min_pos=row.min_pos,
            max_pos=row.max_pos,
            mean_af=round(float(row.mean_af), 6) if row.mean_af is not None else None,
        )
        for row in sorted(density_rows, key=lambda r: _chrom_sort_key(r.chrom))
    ]

    def _round(value: Optional[float], digits: int) -> Optional[float]:
        return round(float(value), digits) if value is not None else None

    return SummaryResponse(
        job_id=job_id,
        chrom=canonical_chrom,
        total_variants=int(totals.total),
        variant_type_counts={
            "SNP": snps,
            "INSERTION": int(totals.insertions),
            "DELETION": int(totals.deletions),
            "MNP": int(totals.mnps),
            "COMPLEX": int(totals.complex),
        },
        transitions=transitions,
        transversions=transversions,
        ti_tv_ratio=ti_tv,
        avg_read_depth=_round(totals.avg_dp, 3),
        avg_allele_frequency=_round(totals.avg_af, 6),
        avg_quality=_round(totals.avg_qual, 3),
        chromosome_density=density,
        af_spectrum=AlleleFrequencySpectrum(
            rare=int(totals.af_rare),
            low_freq=int(totals.af_low),
            common=int(totals.af_common),
            unknown=int(totals.af_unknown),
        ),
        query_time_ms=query_time_ms,
    )


@router.get(
    "/fasta/{job_id}",
    response_model=FastaSummaryResponse,
    summary="Contig lengths, GC content, N-base counts and MD5 checksums for a FASTA job",
)
def fasta_summary(job_id: uuid.UUID, db: Session = Depends(get_db)) -> FastaSummaryResponse:
    job = db.get(DatasetJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.file_type != FileType.FASTA:
        raise HTTPException(status_code=400, detail=f"Job {job_id} is not a FASTA job")

    contigs = db.scalars(
        select(FastaContig).where(FastaContig.job_id == job_id).order_by(FastaContig.id)
    ).all()
    return FastaSummaryResponse(
        job_id=job.id,
        filename=job.filename,
        status=job.status,
        total_contigs=len(contigs),
        total_bases=sum(c.sequence_length for c in contigs),
        contigs=[FastaContigOut.model_validate(c) for c in contigs],
    )
