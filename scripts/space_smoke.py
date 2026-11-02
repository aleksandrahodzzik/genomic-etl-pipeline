"""End-to-end check of the demo image (Dockerfile.space) through its one public port, the way
a browser uses it: the seeded sample job completes, statistics and a region query answer, a
small upload through the presigned-URL proxy runs to completion, the synthetic-records
endpoint runs as several chunks, and an upload over the size cap is refused.

    docker run -d -p 7860:7860 variant-etl-space && uv run python scripts/space_smoke.py

CI runs it on every build of the image; `--url https://<space>.hf.space` checks the live demo.
"""

import argparse
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from etl_client import ApiClient

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "src" / "variant_etl" / "api" / "static" / "sample"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    print(f"ok   {message}")


def wait_for_sample(client: ApiClient, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get("/api/v1/demo")["job"]
        if job and job["status"] == "completed":
            return job
        if job and job["status"] == "failed":
            raise SystemExit(f"FAIL: the sample job failed: {job['error']}")
        time.sleep(1)
    raise SystemExit("FAIL: the sample job did not complete")


def region_query(client: ApiClient, job_id: str) -> int:
    chrom = client.get(f"/api/v1/jobs/{job_id}/stats/chromosomes")["chromosomes"][0]["chrom"]
    page = client.get(f"/api/v1/jobs/{job_id}/variants", chrom=chrom, start=1, end=200_000)
    positions = [v["pos"] for v in page["items"]]
    check(
        bool(positions) and all(1 <= p <= 200_000 for p in positions),
        f"region query {chrom}:1-200,000 returns {len(positions)} rows inside the region",
    )
    return len(positions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:7860")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--cap-mb", type=int, default=20, help="the demo's upload cap")
    args = parser.parse_args()

    client = ApiClient(args.url)
    started = time.monotonic()
    client.wait_ready(timeout=args.timeout)
    print(f"ok   /health answers after {time.monotonic() - started:.0f} s")

    demo = client.get("/api/v1/demo")
    check(demo["synthetic"] and demo["max_upload_mb"] == args.cap_mb, "demo limits are set")
    sample = wait_for_sample(client, args.timeout)
    check(sample["counts"]["total"] == 3000, "the seeded 3,000-record sample completed")
    summary = client.get(f"/api/v1/jobs/{sample['id']}/stats/summary")
    check(summary["variants"] > 0 and summary["titv_ratio"] is not None, "statistics answer")
    region_query(client, sample["id"])

    # A small upload through the proxy, made unique so it is processed, not deduplicated.
    with tempfile.TemporaryDirectory() as tmp:
        vcf = (SAMPLE / "variants.vcf").read_bytes()
        cut = vcf.index(b"\n") + 1
        marked = vcf[:cut] + f"##smoke={uuid.uuid4().hex}\n".encode() + vcf[cut:]
        (Path(tmp) / "variants.vcf").write_bytes(marked)
        vcf_key = client.upload(Path(tmp) / "variants.vcf")
        ref_key = client.upload(SAMPLE / "ref.fa")
    job = client.wait_job(client.create_job(vcf_key, ref_key)["id"], timeout=args.timeout)
    check(job["counts"]["total"] == 3000, "an upload through the single port is processed")

    response = client.http.post("/api/v1/demo/synthetic")
    check(response.status_code == 202, "the synthetic-records endpoint accepts a job")
    synthetic = client.wait_job(response.json()["id"], timeout=args.timeout)
    check(
        synthetic["counts"]["total"] == 100_000 and synthetic["progress"]["chunks_total"] == 4,
        f"100,000 synthetic records ran as 4 chunks in {synthetic['duration_seconds']:.1f} s",
    )
    region_query(client, synthetic["id"])

    target = client.http.post("/api/v1/uploads", json={"filename": "too-big.vcf"}).json()
    too_big = httpx.put(target["upload_url"], content=b"#" * ((args.cap_mb + 1) << 20), timeout=120)
    check(too_big.status_code == 413, f"a {args.cap_mb + 1} MB upload is refused with 413")
    print(f"all checks passed in {time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
