"""The pipeline stages run in-process against real PostgreSQL and S3.

These exercise the same functions the Celery tasks call (`run_split`, `run_chunk`,
`finalize`), so failure and redelivery behaviour can be forced deterministically; the
API-driven test in test_api_e2e.py then proves the same through the full stack.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from variant_etl.config import get_settings
from variant_etl.pipeline import jobstate, tasks
from variant_etl.pipeline.chunking import read_chunk
from variant_etl.pipeline.loader import load_chunk
from variant_etl.pipeline.reference_store import open_reference
from variant_etl.pipeline.tasks import chunk_key, process_chunk, run_chunk, run_split
from variant_etl.pipeline.textio import iter_lines
from variant_etl.pipeline.transform import process_lines
from variant_etl.storage.s3 import get_storage

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    # The fixture has 3000 records; 1000 per chunk gives three chunks.
    monkeypatch.setattr(get_settings(), "chunk_size", 1000)


@pytest.fixture
def truth(data_dir):
    return json.loads((data_dir / "truth.json").read_text())


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def run_all_chunks(job_id):
    keys, reference_key = run_split(job_id)
    for index, key in enumerate(keys):
        run_chunk(job_id, index, key, reference_key)
    return keys, reference_key


def variant_count(engine, job_id, chunk_index=None):
    sql = "SELECT count(*) FROM variants WHERE job_id = :j"
    if chunk_index is not None:
        return scalar(engine, sql + " AND chunk_index = :c", j=job_id, c=chunk_index)
    return scalar(engine, sql, j=job_id)


def test_pipeline_output_matches_ground_truth(engine, storage, fixture_job, truth):
    keys, _ = run_all_chunks(fixture_job)
    assert len(keys) == 3
    assert jobstate.finalize(engine, uuid.UUID(fixture_job)) == "completed"

    job = (
        engine.connect()
        .execute(text("SELECT * FROM jobs WHERE id = :j"), {"j": fixture_job})
        .mappings()
        .one()
    )
    assert job["status"] == "completed"
    assert (job["total"], job["accepted"], job["rejected"]) == (
        truth["records_total"],
        truth["records_accepted"],
        truth["records_rejected"],
    )
    assert job["chunk_count"] == 3
    assert job["contigs"] == truth["contigs"]
    assert job["started_at"] is not None and job["finished_at"] >= job["started_at"]

    assert variant_count(engine, fixture_job) == truth["rows_total"]
    by_type = dict(
        engine.connect()
        .execute(
            text("SELECT variant_type, count(*) FROM variants WHERE job_id = :j GROUP BY 1"),
            {"j": fixture_job},
        )
        .all()
    )
    assert by_type == truth["rows_by_type"]
    transitions = scalar(
        engine,
        "SELECT count(*) FROM variants WHERE job_id = :j AND is_transition",
        j=fixture_job,
    )
    assert transitions == truth["snv_transitions"]

    rejected = (
        engine.connect()
        .execute(
            text("SELECT line_no, reason FROM rejected_records WHERE job_id = :j ORDER BY line_no"),
            {"j": fixture_job},
        )
        .all()
    )
    assert [[n, r] for n, r in rejected] == truth["rejected_lines"]


def test_gzipped_inputs_give_identical_results(engine, gzipped_fixture_job, truth):
    run_all_chunks(gzipped_fixture_job)
    jobstate.finalize(engine, uuid.UUID(gzipped_fixture_job))
    assert variant_count(engine, gzipped_fixture_job) == truth["rows_total"]


def test_redelivering_a_finished_chunk_adds_no_rows(engine, fixture_job):
    keys, reference_key = run_all_chunks(fixture_job)
    before = variant_count(engine, fixture_job)

    again = run_chunk(fixture_job, 1, keys[1], reference_key)

    assert again["skipped"] is True
    assert variant_count(engine, fixture_job) == before


def test_crash_just_before_commit_leaves_nothing_and_the_retry_loads_exactly_once(
    engine, fixture_job
):
    keys, reference_key = run_split(fixture_job)

    def crash() -> None:
        raise RuntimeError("worker killed right before COMMIT")

    with pytest.raises(RuntimeError):
        run_chunk(fixture_job, 0, keys[0], reference_key, before_commit=crash)

    # COPY already streamed every row, yet none is visible and there is no marker.
    assert variant_count(engine, fixture_job, 0) == 0
    assert scalar(engine, "SELECT count(*) FROM job_chunks WHERE job_id = :j", j=fixture_job) == 0

    run_chunk(fixture_job, 0, keys[0], reference_key)
    first = variant_count(engine, fixture_job, 0)
    assert first > 0

    # Forcing a full re-run (no skip) replaces rows instead of doubling them.
    storage = get_storage()
    first_line, body = read_chunk(iter_lines(storage.iter_blocks(keys[0])))
    result = process_lines(body, first_line, open_reference(storage, reference_key), 10_000)
    load_chunk(engine, uuid.UUID(fixture_job), 0, result)
    assert variant_count(engine, fixture_job, 0) == first


def test_celery_task_retries_transient_errors_then_succeeds_without_duplicates(
    engine, fixture_job, monkeypatch
):
    keys, reference_key = run_split(fixture_job)
    real = tasks.run_chunk
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("S3 connection reset")
        return real(*args, **kwargs)

    monkeypatch.setattr(tasks, "run_chunk", flaky)
    result = process_chunk.apply(args=(fixture_job, 0, keys[0], reference_key)).get()

    assert calls["n"] == 2
    assert result["status"] == "done"
    assert variant_count(engine, fixture_job, 0) == result["rows"]


def test_permanently_failing_chunk_fails_the_job_but_keeps_the_other_chunks(
    engine, fixture_job, truth
):
    keys, reference_key = run_split(fixture_job)
    run_chunk(fixture_job, 0, keys[0], reference_key)
    run_chunk(fixture_job, 2, keys[2], reference_key)

    # Chunk 1's object is missing: a permanent error, not worth retrying.
    result = process_chunk.apply(
        args=(fixture_job, 1, chunk_key(fixture_job, 99), reference_key)
    ).get()
    assert result["status"] == "failed"

    assert jobstate.finalize(engine, uuid.UUID(fixture_job)) == "failed"
    job = (
        engine.connect()
        .execute(text("SELECT status, error, accepted FROM jobs WHERE id = :j"), {"j": fixture_job})
        .one()
    )
    assert job.status == "failed"
    assert "1 of 3 chunks did not complete" in job.error and "chunk 1" in job.error
    # Partial data is still there, and the job's totals only count what committed.
    assert 0 < variant_count(engine, fixture_job) < truth["rows_total"]
    assert 0 < job.accepted < truth["records_accepted"]


def test_chunk_that_never_reports_also_fails_the_job(engine, fixture_job):
    keys, reference_key = run_split(fixture_job)
    run_chunk(fixture_job, 0, keys[0], reference_key)
    assert jobstate.finalize(engine, uuid.UUID(fixture_job)) == "failed"


def test_input_that_is_not_a_vcf_fails_the_job_with_a_reason(engine, upload, data_dir):
    vcf_key = upload.put("bad.vcf", b"this is not a vcf\nat all\n")
    reference_key = upload.put("ref.fa", (data_dir / "ref.fa").read_bytes())
    job_id = upload.create_job(vcf_key, reference_key)

    result = tasks.split_job.apply(args=(job_id,)).get()

    assert result["chunks"] == 0
    row = (
        engine.connect()
        .execute(text("SELECT status, error FROM jobs WHERE id = :j"), {"j": job_id})
        .one()
    )
    assert row.status == "failed" and "not a VCF" in row.error


def test_header_only_vcf_completes_with_zero_records(engine, upload, data_dir):
    header = "\n".join(
        line
        for line in (data_dir / "variants.vcf").read_text().splitlines()
        if line.startswith("#")
    )
    job_id = upload.create_job(
        upload.put("empty.vcf", (header + "\n").encode()),
        upload.put("ref.fa", (data_dir / "ref.fa").read_bytes()),
    )
    assert tasks.split_job.apply(args=(job_id,)).get()["chunks"] == 0
    row = (
        engine.connect()
        .execute(text("SELECT status, total, chunk_count FROM jobs WHERE id = :j"), {"j": job_id})
        .one()
    )
    assert (row.status, row.total, row.chunk_count) == ("completed", 0, 0)
