"""Check the built GitHub Pages site in a real browser against the API it was exported from.

    uv run --with playwright==1.64.1 playwright install chromium
    uv run --with playwright==1.64.1 python scripts/pages_smoke.py --site site --api http://localhost:8000

Serves site/ under /genomic-etl-pipeline/, the path GitHub Pages gives a project site, and checks:
no console errors, failed requests or horizontal scroll at phone width; the page's numbers equal
the API's; and the variant explorer, which filters the saved rows in the browser, returns exactly
the rows the API returns for the same filters, in the same order. The Pages workflow runs this
before it deploys. Playwright is only needed here, so it is not a project dependency.
"""

import argparse
import http.server
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, sync_playwright

PREFIX = "/genomic-etl-pipeline/"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    print(f"ok   {message}")


def serve(site: Path) -> http.server.ThreadingHTTPServer:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(site), **kwargs)

        def do_GET(self) -> None:
            if not self.path.startswith(PREFIX):
                self.send_error(404)
                return
            self.path = "/" + self.path[len(PREFIX) :]
            super().do_GET()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def api_ids(client: httpx.Client, job_id: str, filters: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    params: dict[str, Any] = {k: v for k, v in filters.items() if v is not None}
    params["limit"] = 1000
    while True:
        page = client.get(f"/api/v1/jobs/{job_id}/variants", params=params)
        page.raise_for_status()
        body = page.json()
        ids.extend(v["id"] for v in body["items"])
        if not body["next_cursor"]:
            return ids
        params["cursor"] = body["next_cursor"]


def page_ids(page: Page, filters: dict[str, Any]) -> list[int]:
    return list(page.evaluate("f => window.variantEtl.query(f).items.map(v => v.id)", filters))


def watch(page: Page, problems: list[str], label: str) -> None:
    """Collect console errors, uncaught exceptions and failed requests of one page."""
    page.on(
        "console",
        lambda m: problems.append(f"{label} console: {m.text}") if m.type == "error" else None,
    )
    page.on("pageerror", lambda e: problems.append(f"{label} page error: {e}"))
    page.on("requestfailed", lambda r: problems.append(f"{label} request failed: {r.url}"))
    page.on(
        "response",
        lambda r: problems.append(f"{label} HTTP {r.status}: {r.url}") if r.status >= 400 else None,
    )


REASONS = [
    "malformed_line",
    "non_numeric_qual",
    "unknown_contig",
    "pos_out_of_bounds",
    "unsupported_symbolic",
    "invalid_allele_chars",
    "ref_mismatch",
]


def expected_kpis(summary: dict[str, Any]) -> list[str]:
    """The six numbers of the results section, written the way the API's summary says."""
    r, titv = summary["records"], summary["titv_ratio"]
    return [
        f"{r['total']:,}",
        f"{r['accepted']:,}",
        f"{r['rejected']:,}",
        f"{summary['variants']:,}",
        "\u2013" if titv is None else f"{titv:.2f}",  # the page's en dash for "no value"
        f"{summary['normalized_percent']:.1f}%",
    ]


def expected_board(summary: dict[str, Any], job: dict[str, Any]) -> list[str]:
    """The ten results of the pipeline board, in its order."""
    counts = [summary["rejected_by_reason"].get(k, {}).get("count", 0) for k in REASONS]
    seconds = job["duration_seconds"]
    took = "" if seconds is None else f" in {seconds:.2f} s"
    return [f"{n:,} refused" if n else "none refused" for n in counts] + [
        f"{summary['records']['accepted']:,} lines → {summary['variants']:,} rows",
        f"{summary['normalized']:,} rows rewritten",
        f"{summary['variants']:,} rows{took}",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--site", type=Path, default=Path("site"))
    parser.add_argument("--api", default="http://localhost:8000")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.api, timeout=60)
    job = client.get("/api/v1/demo").json()["job"]
    check(job is not None and job["status"] == "completed", "the API has the completed sample job")
    summary = client.get(f"/api/v1/jobs/{job['id']}/stats/summary").json()
    chroms = [
        c["chrom"]
        for c in client.get(f"/api/v1/jobs/{job['id']}/stats/chromosomes").json()["chromosomes"]
    ]

    server = serve(args.site)
    url = f"http://127.0.0.1:{server.server_address[1]}{PREFIX}"
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        watch(page, problems, "desktop")
        page.goto(url, wait_until="networkidle")
        snapshot_job = page.evaluate("window.variantEtl.ready.then(s => s.job.id)")
        check(snapshot_job == job["id"], f"the page loaded the snapshot of job {job['id'][:8]}")

        kpis = page.locator("#kpis .rs-kpi__v").all_text_contents()
        check(kpis == expected_kpis(summary), f"the six result numbers equal the API's: {kpis}")
        board = page.locator("#pipeline [data-check] .dg-val").all_text_contents()
        check(
            board == expected_board(summary, job),
            f"the ten board results equal the API's: {board[0]!r} … {board[-1]!r}",
        )

        # Region bounds are real variant positions, so an off-by-one at either edge shows.
        first, last = chroms[0], chroms[-1]
        on_first = client.get(
            f"/api/v1/jobs/{job['id']}/variants", params={"chrom": first, "limit": 60}
        ).json()["items"]
        lo, hi = on_first[10]["pos"], on_first[50]["pos"]
        filter_sets: list[dict[str, Any]] = [
            {},
            {"chrom": first},
            {"chrom": first, "start": lo, "end": hi},
            {"type": "DEL"},
            {"start": hi},
            {"end": lo},
            {"chrom": last, "type": "SNV", "start": lo, "end": 20000},
        ]
        for filters in filter_sets:
            expected = api_ids(client, job["id"], filters)
            got = page_ids(page, filters)
            check(
                got == expected,
                f"explorer {filters or 'no filter'}: {len(got):,} rows, same as the API",
            )

        page.fill("#fStart", "5000")
        page.fill("#fEnd", "1000")
        page.locator("#fEnd").dispatch_event("change")
        # text_content, not inner_text: the label is uppercased by CSS
        check(
            page.text_content("#varCount") == "start must be <= end",
            "start > end shows the API's error",
        )

        page.select_option("#fChrom", first)
        page.fill("#fStart", "1000")
        page.fill("#fEnd", "5000")
        page.locator("#fEnd").dispatch_event("change")
        cells = page.locator("#varBody tr td:first-child").all_inner_texts()
        positions = [int(c.split(":")[1].replace(",", "")) for c in cells]
        expected_rows = min(
            25, len(api_ids(client, job["id"], {"chrom": first, "start": 1000, "end": 5000}))
        )
        check(
            len(cells) == expected_rows
            and all(c.startswith(first + ":") for c in cells)
            and all(1000 <= p <= 5000 for p in positions),
            f"the table shows {len(cells)} rows of {first}:1,000-5,000",
        )

        page.click("#tabRej")
        rejected = page.locator("#rejBody tr").count()
        check(
            rejected
            == min(25, page.evaluate("window.variantEtl.ready.then(s => s.rejected.length)")),
            f"the refused-lines tab lists {rejected} lines",
        )

        # The phone layout runs its own scenes (the board scrubs in flow instead of pinning);
        # scroll the whole page so each one starts while the error listeners are attached.
        phone = browser.new_page(viewport={"width": 360, "height": 780})
        watch(phone, problems, "phone")
        phone.goto(url, wait_until="networkidle")
        phone.evaluate("window.variantEtl.ready")
        for section in ("pipeline", "results", "scaling", "claims", "run"):
            phone.evaluate(f"document.getElementById('{section}').scrollIntoView()")
            phone.wait_for_timeout(600)
        width = phone.evaluate("document.documentElement.scrollWidth")
        check(width <= 360, f"no horizontal scroll at 360 px (page is {width} px wide)")
        browser.close()
    server.shutdown()

    check(
        not problems,
        "no console errors or failed requests"
        + ("" if not problems else ": " + "; ".join(problems)),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
