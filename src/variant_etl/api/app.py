from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from variant_etl import __version__
from variant_etl.api.routers import jobs, stats
from variant_etl.db.session import get_engine

STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
Asynchronous ETL for VCF variant files, validated and normalized against a FASTA reference,
with analytics endpoints over the loaded variants.

1. `POST /api/v1/uploads` for each file, then `PUT` the bytes to the returned URL.
2. `POST /api/v1/jobs` with the two keys; poll `GET /api/v1/jobs/{id}`.
3. Query `/stats/*` and `/variants` (partial results are available, and flagged, while a
   job runs or if it failed).
"""


def create_app() -> FastAPI:
    app = FastAPI(title="Variant ETL", version=__version__, description=DESCRIPTION)
    app.include_router(jobs.router)
    app.include_router(stats.router)

    # A single static page (no build step, no JS framework) that drives the same public API
    # as any other client: presigned upload, create job, poll, read stats.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health", tags=["ops"], summary="Liveness and database connectivity")
    def health() -> dict[str, str]:
        # Only the database: Redis or S3 being down must not make the API restart-loop.
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(503, "database unavailable") from exc
        return {"status": "ok"}

    return app


app = create_app()
