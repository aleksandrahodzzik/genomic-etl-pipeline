"""Shared fixtures: in-memory SQLite (StaticPool), fake S3 storage and fake Celery dispatch.

No Docker, PostgreSQL, Redis or MinIO is required to run the test suite.
"""

from typing import Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.api.routes_etl import get_storage_client
from app.database import Base, get_db
from app.main import app

SAMPLE_VCF = "\n".join(
    [
        "##fileformat=VCFv4.2",
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
        "1\t100\trs1\tATCG\tATGG\t50\tPASS\tDP=100;AF=0.02",
        "chr2\t200\t.\tA\tG\t60\tPASS\tDP=50;AF=0.3",
        "chr2\t300\t.\tC\tT\t40\tPASS\tDP=30;AF=0.004",
        "chrX\t400\t.\tA\tC\t45\tPASS\tDP=80;AF=0.06",
        "MT\t10\t.\tG\tGA\t70\tPASS\tDP=20;AF=0.5",
        "chr3\t500\t.\tA\tG,T\t99\tPASS\tDP=77;AF=0.1,0.02",
        "chr4\t0\t.\tA\tG\t10\tPASS\tDP=5",
        "chr5\t600\t.\tA\tA\t10\tPASS\tDP=5",
        "chr6\t700\t.\tA\tG\t-5\tPASS\tDP=5",
        "chrUn\t800\t.\tA\tG\t10\tPASS\tDP=5",
        "chr7\t900\t.\tAC\tA\t80\tLowQual\tDP=11;AF=0.9",
        "",
    ]
)
# 12 decomposed records: 8 valid (6 SNP: 3 Ti / 3 Tv, 1 insertion, 1 deletion), 4 rejected.

SAMPLE_FASTA = "\n".join(
    [
        ">seq1 first test contig",
        "ACGTNNACGT",
        "ACGT",
        ">seq2",
        "GGCC",
        "",
    ]
)


class FakeStorage:
    """In-memory stand-in for `S3StorageClient`."""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}

    def upload_fileobj(self, file_obj, s3_key: str) -> None:
        self.objects[s3_key] = file_obj.read()

    def upload_bytes(self, data: bytes, s3_key: str) -> None:
        self.objects[s3_key] = data

    def stream_lines(self, s3_key: str, chunk_size: int = 1024 * 1024) -> Iterator[str]:
        for line in self.objects[s3_key].decode("utf-8").splitlines():
            yield line

    def ping(self) -> bool:
        return True


class FakeTask:
    """Replaces the Celery task object; records `.delay()` calls instead of using Redis."""

    def __init__(self) -> None:
        self.dispatched: List[str] = []

    def delay(self, job_id: str) -> None:
        self.dispatched.append(job_id)


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def db_session(session_factory: sessionmaker) -> Iterator[Session]:
    session = session_factory()
    yield session
    session.close()


@pytest.fixture()
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture()
def fake_task(monkeypatch: pytest.MonkeyPatch) -> FakeTask:
    task = FakeTask()
    monkeypatch.setattr("app.api.routes_etl.process_genomic_dataset_task", task)
    return task


@pytest.fixture()
def client(
    session_factory: sessionmaker,
    fake_storage: FakeStorage,
    fake_task: FakeTask,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_storage_client] = lambda: fake_storage
    monkeypatch.setattr("app.main.get_storage_client", lambda: fake_storage)
    monkeypatch.setattr("app.main.check_redis", lambda: None)
    # Not used as a context manager, so the lifespan (real DB init) is not triggered.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def sample_vcf_text() -> str:
    return SAMPLE_VCF


@pytest.fixture()
def sample_fasta_text() -> str:
    return SAMPLE_FASTA
