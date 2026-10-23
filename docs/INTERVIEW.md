# Interview notes

Numbers quoted here are the measured ones from [`BENCHMARKS.md`](BENCHMARKS.md) (one laptop, Docker Desktop, 1M records). Say "on my laptop" when you quote them; they are not cluster numbers.

## The 60-second pitch

"It's an asynchronous ETL service for genomic variants. A client uploads a VCF and a reference genome directly to S3-compatible storage with presigned URLs, then starts a job through a FastAPI endpoint. A Celery task splits the VCF into 100k-record chunks, and a chord fans those out to worker containers. Each worker validates records with seven fixed reject reasons, normalizes them the way `bcftools norm` does (splitting multi-allelic records and left-aligning indels), and bulk-loads them into PostgreSQL with `COPY`, in one transaction per chunk. The write is idempotent, so retries and redelivery can't create duplicates. When all chunks are done, a finalize step marks the job complete, and the API serves stats and keyset-paginated queries. I checked correctness against bcftools as an oracle and against a data generator with built-in ground truth. On a 1M-record file, going from 1 to 4 worker containers made the job 1.7 to 1.9 times faster on my laptop (two benchmark sessions), and an index made region queries roughly 30 times faster. The honest limits are that it's sites-only VCF and I only benchmarked on one machine."

## Questions and answers

### 1. Why presigned uploads instead of uploading through the API?
The file is the one big thing in the system. If it goes through the API, the API needs the memory, bandwidth, timeouts and a worker slot for every upload, and a multi-GB VCF becomes a reliability problem for the service. With a presigned PUT URL the API only signs a short-lived URL (1 hour here) and later checks the object exists; the bytes go client → storage. The catch I had to handle: the URL must be signed for the endpoint the *client* can reach (`S3_PUBLIC_ENDPOINT_URL`), not the internal hostname that containers use.

### 2. What delivery guarantee do you have, and how do you get correct results from it?
At-least-once. Workers use `acks_late` and `reject_on_worker_lost`, so if a worker dies mid-chunk the broker redelivers it. Exactly-once isn't available from the queue, so I make the *effect* idempotent: in one transaction the loader takes a `pg_advisory_xact_lock` for (job, chunk), deletes anything that chunk wrote before, `COPY`s the rows and writes a marker row. A second delivery either sees the marker `done` and skips, or redoes the same work and replaces it. An integration test crashes a chunk right before commit and checks there are no duplicates after the retry.

### 3. What happens when one chunk of the chord fails?
Transient errors (S3 5xx, DB connection errors, timeouts) are retried up to 5 times with jittered exponential backoff. A permanent error (e.g. the chunk is corrupt) is recorded in `job_chunks` as failed and `process_chunk` *returns* a failure result rather than raising. That matters because a Celery chord body does not run if a header task raises, and the job would hang forever in `running`. With a return value, `finalize_job` always runs, recomputes everything from committed chunk markers, and marks the job `failed` listing the bad chunks. A job is `completed` only if every chunk committed. Resubmitting a failed job deletes it and starts again.

### 4. Why a chord of chunks instead of one task per file?
One task can't use more than one worker, and a failure at 95% throws away all the work. The chunk is the unit of parallelism, retry and transaction. 100k records is a compromise: big enough that per-chunk overhead (S3 round trips, a transaction) is small, small enough that a retry is cheap and there are enough chunks to spread over workers (the 1M benchmark has 10). The chord is built inside `split_job`, because only that task knows how many chunks the file has.

### 5. COPY vs. executemany vs. the ORM: what did you measure?
Inserting the same 100k real variant rows into a bare table: `COPY` 0.45 s (about 224k rows/s), psycopg `executemany` 6.49 s, SQLAlchemy Core batched 7.83 s, ORM `add_all` 15.89 s, and one `INSERT` per row about 1,000 rows/s (1,043). In an earlier session `COPY` was 0.33 s and the ORM 14.47 s, so the gap is consistently 35–45x. `COPY` streams rows in one command with no per-statement parsing or round trip. The ORM additionally builds objects and tracks state. Caveat: the database is in the Docker Desktop VM, which makes round-trip-heavy methods look worse than on a tuned host; the ordering would hold, the ratios may not. The real table also maintains two indexes, so real throughput per worker is lower than the bare-table figure.

### 6. How did you choose the indexes?
From the queries: region queries filter on job, chromosome and a position range, so `(job_id, chrom, pos)`; type breakdowns use `(job_id, variant_type)`. I verified with `EXPLAIN (ANALYZE, BUFFERS)`: with the index the region query is an Index Scan (median 2.09 ms over 100 random 10 kb windows); without it, a parallel sequential scan over the whole job (median 63.78 ms), 31x slower; an earlier session gave 28x. The `(chrom, pos, id)` keyset cursor reuses the first index; its plan is an index scan plus a small incremental sort, 1.24 ms for a page in the middle of the job.

### 7. Why keyset pagination instead of OFFSET?
`OFFSET n` makes the database read and discard n rows, so deep pages get slower linearly. A keyset cursor (`WHERE (chrom, pos, id) > (last values)`) seeks into the index and reads one page, so page 1 and page 10,000 cost about the same. It's also stable under concurrent inserts. The cursor is an opaque base64 string so clients don't depend on its structure. The cost: you can't jump to "page 57", only go next.

### 8. Why not just load the file into pandas?
Memory and failure mode. pandas wants the whole table in RAM; a 1M-record VCF is small, but a real one is not, and the service is supposed to accept files bigger than any worker's memory. Here the splitter streams the file, and a worker holds one chunk (100k records) at a time, so memory is bounded by chunk size and not by file size. Also, normalization is per-record logic with reference lookups, which doesn't vectorize well anyway.

### 9. Explain left-alignment on a whiteboard.
Reference `G C A C A C A G` at positions 1–8. Deleting the last `CA` of the repeat can be written `ACA → A` at position 5, or `CAC → C` at 4, or `GCA → G` at 1: the same event, written three ways. VCF normalization picks one canonical form so equal variants compare equal: repeatedly, if REF and ALT end in the same base, drop it, and if an allele would become empty, prepend the preceding reference base; this slides the variant left until it can't move. The result is `GCA → G` at position 1. Without this, the same deletion from two callers would be counted twice or missed in a join. I tested this against `bcftools norm`, which implements the algorithm from Tan et al. 2015.

### 10. What is Ti/Tv, and what's a typical value?
Transitions are purine↔purine or pyrimidine↔pyrimidine substitutions (A↔G, C↔T); transversions are the rest. Chance alone would give 0.5 (1 transition to 2 possible transversions), but biology favours transitions, so whole-genome human data is commonly cited around 2.0–2.1 and exomes higher (about 3). It's a cheap QC signal: a value far from the expected one suggests false positives. Important honesty point: the demo reports 2.0 because my *generator* steers the data to 2.0; it's not a biological finding. On real data the number would be an actual quality check.

### 11. What would you do to scale 100×?
First measure where the limit is: on my laptop, adding workers gave about 1.6x at 2 and 1.7–1.9x at 4 (it varied between sessions), and per chunk the `COPY` (1.63 s) costs about as much as parsing (1.40 s), so the single PostgreSQL instance is a probable bottleneck, but I did not isolate it, so I'd confirm before acting. Then, in order: partition `variants` by contig or job, so loads and deletes hit separate partitions; load into unlogged or partitioned staging tables and attach; move analytics to Parquet + DuckDB so aggregates don't compete with loads; run workers on multiple hosts (nothing in the design is host-local except the reference cache); and move Redis to a managed broker or RabbitMQ if queue durability becomes a concern.

### 12. MinIO's community edition stopped publishing images. What did you do?
All storage access goes through one module (`storage/s3.py`, boto3), configured only by endpoint, credentials and bucket. So the choice of S3 server is an environment variable, not a code change: compose pins a community-maintained MinIO fork (`pgsty/minio`) at an exact tag so builds are reproducible, and the same code can talk to AWS S3 or any S3-compatible store. The lesson is to depend on the protocol, not the product, and to pin versions of anything whose distribution you don't control.

### 13. Why a hand-written VCF parser instead of pysam?
I need eight columns and `AF` from INFO, streamed line by line; pysam brings the full htslib and a compiled dependency into the image. A small parser is easy to test exhaustively, and I still validated it independently with bcftools as a test oracle. The trade-off is scope: no genotypes, no symbolic alleles, no BCF. If the requirement grew, I'd switch to pysam or cyvcf2 and keep my tests as the safety net.

### 14. How do you know the pipeline is correct?
Three independent checks. (1) An oracle: the same records go through `bcftools norm -m -any -f` and my normalizer, and the outputs are compared; a guard test confirms the comparison fails for a normalizer that doesn't left-align, so the oracle can detect a bug. (2) Ground truth: the generator writes a `truth.json` computed from how the data was *constructed* (counts per variant type and reject reason), never from the pipeline, and the tests assert the pipeline reproduces it. (3) Integration tests on the real compose stack, including a forced chunk failure and retry.

### 15. How does deduplication of jobs work?
A job's identity is a checksum of both input objects (ETag and size), so submitting the same files again returns the existing job with HTTP 200 and `deduplicated: true` rather than redoing the work. A failed job is deleted and recreated on resubmit. Limit: ETag-based identity is only as good as the storage's ETag; for multipart uploads that isn't a content hash. If that mattered I'd compute a SHA-256 on upload.

### 16. What is `was_normalized`, and why do record counts differ from variant counts?
Counts of `total`, `accepted`, `rejected` are VCF *records*. A multi-allelic record is split into one row per ALT, so there are more rows than records (in the demo, 200,000 records gave 199,000 accepted and 1,000 rejected, and the 199,000 accepted records produced 218,038 variant rows). `was_normalized` marks rows that differ from how they were written or came from a split, so analysts can see how much the cleanup changed.

### 17. What would break first in production, and what's missing?
No authentication or rate limiting on the API; no genotype/sample data; benchmarks only on one machine; the reference FASTA is cached per worker container; storing only a bounded sample of rejected lines (counts are exact). Operationally I'd add metrics (chunk durations, retry counts, queue depth) and alerts on jobs stuck in `running`. These are listed in the README's limitations section.

### 18. Why did you keep the indexes on during bulk load?
I measured it twice and the answer was not stable: in one session, dropping both secondary indexes before loading and rebuilding after gave 29.1 s total against 33.7 s; in the other, 37.5 s against 36.8 s, i.e. no gain. I don't have a measurement that explains the difference, so I won't claim a benefit. On top of that, chunks commit concurrently and results become queryable per chunk, and dropping indexes would make every other job's queries slow during the load. So I kept the always-correct default and documented the trade-off in the migration; a "bulk import mode" would be an option to test properly, on a quieter machine, before adopting. (Good interview point: say what you measured and that it was noisy.)

### 19. How is the API tested without flakiness?
Unit tests need no services. Integration tests use the actual compose stack and skip if it's unreachable, unless `REQUIRE_INTEGRATION=1`, which CI sets so a missing stack fails the build instead of silently skipping. The chunk-failure test uses a `before_commit` hook in the loader to simulate a crash at the exact worst moment, rather than sleeping and hoping.

## Read the code in this order

1. `src/variant_etl/pipeline/normalize.py` — the algorithm (81 lines); read with `tests/unit/test_normalize.py`.
2. `src/variant_etl/pipeline/validate.py` and `vcf.py` — the reject reasons and their precedence.
3. `src/variant_etl/pipeline/tasks.py` — the chord, retries, failure handling.
4. `src/variant_etl/pipeline/loader.py` — the idempotent `COPY` transaction.
5. `src/variant_etl/api/routers/jobs.py` — presigned uploads, dedup, job creation.
6. `src/variant_etl/db/queries.py` and `api/routers/stats.py` — keyset pagination and the stats queries.
7. `tests/unit/test_oracle_bcftools.py` — how correctness is established.
