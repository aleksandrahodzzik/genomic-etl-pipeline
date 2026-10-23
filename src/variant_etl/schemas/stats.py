import uuid

from pydantic import BaseModel, ConfigDict, Field


class Flagged(BaseModel):
    job_status: str
    partial: bool = Field(
        description="True unless the job completed: partial results stay queryable but incomplete."
    )


class RejectReasonStat(BaseModel):
    count: int
    percent_of_records: float


class RecordCounts(BaseModel):
    total: int
    accepted: int
    rejected: int
    rejected_percent: float


class SummaryStats(Flagged):
    job_id: uuid.UUID
    records: RecordCounts
    variants: int = Field(description="Normalized biallelic rows (>= accepted records).")
    by_type: dict[str, int]
    transitions: int
    transversions: int
    titv_ratio: float | None = Field(description="SNVs only; null when there are no transversions.")
    normalized: int
    normalized_percent: float = Field(description="Share of variant rows changed by normalization.")
    rejected_by_reason: dict[str, RejectReasonStat]

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "0b5f3f0e-6f55-4c53-8c33-0d7a2f4a9c11",
                    "job_status": "completed",
                    "partial": False,
                    "records": {
                        "total": 1000000,
                        "accepted": 995000,
                        "rejected": 5000,
                        "rejected_percent": 0.5,
                    },
                    "variants": 1080000,
                    "by_type": {"SNV": 910000, "INS": 80000, "DEL": 78000, "MNV": 12000},
                    "transitions": 606000,
                    "transversions": 304000,
                    "titv_ratio": 1.9934,
                    "normalized": 142311,
                    "normalized_percent": 13.18,
                    "rejected_by_reason": {
                        "ref_mismatch": {"count": 715, "percent_of_records": 0.0715}
                    },
                }
            ]
        }
    )


class ChromosomeStat(BaseModel):
    chrom: str
    length: int | None
    variants: int
    density_per_mb: float | None


class ChromosomeStats(Flagged):
    job_id: uuid.UUID
    chromosomes: list[ChromosomeStat]

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "0b5f3f0e-6f55-4c53-8c33-0d7a2f4a9c11",
                    "job_status": "completed",
                    "partial": False,
                    "chromosomes": [
                        {
                            "chrom": "chr1",
                            "length": 5000000,
                            "variants": 360000,
                            "density_per_mb": 72000.0,
                        }
                    ],
                }
            ]
        }
    )


class AfBin(BaseModel):
    bin_start: float
    bin_end: float
    count: int


class AfHistogram(Flagged):
    job_id: uuid.UUID
    bins: list[AfBin]
    missing_af: int = Field(description="Variant rows without an AF value.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_id": "0b5f3f0e-6f55-4c53-8c33-0d7a2f4a9c11",
                    "job_status": "completed",
                    "partial": False,
                    "bins": [
                        {"bin_start": 0.0, "bin_end": 0.05, "count": 512000},
                        {"bin_start": 0.05, "bin_end": 0.1, "count": 201000},
                    ],
                    "missing_af": 0,
                }
            ]
        }
    )


class VariantOut(BaseModel):
    id: int
    chrom: str
    pos: int
    ref: str
    alt: str
    qual: float | None
    filter: str | None
    variant_type: str
    is_transition: bool
    af: float | None
    was_normalized: bool


class VariantPage(Flagged):
    items: list[VariantOut]
    next_cursor: str | None = Field(
        description="Opaque. Pass as `cursor` to get the next page; null on the last page."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "job_status": "completed",
                    "partial": False,
                    "items": [
                        {
                            "id": 101,
                            "chrom": "chr1",
                            "pos": 4512,
                            "ref": "CA",
                            "alt": "C",
                            "qual": 38.2,
                            "filter": "PASS",
                            "variant_type": "DEL",
                            "is_transition": False,
                            "af": 0.0412,
                            "was_normalized": True,
                        }
                    ],
                    "next_cursor": "WyJjaHIxIiw0NTEyLDEwMV0=",
                }
            ]
        }
    )
