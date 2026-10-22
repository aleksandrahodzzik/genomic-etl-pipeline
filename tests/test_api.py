"""API + pipeline integration tests (SQLite, fake S3, fake Celery dispatch)."""

import hashlib
import uuid

import pytest

from app.etl.loader import bulk_insert_variants, naive_insert_variants, transform_records
from app.etl.parser import parse_vcf_stream
from app.models import DatasetJob, GenomicVariant
from app.worker.tasks import JobNotFoundError, run_etl_pipeline_sync


def upload(client, name: str, content: str):
    return client.post(
        "/api/v1/etl/upload", files={"file": (name, content.encode(), "text/plain")}
    )


@pytest.fixture()
def processed_vcf_job(client, fake_task, fake_storage, session_factory, sample_vcf_text):
    response = upload(client, "sample.vcf", sample_vcf_text)
    assert response.status_code == 202
    job_id = response.json()["id"]
    result = run_etl_pipeline_sync(job_id, session_factory=session_factory, storage=fake_storage)
    assert result["status"] == "COMPLETED"
    return job_id


# -------------------------------- health ------------------------------- #
def test_health_all_up(client, session_factory):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["postgres"]["status"] == "up"
    assert body["redis"]["status"] == "up"
    assert body["minio"]["status"] == "up"


def test_health_degraded_when_redis_down(client, monkeypatch):
    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.main.check_redis", broken)
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["redis"]["status"] == "down"
    assert "redis down" in body["redis"]["detail"]


# -------------------------------- upload ------------------------------- #
def test_upload_vcf_creates_job_and_dispatches_task(client, fake_task, fake_storage):
    response = upload(client, "my sample.vcf", "##fileformat=VCFv4.2\n")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["file_type"] == "VCF"
    assert body["filename"] == "my_sample.vcf"
    assert body["s3_key"] in fake_storage.objects
    assert fake_task.dispatched == [body["id"]]


def test_upload_fasta_extensions(client, fake_task):
    for name in ("ref.fasta", "ref.fa"):
        response = upload(client, name, ">a\nACGT\n")
        assert response.status_code == 202
        assert response.json()["file_type"] == "FASTA"
    assert len(fake_task.dispatched) == 2


def test_upload_rejects_unsupported_extension(client, fake_task):
    response = upload(client, "notes.txt", "hello")
    assert response.status_code == 415
    assert fake_task.dispatched == []


def test_upload_sanitizes_path_traversal(client, fake_storage):
    response = upload(client, "../../etc/passwd.vcf", "##x\n")
    assert response.status_code == 202
    assert ".." not in response.json()["s3_key"]


def test_task_dispatch_failure_marks_job_failed(client, monkeypatch, session_factory):
    class BrokenTask:
        def delay(self, job_id):
            raise ConnectionError("broker down")

    monkeypatch.setattr("app.api.routes_etl.process_genomic_dataset_task", BrokenTask())
    response = upload(client, "a.vcf", "##x\n")
    assert response.status_code == 503
    with session_factory() as session:
        job = session.query(DatasetJob).one()
        assert job.status == "FAILED"
        assert "broker down" in job.error_message


# ------------------------------ demo + jobs ---------------------------- #
def test_demo_endpoint_generates_dataset_and_pipeline_processes_it(
    client, fake_task, fake_storage, session_factory
):
    response = client.post("/api/v1/etl/demo-100k", params={"num_variants": 1000})
    assert response.status_code == 202
    job = response.json()
    assert job["file_type"] == "VCF"
    assert fake_task.dispatched == [job["id"]]
    assert fake_storage.objects[job["s3_key"]].startswith(b"##fileformat=VCFv4.2")

    result = run_etl_pipeline_sync(job["id"], session_factory=session_factory, storage=fake_storage)
    assert result["status"] == "COMPLETED"
    assert result["total_records"] >= 1000
    assert result["rejected_records"] > 0  # ~1.5% intentionally invalid rows
    assert result["valid_records"] + result["rejected_records"] == result["total_records"]


def test_demo_endpoint_validates_bounds(client):
    assert client.post("/api/v1/etl/demo-100k", params={"num_variants": 10}).status_code == 422
    assert client.post("/api/v1/etl/demo-100k", params={"num_variants": 600000}).status_code == 422


def test_job_progress_metrics_after_processing(client, processed_vcf_job):
    body = client.get(f"/api/v1/etl/jobs/{processed_vcf_job}").json()
    assert body["status"] == "COMPLETED"
    assert body["total_records"] == 12
    assert body["valid_records"] == 8
    assert body["rejected_records"] == 4
    assert body["batches_processed"] == 1
    assert body["processing_time_ms"] > 0
    assert body["throughput_records_per_sec"] > 0
    assert body["completed_at"] is not None
    assert body["error_message"] is None


def test_list_jobs_and_filter(client, processed_vcf_job):
    upload(client, "pending.vcf", "##x\n")
    everything = client.get("/api/v1/etl/jobs").json()
    assert everything["total"] == 2
    completed = client.get("/api/v1/etl/jobs", params={"status": "COMPLETED"}).json()
    assert completed["total"] == 1
    assert completed["items"][0]["id"] == processed_vcf_job
    assert client.get("/api/v1/etl/jobs", params={"status": "BOGUS"}).status_code == 422


def test_get_unknown_job_is_404(client):
    assert client.get(f"/api/v1/etl/jobs/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/v1/etl/jobs/not-a-uuid").status_code == 422


# ------------------------------- analytics ----------------------------- #
def test_variants_query_all_and_normalization(client, processed_vcf_job):
    body = client.get("/api/v1/analytics/variants", params={"job_id": processed_vcf_job}).json()
    assert body["total"] == 8
    assert body["query_time_ms"] >= 0
    first = body["items"][0]
    assert first["chrom"] == "chr1"
    assert (first["pos"], first["ref_allele"], first["alt_allele"]) == (102, "C", "G")
    assert first["variant_type"] == "SNP"
    assert first["is_transition"] is False
    assert {item["chrom"] for item in body["items"]} == {
        "chr1", "chr2", "chr3", "chr7", "chrX", "chrM",
    }


def test_variants_region_query(client, processed_vcf_job):
    body = client.get(
        "/api/v1/analytics/variants",
        params={"chrom": "2", "start_pos": 150, "end_pos": 250},
    ).json()
    assert body["total"] == 1
    assert body["items"][0]["pos"] == 200


def test_variants_filters(client, processed_vcf_job):
    def query(**params):
        return client.get("/api/v1/analytics/variants", params=params).json()

    assert query(variant_type="insertion")["total"] == 1
    assert query(variant_type="DELETION")["total"] == 1
    assert query(variant_type="SNP")["total"] == 6
    assert query(min_af=0.2)["total"] == 3  # 0.3, 0.5, 0.9
    assert query(max_af=0.01)["total"] == 1  # 0.004
    assert query(min_dp=80)["total"] == 2  # DP=100, DP=80
    assert query(min_qual=70)["total"] == 4  # 70, 80, 99 (multi-allelic site x2)
    assert query(filter_status="LowQual")["total"] == 1


def test_variants_pagination(client, processed_vcf_job):
    page1 = client.get("/api/v1/analytics/variants", params={"limit": 3, "offset": 0}).json()
    page2 = client.get("/api/v1/analytics/variants", params={"limit": 3, "offset": 3}).json()
    assert page1["total"] == page2["total"] == 8
    assert len(page1["items"]) == 3 and len(page2["items"]) == 3
    assert not {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]}


@pytest.mark.parametrize(
    "params",
    [
        {"chrom": "chrBogus"},
        {"variant_type": "WEIRD"},
        {"start_pos": 500, "end_pos": 100},
        {"min_af": 1.5},
        {"limit": 0},
        {"limit": 5000},
    ],
)
def test_variants_invalid_parameters(client, params):
    assert client.get("/api/v1/analytics/variants", params=params).status_code == 422


def test_summary_statistics(client, processed_vcf_job):
    body = client.get("/api/v1/analytics/summary", params={"job_id": processed_vcf_job}).json()
    assert body["total_variants"] == 8
    assert body["variant_type_counts"] == {
        "SNP": 6, "INSERTION": 1, "DELETION": 1, "MNP": 0, "COMPLEX": 0,
    }
    assert body["transitions"] == 3
    assert body["transversions"] == 3
    assert body["ti_tv_ratio"] == pytest.approx(1.0)
    assert body["avg_read_depth"] == pytest.approx((100 + 50 + 30 + 80 + 20 + 77 + 77 + 11) / 8)
    assert body["avg_allele_frequency"] == pytest.approx(
        (0.02 + 0.3 + 0.004 + 0.06 + 0.5 + 0.1 + 0.02 + 0.9) / 8, abs=1e-5
    )
    assert body["avg_quality"] == pytest.approx((50 + 60 + 40 + 45 + 70 + 99 + 99 + 80) / 8)
    assert body["af_spectrum"] == {"rare": 1, "low_freq": 2, "common": 5, "unknown": 0}

    density = {d["chrom"]: d for d in body["chromosome_density"]}
    assert [d["chrom"] for d in body["chromosome_density"]] == [
        "chr1", "chr2", "chr3", "chr7", "chrX", "chrM",
    ]
    assert density["chr2"]["count"] == 2
    assert (density["chr2"]["min_pos"], density["chr2"]["max_pos"]) == (200, 300)
    assert density["chr2"]["mean_af"] == pytest.approx((0.3 + 0.004) / 2)


def test_summary_chrom_filter_and_empty(client, processed_vcf_job):
    body = client.get("/api/v1/analytics/summary", params={"chrom": "chr2"}).json()
    assert body["total_variants"] == 2
    assert body["ti_tv_ratio"] is None  # both SNPs are transitions -> no transversions
    assert body["transitions"] == 2

    empty = client.get("/api/v1/analytics/summary", params={"job_id": str(uuid.uuid4())}).json()
    assert empty["total_variants"] == 0
    assert empty["avg_read_depth"] is None
    assert empty["chromosome_density"] == []


# ------------------------------- FASTA --------------------------------- #
def test_fasta_pipeline_and_endpoint(client, fake_storage, session_factory, sample_fasta_text):
    job_id = upload(client, "ref.fasta", sample_fasta_text).json()["id"]
    result = run_etl_pipeline_sync(job_id, session_factory=session_factory, storage=fake_storage)
    assert result["status"] == "COMPLETED"
    assert result["total_records"] == 2

    body = client.get(f"/api/v1/analytics/fasta/{job_id}").json()
    assert body["total_contigs"] == 2
    assert body["total_bases"] == 18
    first = body["contigs"][0]
    assert first["contig_id"] == "seq1"
    assert first["sequence_length"] == 14
    assert first["n_base_count"] == 2
    assert first["gc_content_percent"] == pytest.approx(50.0)
    assert first["md5_checksum"] == hashlib.md5(b"ACGTNNACGTACGT").hexdigest()


def test_fasta_endpoint_errors(client, processed_vcf_job):
    assert client.get(f"/api/v1/analytics/fasta/{uuid.uuid4()}").status_code == 404
    assert client.get(f"/api/v1/analytics/fasta/{processed_vcf_job}").status_code == 400


# --------------------------- pipeline behaviours ----------------------- #
def test_pipeline_processes_multiple_batches(
    client, fake_storage, session_factory, sample_vcf_text, monkeypatch
):
    monkeypatch.setattr("app.worker.tasks.settings.ETL_BATCH_SIZE", 5)
    job_id = upload(client, "sample.vcf", sample_vcf_text).json()["id"]
    result = run_etl_pipeline_sync(job_id, session_factory=session_factory, storage=fake_storage)
    assert result["status"] == "COMPLETED"
    assert result["batches_processed"] == 3  # 12 records / 5 per batch
    assert result["total_records"] == 12


def test_pipeline_is_idempotent_on_rerun(client, fake_storage, session_factory, processed_vcf_job):
    again = run_etl_pipeline_sync(
        processed_vcf_job, session_factory=session_factory, storage=fake_storage
    )
    assert again["status"] == "COMPLETED"
    with session_factory() as session:
        assert session.query(GenomicVariant).count() == 8


def test_pipeline_failure_marks_job_failed_and_cleans_up(
    client, fake_storage, session_factory, sample_vcf_text, monkeypatch
):
    monkeypatch.setattr("app.worker.tasks.settings.ETL_BATCH_SIZE", 5)
    job_id = upload(client, "sample.vcf", sample_vcf_text).json()["id"]

    calls = {"n": 0}
    real_bulk_insert = bulk_insert_variants

    def flaky(session, rows, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated database failure")
        return real_bulk_insert(session, rows, *args, **kwargs)

    monkeypatch.setattr("app.worker.tasks.bulk_insert_variants", flaky)
    result = run_etl_pipeline_sync(job_id, session_factory=session_factory, storage=fake_storage)
    assert result["status"] == "FAILED"

    job = client.get(f"/api/v1/etl/jobs/{job_id}").json()
    assert job["status"] == "FAILED"
    assert "simulated database failure" in job["error_message"]
    with session_factory() as session:
        assert session.query(GenomicVariant).count() == 0  # no partial dataset exposed


def test_pipeline_fails_when_object_is_missing(client, fake_storage, session_factory):
    job_id = upload(client, "gone.vcf", "##x\n").json()["id"]
    fake_storage.objects.clear()
    result = run_etl_pipeline_sync(job_id, session_factory=session_factory, storage=fake_storage)
    assert result["status"] == "FAILED"


def test_pipeline_unknown_job_raises(session_factory, fake_storage):
    with pytest.raises(JobNotFoundError):
        run_etl_pipeline_sync(
            str(uuid.uuid4()), session_factory=session_factory, storage=fake_storage
        )


def test_empty_fasta_fails_job(client, fake_storage, session_factory):
    job_id = upload(client, "empty.fa", "no headers here\n").json()["id"]
    result = run_etl_pipeline_sync(job_id, session_factory=session_factory, storage=fake_storage)
    assert result["status"] == "FAILED"


# ------------------------------ bulk loaders --------------------------- #
def test_bulk_and_naive_loaders_produce_identical_rows(
    session_factory, db_session, sample_vcf_text
):
    job_a, job_b = uuid.uuid4(), uuid.uuid4()
    for job_id in (job_a, job_b):
        db_session.add(DatasetJob(id=job_id, filename="x.vcf", file_type="VCF", s3_key="k"))
    db_session.commit()

    records = [r for batch in parse_vcf_stream(sample_vcf_text.splitlines()) for r in batch]
    bulk_rows = transform_records(records, job_a).rows
    naive_rows = transform_records(records, job_b).rows

    assert bulk_insert_variants(db_session, bulk_rows, chunk_size=3, page_size=2) == 8
    db_session.commit()
    assert naive_insert_variants(db_session, naive_rows) == 8

    def fingerprint(job_id):
        rows = db_session.query(GenomicVariant).filter_by(job_id=job_id).all()
        return sorted(
            (r.chrom, r.pos, r.ref_allele, r.alt_allele, r.variant_type, r.allele_frequency)
            for r in rows
        )

    assert fingerprint(job_a) == fingerprint(job_b)
    assert len(fingerprint(job_a)) == 8


def test_bulk_insert_empty_is_noop(db_session):
    assert bulk_insert_variants(db_session, []) == 0
