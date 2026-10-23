"""Full path through the running compose stack: presigned upload -> API -> Celery worker(s)
-> PostgreSQL -> analytics, checked against the generator's ground truth.

Needs `make up` (the API on :8000 plus real worker containers).
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from tests.integration.conftest import unavailable
from variant_etl.pipeline.tasks import chunk_key, process_chunk

pytestmark = pytest.mark.integration

API_URL = os.environ.get("API_URL", "http://localhost:8000")
ROOT = Path(__file__).resolve().parents[2]
RECORDS = 250_000  # 3 chunks at the default CHUNK_SIZE of 100_000


@pytest.fixture(scope="module")
def api(engine, storage) -> httpx.Client:
    client = httpx.Client(base_url=API_URL, timeout=120)
    try:
        client.get("/health").raise_for_status()
    except httpx.HTTPError as exc:
        unavailable(f"API at {API_URL}: {exc.__class__.__name__}")
    return client


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("e2e-data")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_data.py"),
            "--contigs", "3",
            "--contig-length", "1_000_000",
            "--variants", str(RECORDS),
            "--titv", "2.0",
            "--error-rate", "0.005",
            "--seed", "123",
            "--out-dir", str(out),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return out


def upload(api: httpx.Client, path: Path) -> str:
    body = api.post("/api/v1/uploads", json={"filename": path.name}).json()
    httpx.put(body["upload_url"], content=path.read_bytes(), timeout=300).raise_for_status()
    return str(body["key"])


@pytest.fixture(scope="module")
def finished_job(api, dataset, engine, storage):
    vcf_key, reference_key = upload(api, dataset / "variants.vcf"), upload(api, dataset / "ref.fa")
    created = api.post("/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": reference_key})
    assert created.status_code in (200, 202), created.text
    job = created.json()

    deadline = time.monotonic() + 600
    while job["status"] not in ("completed", "failed"):
        assert time.monotonic() < deadline, f"job stuck: {job}"
        time.sleep(1)
        job = api.get(f"/api/v1/jobs/{job['id']}").json()
    yield job, reference_key
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job["id"]})
    storage.delete_prefix(f"chunks/{job['id']}/")
    for key in (vcf_key, reference_key):
        storage.delete_prefix(key.rsplit("/", 1)[0] + "/")


@pytest.fixture(scope="module")
def truth(dataset):
    return json.loads((dataset / "truth.json").read_text())


def test_job_completes_with_the_expected_counts(finished_job, truth):
    job, _ = finished_job
    assert job["status"] == "completed", job["error"]
    assert job["counts"]["total"] == truth["records_total"] == RECORDS
    assert job["counts"]["rejected"] == truth["records_rejected"]
    assert job["progress"]["chunks_total"] == 3 and job["progress"]["chunks_done"] == 3
    assert job["rejected_by_reason"] == truth["rejected_by_reason"]


def test_stats_match_ground_truth_including_titv(api, finished_job, truth):
    job, _ = finished_job
    summary = api.get(f"/api/v1/jobs/{job['id']}/stats/summary").json()
    assert summary["variants"] == truth["rows_total"]
    assert summary["by_type"] == {k: v for k, v in truth["rows_by_type"].items() if v}
    assert summary["transitions"] == truth["snv_transitions"]
    assert summary["transversions"] == truth["snv_transversions"]
    assert summary["titv_ratio"] == pytest.approx(2.0, abs=0.02)


def test_chunk_redelivery_and_forced_reprocessing_never_duplicate_rows(engine, finished_job, truth):
    job, reference_key = finished_job
    job_id = job["id"]

    def rows() -> int:
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT count(*) FROM variants WHERE job_id = :j"), {"j": job_id}
            ).scalar_one()

    assert rows() == truth["rows_total"]
    args = (job_id, 1, chunk_key(job_id, 1), reference_key)

    # 1. The broker redelivers a chunk that already succeeded (worker died before the ack).
    assert process_chunk.apply(args=args).get()["skipped"] is True
    assert rows() == truth["rows_total"]

    # 2. Force a genuine re-run: forget the completion marker, leaving the old rows in place.
    #    The loader must replace them (delete + COPY in one transaction), not add to them.
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM job_chunks WHERE job_id = :j AND chunk_index = 1"), {"j": job_id}
        )
    assert process_chunk.apply(args=args).get()["status"] == "done"
    assert rows() == truth["rows_total"]


def test_paginating_everything_returns_each_row_once(api, finished_job, truth):
    job, _ = finished_job
    seen, cursor = 0, None
    while True:
        params = {"limit": 1000, **({"cursor": cursor} if cursor else {})}
        page = api.get(f"/api/v1/jobs/{job['id']}/variants", params=params).json()
        seen += len(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == truth["rows_total"]


def test_resubmitting_the_same_files_returns_the_same_job(api, finished_job, dataset, storage):
    job, _ = finished_job
    # Same bytes, new object keys.
    vcf_key, reference_key = upload(api, dataset / "variants.vcf"), upload(api, dataset / "ref.fa")
    try:
        again = api.post("/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": reference_key})
        assert again.status_code == 200
        assert again.json()["id"] == job["id"] and again.json()["deduplicated"] is True
    finally:
        for key in (vcf_key, reference_key):
            storage.delete_prefix(key.rsplit("/", 1)[0] + "/")


def test_random_job_id_is_404(api):
    assert api.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404
