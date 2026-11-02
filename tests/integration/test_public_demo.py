"""The public demo's additions (all off by default): the admission limit, the synthetic-records
endpoint and the janitor. They run against the shared test stack, so nothing here deletes
anything it did not create: the janitor's job query runs in a transaction that is rolled back,
and its file sweep is pointed at this test's own prefix."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from variant_etl.api.app import create_app
from variant_etl.api.deps import enqueue_dependency
from variant_etl.config import get_settings
from variant_etl.janitor import delete_expired_jobs


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    # Jobs are created but not run: these tests are about admission, not processing.
    app.dependency_overrides[enqueue_dependency] = lambda: lambda job_id: None
    return TestClient(app)


def active_jobs(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")
        ).scalar_one()


def test_a_full_demo_refuses_new_jobs_with_429(client, engine, upload, data_dir, monkeypatch):
    upload.create_job(upload.put("busy.vcf", b"x"), upload.put("busy.fa", b"x"))  # queued
    monkeypatch.setattr(get_settings(), "max_active_jobs", active_jobs(engine))
    # New content: the fixture files are also the seeded sample, and resubmitting an existing
    # job is answered from the existing one (200) even when the demo is full.
    vcf = (data_dir / "variants.vcf").read_bytes()
    cut = vcf.index(b"\n") + 1
    unique = vcf[:cut] + f"##test={uuid.uuid4().hex}\n".encode() + vcf[cut:]
    vcf_key = upload.put("variants.vcf", unique)
    ref_key = upload.put("ref.fa", (data_dir / "ref.fa").read_bytes())
    before = active_jobs(engine)

    response = client.post("/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": ref_key})

    assert response.status_code == 429
    assert "at most" in response.json()["detail"]
    assert active_jobs(engine) == before


def test_synthetic_endpoint_submits_a_fresh_copy_each_time(
    client, engine, storage, data_dir: Path, monkeypatch
):
    assert client.post("/api/v1/demo/synthetic").status_code == 404  # off by default

    monkeypatch.setattr(get_settings(), "demo_synthetic_dir", str(data_dir))
    monkeypatch.setattr(get_settings(), "max_active_jobs", 0)
    jobs = [client.post("/api/v1/demo/synthetic").json() for _ in range(2)]
    try:
        assert jobs[0]["id"] != jobs[1]["id"]  # a run marker defeats deduplication
        assert all(job["status"] == "queued" for job in jobs)
        stored = b"".join(storage.iter_blocks(jobs[0]["vcf_key"]))
        original = (data_dir / "variants.vcf").read_bytes()
        first, marker, rest = stored.split(b"\n", 2)
        assert marker.startswith(b"##demo_run=")
        assert first + b"\n" + rest == original
        demo = client.get("/api/v1/demo").json()
        assert demo["synthetic"] is True and demo["chunk_size"] == get_settings().chunk_size
    finally:
        with engine.begin() as conn:
            for job in jobs:
                conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job["id"]})
        for job in jobs:
            storage.delete_prefix(job["vcf_key"].rsplit("/", 1)[0] + "/")


def test_janitor_deletes_expired_jobs_but_keeps_the_sample(engine, upload):
    old, kept, fresh = (upload.create_job(f"uploads/{n}.vcf", f"uploads/{n}.fa") for n in "okf")
    with Session(engine) as session:
        session.execute(
            text("UPDATE jobs SET created_at = now() - interval '2 hours' WHERE id IN (:a, :b)"),
            {"a": old, "b": kept},
        )
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        deleted = delete_expired_jobs(session, cutoff, {uuid.UUID(kept)})
        remaining = set(session.scalars(text("SELECT id::text FROM jobs")))
        session.rollback()  # other jobs in a shared database may be old too
    assert deleted >= 1
    assert old not in remaining
    assert {kept, fresh} <= remaining


def test_janitor_file_sweep_skips_the_kept_prefixes(storage, upload):
    upload.put("expired.vcf", b"x")
    upload.put("keep/ref.fa", b"x")
    cutoff = datetime.now(UTC) + timedelta(minutes=1)  # everything just written is "older"

    deleted = storage.delete_older_than((upload.prefix + "/",), cutoff, (upload.prefix + "/keep/",))

    assert deleted == 1
    storage.head(f"{upload.prefix}/keep/ref.fa")  # still there
