"""ORM models. The Alembic migration in `alembic/versions` is the source of truth for DDL;
`alembic check` in CI fails if the two drift apart."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.QUEUED)
    vcf_key: Mapped[str] = mapped_column(Text, nullable=False)
    reference_key: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256 over (etag, size) of both inputs: identical inputs map to one job.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normalized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    # contig name -> length, captured from the reference .fai; used for density per Mb.
    contigs: Mapped[dict[str, int] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class JobChunk(Base):
    """One row per chunk, written in the same transaction as the chunk's variants.

    It is the idempotency marker (see `pipeline/loader.py`), the source of progress, and
    where full reject-reason counts live (the raw rejected lines are only sampled).
    """

    __tablename__ = "job_chunks"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # done | failed
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normalized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reject_reasons: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text)


class Variant(Base):
    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chrom: Mapped[str] = mapped_column(Text, nullable=False)
    pos: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    alt: Mapped[str] = mapped_column(Text, nullable=False)
    qual: Mapped[float | None] = mapped_column(Float)
    filter: Mapped[str | None] = mapped_column(Text)
    variant_type: Mapped[str] = mapped_column(String(8), nullable=False)  # SNV MNV INS DEL COMPLEX
    is_transition: Mapped[bool] = mapped_column(Boolean, nullable=False)
    af: Mapped[float | None] = mapped_column(Float)
    was_normalized: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        # Region queries: WHERE job_id = ? AND chrom = ? AND pos BETWEEN ? AND ?, and the
        # keyset-pagination order (chrom, pos, id) inside one job.
        Index("ix_variants_job_chrom_pos", "job_id", "chrom", "pos"),
        # Per-job counts by class (summary endpoint).
        Index("ix_variants_job_type", "job_id", "variant_type"),
    )


class RejectedRecord(Base):
    __tablename__ = "rejected_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_line: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_rejected_job_chunk", "job_id", "chunk_index"),)
