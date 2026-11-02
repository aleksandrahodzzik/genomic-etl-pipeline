"""Runtime configuration, read from environment variables (and `.env` for local runs)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://variants:variants@localhost:5432/variants"
    redis_url: str = "redis://localhost:6379/0"

    # Object storage is addressed through plain S3, so swapping MinIO for AWS S3, Garage or
    # SeaweedFS is a configuration change. Two endpoints are needed because the in-network
    # name (http://minio:9000) is not reachable from the client that uses a presigned URL.
    s3_endpoint_url: str = "http://localhost:9000"
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "variant-etl"
    s3_region: str = "us-east-1"
    presign_expires_seconds: int = 3600

    # Records per chunk: the unit of parallelism, of retry and of the database transaction.
    chunk_size: int = 100_000

    # Worker-local cache for reference FASTA files (pyfaidx needs a seekable file + .fai).
    reference_cache_dir: str = "/tmp/variant-etl-reference"

    # Full per-reason counts are always kept; only this many raw lines per chunk are stored.
    max_rejected_samples_per_chunk: int = 10_000

    # Public demo only (the Hugging Face Space sets these). The defaults change nothing.
    # More than 0: `POST /jobs` answers 429 while this many jobs are queued or running.
    max_active_jobs: int = 0
    # A directory holding variants.vcf + ref.fa; enables `POST /demo/synthetic`.
    demo_synthetic_dir: str = ""
    # Largest upload the demo accepts, in MB. Shown and checked in the web UI; the demo's
    # reverse proxy enforces it.
    demo_max_upload_mb: int = 0
    # A line about the demo's limits, shown in the web UI.
    demo_notice: str = ""
    # Jobs and files older than this are deleted by `python -m variant_etl.janitor`.
    demo_retention_minutes: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
