"""Benchmark the running stack and write docs/BENCHMARKS.md from what was measured.

    make bench        # = generate data/bench, then run this script

What it measures (nothing is estimated; the report contains only these numbers):
  1. End-to-end job time and records/s for 1, 2 and 4 worker containers
     (`docker compose up --scale worker=N`), on the same 1M-record file.
  2. Region-query latency with and without the (job_id, chrom, pos) index, plus
     EXPLAIN (ANALYZE, BUFFERS) of the query, and of a deep keyset-pagination page.
  3. The cost of keeping the two indexes during bulk load: the same job loaded with the
     indexes dropped, plus the time to build them afterwards.

Each measured job uploads a copy of the VCF with one extra header line, so its content
checksum is new and the API cannot return a cached job. Upload time is not included in the
job time. The job's own rows are deleted afterwards; nothing else in the database is touched.
"""

import argparse
import json
import platform
import random
import re
import shlex
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from etl_client import ApiClient
from variant_etl.db.session import get_engine
from variant_etl.storage.s3 import get_storage

ROOT = Path(__file__).resolve().parents[1]
INDEXES = {
    "ix_variants_job_chrom_pos": "CREATE INDEX ix_variants_job_chrom_pos ON variants "
    "(job_id, chrom, pos)",
    "ix_variants_job_type": "CREATE INDEX ix_variants_job_type ON variants (job_id, variant_type)",
}


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=ROOT).stdout


def machine_specs() -> dict[str, str]:
    cpu = platform.processor() or "unknown"
    try:
        if sys.platform == "win32":
            cpu = sh(
                "powershell", "-NoProfile", "-Command",
                "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name",
            ).strip()  # fmt: skip
            ram = (
                sh(
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)",
                )
                .strip()
                .replace(",", ".")
                + " GB"
            )  # fmt: skip  (PowerShell may print a comma)
        elif sys.platform == "darwin":
            cpu = sh("sysctl", "-n", "machdep.cpu.brand_string").strip()
            ram = f"{int(sh('sysctl', '-n', 'hw.memsize')) / 2**30:.1f} GB"
        else:
            info = Path("/proc/cpuinfo").read_text()
            cpu = re.search(r"model name\s*:\s*(.+)", info).group(1)  # type: ignore[union-attr]
            kib = int(re.search(r"MemTotal:\s*(\d+)", Path("/proc/meminfo").read_text()).group(1))  # type: ignore[union-attr]
            ram = f"{kib / 2**20:.1f} GB"
    except (subprocess.CalledProcessError, OSError, AttributeError):
        ram = "unknown"
    docker = sh(
        "docker", "info", "--format", "{{.NCPU}} CPUs, {{.MemTotal}} bytes, {{.OperatingSystem}}"
    )
    ncpu, rest = docker.strip().split(" CPUs, ", 1)
    mem_bytes, os_name = rest.split(" bytes, ", 1)
    return {
        "host": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": cpu,
        "ram": ram,
        "docker_engine": f"{os_name.strip()}; {ncpu} CPUs and {int(mem_bytes) / 2**30:.1f} GiB visible to containers",
        "python": platform.python_version(),
        "postgres": sh("docker", "compose", "exec", "-T", "postgres", "postgres", "-V").strip(),
        "git_commit": sh("git", "rev-parse", "--short", "HEAD").strip(),
    }


def scale_workers(count: int) -> None:
    sh("docker", "compose", "up", "-d", "--wait", "--no-recreate", "--scale", f"worker={count}")


class Bench:
    def __init__(self, api: ApiClient, data_dir: Path) -> None:
        self.api = api
        self.data_dir = data_dir
        self.engine = get_engine()
        self.storage = get_storage()
        self.reference_key = api.upload(data_dir / "ref.fa")
        self.vcf = (data_dir / "variants.vcf").read_bytes()

    def unique_vcf_key(self) -> str:
        first_line_end = self.vcf.index(b"\n") + 1
        marked = (
            self.vcf[:first_line_end]
            + f"##benchmark_run={uuid.uuid4()}\n".encode()
            + self.vcf[first_line_end:]
        )
        response = self.api.http.post("/api/v1/uploads", json={"filename": "variants.vcf"})
        body = response.json()
        httpx.put(body["upload_url"], content=marked, timeout=900).raise_for_status()
        return str(body["key"])

    def run_job(self) -> tuple[float, dict[str, Any]]:
        vcf_key = self.unique_vcf_key()
        started = time.perf_counter()
        job = self.api.create_job(vcf_key, self.reference_key)
        assert not job["deduplicated"], "benchmark job was served from cache"
        job = self.api.wait_job(job["id"], timeout=3600)
        return time.perf_counter() - started, job

    def delete_job(self, job_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        self.storage.delete_prefix(f"chunks/{job_id}/")

    def vacuum(self) -> None:
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("VACUUM (ANALYZE) variants"))

    def scalability(self, workers: list[int], runs: int, warmup: int) -> list[dict[str, Any]]:
        results = []
        for count in workers:
            print(f"== {count} worker container(s)", flush=True)
            scale_workers(count)
            for _ in range(warmup):  # primes each worker's reference cache and connections
                _, job = self.run_job()
                self.delete_job(job["id"])
            seconds, pipeline = [], []
            for i in range(runs):
                wall, job = self.run_job()
                seconds.append(wall)
                pipeline.append(job["duration_seconds"])
                total = job["counts"]["total"]
                print(f"   run {i + 1}: {wall:.1f}s  {total / wall:,.0f} records/s", flush=True)
                self.delete_job(job["id"])
            results.append(
                {
                    "workers": count,
                    "records": total,
                    "seconds": seconds,
                    "pipeline_seconds": pipeline,
                }
            )
        return results

    # -- query latency ----------------------------------------------------------------------

    def region_queries(
        self, job_id: str, contigs: dict[str, int], n: int
    ) -> list[tuple[str, int, int]]:
        rng = random.Random(7)
        names = sorted(contigs)
        out = []
        for _ in range(n):
            chrom = rng.choice(names)
            start = rng.randint(1, contigs[chrom] - 10_001)
            out.append((chrom, start, start + 10_000))
        return out

    def time_queries(
        self, conn: Any, job_id: str, queries: list[tuple[str, int, int]]
    ) -> list[float]:
        sql = text(
            "SELECT id, chrom, pos, ref, alt FROM variants "
            "WHERE job_id = :j AND chrom = :c AND pos BETWEEN :a AND :b ORDER BY pos"
        )
        latencies = []
        for chrom, start, end in queries:
            t0 = time.perf_counter()
            conn.execute(sql, {"j": job_id, "c": chrom, "a": start, "b": end}).fetchall()
            latencies.append((time.perf_counter() - t0) * 1000)
        return latencies

    def explain(self, conn: Any, sql: str, params: dict[str, Any]) -> str:
        rows = conn.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + sql), params).all()
        return "\n".join(r[0] for r in rows)

    def index_query_benchmark(self, job_id: str, contigs: dict[str, int], n: int) -> dict[str, Any]:
        queries = self.region_queries(job_id, contigs, n)
        probe = queries[0]
        region_sql = (
            "SELECT id, chrom, pos, ref, alt FROM variants "
            "WHERE job_id = :j AND chrom = :c AND pos BETWEEN :a AND :b ORDER BY pos"
        )
        probe_params = {"j": job_id, "c": probe[0], "a": probe[1], "b": probe[2]}
        result: dict[str, Any] = {"queries": n, "probe": probe}

        with self.engine.connect() as conn:
            self.time_queries(conn, job_id, queries[:20])  # warm the cache
            result["with_index"] = self.time_queries(conn, job_id, queries)
            result["plan_with_index"] = self.explain(conn, region_sql, probe_params)
            rows = conn.execute(
                text("SELECT count(*) FROM variants WHERE job_id = :j"), {"j": job_id}
            )
            result["rows_in_job"] = rows.scalar_one()

            # Keyset page deep inside the job: the cursor is the middle row's sort key.
            middle = conn.execute(
                text(
                    "SELECT chrom, pos, id FROM variants WHERE job_id = :j "
                    "ORDER BY chrom, pos, id OFFSET (SELECT count(*) / 2 FROM variants "
                    "WHERE job_id = :j) LIMIT 1"
                ),
                {"j": job_id},
            ).one()
            keyset_sql = (
                "SELECT id, chrom, pos, ref, alt FROM variants WHERE job_id = :j "
                "AND (chrom, pos, id) > (:cc, :cp, :ci) ORDER BY chrom, pos, id LIMIT 101"
            )
            result["keyset_cursor"] = list(middle)
            result["plan_keyset"] = self.explain(
                conn, keyset_sql, {"j": job_id, "cc": middle[0], "cp": middle[1], "ci": middle[2]}
            )
            t0 = time.perf_counter()
            for _ in range(20):
                conn.execute(
                    text(keyset_sql),
                    {"j": job_id, "cc": middle[0], "cp": middle[1], "ci": middle[2]},
                ).fetchall()
            result["keyset_ms"] = (time.perf_counter() - t0) / 20 * 1000

        # Without the index: DROP INDEX is transactional in PostgreSQL, so drop it inside a
        # transaction we roll back. The index is guaranteed to exist again afterwards.
        with self.engine.connect() as conn:
            conn.execute(text("DROP INDEX ix_variants_job_chrom_pos"))
            try:
                result["without_index"] = self.time_queries(conn, job_id, queries)
                result["plan_without_index"] = self.explain(conn, region_sql, probe_params)
            finally:
                conn.rollback()
        return result

    def index_load_cost(self) -> dict[str, Any]:
        """Load the same file with both secondary indexes dropped, then build them."""
        scale_workers(1)
        outcome: dict[str, Any] = {}
        with self.engine.begin() as conn:
            for name in INDEXES:
                conn.execute(text(f"DROP INDEX {name}"))
        try:
            wall, job = self.run_job()
            outcome["load_without_indexes_s"] = wall
            outcome["records"] = job["counts"]["total"]
            build = {}
            with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                for name, ddl in INDEXES.items():
                    t0 = time.perf_counter()
                    conn.execute(text(ddl))
                    build[name] = time.perf_counter() - t0
            outcome["index_build_s"] = build
            self.delete_job(job["id"])
        finally:
            with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                for ddl in INDEXES.values():
                    conn.execute(text(ddl.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS")))
        return outcome

    def chunk_timings(self, job_id: str) -> dict[str, float]:
        logs = sh("docker", "compose", "logs", "--no-color", "worker")
        pattern = re.compile(
            rf"job {job_id} chunk \d+: .* parse\+normalize ([\d.]+)s copy ([\d.]+)s"
        )
        parse, copy = [], []
        for match in pattern.finditer(logs):
            parse.append(float(match.group(1)))
            copy.append(float(match.group(2)))
        return {
            "chunks": len(parse),
            "parse_normalize_median_s": statistics.median(parse),
            "copy_median_s": statistics.median(copy),
        }


def fmt_stats(values: list[float]) -> str:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return f"{statistics.median(values):.2f} / {p95:.2f} / {max(values):.2f}"


def render(report: dict[str, Any]) -> str:
    specs = report["specs"]
    truth = report["truth"]
    lines = [
        "# Benchmarks",
        "",
        "Generated by `scripts/benchmark.py`; every number below was measured on the machine "
        "described here, in one session. Re-run it to reproduce.",
        "",
        "## Environment",
        "",
        *(f"- **{key.replace('_', ' ')}**: {value}" for key, value in specs.items()),
        f"- **measured at**: {report['measured_at']}",
        "",
        "## Exact commands",
        "",
        "```",
        "make up",
        report["command"],
        "```",
        "",
        "`make bench` first runs the data generator with:",
        "",
        "```",
        report["generate_command"],
        "```",
        "",
        f"Dataset: {truth['records_total']:,} VCF records "
        f"({truth['records_rejected']:,} deliberately invalid) on {len(truth['contigs'])} contigs "
        f"of {next(iter(truth['contigs'].values())):,} bp, producing {truth['rows_total']:,} "
        f"normalized variant rows. CHUNK_SIZE = 100,000 records, so the file is split into "
        f"{-(-truth['records_total'] // 100_000)} chunks.",
        "",
        "## 1. Throughput vs. number of worker containers",
        "",
        "Time from `POST /jobs` returning to the job reaching `completed` (upload excluded). "
        "Each worker container runs one Celery process (`--concurrency=1`), so N containers "
        "process N chunks at once. The same machine also runs PostgreSQL, Redis, MinIO and the "
        "API, so this is a single-host measurement of the architecture, not a cluster benchmark.",
        "",
        "| workers | runs | wall time per run (s) | median (s) | records/s (median) | speed-up vs 1 |",
        "|---:|---:|---|---:|---:|---:|",
    ]
    base = statistics.median(report["scalability"][0]["seconds"])
    for row in report["scalability"]:
        median = statistics.median(row["seconds"])
        lines.append(
            f"| {row['workers']} | {len(row['seconds'])} | "
            f"{', '.join(f'{s:.1f}' for s in row['seconds'])} | {median:.1f} | "
            f"{row['records'] / median:,.0f} | {base / median:.2f}x |"
        )
    timing = report.get("chunk_timings")
    if timing:
        lines += [
            "",
            f"Per-chunk time inside a worker (median over {timing['chunks']} chunks of one "
            f"single-worker run, from the worker logs): parse + validate + normalize "
            f"**{timing['parse_normalize_median_s']:.2f} s**, COPY into PostgreSQL "
            f"**{timing['copy_median_s']:.2f} s**.",
        ]
    q = report["queries"]
    with_index, without = q["with_index"], q["without_index"]
    lines += [
        "",
        "## 2. Region-query latency, with and without the `(job_id, chrom, pos)` index",
        "",
        f"{q['queries']} random 10 kb windows over a job of {q['rows_in_job']:,} rows "
        f"(`WHERE job_id = ? AND chrom = ? AND pos BETWEEN ? AND ? ORDER BY pos`), measured "
        'directly against PostgreSQL (no HTTP). "Without" drops the index inside a transaction '
        "that is rolled back.",
        "",
        "| | median (ms) | p95 (ms) | max (ms) |",
        "|---|---:|---:|---:|",
        f"| with index | {statistics.median(with_index):.2f} | "
        f"{sorted(with_index)[int(0.95 * len(with_index))]:.2f} | {max(with_index):.2f} |",
        f"| without index | {statistics.median(without):.2f} | "
        f"{sorted(without)[int(0.95 * len(without))]:.2f} | {max(without):.2f} |",
        "",
        f"Median speed-up from the index: **{statistics.median(without) / statistics.median(with_index):.0f}x**.",
        "",
        f"`EXPLAIN (ANALYZE, BUFFERS)` for one window ({q['probe'][0]}:{q['probe'][1]}-{q['probe'][2]}), with the index:",
        "",
        "```",
        q["plan_with_index"],
        "```",
        "",
        "Without it:",
        "",
        "```",
        q["plan_without_index"],
        "```",
        "",
        "### Keyset pagination stays cheap deep into a job",
        "",
        f"One page (101 rows) after the cursor `{q['keyset_cursor']}`, which is the middle row of the job's "
        f"sort order; mean of 20 runs: **{q['keyset_ms']:.2f} ms**. An OFFSET page at the same depth "
        "would have to read and discard half the job first.",
        "",
        "```",
        q["plan_keyset"],
        "```",
    ]
    cost = report.get("index_load_cost")
    if cost:
        with_idx = statistics.median(report["scalability"][0]["seconds"])
        build_total = sum(cost["index_build_s"].values())
        lines += [
            "",
            "## 3. Cost of keeping the indexes during bulk load",
            "",
            'Same file, 1 worker. "With indexes" is the row from table 1; "without" dropped both '
            "secondary indexes before the job and built them afterwards.",
            "",
            "| strategy | load (s) | index build (s) | total (s) |",
            "|---|---:|---:|---:|",
            f"| indexes maintained during load (default) | {with_idx:.1f} | 0 | {with_idx:.1f} |",
            f"| drop indexes, load, then build | {cost['load_without_indexes_s']:.1f} | "
            f"{build_total:.1f} | {cost['load_without_indexes_s'] + build_total:.1f} |",
            "",
            "Build times: "
            + ", ".join(f"`{n}` {s:.1f} s" for n, s in cost["index_build_s"].items())
            + ".",
        ]
    lines += ["", "## Raw numbers", "", "```json", json.dumps(report["raw"], indent=1), "```", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--data-dir", default="data/bench", type=Path)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--query-count", type=int, default=100)
    parser.add_argument("--skip-index-load", action="store_true")
    parser.add_argument("--out", default="docs/BENCHMARKS.md", type=Path)
    args = parser.parse_args()

    api = ApiClient(args.api)
    api.wait_ready()
    truth = json.loads((args.data_dir / "truth.json").read_text())
    bench = Bench(api, args.data_dir)

    scalability = bench.scalability(args.workers, args.runs, args.warmup)

    print("== region-query latency", flush=True)
    scale_workers(1)
    _, job = bench.run_job()
    chunk_timings = bench.chunk_timings(job["id"])
    bench.vacuum()
    queries = bench.index_query_benchmark(job["id"], truth["contigs"], args.query_count)
    bench.delete_job(job["id"])

    index_load = None
    if not args.skip_index_load:
        print("== index cost during bulk load", flush=True)
        index_load = bench.index_load_cost()
        bench.vacuum()

    params = truth["params"]
    report = {
        "specs": machine_specs(),
        "measured_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "command": "uv run python " + " ".join(shlex.quote(a) for a in sys.argv),
        "generate_command": (
            f"uv run python scripts/generate_data.py --contigs {params['contigs']} "
            f"--contig-length {params['contig_length']} --variants {params['variants']} "
            f"--titv {params['titv']} --error-rate {params['error_rate']} --seed {params['seed']} "
            f"--out-dir {args.data_dir}"
        ),
        "truth": truth,
        "scalability": scalability,
        "chunk_timings": chunk_timings,
        "queries": queries,
        "index_load_cost": index_load,
    }
    report["raw"] = {
        "scalability": scalability,
        "chunk_timings": chunk_timings,
        "region_query_ms": {
            "with_index": queries["with_index"],
            "without_index": queries["without_index"],
        },
        "keyset_ms": queries["keyset_ms"],
        "index_load_cost": index_load,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(report), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
