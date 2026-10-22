"""ORM models: ETL job tracking, normalized variants and FASTA contig statistics."""

import uuid
from datetime import datetime, timezone
from typing import Optional

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
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# BIGINT identity on PostgreSQL; plain INTEGER on SQLite so it auto-increments in tests.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus:
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FileType:
    VCF = "VCF"
    FASTA = "FASTA"


class DatasetJob(Base):
    __tablename__ = "dataset_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)  # VCF | FASTA
    s3_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=JobStatus.PENDING
    )
    total_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    valid_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rejected_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    batches_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_time_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    throughput_records_per_sec: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class GenomicVariant(Base):
    __tablename__ = "genomic_variants"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("dataset_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    chrom: Mapped[str] = mapped_column(String(16), nullable=False)
    pos: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based
    variant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ref_allele: Mapped[str] = mapped_column(String(512), nullable=False)
    alt_allele: Mapped[str] = mapped_column(String(512), nullable=False)
    quality: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    filter_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PASS", server_default="PASS"
    )
    variant_type: Mapped[str] = mapped_column(String(16), nullable=False)
    read_depth: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    allele_frequency: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_transition: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        # Genomic region range queries: WHERE chrom = ? AND pos BETWEEN ? AND ?
        Index("ix_variants_chrom_pos", "chrom", "pos"),
        # Variant class + allele frequency filtering
        Index("ix_variants_type_af", "variant_type", "allele_frequency"),
        # Job-scoped quality filtering (also serves job_id-only lookups / cascades)
        Index("ix_variants_job_filter", "job_id", "filter_status"),
    )


class FastaContig(Base):
    __tablename__ = "fasta_contigs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("dataset_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contig_id: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    sequence_length: Mapped[int] = mapped_column(Integer, nullable=False)
    gc_content_percent: Mapped[float] = mapped_column(Float, nullable=False)
    n_base_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    md5_checksum: Mapped[str] = mapped_column(String(32), nullable=False)
