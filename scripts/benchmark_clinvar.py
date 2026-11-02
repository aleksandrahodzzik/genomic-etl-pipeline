"""Benchmark the pipeline on real data (ClinVar GRCh38) next to the synthetic 1M-record set,
and write docs/BENCHMARKS_CLINVAR.md from what was measured.

    make up && make bench-clinvar      # = `make data`, then this script

1. Inputs, downloaded once into data/clinvar/ and checked against the MD5 sums their
   publishers post next to them:
     - a pinned ClinVar GRCh38 release: a sites-only VCF (8 columns, no samples), which is
       exactly what the pipeline accepts;
     - the GRCh38 sequence of the chosen chromosomes, one UCSC hg38 file per chromosome.
   The whole GRCh38 reference (3.1 Gb) would be copied into, and indexed by, every worker
   container, so by default only chromosomes 1, 2 and 17 are used: the three with the most
   ClinVar records, about 1.1M records, close to the size of the synthetic set.
2. The ClinVar records on those chromosomes are kept, and the rest dropped before upload:
   against a partial reference they would all be rejected as `unknown_contig`, which says
   nothing about the data.
3. Both datasets are measured in the same session, alternating run by run so that drift on
   the machine affects them alike:
     - job time (POST /jobs returning to `completed`; upload excluded) and records/s for 1 and
       4 worker containers: median of --runs runs, after a warm-up run;
     - that warm-up run with one worker, which also downloads and indexes the reference;
     - per-chunk parse+normalize and COPY time, from the worker logs;
     - rejected records by reason, with what the rejected lines have in common;
     - region queries: 10 kb windows centred on randomly chosen stored variants, timed in
       PostgreSQL with and without the (job_id, chrom, pos) index, and as one API page.
4. A cross-check of normalization on the same records with `bcftools norm -m -any -f`
   (bcftools from PATH, or the pinned container the oracle test uses; --no-bcftools skips it).

Each measured job uploads the VCF with one extra `##` meta line, so its content checksum is
new and the API cannot return a cached job. Jobs created here are deleted afterwards; nothing
else in the database is touched. Ends with the stack scaled back to one worker.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
import uuid
import zlib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from benchmark import Bench, machine_specs, scale_workers, sh
from etl_client import ApiClient
from variant_etl.db.session import get_engine
from variant_etl.storage.s3 import get_storage

ROOT = Path(__file__).resolve().parents[1]
CLINVAR_RELEASE = "20261025"
CLINVAR_ARCHIVE = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/archive_2.0"
UCSC_HG38 = "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes"
DEFAULT_CHROMOSOMES = ["1", "2", "17"]
WINDOW_BP = 10_000
# Recorded with the command when set: they say which ports the stack ran on.
ENV_SHOWN = ("API_PORT", "POSTGRES_PORT", "DATABASE_URL")
BCFTOOLS_IMAGE = "quay.io/biocontainers/bcftools:1.21--h8b25389_0"  # as in the oracle test
# One concrete lookup, made when its chromosome is part of the run: BRCA1 on GRCh38
# (NCBI Gene 672, NC_000017.11:43044295-43125483).
GENES = {"17": ("BRCA1", 43_044_295, 43_125_483)}
CHUNK_LOG = re.compile(
    r"job (?P<job>[0-9a-f-]{36}) chunk \d+: .* parse\+normalize (?P<parse>[\d.]+)s "
    r"copy (?P<copy>[\d.]+)s"
)


# -- inputs -----------------------------------------------------------------------------------


def md5_of(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while block := stream.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    print(f"downloading {url}", flush=True)
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
        response.raise_for_status()
        with partial.open("wb") as out:
            for block in response.iter_bytes(1 << 20):
                out.write(block)
    partial.replace(dest)


def published_md5(listing_url: str, listing: Path, name: str) -> str:
    """The MD5 the publisher lists for `name` (ClinVar's .md5 file, UCSC's md5sum.txt)."""
    if not listing.exists():
        download(listing_url, listing)
    for line in listing.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and Path(parts[1].lstrip("*")).name == name:
            return parts[0].lower()
    raise SystemExit(f"{listing} lists no checksum for {name}")


def fetch_verified(url: str, dest: Path, expected_md5: str) -> None:
    if dest.exists() and md5_of(dest) == expected_md5:
        return
    download(url, dest)
    actual = md5_of(dest)
    if actual != expected_md5:
        raise SystemExit(f"{dest}: MD5 {actual}, but the publisher lists {expected_md5}")


def ucsc_name(chromosome: str) -> str:
    return "chrM" if chromosome in {"M", "MT"} else f"chr{chromosome}"


def prepare_inputs(data_dir: Path, release: str, chromosomes: list[str]) -> dict[str, Any]:
    """Download and verify the inputs, then build the VCF subset and the reference."""
    clinvar_name = f"clinvar_{release}.vcf.gz"
    clinvar_url = f"{CLINVAR_ARCHIVE}/{release[:4]}/{clinvar_name}"
    clinvar = data_dir / clinvar_name
    clinvar_md5 = published_md5(
        f"{clinvar_url}.md5", data_dir / f"{clinvar_name}.md5", clinvar_name
    )
    fetch_verified(clinvar_url, clinvar, clinvar_md5)

    hg38 = data_dir / "hg38"
    references = []
    for chromosome in chromosomes:
        name = f"{ucsc_name(chromosome)}.fa.gz"
        checksum = published_md5(f"{UCSC_HG38}/md5sum.txt", hg38 / "md5sum.txt", name)
        fetch_verified(f"{UCSC_HG38}/{name}", hg38 / name, checksum)
        references.append({"file": name, "url": f"{UCSC_HG38}/{name}", "md5": checksum})

    tag = "-".join(chromosomes)
    vcf = data_dir / f"clinvar_{release}.chr{tag}.vcf.gz"
    subset = subset_vcf(clinvar, set(chromosomes), vcf)
    reference = data_dir / f"hg38.chr{tag}.fa.gz"
    reference_stats = build_reference([hg38 / r["file"] for r in references], reference)
    return {
        "release": release,
        "clinvar_url": clinvar_url,
        "clinvar_md5": clinvar_md5,
        "chromosomes": chromosomes,
        "references": references,
        "vcf": vcf,
        "reference": reference,
        **subset,
        **reference_stats,
    }


def subset_vcf(source: Path, chromosomes: set[str], dest: Path) -> dict[str, Any]:
    """Keep the header and the records on `chromosomes`, and count records per chromosome.

    The output is two gzip members, the first holding only the `##fileformat` line, so a run
    marker can be spliced in right after it without recompressing (see ClinVarSet).
    """
    stats_path = dest.with_name(dest.name + ".json")
    if dest.exists() and stats_path.exists():
        return dict(json.loads(stats_path.read_text()))
    print(f"keeping the records on chromosome(s) {', '.join(sorted(chromosomes))}", flush=True)
    per_chromosome: Counter[str] = Counter()
    partial = dest.with_name(dest.name + ".part")
    with gzip.open(source, "rb") as src, partial.open("wb") as raw:
        raw.write(gzip.compress(src.readline(), mtime=0))
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as out:
            batch: list[bytes] = []
            for line in src:
                if not line.startswith(b"#"):
                    chromosome = line[: line.index(b"\t")].decode()
                    per_chromosome[chromosome] += 1
                    if chromosome not in chromosomes:
                        continue
                batch.append(line)
                if len(batch) >= 50_000:
                    out.write(b"".join(batch))
                    batch.clear()
            out.write(b"".join(batch))
    partial.replace(dest)
    stats = {
        "records_in_release": sum(per_chromosome.values()),
        "records_kept": sum(per_chromosome[c] for c in chromosomes),
        "records_by_chromosome": dict(per_chromosome.most_common()),
    }
    stats_path.write_text(json.dumps(stats, indent=1))
    return stats


def build_reference(parts: list[Path], dest: Path) -> dict[str, Any]:
    """Concatenate per-chromosome .fa.gz files: the result is a valid multi-member gzip file,
    which the pipeline reads as one FASTA. Also counts the bases of each contig."""
    stats_path = dest.with_name(dest.name + ".json")
    if dest.exists() and stats_path.exists():
        return dict(json.loads(stats_path.read_text()))
    lengths: dict[str, int] = {}
    partial = dest.with_name(dest.name + ".part")
    with partial.open("wb") as out:
        for part in parts:
            out.write(part.read_bytes())
            with gzip.open(part, "rb") as fasta:
                name = ""
                for line in fasta:
                    if line.startswith(b">"):
                        name = line[1:].split()[0].decode()
                        lengths[name] = 0
                    else:
                        lengths[name] += len(line.rstrip())
    partial.replace(dest)
    stats = {"contig_lengths": lengths, "reference_bp": sum(lengths.values())}
    stats_path.write_text(json.dumps(stats, indent=1))
    return stats


def bcftools_check(vcf: Path, reference: Path, chromosomes: list[str], work: Path) -> dict | None:
    """Run `bcftools norm -m -any -f` over the same records and return its own tally.

    bcftools needs the contigs declared in the header and the reference to use the VCF's
    contig names (`1`, not `chr1`), so both are rewritten into a scratch directory first.
    """
    if shutil.which("bcftools"):
        prefix, ref_arg, vcf_arg = ["bcftools"], str(work / "ref.fa"), str(work / "in.vcf")
    elif shutil.which("docker"):
        prefix = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{work.resolve()}:/work",
            BCFTOOLS_IMAGE,
            "bcftools",
        ]
        ref_arg, vcf_arg = "/work/ref.fa", "/work/in.vcf"
    else:
        print("bcftools cross-check skipped: neither bcftools nor docker is on PATH")
        return None
    print("== bcftools norm over the same records", flush=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        ucsc_to_clinvar = {ucsc_name(c): c for c in chromosomes}
        with gzip.open(reference, "rb") as src, (work / "ref.fa").open("wb") as out:
            for line in src:
                if line.startswith(b">"):
                    name = line[1:].split()[0].decode()
                    line = f">{ucsc_to_clinvar.get(name, name)}\n".encode()
                out.write(line)
        with gzip.open(vcf, "rb") as src, (work / "in.vcf").open("wb") as out:
            for line in src:
                if line.startswith(b"#CHROM"):
                    out.write(b"".join(f"##contig=<ID={c}>\n".encode() for c in chromosomes))
                out.write(line)
        command = [*prefix, "norm", "-m", "-any", "-c", "w", "-f", ref_arg, "-Ou", "-o"]
        proc = subprocess.run(
            [*command, "/dev/null", vcf_arg], capture_output=True, text=True, timeout=1800
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    tally = re.search(r"total/split/joined/realigned/removed/skipped:\s+([\d/]+)", proc.stderr)
    if proc.returncode != 0 or not tally:
        print(f"bcftools cross-check failed:\n{proc.stderr[-2000:]}")
        return None
    total, split, _joined, realigned, removed, skipped = map(int, tally.group(1).split("/"))
    return {
        "version": "bcftools "
        + (BCFTOOLS_IMAGE.split(":")[1] if prefix[0] == "docker" else "(PATH)"),
        "total": total,
        "split": split,
        "realigned": realigned,
        "removed": removed,
        "skipped": skipped,
        "ref_mismatch_warnings": proc.stderr.count("REF_MISMATCH"),
    }


# -- measurement ------------------------------------------------------------------------------


class Dataset(Bench):
    """One input pair on the running stack. Reuses the synthetic benchmark's job helpers."""

    def __init__(self, label: str, api: ApiClient, vcf: Path, reference: Path) -> None:
        self.label = label
        self.api = api
        self.engine = get_engine()
        self.storage = get_storage()
        print(f"uploading {label}: {reference.name}, {vcf.name}", flush=True)
        self.reference_key = api.upload(reference)
        self.vcf = vcf.read_bytes()

    def marked_vcf(self) -> bytes:
        marker = f"##benchmark_run={uuid.uuid4()}\n".encode()
        if self.vcf[:2] != b"\x1f\x8b":
            end = self.vcf.index(b"\n") + 1
            return self.vcf[:end] + marker + self.vcf[end:]
        # gzip from subset_vcf: the first member is the ##fileformat line alone.
        first = zlib.decompressobj(wbits=31)
        first.decompress(self.vcf)
        assert first.eof and first.unused_data, "expected a two-member gzip from subset_vcf"
        boundary = len(self.vcf) - len(first.unused_data)
        return self.vcf[:boundary] + gzip.compress(marker, mtime=0) + self.vcf[boundary:]

    def unique_vcf_key(self) -> str:
        name = "variants.vcf.gz" if self.vcf[:2] == b"\x1f\x8b" else "variants.vcf"
        response = self.api.http.post("/api/v1/uploads", json={"filename": name})
        response.raise_for_status()
        body = response.json()
        httpx.put(body["upload_url"], content=self.marked_vcf(), timeout=900).raise_for_status()
        return str(body["key"])


def chunk_times(job_ids: set[str]) -> dict[str, list[float]]:
    logs = sh("docker", "compose", "logs", "--no-color", "worker")
    parse, copy = [], []
    for match in CHUNK_LOG.finditer(logs):
        if match.group("job") in job_ids:
            parse.append(float(match.group("parse")))
            copy.append(float(match.group("copy")))
    return {"parse_normalize_s": parse, "copy_s": copy}


def throughput(
    datasets: list[Dataset], workers: list[int], runs: int, warmup: int
) -> dict[str, Any]:
    """Job time per dataset and worker count; the datasets alternate run by run."""
    result: dict[str, Any] = {d.label: {"by_workers": {}} for d in datasets}
    single_worker_jobs: dict[str, set[str]] = defaultdict(set)
    for count in workers:
        print(f"== {count} worker container(s)", flush=True)
        scale_workers(count)
        for dataset in datasets:
            for i in range(warmup):
                wall, job = dataset.run_job()
                print(f"   {dataset.label} warm-up: {wall:.1f}s", flush=True)
                if count == workers[0] and i == 0:
                    result[dataset.label]["first_run"] = {"workers": count, "seconds": wall}
                dataset.delete_job(job["id"])
        walls: dict[str, list[float]] = defaultdict(list)
        for i in range(runs):
            for dataset in datasets:
                wall, job = dataset.run_job()
                total = job["counts"]["total"]
                walls[dataset.label].append(wall)
                result[dataset.label]["records"] = total
                print(
                    f"   run {i + 1} {dataset.label}: {wall:.1f}s, {total / wall:,.0f} records/s",
                    flush=True,
                )
                if count == 1:
                    single_worker_jobs[dataset.label].add(job["id"])
                dataset.delete_job(job["id"])
        for dataset in datasets:
            result[dataset.label]["by_workers"][str(count)] = walls[dataset.label]
    for dataset in datasets:
        if single_worker_jobs[dataset.label]:
            result[dataset.label]["chunks"] = chunk_times(single_worker_jobs[dataset.label])
    return result


def all_rejected(api: ApiClient, job_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    after = 0
    while True:
        page = api.get(f"/api/v1/jobs/{job_id}/rejected", after_line=after, limit=500)
        items += page["items"]
        if page.get("next_after_line") is None or not page["items"]:
            return items
        after = page["next_after_line"]


def reject_patterns(rejected: list[dict[str, Any]]) -> dict[str, Any]:
    """What the rejected lines of each reason have in common, from their raw text."""
    out: dict[str, Any] = {}
    by_reason: dict[str, list[list[str]]] = defaultdict(list)
    for item in rejected:
        by_reason[item["reason"]].append(item["raw_line"].split("\t"))
    for reason, rows in by_reason.items():
        pattern: Counter[str] = Counter()
        for fields in rows:
            alt = fields[4] if len(fields) > 4 else ""
            if reason == "unsupported_symbolic":
                pattern[f"ALT {alt}"] += 1
            elif reason == "invalid_allele_chars":
                odd = set(fields[3] + alt.replace(",", "")) - set("ACGTNacgtn")
                pattern["letters " + "".join(sorted(odd))] += 1
            elif reason in {"unknown_contig", "pos_out_of_bounds"}:
                pattern[f"CHROM {fields[0]}"] += 1
            elif reason == "non_numeric_qual" and len(fields) > 5:
                pattern[f"QUAL {fields[5]}"] += 1
            # ClinVar's own description of the record, where present.
            info = fields[7] if len(fields) > 7 else ""
            hgvs = re.search(r"CLNHGVS=([^;]+)", info)
            if hgvs and hgvs.group(1).endswith("%3D"):  # "g.N=": no change at position N
                pattern["HGVS g.N= (no change)"] += 1
            clinvar_type = re.search(r"CLNVC=([^;]+)", info)
            if clinvar_type:
                pattern[f"CLNVC {clinvar_type.group(1)}"] += 1
        examples = ["\t".join(fields[:7]) for fields in rows[:3]]
        hgvs_examples = [
            m.group(1).replace("%3D", "=")
            for fields in rows[:3]
            if len(fields) > 7 and (m := re.search(r"CLNHGVS=([^;]+)", fields[7]))
        ]
        out[reason] = {
            "sampled": len(rows),
            "patterns": dict(pattern.most_common(8)),
            "examples": examples,
            "hgvs_examples": hgvs_examples,
        }
    return out


def region_queries(dataset: Dataset, job_id: str, count: int, http_count: int) -> dict[str, Any]:
    """10 kb windows centred on randomly chosen stored variants of the job."""
    sql = text(
        "SELECT id, chrom, pos, ref, alt FROM variants "
        "WHERE job_id = :j AND chrom = :c AND pos BETWEEN :a AND :b ORDER BY pos"
    )

    def timed(conn: Any, windows: list[tuple[str, int, int]]) -> tuple[list[float], list[int]]:
        latencies, sizes = [], []
        for chrom, start, end in windows:
            started = time.perf_counter()
            rows = conn.execute(sql, {"j": job_id, "c": chrom, "a": start, "b": end}).fetchall()
            latencies.append((time.perf_counter() - started) * 1000)
            sizes.append(len(rows))
        return latencies, sizes

    with dataset.engine.connect() as conn:
        conn.execute(text("SELECT setseed(0.42)"))
        centres = conn.execute(
            text("SELECT chrom, pos FROM variants WHERE job_id = :j ORDER BY random() LIMIT :n"),
            {"j": job_id, "n": count},
        ).all()
        windows = [(c, max(1, p - WINDOW_BP // 2), p + WINDOW_BP // 2) for c, p in centres]
        timed(conn, windows[:20])  # warm the cache
        with_index, sizes = timed(conn, windows)
    # DROP INDEX is transactional: drop it inside a transaction that is rolled back.
    with dataset.engine.connect() as conn:
        conn.execute(text("DROP INDEX ix_variants_job_chrom_pos"))
        try:
            without_index, _ = timed(conn, windows)
        finally:
            conn.rollback()

    def page(chrom: str, start: int, end: int) -> tuple[float, int]:
        started = time.perf_counter()
        body = dataset.api.get(
            f"/api/v1/jobs/{job_id}/variants", chrom=chrom, start=start, end=end, limit=100
        )
        return (time.perf_counter() - started) * 1000, len(body["items"])

    page(*windows[0])
    http = [page(*window)[0] for window in windows[:http_count]]
    return {
        "windows": len(windows),
        "rows_per_window": sizes,
        "with_index_ms": with_index,
        "without_index_ms": without_index,
        "http_page_ms": http,
    }


def gene_lookup(dataset: Dataset, job_id: str, chromosomes: list[str]) -> dict[str, Any] | None:
    for chromosome, (gene, start, end) in GENES.items():
        if chromosome not in chromosomes:
            continue
        stored = ucsc_name(chromosome)
        with dataset.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT count(*) FROM variants "
                    "WHERE job_id = :j AND chrom = :c AND pos BETWEEN :a AND :b"
                ),
                {"j": job_id, "c": stored, "a": start, "b": end},
            ).scalar_one()
        timings = []
        for _ in range(11):
            started = time.perf_counter()
            body = dataset.api.get(
                f"/api/v1/jobs/{job_id}/variants", chrom=stored, start=start, end=end, limit=100
            )
            timings.append((time.perf_counter() - started) * 1000)
        return {
            "gene": gene,
            "region": f"{stored}:{start:,}-{end:,}",
            "rows": rows,
            "page_items": len(body["items"]),
            "page_ms": timings[1:],  # the first call warms the cache
        }
    return None


def analyse(
    dataset: Dataset, query_count: int, http_count: int, chromosomes: list[str] | None
) -> dict[str, Any]:
    """One more job, kept long enough to read its stats, rejects and query latency."""
    print(f"== {dataset.label}: statistics, rejected records, region queries", flush=True)
    _, job = dataset.run_job()
    job_id = job["id"]
    try:
        summary = dataset.api.get(f"/api/v1/jobs/{job_id}/stats/summary")
        rejected = all_rejected(dataset.api, job_id)
        dataset.vacuum()
        result = {
            "job": {k: job[k] for k in ("counts", "progress", "duration_seconds")},
            "summary": summary,
            "reject_patterns": reject_patterns(rejected),
            "queries": region_queries(dataset, job_id, query_count, http_count),
        }
        if chromosomes:
            result["gene"] = gene_lookup(dataset, job_id, chromosomes)
        return result
    finally:
        dataset.delete_job(job_id)


# -- report -----------------------------------------------------------------------------------


def med(values: list[float]) -> float:
    return statistics.median(values)


def p95(values: list[float]) -> float:
    return sorted(values)[int(0.95 * (len(values) - 1))]


def render(report: dict[str, Any]) -> str:
    inputs = report["inputs"]
    labels = list(report["throughput"])
    tp, an = report["throughput"], report["analysis"]
    workers = sorted({int(w) for label in labels for w in tp[label]["by_workers"]})
    chromosomes = ", ".join(inputs["chromosomes"])
    lines = [
        "# Benchmark on real data: ClinVar GRCh38",
        "",
        "Generated by `scripts/benchmark_clinvar.py`. Every number below was measured on the "
        "machine described at the end, in one session; the synthetic set was measured in the "
        "same session, alternating with ClinVar run by run, so the two columns are comparable. "
        "Re-run it to reproduce.",
        "",
        "## Inputs",
        "",
        f"- **ClinVar** release {inputs['release']} for GRCh38 ([{Path(inputs['clinvar_url']).name}]"
        f"({inputs['clinvar_url']}), MD5 `{inputs['clinvar_md5']}`, checked against NCBI's "
        f"`.md5`): {inputs['records_in_release']:,} records, sites only (no sample columns).",
        f"- **Subset**: chromosomes {chromosomes}, the {len(inputs['chromosomes'])} with the most "
        f"ClinVar records: **{inputs['records_kept']:,} records** "
        f"({100 * inputs['records_kept'] / inputs['records_in_release']:.1f}% of the release). "
        "The other records are dropped before upload, because against this reference they would "
        "all be rejected as `unknown_contig`.",
        "- **Reference**: GRCh38 as UCSC hg38 per-chromosome FASTA ("
        + ", ".join(f"[{r['file']}]({r['url']}) `{r['md5']}`" for r in inputs["references"])
        + f"; MD5s checked against UCSC's `md5sum.txt`), {inputs['reference_bp']:,} bp. ClinVar "
        f"names the contigs {', '.join(f'`{c}`' for c in inputs['chromosomes'])}, the reference "
        f"{', '.join(f'`{ucsc_name(c)}`' for c in inputs['chromosomes'])}; the pipeline's alias "
        "map matches them, and rows are stored under the reference's names.",
        "- **Synthetic**: the `make data` set: 1,000,000 records on 3 contigs of 5 Mb, 0.5% "
        "deliberately invalid (see [BENCHMARKS.md](BENCHMARKS.md)).",
        "",
        "## Side by side",
        "",
        "Job time is from `POST /jobs` returning to the job reaching `completed` (upload "
        f"excluded), median of {report['params']['runs']} runs after a warm-up run. Each worker "
        "container runs one Celery process; PostgreSQL, Redis, MinIO and the API run on the same "
        "machine.",
        "",
        "| | " + " | ".join(labels) + " |",
        "|---|" + "---:|" * len(labels),
    ]

    def row(name: str, values: list[str]) -> None:
        lines.append(f"| {name} | " + " | ".join(values) + " |")

    row("VCF records", [f"{tp[label]['records']:,}" for label in labels])
    row("variant rows stored", [f"{an[label]['summary']['variants']:,}" for label in labels])
    row(
        "chunks of 100k records",
        [str(an[label]["job"]["progress"]["chunks_total"]) for label in labels],
    )
    for count in workers:
        cells = []
        for label in labels:
            walls = tp[label]["by_workers"][str(count)]
            cells.append(
                f"{med(walls):.1f} s, **{tp[label]['records'] / med(walls):,.0f} records/s**"
            )
        row(f"{count} worker container{'s' if count > 1 else ''}", cells)
    if len(workers) > 1:
        row(
            f"speed-up, {workers[-1]} vs {workers[0]} workers",
            [
                f"{med(tp[label]['by_workers'][str(workers[0])]) / med(tp[label]['by_workers'][str(workers[-1])]):.2f}x"
                for label in labels
            ],
        )
    if all("first_run" in tp[label] for label in labels):
        row(
            f"first job on a fresh worker ({tp[labels[0]]['first_run']['workers']} worker; it also "
            "downloads and indexes the reference)",
            [f"{tp[label]['first_run']['seconds']:.1f} s" for label in labels],
        )
    if all(tp[label].get("chunks", {}).get("copy_s") for label in labels):
        row(
            "per chunk, parse + validate + normalize (median, 1 worker)",
            [f"{med(tp[label]['chunks']['parse_normalize_s']):.2f} s" for label in labels],
        )
        row(
            "per chunk, COPY into PostgreSQL (median, 1 worker)",
            [f"{med(tp[label]['chunks']['copy_s']):.2f} s" for label in labels],
        )
    row(
        "rejected records",
        [
            f"{an[label]['summary']['records']['rejected']:,} "
            f"({an[label]['summary']['records']['rejected_percent']}%)"
            for label in labels
        ],
    )
    row(
        "rows rewritten by normalization",
        [
            f"{an[label]['summary']['normalized']:,} ({an[label]['summary']['normalized_percent']}%)"
            for label in labels
        ],
    )
    row("Ti/Tv of SNVs", [str(an[label]["summary"]["titv_ratio"]) for label in labels])

    def type_mix(summary: dict[str, Any]) -> str:
        rows = max(summary["variants"], 1)
        by_type = sorted(summary["by_type"].items(), key=lambda kv: -kv[1])
        return ", ".join(f"{name} {100 * count / rows:.1f}%" for name, count in by_type)

    row("variant types", [type_mix(an[label]["summary"]) for label in labels])
    q = {label: an[label]["queries"] for label in labels}
    row(
        "region query (10 kb), with index: median / p95",
        [
            f"{med(q[label]['with_index_ms']):.2f} / {p95(q[label]['with_index_ms']):.2f} ms"
            for label in labels
        ],
    )
    row(
        "same, without the index",
        [
            f"{med(q[label]['without_index_ms']):.2f} / {p95(q[label]['without_index_ms']):.2f} ms"
            for label in labels
        ],
    )
    row(
        "rows per 10 kb window: median (max)",
        [
            f"{med(q[label]['rows_per_window']):.0f} ({max(q[label]['rows_per_window']):,})"
            for label in labels
        ],
    )
    row(
        "one API page (`GET /variants`, up to 100 rows): median",
        [f"{med(q[label]['http_page_ms']):.1f} ms" for label in labels],
    )
    n_windows = q[labels[0]]["windows"]
    lines += [
        "",
        f"Region queries: {n_windows} windows of {WINDOW_BP // 1000} kb, each centred on a stored "
        "variant picked at random (so every window holds data, as when someone looks up a gene "
        "or a variant), timed directly in PostgreSQL "
        '(`WHERE job_id = ? AND chrom = ? AND pos BETWEEN ? AND ? ORDER BY pos`). "Without the '
        'index" drops `(job_id, chrom, pos)` inside a transaction that is rolled back. The API '
        f"page is the same query through FastAPI for the first {len(q[labels[0]]['http_page_ms'])} "
        "windows, timed from the client.",
    ]
    for note in report.get("notes", []):
        lines += ["", f"**About this run.** {note}"]
    bcftools = report.get("bcftools")
    if bcftools and "ClinVar" in an:
        rewritten = an["ClinVar"]["summary"]["normalized"]
        agree = bcftools["split"] + bcftools["realigned"] == 0 and rewritten == 0
        lines += [
            "",
            f"**Normalization cross-check.** `bcftools norm -m -any -f` ({bcftools['version']}) "
            f"over the same {bcftools['total']:,} ClinVar records: {bcftools['split']:,} split, "
            f"{bcftools['realigned']:,} realigned, {bcftools['ref_mismatch_warnings']:,} REF "
            f"mismatches. The pipeline rewrote {rewritten:,} rows. "
            + (
                "Both agree that ClinVar ships its records already split, trimmed and "
                "left-aligned, so zero is the expected answer here; on the synthetic set, which "
                "contains unaligned indels on purpose, the same normalizer rewrites about a "
                "quarter of the rows."
                if agree
                else "See the raw numbers below."
            ),
        ]
    gene = an.get("ClinVar", {}).get("gene")
    if gene:
        lines += [
            "",
            f"**{gene['gene']}** ({gene['region']}): {gene['rows']:,} ClinVar variants stored; the "
            f"first API page ({gene['page_items']} rows) takes {med(gene['page_ms']):.1f} ms "
            f"(median of {len(gene['page_ms'])}).",
        ]

    clinvar = an.get("ClinVar")
    if clinvar:
        reasons = clinvar["summary"]["rejected_by_reason"]
        lines += [
            "",
            "## Rejected ClinVar records and why",
            "",
            "Counts are exact (from the job); the patterns and examples come from the stored "
            "rejected lines.",
            "",
            "| reason | records | what the lines have in common | example (first 7 columns) |",
            "|---|---:|---|---|",
        ]
        for reason, stat in sorted(reasons.items(), key=lambda kv: -kv[1]["count"]):
            info = clinvar["reject_patterns"].get(reason, {})
            patterns = "; ".join(f"{k}: {v:,}" for k, v in info.get("patterns", {}).items())
            example = "`" + info.get("examples", [""])[0].replace("\t", " ") + "`"
            hgvs = info.get("hgvs_examples", [])
            if hgvs:
                example += f", `CLNHGVS={hgvs[0]}`"
            lines.append(f"| `{reason}` | {stat['count']:,} | {patterns} | {example} |")
        symbolic = clinvar["reject_patterns"].get("unsupported_symbolic", {}).get("patterns", {})
        if "ALT ." in symbolic:
            lines += [
                "",
                "`ALT .` means the record has no alternate allele. In ClinVar these are "
                "submissions about the reference allele itself (the HGVS expression `g.N=` means "
                '"no change at position N"): there is no variant to normalize or store, so the '
                "pipeline refuses them as `unsupported_symbolic`, the same rule that refuses "
                "`<DEL>` or `*`.",
            ]
    lines += [
        "",
        "## Environment",
        "",
        *(f"- **{key.replace('_', ' ')}**: {value}" for key, value in report["specs"].items()),
        f"- **measured at**: {report['measured_at']}",
        "",
        "## Exact commands",
        "",
        "```",
        "make up",
        "make data",
        report["command"],
        "```",
        "",
        "## Raw numbers",
        "",
        "```json",
        json.dumps(report["raw"], indent=1),
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--data-dir", default=ROOT / "data" / "clinvar", type=Path)
    parser.add_argument("--release", default=CLINVAR_RELEASE, help="ClinVar release, YYYYMMDD")
    parser.add_argument("--chromosomes", nargs="+", default=DEFAULT_CHROMOSOMES)
    parser.add_argument("--synthetic-dir", default=ROOT / "data" / "bench", type=Path)
    parser.add_argument("--no-synthetic", action="store_true", help="measure ClinVar only")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--query-count", type=int, default=100)
    parser.add_argument("--http-count", type=int, default=30)
    parser.add_argument("--no-bcftools", action="store_true", help="skip the bcftools cross-check")
    parser.add_argument("--out", default=ROOT / "docs" / "BENCHMARKS_CLINVAR.md", type=Path)
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="a remark about this run's conditions, printed under the table (repeatable)",
    )
    parser.add_argument(
        "--render",
        type=Path,
        metavar="JSON",
        help="only rewrite --out from a saved measurement (data/clinvar/last_report.json)",
    )
    args = parser.parse_args()

    if args.render:
        report = json.loads(args.render.read_text())
        args.out.write_text(render(report), encoding="utf-8", newline="\n")
        print(f"wrote {args.out}")
        return 0

    synthetic_vcf = args.synthetic_dir / "variants.vcf"
    if not args.no_synthetic and not synthetic_vcf.exists():
        print(f"{synthetic_vcf} is missing: run `make data` (or pass --no-synthetic)")
        return 1
    inputs = prepare_inputs(args.data_dir, args.release, args.chromosomes)
    bcftools = None
    if not args.no_bcftools:
        bcftools = bcftools_check(
            inputs["vcf"], inputs["reference"], args.chromosomes, args.data_dir / "bcftools-work"
        )

    api = ApiClient(args.api)
    api.wait_ready()
    datasets = []
    if not args.no_synthetic:
        datasets.append(Dataset("synthetic", api, synthetic_vcf, args.synthetic_dir / "ref.fa"))
    datasets.append(Dataset("ClinVar", api, inputs["vcf"], inputs["reference"]))

    try:
        throughput_results = throughput(datasets, args.workers, args.runs, args.warmup)
        analysis = {
            d.label: analyse(
                d,
                args.query_count,
                args.http_count,
                args.chromosomes if d.label == "ClinVar" else None,
            )
            for d in datasets
        }
    finally:
        scale_workers(1)

    report = {
        "specs": machine_specs(),
        "measured_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        # The stack's ports, when not the defaults, are part of how to reproduce the run.
        "command": " ".join(
            [
                *(f"{k}={os.environ[k]}" for k in ENV_SHOWN if k in os.environ),
                "uv run python",
                *(shlex.quote(a) for a in sys.argv),
            ]
        ),
        "notes": args.note,
        "params": {"runs": args.runs, "warmup": args.warmup, "workers": args.workers},
        "inputs": {k: str(v) if isinstance(v, Path) else v for k, v in inputs.items()},
        "throughput": throughput_results,
        "analysis": analysis,
        "bcftools": bcftools,
    }
    report["raw"] = {
        "params": report["params"],
        "throughput": throughput_results,
        "region_queries": {label: a["queries"] for label, a in analysis.items()},
        "rejected_by_reason": {
            label: a["summary"]["rejected_by_reason"] for label, a in analysis.items()
        },
        "records_by_chromosome_in_release": inputs["records_by_chromosome"],
        "bcftools_norm": bcftools,
    }
    # JSON round trip first, so a later `--render` sees exactly what this run renders.
    saved = args.data_dir / "last_report.json"
    saved.write_text(json.dumps(report, indent=1))
    report = json.loads(saved.read_text())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(report), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
