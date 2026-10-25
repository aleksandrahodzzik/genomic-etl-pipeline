from pathlib import Path

from fastapi.testclient import TestClient

from variant_etl.api.app import STATIC_DIR, create_app

client = TestClient(create_app())


def test_root_serves_the_web_ui():
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Variant ETL" in response.text


def test_ui_is_not_listed_in_the_openapi_schema():
    # The page is not part of the REST contract.
    assert "/" not in client.get("/openapi.json").json()["paths"]


def test_sample_files_are_served_and_match_the_test_fixtures():
    # The UI's "Try with sample data" button fetches these; a drift from the fixtures the
    # tests verify against would make the demo show numbers nobody has checked.
    fixtures = Path(__file__).parent.parent / "data"
    for name in ("ref.fa", "variants.vcf"):
        served = client.get(f"/static/sample/{name}")
        assert served.status_code == 200
        assert served.content == (fixtures / name).read_bytes()
        assert (STATIC_DIR / "sample" / name).stat().st_size > 0
