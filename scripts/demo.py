"""End-to-end demo against a running stack: upload -> job -> poll -> stats summary.

make up && make demo
"""

import argparse
import json
import sys
import time
from pathlib import Path

from etl_client import ApiClient, JobFailed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--data-dir", default="data/demo", type=Path)
    args = parser.parse_args()

    api = ApiClient(args.api)
    api.wait_ready()

    started = time.monotonic()
    print("uploading reference and VCF straight to object storage (presigned URLs)...")
    reference_key = api.upload(args.data_dir / "ref.fa")
    vcf_key = api.upload(args.data_dir / "variants.vcf")
    print(f"  uploaded in {time.monotonic() - started:.1f}s")

    job = api.create_job(vcf_key, reference_key)
    job_id = job["id"]
    print(
        f"job {job_id} {job['status']}"
        + (" (identical job already existed)" if job["deduplicated"] else "")
    )

    last = ""

    def progress(state: dict) -> None:
        nonlocal last
        p = state["progress"]
        line = f"  {state['status']:<9} chunks {p['chunks_done']}/{p['chunks_total'] or '?'}"
        if line != last:
            print(line)
            last = line

    try:
        job = api.wait_job(job_id, on_progress=progress)
    except JobFailed as exc:
        print(f"job failed: {exc}", file=sys.stderr)
        return 1

    print(f"done in {job['duration_seconds']:.1f}s of pipeline time\n")
    summary = api.get(f"/api/v1/jobs/{job_id}/stats/summary")
    print("GET /api/v1/jobs/{id}/stats/summary")
    print(json.dumps(summary, indent=2))
    print("\nTry also:")
    base = f"{args.api}/api/v1/jobs/{job_id}"
    print(f"  curl '{base}/stats/chromosomes'")
    print(f"  curl '{base}/stats/af-histogram?bins=10'")
    print(f"  curl '{base}/variants?chrom=chr1&start=1&end=50000&type=DEL&limit=5'")
    print(f"  interactive docs: {args.api}/docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
