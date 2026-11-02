"""scripts/export_pages.py: what the GitHub Pages snapshot is built from. A fake API stands in for
the stack, so these run without Docker; the Pages workflow runs the real thing."""

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import export_pages  # noqa: E402

JOB_ID = "11111111-2222-3333-4444-555555555555"
VARIANTS = [{"id": i, "chrom": "chr1", "pos": 10 * i} for i in range(1, 8)]
REJECTED = [{"line_no": n, "reason": "malformed_line", "raw_line": "x"} for n in (4, 9, 12)]


def job(status: str = "completed") -> dict[str, Any]:
    return {"id": JOB_ID, "status": status, "error": None}


class FakeApi:
    """The endpoints the export reads, paginated the way the real API paginates."""

    def __init__(self, demo_jobs: list[dict[str, Any] | None]) -> None:
        self.demo_jobs = demo_jobs  # one per GET /demo; the last one repeats
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, request.url.params
        self.calls.append(str(request.url))
        base = f"/api/v1/jobs/{JOB_ID}"
        if path == "/api/v1/demo":
            current = self.demo_jobs.pop(0) if len(self.demo_jobs) > 1 else self.demo_jobs[0]
            return httpx.Response(200, json={"job": current, "chunk_size": 100000})
        if path == base + "/stats/summary":
            return httpx.Response(200, json={"records": {"total": 10}})
        if path == base + "/stats/chromosomes":
            return httpx.Response(200, json={"chromosomes": [{"chrom": "chr1"}]})
        if path == base + "/stats/af-histogram":
            assert q["bins"] == "10"
            return httpx.Response(200, json={"bins": []})
        if path == base + "/variants":
            limit, start = int(q["limit"]), int(q.get("cursor", "0"))
            page = VARIANTS[start : start + limit]
            more = start + limit < len(VARIANTS)
            return httpx.Response(
                200, json={"items": page, "next_cursor": str(start + limit) if more else None}
            )
        if path == base + "/rejected":
            limit, after = int(q["limit"]), int(q.get("after_line", "0"))
            rest = [r for r in REJECTED if r["line_no"] > after]
            page = rest[:limit]
            return httpx.Response(
                200,
                json={
                    "items": page,
                    "next_after_line": page[-1]["line_no"] if len(rest) > limit else None,
                    "note": "bounded sample",
                },
            )
        return httpx.Response(404)


def client_for(api: FakeApi) -> httpx.Client:
    return httpx.Client(base_url="http://api", transport=httpx.MockTransport(api))


def test_collect_walks_every_variant_and_rejected_page_in_order(monkeypatch):
    monkeypatch.setattr(export_pages, "VARIANT_PAGE", 3)
    monkeypatch.setattr(export_pages, "REJECTED_PAGE", 2)
    snapshot = export_pages.collect(client_for(FakeApi([job()])), timeout=5)

    assert snapshot["job"]["id"] == JOB_ID
    assert snapshot["summary"] == {"records": {"total": 10}}
    assert snapshot["chromosomes"] == {"chromosomes": [{"chrom": "chr1"}]}
    assert snapshot["af_histogram"] == {"bins": []}
    assert snapshot["variants"] == VARIANTS  # 3 pages: 3 + 3 + 1, API order kept
    assert snapshot["rejected"] == REJECTED  # 2 pages: 2 + 1
    assert snapshot["rejected_note"] == "bounded sample"
    assert snapshot["generated_at"]


def test_collect_waits_for_a_running_job():
    api = FakeApi([None, job("running"), job("completed")])
    snapshot = export_pages.collect(client_for(api), timeout=5, poll=0)
    assert snapshot["job"]["status"] == "completed"
    assert sum("/demo" in c for c in api.calls) == 3


def test_collect_refuses_a_failed_job():
    with pytest.raises(SystemExit, match="failed"):
        export_pages.collect(client_for(FakeApi([job("failed")])), timeout=5, poll=0)


def test_collect_gives_up_when_no_job_appears():
    with pytest.raises(SystemExit, match="did not complete"):
        export_pages.collect(client_for(FakeApi([None])), timeout=0, poll=0)


def test_build_site_copies_pages_and_writes_the_snapshot(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "index.html").write_text("<!doctype html>", encoding="utf-8")
    out = tmp_path / "site"

    export_pages.build_site({"variants": VARIANTS}, out, pages_dir=pages)

    assert (out / "index.html").read_text(encoding="utf-8") == "<!doctype html>"
    assert json.loads((out / "data" / "snapshot.json").read_text(encoding="utf-8")) == {
        "variants": VARIANTS
    }


def test_build_site_replaces_an_old_build(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "index.html").write_text("new", encoding="utf-8")
    out = tmp_path / "site"
    (out / "data").mkdir(parents=True)
    (out / "data" / "snapshot.json").write_text("{}", encoding="utf-8")
    (out / "stale.js").write_text("old", encoding="utf-8")

    export_pages.build_site({}, out, pages_dir=pages)

    assert not (out / "stale.js").exists()
    assert (out / "index.html").read_text(encoding="utf-8") == "new"


def test_build_site_refuses_a_folder_that_is_not_a_previous_build(tmp_path):
    # `--out .` or a typo must not delete a working tree: only an earlier build is replaced.
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "index.html").write_text("new", encoding="utf-8")
    out = tmp_path / "work"
    out.mkdir()
    (out / "notes.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(SystemExit, match="not a previous build"):
        export_pages.build_site({}, out, pages_dir=pages)

    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_build_site_refuses_to_write_over_its_own_sources(tmp_path):
    pages = tmp_path / "pages"
    (pages / "data").mkdir(parents=True)
    (pages / "data" / "snapshot.json").write_text("{}", encoding="utf-8")
    (pages / "index.html").write_text("source", encoding="utf-8")

    for out in (pages, tmp_path):
        with pytest.raises(SystemExit, match="pages"):
            export_pages.build_site({}, out, pages_dir=pages)

    assert (pages / "index.html").read_text(encoding="utf-8") == "source"


def test_the_template_license_ships_with_the_page():
    # pages/ adapts the MIT-licensed Reflow template; MIT requires its notice in every copy, and
    # build_site copies the whole folder, so the published site carries it too.
    notice = (export_pages.PAGES / "LICENSE-reflow.txt").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 8B (https://8b.com)" in notice
    assert "Permission is hereby granted, free of charge" in notice
