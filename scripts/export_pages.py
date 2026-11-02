"""Build the GitHub Pages site: the landing page in pages/ plus a snapshot of a real run.

    make up && uv run python scripts/export_pages.py --api http://localhost:8000 --out site

Waits until the stack has processed the bundled sample (the `seed` service), then saves the
API's own answers for that job into site/data/snapshot.json: the statistics, every variant row
and every stored rejected line. The page renders from that file, so what a visitor sees on
GitHub Pages is the output of the pipeline at this commit, not numbers typed into HTML.
The Pages workflow runs this on every push; scripts/pages_smoke.py then checks the result.
"""

import argparse
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "pages"
VARIANT_PAGE = 1000  # the API's maximum page sizes
REJECTED_PAGE = 500


def _get(client: httpx.Client, path: str, **params: Any) -> Any:
    response = client.get(path, params=params)
    response.raise_for_status()
    return response.json()


def wait_for_sample(client: httpx.Client, timeout: float, poll: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        job = _get(client, "/api/v1/demo")["job"]
        if job and job["status"] == "completed":
            return dict(job)
        if job and job["status"] == "failed":
            raise SystemExit(f"the sample job failed: {job['error']}")
        if time.monotonic() >= deadline:
            raise SystemExit("the sample job did not complete; is the stack up with `seed`?")
        time.sleep(poll)


def collect(client: httpx.Client, timeout: float, poll: float = 1.0) -> dict[str, Any]:
    """Everything the page shows, read from a running API."""
    job = wait_for_sample(client, timeout, poll)
    base = f"/api/v1/jobs/{job['id']}"

    variants: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": VARIANT_PAGE}
        if cursor:
            params["cursor"] = cursor
        page = _get(client, base + "/variants", **params)
        variants.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break

    rejected: list[dict[str, Any]] = []
    after_line = 0
    note = None
    while True:
        page = _get(client, base + "/rejected", limit=REJECTED_PAGE, after_line=after_line)
        rejected.extend(page["items"])
        note = page.get("note")
        if page["next_after_line"] is None:
            break
        after_line = page["next_after_line"]

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": os.environ.get("GITHUB_SHA"),
        "job": job,
        "summary": _get(client, base + "/stats/summary"),
        "chromosomes": _get(client, base + "/stats/chromosomes"),
        "af_histogram": _get(client, base + "/stats/af-histogram", bins=10),
        "variants": variants,
        "rejected": rejected,
        "rejected_note": note,
    }


def build_site(snapshot: dict[str, Any], out: Path, pages_dir: Path = PAGES) -> None:
    """A fresh copy of pages/ in `out`, with the snapshot in out/data/snapshot.json."""
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(pages_dir, out)
    (out / "data").mkdir(exist_ok=True)
    (out / "data" / "snapshot.json").write_text(
        json.dumps(snapshot, separators=(",", ":")), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--out", type=Path, default=ROOT / "site")
    parser.add_argument("--timeout", type=float, default=300, help="seconds to wait for the seed")
    args = parser.parse_args()

    with httpx.Client(base_url=args.api, timeout=60) as client:
        snapshot = collect(client, args.timeout)
    build_site(snapshot, args.out)
    size_kb = (args.out / "data" / "snapshot.json").stat().st_size / 1024
    print(
        f"wrote {args.out}: job {snapshot['job']['id']}, {len(snapshot['variants']):,} variants, "
        f"{len(snapshot['rejected']):,} rejected lines, snapshot {size_kb:,.0f} KB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
