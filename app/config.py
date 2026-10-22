"""Application settings, loaded from environment variables / `.env`."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Defaults match `docker-compose.yml` port mappings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # PostgreSQL
    DATABASE_URL: str = (
        "postgresql+psycopg2://genomics:genomics_secret@localhost:5432/genomics_db"
    )

    # Redis (Celery broker + result backend)
    REDIS_URL: str = "redis://localhost:6379/0"

    # S3-compatible storage (MinIO)
    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET_NAME: str = "genomic-datasets"
    S3_REGION: str = "us-east-1"

    # ETL tuning
    ETL_BATCH_SIZE: int = 100_000  # records parsed/validated/normalized per batch
    DB_CHUNK_SIZE: int = 25_000  # rows per bulk-insert round trip within a batch
    DB_PAGE_SIZE: int = 10_000  # rows per INSERT statement (execute_values page_size)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
