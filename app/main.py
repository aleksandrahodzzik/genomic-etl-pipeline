"""FastAPI application entry point."""

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.api import routes_analytics, routes_etl
from app.api.routes_etl import get_storage_client
from app.config import settings
from app.database import get_db, init_db
from app.schemas import ComponentHealth, HealthResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

DB_INIT_ATTEMPTS = 15
DB_INIT_DELAY_SECONDS = 2.0

DESCRIPTION = """
Asynchronous ETL pipeline and analytics service for genomic variant datasets.

* **ETL** - upload VCF/FASTA files (or generate a 100k-variant demo set); a Celery worker
  streams them from MinIO, validates, normalizes and bulk-loads them into PostgreSQL.
* **Analytics** - indexed region/class/frequency queries and real-time summary statistics
  such as the Ti/Tv ratio.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    for attempt in range(1, DB_INIT_ATTEMPTS + 1):
        try:
            init_db()
            logger.info("Database schema ready")
            break
        except Exception as exc:  # database may still be starting
            if attempt == DB_INIT_ATTEMPTS:
                raise
            logger.warning(
                "Database not ready (attempt %d/%d): %s", attempt, DB_INIT_ATTEMPTS, exc
            )
            time.sleep(DB_INIT_DELAY_SECONDS)
    yield


app = FastAPI(
    title="Genomic Data ETL Pipeline & Variant Analytics Service",
    description=DESCRIPTION,
    version=__version__,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "ETL", "description": "Dataset ingestion and job tracking"},
        {"name": "Analytics", "description": "Indexed variant queries and statistics"},
        {"name": "Operations", "description": "Health checks"},
    ],
)

app.include_router(routes_etl.router)
app.include_router(routes_analytics.router)


def check_redis() -> None:
    """Raise if the Redis broker is unreachable."""
    client = redis.Redis.from_url(
        settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        client.ping()
    finally:
        client.close()


def _probe(check) -> ComponentHealth:
    try:
        check()
        return ComponentHealth(status="up")
    except Exception as exc:
        return ComponentHealth(status="down", detail=f"{type(exc).__name__}: {exc}"[:300])


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Operations"],
    summary="Connectivity check for PostgreSQL, Redis and MinIO",
    responses={503: {"model": HealthResponse, "description": "A dependency is down"}},
)
def health(db: Session = Depends(get_db)):
    # The storage client is resolved inside the probe: constructing it contacts MinIO,
    # and an outage must be reported as "down" rather than failing the whole request.
    payload = HealthResponse(
        status="healthy",
        postgres=_probe(lambda: db.execute(text("SELECT 1"))),
        redis=_probe(check_redis),
        minio=_probe(lambda: get_storage_client().ping()),
    )
    if "down" in {
        payload.postgres.status,
        payload.redis.status,
        payload.minio.status,
    }:
        payload.status = "degraded"
        return JSONResponse(status_code=503, content=payload.model_dump())
    return payload
