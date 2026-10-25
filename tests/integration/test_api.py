"""HTTP API against real PostgreSQL and S3. Only the broker is faked (the enqueue hook is
replaced), and the pipeline stages are run in-process, so these tests are about the API
contract; test_api_e2e.py covers the same flow through real Celery workers."""

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from variant_etl.api.app import create_app
from variant_etl.api.deps import enqueue_dependency
from variant_etl.config import get_settings
from variant_etl.pipeline import jobstate
from variant_etl.pipeline.tasks import run_chunk, run_split

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def enqueued() -> list[str]:
    return []


@pytest.fixture(scope="module")
def client(enqueued, engine) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[enqueue_dependency] = lambda: enqueued.append
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def truth(data_dir):
    return json.loads((data_dir / "truth.json").read_text())


def upload_via_api(client: TestClient, name: str, data: bytes) -> str:
    """The real client flow: ask for a presigned URL, then PUT straight to object storage."""
    response = client.post("/api/v1/uploads", json={"filename": name})
    assert response.status_code == 200, response.text
    body = response.json()
    put = httpx.put(body["upload_url"], content=data, timeout=30)
    assert put.status_code == 200, put.text
    return str(body["key"])


@pytest.fixture(scope="module")
def completed_job(client, engine, storage, data_dir) -> Iterator[str]:
    settings = get_settings()
    original_chunk_size = settings.chunk_size
    settings.chunk_size = 1000  # 3000 records -> 3 chunks, so pagination crosses chunks
    try:
        # Jobs are deduplicated by content. The same fixture may already have been processed
        # through the web UI's "sample data" button or `make demo`, and this fixture drives the
        # pipeline stages itself, so it needs a job of its own: a FASTA header comment changes
        # the checksum without changing the sequence (pyfaidx keys on the first word).
        ref = (data_dir / "ref.fa").read_bytes()
        unique_ref = ref.replace(b">chr1\n", b">chr1 test-run-%s\n" % uuid.uuid4().hex.encode(), 1)
        assert unique_ref != ref
        vcf_key = upload_via_api(client, "variants.vcf", (data_dir / "variants.vcf").read_bytes())
        ref_key = upload_via_api(client, "ref.fa", unique_ref)
        created = client.post("/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": ref_key})
        job_id = created.json()["id"]
        keys, reference_key = run_split(job_id)
        for index, key in enumerate(keys):
            run_chunk(job_id, index, key, reference_key)
        jobstate.finalize(engine, uuid.UUID(job_id))
        yield job_id
    finally:
        settings.chunk_size = original_chunk_size
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        storage.delete_prefix(f"chunks/{job_id}/")
        for key in (vcf_key, ref_key):
            storage.delete_prefix(key.rsplit("/", 1)[0] + "/")


# --- uploads and job creation ---------------------------------------------------------------


def test_presigned_url_is_signed_for_the_public_endpoint(client):
    body = client.post("/api/v1/uploads", json={"filename": "../../etc/pass wd.vcf"}).json()
    assert body["upload_url"].startswith(get_settings().s3_public_endpoint_url)
    assert body["key"].startswith("uploads/") and body["key"].endswith("/pass_wd.vcf")
    assert ".." not in body["key"]
    assert body["method"] == "PUT" and body["expires_in"] == get_settings().presign_expires_seconds


def test_job_creation_is_queued_enqueued_and_deduplicated_by_content(
    client, enqueued, storage, engine, data_dir
):
    vcf = (data_dir / "variants.vcf").read_bytes() + b"\n# unique %s\n" % uuid.uuid4().hex.encode()
    ref = (data_dir / "ref.fa").read_bytes()
    vcf_key, ref_key = upload_via_api(client, "a.vcf", vcf), upload_via_api(client, "r.fa", ref)
    try:
        first = client.post("/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": ref_key})
        assert first.status_code == 202
        job = first.json()
        assert job["status"] == "queued" and job["partial"] is True
        assert job["deduplicated"] is False
        assert job["progress"]["chunks_total"] is None
        assert enqueued[-1] == job["id"]

        # Same bytes under brand-new keys: same content checksum, same job, nothing enqueued.
        n = len(enqueued)
        vcf_key2, ref_key2 = (
            upload_via_api(client, "b.vcf", vcf),
            upload_via_api(client, "s.fa", ref),
        )
        again = client.post("/api/v1/jobs", json={"vcf_key": vcf_key2, "reference_key": ref_key2})
        assert again.status_code == 200
        assert again.json()["id"] == job["id"] and again.json()["deduplicated"] is True
        assert len(enqueued) == n
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job["id"]})
        for key in (vcf_key, ref_key, vcf_key2, ref_key2):
            storage.delete_prefix(key.rsplit("/", 1)[0] + "/")


def test_resubmitting_a_failed_job_starts_a_fresh_one(client, enqueued, storage, engine):
    vcf_key = upload_via_api(client, "x.vcf", b"#CHROM\tPOS\n" + uuid.uuid4().hex.encode())
    ref_key = upload_via_api(client, "x.fa", b">c\nACGT\n")
    try:
        old = client.post(
            "/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": ref_key}
        ).json()
        jobstate.mark_failed(engine, uuid.UUID(old["id"]), "boom")

        new = client.post("/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": ref_key})
        assert new.status_code == 202
        assert new.json()["id"] != old["id"] and new.json()["status"] == "queued"
        assert client.get(f"/api/v1/jobs/{old['id']}").status_code == 404
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE vcf_key = :k"), {"k": vcf_key})
        for key in (vcf_key, ref_key):
            storage.delete_prefix(key.rsplit("/", 1)[0] + "/")


def test_job_is_removed_and_503_returned_when_the_queue_is_down(client, enqueued, storage, engine):
    def broken(job_id: str) -> None:
        raise ConnectionError("redis down")

    client.app.dependency_overrides[enqueue_dependency] = lambda: broken
    vcf_key = upload_via_api(client, "q.vcf", b"#CHROM\n" + uuid.uuid4().hex.encode())
    ref_key = upload_via_api(client, "q.fa", b">c\nACGT\n")
    try:
        response = client.post("/api/v1/jobs", json={"vcf_key": vcf_key, "reference_key": ref_key})
        assert response.status_code == 503
        with engine.connect() as conn:
            left = conn.execute(
                text("SELECT count(*) FROM jobs WHERE vcf_key = :k"), {"k": vcf_key}
            ).scalar()
        assert left == 0
    finally:
        client.app.dependency_overrides[enqueue_dependency] = lambda: enqueued.append
        for key in (vcf_key, ref_key):
            storage.delete_prefix(key.rsplit("/", 1)[0] + "/")


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"vcf_key": "uploads/none/x.vcf", "reference_key": "uploads/none/y.fa"}, 422),
        ({"vcf_key": "chunks/secret/000000.vcf", "reference_key": "uploads/none/y.fa"}, 422),
        ({"vcf_key": "uploads/none/x.vcf"}, 422),
    ],
)
def test_invalid_job_requests(client, payload, status):
    assert client.post("/api/v1/jobs", json=payload).status_code == status


def test_unknown_and_malformed_job_ids(client):
    assert client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/v1/jobs/not-a-uuid").status_code == 422
    assert client.get(f"/api/v1/jobs/{uuid.uuid4()}/stats/summary").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# --- job status -----------------------------------------------------------------------------


def test_completed_job_status(client, completed_job, truth):
    job = client.get(f"/api/v1/jobs/{completed_job}").json()
    assert job["status"] == "completed" and job["partial"] is False
    assert job["counts"]["total"] == truth["records_total"]
    assert job["counts"]["accepted"] == truth["records_accepted"]
    assert job["counts"]["rejected"] == truth["records_rejected"]
    assert job["progress"] == {
        "chunks_total": 3,
        "chunks_done": 3,
        "chunks_failed": 0,
        "fraction": 1.0,
    }
    assert job["rejected_by_reason"] == truth["rejected_by_reason"]
    assert job["duration_seconds"] is not None and job["duration_seconds"] >= 0
    assert job["error"] is None and job["failed_chunks"] == []


def test_rejected_records_are_paged_in_line_order(client, completed_job, truth):
    collected, after = [], 0
    while True:
        page = client.get(
            f"/api/v1/jobs/{completed_job}/rejected", params={"limit": 10, "after_line": after}
        ).json()
        collected += [[r["line_no"], r["reason"]] for r in page["items"]]
        if page["next_after_line"] is None:
            break
        after = page["next_after_line"]
    assert collected == truth["rejected_lines"]


# --- analytics ------------------------------------------------------------------------------


def test_summary_matches_ground_truth(client, completed_job, truth):
    summary = client.get(f"/api/v1/jobs/{completed_job}/stats/summary").json()
    assert summary["partial"] is False and summary["job_status"] == "completed"
    assert summary["variants"] == truth["rows_total"]
    assert summary["by_type"] == {k: v for k, v in truth["rows_by_type"].items() if v}
    assert summary["transitions"] == truth["snv_transitions"]
    assert summary["transversions"] == truth["snv_transversions"]
    assert summary["titv_ratio"] == pytest.approx(truth["titv"], abs=1e-3)
    assert summary["titv_ratio"] == pytest.approx(truth["params"]["titv"], abs=0.05)
    assert summary["records"]["rejected_percent"] == pytest.approx(
        100 * truth["records_rejected"] / truth["records_total"], abs=1e-3
    )
    assert {r: s["count"] for r, s in summary["rejected_by_reason"].items()} == truth[
        "rejected_by_reason"
    ]
    assert 0 < summary["normalized_percent"] < 100


def test_chromosome_stats(client, completed_job, truth):
    stats = client.get(f"/api/v1/jobs/{completed_job}/stats/chromosomes").json()
    names = [c["chrom"] for c in stats["chromosomes"]]
    assert names == ["chr1", "chr2", "chr3"]
    assert sum(c["variants"] for c in stats["chromosomes"]) == truth["rows_total"]
    for c in stats["chromosomes"]:
        assert c["length"] == 20_000
        assert c["density_per_mb"] == pytest.approx(c["variants"] / 0.02, abs=0.01)


def test_af_histogram(client, completed_job, truth):
    hist = client.get(f"/api/v1/jobs/{completed_job}/stats/af-histogram?bins=20").json()
    assert len(hist["bins"]) == 20
    assert hist["bins"][0]["bin_start"] == 0.0 and hist["bins"][-1]["bin_end"] == 1.0
    assert sum(b["count"] for b in hist["bins"]) + hist["missing_af"] == truth["rows_total"]
    assert hist["missing_af"] == 0
    assert hist["bins"][0]["count"] > hist["bins"][-1]["count"]  # AF is skewed towards rare

    single = client.get(f"/api/v1/jobs/{completed_job}/stats/af-histogram?bins=1").json()
    assert single["bins"] == [{"bin_start": 0.0, "bin_end": 1.0, "count": truth["rows_total"]}]


@pytest.mark.parametrize("bins", [0, 201, -1])
def test_af_histogram_bins_are_validated(client, completed_job, bins):
    response = client.get(f"/api/v1/jobs/{completed_job}/stats/af-histogram?bins={bins}")
    assert response.status_code == 422


def walk(client, job_id, **params):
    items, cursor, pages = [], None, 0
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        page = client.get(f"/api/v1/jobs/{job_id}/variants", params=query).json()
        items += page["items"]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            return items, pages


def test_keyset_pagination_visits_every_row_once_in_order(client, completed_job, truth):
    items, pages = walk(client, completed_job, limit=500)
    assert pages == -(-truth["rows_total"] // 500)
    assert len(items) == truth["rows_total"]
    ids = [v["id"] for v in items]
    assert len(set(ids)) == len(ids)
    keys = [(v["chrom"], v["pos"], v["id"]) for v in items]
    assert keys == sorted(keys)


def test_variant_filters(client, completed_job):
    region, _ = walk(client, completed_job, chrom="chr2", start=5000, end=6000, limit=50)
    assert region and all(v["chrom"] == "chr2" and 5000 <= v["pos"] <= 6000 for v in region)
    dels, _ = walk(client, completed_job, type="DEL", limit=1000)
    assert dels and all(v["variant_type"] == "DEL" for v in dels)
    assert any(v["was_normalized"] for v in dels)


def test_variant_query_validation(client, completed_job):
    base = f"/api/v1/jobs/{completed_job}/variants"
    assert client.get(base, params={"start": 10, "end": 5}).status_code == 422
    assert client.get(base, params={"cursor": "%%%"}).status_code == 422
    assert client.get(base, params={"type": "NOPE"}).status_code == 422
    assert client.get(base, params={"limit": 0}).status_code == 422
    assert client.get(base, params={"limit": 1001}).status_code == 422


def test_partial_results_are_flagged(client, engine, storage, completed_job):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET status = 'failed', error = 'x' WHERE id = :id"),
            {"id": completed_job},
        )
    try:
        summary = client.get(f"/api/v1/jobs/{completed_job}/stats/summary").json()
        assert summary["partial"] is True and summary["job_status"] == "failed"
        assert summary["variants"] > 0  # still queryable
        page = client.get(f"/api/v1/jobs/{completed_job}/variants?limit=1").json()
        assert page["partial"] is True
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET status = 'completed', error = NULL WHERE id = :id"),
                {"id": completed_job},
            )


def test_openapi_documents_examples(client):
    schema = client.get("/openapi.json").json()
    for name in ("JobOut", "SummaryStats", "VariantPage", "UploadResponse", "AfHistogram"):
        assert schema["components"]["schemas"][name]["examples"], name
