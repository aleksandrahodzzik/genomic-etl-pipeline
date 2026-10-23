import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200, examples=["sample.vcf.gz"])


class UploadResponse(BaseModel):
    key: str = Field(description="Object key to pass to POST /jobs once the upload is done.")
    upload_url: str = Field(description="Presigned URL: PUT the file body here, bypassing the API.")
    method: Literal["PUT"] = "PUT"
    expires_in: int = Field(description="Seconds until the URL stops working.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "key": "uploads/3f2b6c0e9a1d4c7e8b5f/sample.vcf.gz",
                    "upload_url": "http://localhost:9000/variant-etl/uploads/3f2b6c0e9a1d4c7e8b5f/"
                    "sample.vcf.gz?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=...",
                    "method": "PUT",
                    "expires_in": 3600,
                }
            ]
        }
    )


class JobCreate(BaseModel):
    vcf_key: str = Field(examples=["uploads/3f2b6c0e9a1d4c7e8b5f/sample.vcf.gz"])
    reference_key: str = Field(examples=["uploads/a81c0d2e44b94f10a7d2/ref.fa"])


class JobCounts(BaseModel):
    total: int = Field(description="VCF records seen (data lines, blank lines excluded).")
    accepted: int
    rejected: int
    normalized: int = Field(
        description="Variant rows that differ from the input as written: trimmed, "
        "left-aligned, or produced by splitting a multi-allelic site."
    )


class JobProgress(BaseModel):
    chunks_total: int | None = Field(description="Known once the split step has finished.")
    chunks_done: int
    chunks_failed: int
    fraction: float = Field(ge=0, le=1)


class FailedChunk(BaseModel):
    chunk_index: int
    error: str | None


class JobOut(BaseModel):
    id: uuid.UUID
    status: Literal["queued", "running", "completed", "failed"]
    vcf_key: str
    reference_key: str
    counts: JobCounts
    progress: JobProgress
    rejected_by_reason: dict[str, int]
    failed_chunks: list[FailedChunk]
    error: str | None
    partial: bool = Field(
        description="True unless the job completed: results exist but may be incomplete."
    )
    deduplicated: bool = Field(
        default=False, description="True when an identical earlier job was returned."
    )
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "0b5f3f0e-6f55-4c53-8c33-0d7a2f4a9c11",
                    "status": "completed",
                    "vcf_key": "uploads/3f2b6c0e9a1d4c7e8b5f/sample.vcf.gz",
                    "reference_key": "uploads/a81c0d2e44b94f10a7d2/ref.fa",
                    "counts": {
                        "total": 1000000,
                        "accepted": 995000,
                        "rejected": 5000,
                        "normalized": 142311,
                    },
                    "progress": {
                        "chunks_total": 10,
                        "chunks_done": 10,
                        "chunks_failed": 0,
                        "fraction": 1.0,
                    },
                    "rejected_by_reason": {"ref_mismatch": 715, "unknown_contig": 714},
                    "failed_chunks": [],
                    "error": None,
                    "partial": False,
                    "deduplicated": False,
                    "created_at": "2026-10-22T10:00:00Z",
                    "started_at": "2026-10-22T10:00:01Z",
                    "finished_at": "2026-10-22T10:00:41Z",
                    "duration_seconds": 40.0,
                }
            ]
        }
    )


class RejectedRecordOut(BaseModel):
    line_no: int
    chunk_index: int
    reason: str
    raw_line: str


class RejectedPage(BaseModel):
    items: list[RejectedRecordOut]
    next_after_line: int | None = Field(description="Pass as `after_line` to get the next page.")
    note: str = "Only a bounded sample of rejected lines is stored; reason counts are exact."
