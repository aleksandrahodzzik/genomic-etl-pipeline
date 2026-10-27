"""The pre-loaded sample dataset: seeding is idempotent and `GET /demo` finds the job."""

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from variant_etl.api.app import create_app
from variant_etl.demo import find_demo_job, seed_demo


def test_seeding_is_idempotent_and_the_demo_endpoint_finds_the_job(engine, storage):
    enqueued: list[str] = []
    with Session(engine) as session:
        job, first_was_duplicate = seed_demo(session, storage, enqueued.append)
        try:
            again, second_was_duplicate = seed_demo(session, storage, enqueued.append)
            assert again.id == job.id and second_was_duplicate is True
            # Nothing is enqueued twice; and if the compose `seed` service already loaded the
            # dataset, nothing is enqueued at all.
            assert len(enqueued) == (0 if first_was_duplicate else 1)

            assert find_demo_job(session, storage).id == job.id

            response = TestClient(create_app()).get("/api/v1/demo")
            assert response.status_code == 200
            assert response.json()["job"]["id"] == str(job.id)
        finally:
            if not first_was_duplicate:
                # Only remove a job this test created: the stack's own seeded job is what the
                # web UI shows, and must survive the test run.
                session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job.id})
                session.commit()
                storage.delete_prefix(f"chunks/{job.id}/")
