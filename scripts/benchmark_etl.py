"""Benchmark: naive ingestion vs. streaming + bulk-loading ETL.

BASELINE   reads the whole file into memory, builds every row up front and inserts them
           one by one with the ORM (`session.add()` + `commit()` per row).
OPTIMIZED  streams the file line by line, processes fixed-size batches
           (parse -> validate -> normalize) and bulk-inserts each batch
           (`execute_values` on PostgreSQL, Core executemany on SQLite).

Both pipelines share the exact same parser, validator and normalizer; only the I/O
strategy and the insertion strategy differ. Time and memory are measured in separate
passes (tracemalloc slows Python down, so it must not run during the timing pass).

Usage:
    python scripts/benchmark_etl.py [--records 20000] [--batch-size 5000]
    BENCHMARK_DATABASE_URL=postgresql+psycopg2://... python scripts/benchmark_etl.py
Exit status is 1 when the optimized pipeline does not beat the baseline by --min-reduction %.
"""

import argparse
import gc
import os
import sys
import tempfile
import time
import tracemalloc
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, delete, func, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402
from app.etl.loader import (  # noqa: E402
    bulk_insert_variants,
    naive_insert_variants,
    transform_records,
)
from app.etl.parser import parse_vcf_stream  # noqa: E402
from app.models import DatasetJob, GenomicVariant  # noqa: E402
from scripts.generate_sample_data import write_vcf_stream  # noqa: E402


@dataclass
class RunResult:
    name: str
    seconds: float
    records: int
    valid: int
    inserted: int
    peak_mb: float = 0.0

    @property
    def throughput(self) -> float:
        return self.records / self.seconds if self.seconds > 0 else 0.0


def build_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    return create_engine(url, pool_pre_ping=True)


def stream_file_lines(path: Path) -> Iterator[str]:
    """Constant-memory line iterator (stand-in for `S3StorageClient.stream_lines`)."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line in handle:
            yield line.rstrip("\r\n")


def run_baseline(session: Session, job_id: uuid.UUID, path: Path) -> Tuple[int, int, int]:
    """Full-file load + row-by-row ORM commits."""
    lines = path.read_text(encoding="utf-8").splitlines()  # whole file in memory
    records = [
        record
        for batch in parse_vcf_stream(lines, batch_size=len(lines) + 1)
        for record in batch
    ]
    transformed = transform_records(records, job_id)  # every row materialised at once
    inserted = naive_insert_variants(session, transformed.rows)
    return transformed.total, transformed.valid, inserted


def run_optimized(
    session: Session, job_id: uuid.UUID, path: Path, batch_size: int
) -> Tuple[int, int, int]:
    """Streaming batches + bulk insert."""
    total = valid = inserted = 0
    for batch in parse_vcf_stream(stream_file_lines(path), batch_size=batch_size):
        transformed = transform_records(batch, job_id)
        inserted += bulk_insert_variants(session, transformed.rows)
        session.commit()
        total += transformed.total
        valid += transformed.valid
    return total, valid, inserted


def prepare_job(factory: sessionmaker) -> uuid.UUID:
    """Create a dedicated job row owning this run's variants."""
    job_id = uuid.uuid4()
    with factory() as session:
        session.add(
            DatasetJob(id=job_id, filename="benchmark.vcf", file_type="VCF", s3_key="-")
        )
        session.commit()
    return job_id


def cleanup_job(factory: sessionmaker, job_id: uuid.UUID) -> None:
    """Remove only this benchmark run's rows; data from other jobs is never touched."""
    with factory() as session:
        session.execute(delete(GenomicVariant).where(GenomicVariant.job_id == job_id))
        session.execute(delete(DatasetJob).where(DatasetJob.id == job_id))
        session.commit()


def measure(
    name: str,
    factory: sessionmaker,
    pipeline: Callable[[Session, uuid.UUID], Tuple[int, int, int]],
    with_memory: bool,
) -> RunResult:
    job_id = prepare_job(factory)
    gc.collect()
    if with_memory:
        tracemalloc.start()
    started = time.perf_counter()
    with factory() as session:
        total, valid, inserted = pipeline(session, job_id)
    seconds = time.perf_counter() - started
    peak_mb = 0.0
    if with_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_mb = peak / (1024 * 1024)
    with factory() as session:
        stored = (
            session.scalar(
                select(func.count())
                .select_from(GenomicVariant)
                .where(GenomicVariant.job_id == job_id)
            )
            or 0
        )
    cleanup_job(factory, job_id)
    if stored != inserted:
        raise RuntimeError(f"{name}: inserted {inserted} rows but job holds {stored}")
    gc.collect()
    return RunResult(name, seconds, total, valid, inserted, peak_mb)


def reduction(baseline: float, optimized: float) -> float:
    return (baseline - optimized) / baseline * 100.0 if baseline > 0 else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--records", type=int, default=20_000, help="VCF data lines to generate")
    parser.add_argument("--batch-size", type=int, default=5_000, help="optimized batch size")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("BENCHMARK_DATABASE_URL", "sqlite://"),
        help="default: in-memory SQLite (set to a PostgreSQL URL for real numbers)",
    )
    parser.add_argument("--min-reduction", type=float, default=45.0)
    args = parser.parse_args()

    engine = build_engine(args.database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dialect = engine.dialect.name

    with tempfile.TemporaryDirectory() as tmp:
        vcf_path = Path(tmp) / "benchmark.vcf"
        with vcf_path.open("wb") as handle:
            write_vcf_stream(handle, args.records, seed=42)
        size_mb = vcf_path.stat().st_size / (1024 * 1024)

        baseline_fn = lambda s, j: run_baseline(s, j, vcf_path)  # noqa: E731
        optimized_fn = lambda s, j: run_optimized(s, j, vcf_path, args.batch_size)  # noqa: E731

        print(f"Benchmarking {args.records:,} VCF lines ({size_mb:.1f} MB) on {dialect}...")
        # Pass 1: timing only. Pass 2: peak Python heap with tracemalloc.
        base_time = measure("Baseline", factory, baseline_fn, with_memory=False)
        opt_time = measure("Optimized", factory, optimized_fn, with_memory=False)
        base_mem = measure("Baseline", factory, baseline_fn, with_memory=True)
        opt_mem = measure("Optimized", factory, optimized_fn, with_memory=True)

    base_time.peak_mb = base_mem.peak_mb
    opt_time.peak_mb = opt_mem.peak_mb
    time_cut = reduction(base_time.seconds, opt_time.seconds)
    mem_cut = reduction(base_time.peak_mb, opt_time.peak_mb)
    speedup = base_time.seconds / opt_time.seconds if opt_time.seconds > 0 else float("inf")
    loader = (
        "psycopg2 execute_values (page_size=10000)"
        if dialect == "postgresql"
        else "SQLAlchemy Core executemany (SQLite fallback)"
    )

    print()
    print("## Genomic ETL benchmark")
    print()
    print(f"- Input: {args.records:,} VCF lines -> {opt_time.records:,} decomposed records "
          f"({opt_time.valid:,} valid, {opt_time.records - opt_time.valid:,} rejected)")
    print(f"- Database: {dialect}; optimized loader: {loader}; batch size: {args.batch_size:,}")
    print("- Baseline: whole file in RAM + one ORM object and one COMMIT per row")
    print("- Optimized: line streaming + batched validate/normalize + bulk insert")
    print()
    print("| Pipeline  | Ingestion time (s) | Throughput (records/s) | Peak Python heap (MB) |")
    print("|-----------|-------------------:|-----------------------:|----------------------:|")
    for run in (base_time, opt_time):
        print(f"| {run.name:<9} | {run.seconds:>18.2f} | {run.throughput:>22,.0f} | {run.peak_mb:>21.1f} |")
    print(f"| Reduction | {time_cut:>17.1f}% | {speedup:>21.1f}x | {mem_cut:>20.1f}% |")
    print()

    passed = time_cut >= args.min_reduction and mem_cut >= args.min_reduction
    verdict = "PASS" if passed else "FAIL"
    print(f"{verdict}: ingestion time reduced by {time_cut:.1f}% and memory by {mem_cut:.1f}% "
          f"(target >= {args.min_reduction:.0f}%)")
    engine.dispose()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
