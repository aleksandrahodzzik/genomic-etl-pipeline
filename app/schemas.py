"""Pydantic response/request schemas exposed through the OpenAPI document."""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    file_type: str = Field(description="VCF or FASTA")
    s3_key: str
    status: str = Field(description="PENDING | PROCESSING | COMPLETED | FAILED")
    total_records: int
    valid_records: int
    rejected_records: int
    batches_processed: int
    processing_time_ms: Optional[float] = None
    throughput_records_per_sec: Optional[float] = None
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


class JobListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[JobResponse]


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: uuid.UUID
    chrom: str
    pos: int
    variant_id: Optional[str] = None
    ref_allele: str
    alt_allele: str
    quality: Optional[float] = None
    filter_status: str
    variant_type: str
    read_depth: Optional[int] = None
    allele_frequency: Optional[float] = None
    is_transition: bool


class VariantQueryResponse(BaseModel):
    query_time_ms: float = Field(description="Database execution time (data + count)")
    total: int = Field(description="Total rows matching the filters")
    limit: int
    offset: int
    items: List[VariantOut]


class ChromosomeDensity(BaseModel):
    chrom: str
    count: int
    min_pos: Optional[int] = None
    max_pos: Optional[int] = None
    mean_af: Optional[float] = None


class AlleleFrequencySpectrum(BaseModel):
    rare: int = Field(description="AF < 0.01")
    low_freq: int = Field(description="0.01 <= AF <= 0.05")
    common: int = Field(description="AF > 0.05")
    unknown: int = Field(description="AF not annotated")


class SummaryResponse(BaseModel):
    job_id: Optional[uuid.UUID] = None
    chrom: Optional[str] = None
    total_variants: int
    variant_type_counts: Dict[str, int]
    transitions: int
    transversions: int
    ti_tv_ratio: Optional[float] = Field(
        default=None, description="transitions / transversions over SNPs"
    )
    avg_read_depth: Optional[float] = None
    avg_allele_frequency: Optional[float] = None
    avg_quality: Optional[float] = None
    chromosome_density: List[ChromosomeDensity]
    af_spectrum: AlleleFrequencySpectrum
    query_time_ms: float


class FastaContigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    contig_id: str
    description: Optional[str] = None
    sequence_length: int
    gc_content_percent: float
    n_base_count: int
    md5_checksum: str


class FastaSummaryResponse(BaseModel):
    job_id: uuid.UUID
    filename: str
    status: str
    total_contigs: int
    total_bases: int
    contigs: List[FastaContigOut]


class ComponentHealth(BaseModel):
    status: str = Field(description="up | down")
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    status: str = Field(description="healthy | degraded")
    postgres: ComponentHealth
    redis: ComponentHealth
    minio: ComponentHealth
