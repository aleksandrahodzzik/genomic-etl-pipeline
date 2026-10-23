"""Fixtures for tests that need the real services (PostgreSQL, S3/MinIO, and for the API
tests the whole compose stack). Connection settings come from the same environment
variables as the application, defaulting to the ports `docker compose up` publishes.

If a service is unreachable the tests skip with a clear message, unless
REQUIRE_INTEGRATION=1 (set in CI) in which case they fail: a broken stack must not look green.
"""

import gzip
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from variant_etl.config import get_settings
from variant_etl.db.session import get_engine
from variant_etl.storage.s3 import ObjectStorage, get_storage

ROOT = Path(__file__).resolve().parents[2]


def unavailable(reason: str) -> None:
    if os.environ.get("REQUIRE_INTEGRATION") == "1":
        pytest.fail(f"integration stack required but unavailable: {reason}")
    pytest.skip(f"integration stack unavailable ({reason}); run `make up` first")


@pytest.fixture(scope="session", autouse=True)
def reference_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    get_settings().reference_cache_dir = str(tmp_path_factory.mktemp("reference-cache"))


@pytest.fixture(scope="session")
def engine() -> Engine:
    eng = get_engine()
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        unavailable(f"PostgreSQL: {exc.__class__.__name__}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")  # idempotent; the compose `migrate` service does the same
    return eng


@pytest.fixture(scope="session")
def storage() -> ObjectStorage:
    store = get_storage()
    try:
        store.ensure_bucket()
    except Exception as exc:
        unavailable(f"S3 endpoint: {exc.__class__.__name__}")
    return store


class Upload:
    def __init__(self, storage: ObjectStorage, engine: Engine) -> None:
        self.storage = storage
        self.engine = engine
        self.prefix = f"uploads/test-{uuid.uuid4().hex}"
        self.job_ids: list[uuid.UUID] = []

    def put(self, name: str, data: bytes) -> str:
        key = f"{self.prefix}/{name}"
        self.storage.put_bytes(key, data)
        return key

    def create_job(self, vcf_key: str, reference_key: str) -> str:
        job_id = uuid.uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (id, status, vcf_key, reference_key, checksum, total,"
                    " accepted, rejected, normalized)"
                    " VALUES (:id, 'queued', :v, :r, :c, 0, 0, 0, 0)"
                ),
                {"id": job_id, "v": vcf_key, "r": reference_key, "c": uuid.uuid4().hex * 2},
            )
        self.job_ids.append(job_id)
        return str(job_id)

    def cleanup(self) -> None:
        with self.engine.begin() as conn:
            for job_id in self.job_ids:
                conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        self.storage.delete_prefix(self.prefix)
        for job_id in self.job_ids:
            self.storage.delete_prefix(f"chunks/{job_id}/")


@pytest.fixture
def upload(storage: ObjectStorage, engine: Engine) -> Iterator[Upload]:
    helper = Upload(storage, engine)
    yield helper
    helper.cleanup()


@pytest.fixture
def fixture_job(upload: Upload, data_dir: Path) -> str:
    vcf_key = upload.put("variants.vcf", (data_dir / "variants.vcf").read_bytes())
    reference_key = upload.put("ref.fa", (data_dir / "ref.fa").read_bytes())
    return upload.create_job(vcf_key, reference_key)


@pytest.fixture
def gzipped_fixture_job(upload: Upload, data_dir: Path) -> str:
    vcf_key = upload.put("variants.vcf.gz", gzip.compress((data_dir / "variants.vcf").read_bytes()))
    reference_key = upload.put("ref.fa.gz", gzip.compress((data_dir / "ref.fa").read_bytes()))
    return upload.create_job(vcf_key, reference_key)
