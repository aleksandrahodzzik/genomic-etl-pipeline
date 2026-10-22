# Genomic Data ETL Pipeline & Variant Analytics Service

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-OpenAPI%2FSwagger-009688?logo=fastapi&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-distributed%20workers-37814A?logo=celery&logoColor=white)
![MinIO](https://img.shields.io/badge/MinIO-S3--compatible-C72E49?logo=minio&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-compose-2496ED?logo=docker&logoColor=white)

> An **asynchronous ETL pipeline** that parses, validates and normalizes genomic variant datasets (**VCF / FASTA**), processing **100k+ records per batch**; **distributed background processing** with **Celery** and **S3-compatible storage (MinIO)**; and an **indexed PostgreSQL** schema served through **FastAPI REST endpoints with OpenAPI/Swagger docs** for fast statistical querying.

| Resume claim | Where it lives in the code |
|---|---|
| Async ETL that parses, validates and normalizes VCF/FASTA, **100k+ records per batch** | `app/etl/parser.py`, `validator.py`, `normalizer.py`; `ETL_BATCH_SIZE=100000` in `app/config.py` |
| Celery + MinIO, **reduces ingestion bottlenecks by 45%+** | `app/worker/tasks.py`, `app/storage.py` (1 MiB streaming), `app/etl/loader.py` (`execute_values`), proven by `scripts/benchmark_etl.py` (CI-enforced) |
| **Indexed PostgreSQL** + **FastAPI** with **OpenAPI/Swagger** | `app/models.py` (3 strategic indexes), `app/api/routes_*.py`, Swagger UI at `/docs` |
| Stack | Python 3.11 · FastAPI · Celery · MinIO · PostgreSQL · Docker |

---

## Architecture

```mermaid
flowchart LR
    Client([Client / Swagger UI]) -->|POST /api/v1/etl/upload<br/>POST /api/v1/etl/demo-100k| API[FastAPI]
    API -->|1. stream file| MinIO[(MinIO<br/>S3 bucket)]
    API -->|2. INSERT job = PENDING| PG[(PostgreSQL)]
    API -->|3. process_genomic_dataset_task.delay| Redis{{Redis<br/>broker + results}}
    Redis --> Worker[Celery worker<br/>prefork x4]
    Worker -->|stream 1 MiB chunks<br/>iter_lines| MinIO

    subgraph Pipeline [Streaming ETL inside the worker]
        direction LR
        Parser[Streaming parser<br/>VCF / FASTA<br/>100k-record batches] --> Validator[Validator<br/>IUPAC, POS, QUAL, AF] --> Normalizer[Normalizer<br/>chrom names, allele trimming,<br/>classification, Ti/Tv]
    end

    Worker --> Parser
    Normalizer --> Loader[Bulk loader<br/>psycopg2 execute_values]
    Loader --> PG
    PG --> Analytics[Analytics API<br/>/variants  /summary  /fasta]
    Analytics --> Client
    Client -->|GET /api/v1/etl/jobs/id| API
```

**Request lifecycle.** The API never parses genomic data. It streams the upload to MinIO, records a `PENDING` job and returns `202 Accepted` immediately. A Celery worker (acks late, prefetch 1) pulls the object back out of MinIO in 1 MiB chunks, pushes 100,000-record batches through parse → validate → normalize → bulk insert, and commits job counters in the same transaction as each batch. Clients poll `GET /api/v1/etl/jobs/{id}` for `batches_processed`, `valid_records`, `rejected_records` and `throughput_records_per_sec`.

---

## Engineering Deep Dive

### 1. Streaming + `execute_values` bulk loading (>45% faster ingestion)

The naive approach to loading a VCF has three bottlenecks:

| Bottleneck | Naive approach | This pipeline |
|---|---|---|
| **I/O + memory** | Download/read the whole file, build every row in RAM | `S3StorageClient.stream_lines()` reads the object body in **1 MiB chunks** (`StreamingBody.iter_lines`); the parser yields **bounded batches**; only one batch is resident at a time |
| **Round trips** | One `INSERT` per row | `psycopg2.extras.execute_values` sends multi-row `INSERT ... VALUES (...), (...)` statements of `page_size=10000` rows; a batch is sent in `DB_CHUNK_SIZE=25000` chunks |
| **Commit / ORM overhead** | One ORM object + one `COMMIT` (WAL flush) per row | One commit per batch; rows go through SQLAlchemy Core / raw cursor, never through ORM identity maps |

`scripts/benchmark_etl.py` runs both strategies on the same data with the *same* parser, validator and normalizer, so the only variables are the I/O and insertion strategy:

- **Baseline**: whole file in memory, every row built up front, `session.add()` + `session.commit()` per row.
- **Optimized**: line streaming, batched validate/normalize, bulk insert (`execute_values` on PostgreSQL, chunked Core `executemany` on SQLite).

**Measured on PostgreSQL 16** (the Docker stack's `postgres` container, `execute_values` loader; 20,000 VCF lines → 21,734 decomposed records; batch size 5,000; Python 3.12 on a Windows laptop, Docker Desktop):

| Pipeline  | Ingestion time (s) | Throughput (records/s) | Peak Python heap (MB) |
|-----------|-------------------:|-----------------------:|----------------------:|
| Baseline  |              82.85 |                    262 |                  25.1 |
| Optimized |               1.94 |                 11,198 |                   9.1 |
| **Reduction** | **97.7%** | **42.7x** | **63.6%** |

Measured on in-memory SQLite (100,000 VCF lines → 108,772 records; 107,272 valid, 1,500 rejected; batch size 25,000), which has no network round trips or WAL flushes and is therefore the *least* favourable comparison:

| Pipeline  | Ingestion time (s) | Throughput (records/s) | Peak Python heap (MB) |
|-----------|-------------------:|-----------------------:|----------------------:|
| Baseline  |              26.60 |                  4,090 |                 124.4 |
| Optimized |               2.29 |                 47,412 |                  44.1 |
| **Reduction** | **91.4%** | **11.6x** | **64.6%** |

```bash
python scripts/benchmark_etl.py --records 100000 --batch-size 25000      # the run above
python scripts/benchmark_etl.py                                           # 20k-line quick run (used in CI)
BENCHMARK_DATABASE_URL=postgresql+psycopg2://genomics:genomics_secret@localhost:5432/genomics_db \
  python scripts/benchmark_etl.py                                         # exercises execute_values
```

The script exits non-zero if either the time or the memory reduction falls below **45%**, and CI runs it on every push, so the 45% bar is a regression gate and not just a claim.

> **Reading these numbers honestly.** Both pipelines read the file from local disk (not MinIO), so the benchmark isolates the parse/insert strategy rather than object-store latency. The PostgreSQL run goes through Docker Desktop's network layer, so absolute numbers depend on your hardware; the *ratio* is what matters. Peak heap is measured with `tracemalloc` (Python allocations only) in a separate pass from timing, because tracing slows Python down. With tiny inputs (a few thousand lines, where one batch is a large fraction of the file) the memory saving shrinks below 45% and the script reports `FAIL`; use the default sizes.
>
> The benchmark only touches rows it creates itself (its own job id) and removes them afterwards, so it is safe to point at a database that holds real data.

### 2. Bioinformatic variant normalization

Different callers write the *same* variant differently. Normalizing before storage is what makes joins, de-duplication and counting correct. Implemented in `app/etl/normalizer.py`:

**Chromosome standardization**

| Input | Stored |
|---|---|
| `1`, `chr1`, `chr01` | `chr1` |
| `x`, `X`, `chrx` | `chrX` |
| `MT`, `M`, `chrM` | `chrM` |
| `chrUn_KI270302v1`, `23`, `chr99` | rejected (not a primary chromosome) |

**Parsimonious allele trimming** (applied to each allele after multi-allelic decomposition):

1. Trim the shared **suffix** while both alleles are longer than 1 base.
2. Trim the shared **prefix** while both alleles are longer than 1 base, incrementing `POS` for each base removed on the left.

| Before (`POS`, `REF`, `ALT`) | After | Class |
|---|---|---|
| `100`, `ATCG`, `ATGG` | `102`, `C`, `G` | `SNP` (transversion) |
| `200`, `ATCGG`, `ATG` | `201`, `TCG`, `T` | `DELETION` |
| `50`, `CAT`, `GAT` | `50`, `C`, `G` | `SNP` (transversion) |
| `100`, `AT`, `A` | `100`, `AT`, `A` (anchor base kept) | `DELETION` |

Trimming is idempotent and reference-free: it removes redundant padding, but does not shift an indel left through a repeat, because that needs the reference genome. Because trimmed forms are canonical, two callers' representations of one event collapse to one row.

**Classification & QC**

- `SNP`, `INSERTION`, `DELETION`, `MNP` by allele length. Length-changing events where the shorter allele is not a prefix/suffix of the longer one (e.g. `AT → GCA`) are labelled `COMPLEX` instead of being mislabelled as indels.
- `is_transition` is true for `A↔G` and `C↔T` SNPs; `/api/v1/analytics/summary` reports the **Ti/Tv ratio** (`transitions / transversions`), the standard variant-call sanity metric.
- Multi-allelic sites (`ALT=G,T`) are **decomposed** into one record per ALT, and `AF`/`DP` are taken from the matching comma-separated position.
- **Validation** rejects (and counts) bad rows instead of failing the batch: non-ACGTN alleles, symbolic alleles (`<DEL>`), `REF == ALT`, `POS <= 0`, negative or non-finite `QUAL`, `AF` outside `[0, 1]`, unknown chromosomes and truncated lines.

### 3. PostgreSQL indexing strategy

Defined in `app/models.py` (`__table_args__`), created automatically on API startup:

| Index | Columns | Serves |
|---|---|---|
| `ix_variants_chrom_pos` | `(chrom, pos)` | Genomic region queries: `chrom = ? AND pos BETWEEN ? AND ?`; also matches the default `ORDER BY chrom, pos, id`, so `LIMIT` pages walk the index in order |
| `ix_variants_type_af` | `(variant_type, allele_frequency)` | Rare-variant and class filters: `variant_type = 'SNP' AND allele_frequency < 0.01` |
| `ix_variants_job_filter` | `(job_id, filter_status)` | Job-scoped QC filtering (`job_id = ? AND filter_status = 'PASS'`); its `job_id` prefix also serves per-job scans and `ON DELETE CASCADE` |

Reproduce the plans on your own data (`docker compose exec postgres psql -U genomics -d genomics_db`):

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM genomic_variants
WHERE chrom = 'chr1' AND pos BETWEEN 1000000 AND 2000000
ORDER BY chrom, pos, id LIMIT 100;

EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM genomic_variants
WHERE variant_type = 'SNP' AND allele_frequency < 0.01;
```

Actual output from PostgreSQL 16 on the demo dataset (107,272 variants, after `ANALYZE`; `COSTS OFF` for brevity):

```text
-- 1. Region query: chr1 1,000,000-2,000,000
Limit (actual time=0.097..0.105 rows=32 loops=1)
  Buffers: shared hit=6
  ->  Sort (actual time=0.096..0.099 rows=32 loops=1)
        Sort Key: pos, id
        ->  Bitmap Heap Scan on genomic_variants (actual time=0.047..0.056 rows=32 loops=1)
              Recheck Cond: (chrom = 'chr1') AND (pos >= 1000000) AND (pos <= 2000000)
              Heap Blocks: exact=3
              ->  Bitmap Index Scan on ix_variants_chrom_pos (actual time=0.040..0.040 rows=36 loops=1)
                    Index Cond: (chrom = 'chr1') AND (pos >= 1000000) AND (pos <= 2000000)
Execution Time: 0.234 ms

-- 2. Rare SNPs: variant_type = 'SNP' AND allele_frequency < 0.01  (50,258 matching rows)
Aggregate (actual time=41.287..41.289 rows=1 loops=1)
  ->  Index Only Scan using ix_variants_type_af on genomic_variants
        Index Cond: (variant_type = 'SNP') AND (allele_frequency < 0.01)
        Heap Fetches: 10040
Execution Time: 41.340 ms

-- 3. Job-scoped QC filter: job_id = ? AND filter_status = 'PASS'  (106,279 matching rows)
Aggregate (actual time=20.794..20.795 rows=1 loops=1)
  ->  Index Only Scan using ix_variants_job_filter on genomic_variants
        Index Cond: (job_id = '...') AND (filter_status = 'PASS')
        Heap Fetches: 190
Execution Time: 21.260 ms
```

Note that query 1 touches 6 buffers in 0.23 ms for a selective region, while queries 2 and 3 return roughly half or all of the table and are served by index-only scans (no heap reads except for pages not yet marked all-visible). Without these indexes the planner has only a `Seq Scan` over the whole table available, whose cost grows linearly with table size. Every variants response includes `query_time_ms` so the effect is visible from the API.

### 4. Where the load time goes (measured)

Profiling one 100,000-record batch (98,615 valid rows) against the Docker PostgreSQL container, from the host:

| Stage | Time |
|---|---:|
| Stream + parse | 0.27 s |
| Validate + normalize | 1.02 s |
| `execute_values` insert, `page_size=10000`, **no indexes** | 3.41 s |
| `execute_values` insert, `page_size=1000`, no indexes | 5.13 s |
| `execute_values` insert, `page_size=10000`, **3 indexes** | 6.87 s |

Two takeaways: the larger `page_size` is measurably faster (fewer statements and round trips), and the three indexes roughly double insert time. The indexes are kept in place while loading so the table stays queryable and the pipeline stays simple; for one-off 100M+ row loads, dropping and rebuilding them around the load is the next optimization. The worker logs the same per-batch breakdown (`stream+parse`, `validate+normalize`, `load`) so you can see it for your own data.

### 5. Verified on the Docker stack

Run against the full `docker compose` stack (PostgreSQL 16, Redis, MinIO, API, one Celery worker with `--concurrency=4`) on a Windows laptop with Docker Desktop:

| Scenario | Result |
|---|---|
| `demo-100k` (100,000 lines) | 108,772 records in 2 batches, ~8.5 s end to end, ~12.5k records/s |
| `demo-100k?num_variants=500000` (the endpoint maximum) | 544,280 records (536,599 valid, 7,681 rejected) in 6 batches, 44 s; worker memory stayed flat at ~300 MiB (streaming, not whole-file loading) |
| 3 demo jobs submitted at once | All `COMPLETED` in ~9 s wall-clock on three different worker processes (`ForkPoolWorker-2/3/4`), versus ~27 s if run serially |
| FASTA upload (5 contigs, 476,569 bp) | `COMPLETED`; per-contig length, GC %, N count and MD5 returned by `/analytics/fasta/{job_id}` |
| Containers | All five report `healthy`; api and worker run as an unprivileged user |

Throughput on this setup is dominated by the PostgreSQL insert (see section 4), and Docker Desktop's network layer on Windows adds overhead; a Linux host with a local database will be faster.

---

## Quick Start

**Prerequisites:** Docker with Compose v2.

```bash
cd genomic-etl-pipeline        # the repository root (where docker-compose.yml lives)
docker compose up --build
```

Five services start in dependency order (each waits for `service_healthy`):

| Service | URL / port | Notes |
|---|---|---|
| `api` | http://localhost:8000/docs | Swagger UI (ReDoc at `/redoc`, schema at `/openapi.json`) |
| `worker` | n/a | Celery, 4 prefork processes |
| `postgres` | `localhost:5432` | `genomics` / `genomics_secret`, db `genomics_db` |
| `redis` | `localhost:6379` | broker + result backend |
| `minio` | http://localhost:9001 | console login `minioadmin` / `minioadmin`; S3 API on `:9000` |

`make up` / `make down` / `make logs` wrap the same commands (`make down` also deletes volumes). On Windows without `make`, run the underlying `docker compose` / `python` commands from the `Makefile` directly.

> **MinIO image.** The official `minio/minio` images are no longer published on Docker Hub or quay.io, so `docker-compose.yml` pins a community build of the same server (`ghcr.io/coollabsio/minio`). It speaks the identical S3 API; swap the `image:` line if you host your own MinIO build.

### One-click demo in Swagger

1. Open http://localhost:8000/docs.
2. Expand **`POST /api/v1/etl/demo-100k`**, click **Try it out → Execute** (default `num_variants=100000`).
3. Copy the returned `id`, then call **`GET /api/v1/etl/jobs/{job_id}`** until `status` is `COMPLETED`.
4. Explore **`GET /api/v1/analytics/summary`** and **`GET /api/v1/analytics/variants`**.

### `curl` walkthrough

```bash
# 1. Generate + ingest a synthetic 100k-variant VCF (returns immediately: 202 Accepted)
curl -X POST "http://localhost:8000/api/v1/etl/demo-100k?num_variants=100000"
```

```json
{
  "id": "b8eb6085-2dfe-4239-a274-2239017a1e21",
  "filename": "demo_100000_variants.vcf",
  "file_type": "VCF",
  "s3_key": "uploads/b8eb6085-2dfe-4239-a274-2239017a1e21/demo_100000_variants.vcf",
  "status": "PENDING",
  "total_records": 0,
  "valid_records": 0,
  "rejected_records": 0,
  "batches_processed": 0,
  "processing_time_ms": null,
  "throughput_records_per_sec": null,
  "error_message": null,
  "created_at": "2026-10-20T14:46:17.483754Z",
  "completed_at": null
}
```

```bash
# 2. Poll the job
curl http://localhost:8000/api/v1/etl/jobs/b8eb6085-2dfe-4239-a274-2239017a1e21
```

Actual response once finished, from the Docker stack (a 100,000-line demo file decomposes to 108,772 records, i.e. two batches at `ETL_BATCH_SIZE=100000`; timings vary with hardware):

```json
{
  "id": "b8eb6085-2dfe-4239-a274-2239017a1e21",
  "filename": "demo_100000_variants.vcf",
  "file_type": "VCF",
  "status": "COMPLETED",
  "total_records": 108772,
  "valid_records": 107272,
  "rejected_records": 1500,
  "batches_processed": 2,
  "processing_time_ms": 8732.941,
  "throughput_records_per_sec": 12455.37,
  "error_message": null,
  "completed_at": "2026-10-20T14:46:26.285756Z"
}
```

```bash
# 3. Region query through ix_variants_chrom_pos (chromosome aliases like "1" are accepted)
curl "http://localhost:8000/api/v1/analytics/variants?chrom=1&start_pos=1&end_pos=20000000&limit=1"
```

```json
{
  "query_time_ms": 9.219,
  "total": 644,
  "limit": 1,
  "offset": 0,
  "items": [
    {
      "id": 1,
      "job_id": "b8eb6085-2dfe-4239-a274-2239017a1e21",
      "chrom": "chr1",
      "pos": 34789,
      "variant_id": "rs715121513",
      "ref_allele": "A",
      "alt_allele": "C",
      "quality": 42.11,
      "filter_status": "PASS",
      "variant_type": "SNP",
      "read_depth": 88,
      "allele_frequency": 0.0813,
      "is_transition": false
    }
  ]
}
```

Add `variant_type`, `min_af`/`max_af`, `min_dp`, `min_qual` or `filter_status` to narrow the result. `total` and `query_time_ms` (measured on the Docker stack, including the count query) vary with filters and hardware.

```bash
# 4. Cohort-level statistics
curl "http://localhost:8000/api/v1/analytics/summary?job_id=b8eb6085-2dfe-4239-a274-2239017a1e21"
```

```json
{
  "job_id": "b8eb6085-2dfe-4239-a274-2239017a1e21",
  "chrom": null,
  "total_variants": 107272,
  "variant_type_counts": {"SNP": 89883, "INSERTION": 5977, "DELETION": 9404, "MNP": 2008, "COMPLEX": 0},
  "transitions": 56452,
  "transversions": 33431,
  "ti_tv_ratio": 1.6886,
  "avg_read_depth": 59.687,
  "avg_allele_frequency": 0.061044,
  "avg_quality": 275.169,
  "chromosome_density": [
    {"chrom": "chr1", "count": 8747, "min_pos": 34789, "max_pos": 248936763, "mean_af": 0.06214},
    {"chrom": "chr2", "count": 8518, "min_pos": 31837, "max_pos": 242131417, "mean_af": 0.063026}
  ],
  "af_spectrum": {"rare": 59865, "low_freq": 26240, "common": 21167, "unknown": 0},
  "query_time_ms": 186.469
}
```

(`chromosome_density` is cut to two of the 24 chromosomes here for readability; the API returns all of them in `chr1..chr22, chrX, chrY, chrM` order.)

```bash
# 5. Upload your own files
curl -X POST http://localhost:8000/api/v1/etl/upload -F "file=@data/sample_100k.vcf"
curl -X POST http://localhost:8000/api/v1/etl/upload -F "file=@data/sample_reference.fasta"

# 6. FASTA statistics: contig length, GC %, N count, MD5
curl http://localhost:8000/api/v1/analytics/fasta/<fasta-job-id>

# 7. Health of PostgreSQL / Redis / MinIO (HTTP 503 if any is down)
curl http://localhost:8000/health
```

### API reference

| Method & path | Purpose |
|---|---|
| `POST /api/v1/etl/upload` (202) | Upload `.vcf`, `.fasta` or `.fa`; stored in MinIO, job created, Celery task dispatched |
| `POST /api/v1/etl/demo-100k` (202) | Generate a synthetic VCF (`num_variants` 1,000-500,000, default 100,000) and process it |
| `GET /api/v1/etl/jobs`, `GET /api/v1/etl/jobs/{job_id}` | List jobs (filter by `status`) / job progress and throughput |
| `GET /api/v1/analytics/variants` | Filters: `job_id`, `chrom`, `start_pos`, `end_pos`, `variant_type`, `filter_status`, `min_af`, `max_af`, `min_dp`, `min_qual`, `limit`, `offset`; returns `query_time_ms` |
| `GET /api/v1/analytics/summary` | Type counts, Ti/Tv, mean DP/AF/QUAL, density per chromosome, AF spectrum (`rare < 0.01`, `low_freq 0.01-0.05`, `common > 0.05`) |
| `GET /api/v1/analytics/fasta/{job_id}` | Contig lengths, GC %, N-base counts, MD5 |
| `GET /health` | PostgreSQL / Redis / MinIO connectivity |

---

## Local development & testing

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

make test              # pytest -v (SQLite + mocked S3/Celery: no Docker needed)
make generate-data     # writes data/sample_100k.vcf and data/sample_reference.fasta
make benchmark         # baseline vs optimized ingestion report
ruff check .           # lint (also run in CI)
```

The test suite (`tests/`, 137 tests, ~94% line coverage; `pytest --cov=app`) uses an in-memory SQLite database (`StaticPool`), an in-memory fake of the S3 client and a fake Celery task, and finishes in about two seconds. It covers allele trimming, chromosome normalization, classification, Ti/Tv detection, INFO parsing, validator edge cases, batch and multi-allelic parsing, FASTA GC/MD5, the REST endpoints, idempotent re-runs, failure handling (a failed job leaves no partial data), the S3 client (bucket auto-creation, 1 MiB streaming, cleanup) and the chunking/`page_size` logic of the PostgreSQL `execute_values` branch (against a fake cursor). Real PostgreSQL behaviour is exercised by the Docker stack and by the benchmark's `BENCHMARK_DATABASE_URL` mode.

**CI** (`.github/workflows/ci.yml`) runs on push and pull request to `main`/`master`: Python 3.11, `ruff check .`, `pytest -v`, then `scripts/benchmark_etl.py` (fails the build if the 45% reduction is lost).

## Design notes

- **Idempotent, crash-safe tasks.** Celery runs with `task_acks_late=True` and `task_reject_on_worker_lost=True`, so a job on a dead worker is redelivered. Each run first deletes any rows from a previous attempt for that job, so redelivery never duplicates data. On failure the job is marked `FAILED` with the traceback and partial rows are removed.
- **Fair scheduling.** `worker_prefetch_multiplier=1` stops one worker from reserving a queue of long jobs while others sit idle.
- **Progress with atomicity.** Job counters are committed in the same transaction as the batch they describe.
- **Dispatch failures are visible.** If Redis is unreachable at upload time, the job is persisted as `FAILED` and the API returns `503`.
- **Schema management.** Tables and indexes are created with `create_all` at API start-up (with retry while PostgreSQL boots). For a long-lived deployment, move to Alembic migrations.
- **Scope limits.** Inputs are plain-text `.vcf` / `.fasta` / `.fa` (no gzip/BGZF yet); genotype/sample columns after `INFO` are ignored; trimming is reference-free (see above); the primary-chromosome whitelist targets human data.

## Project layout

```text
app/
  main.py  config.py  database.py  models.py  schemas.py  storage.py
  api/      routes_etl.py  routes_analytics.py
  etl/      parser.py  validator.py  normalizer.py  loader.py
  worker/   celery_app.py  tasks.py
scripts/    generate_sample_data.py  benchmark_etl.py
tests/      conftest.py  test_normalizer.py  test_parser.py  test_api.py
```
