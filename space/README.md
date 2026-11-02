---
title: Genomic Variant ETL
emoji: 🧬
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: VCF in, validated and normalized variants out
---

# Genomic Variant ETL: live demo

Upload a VCF file and a reference FASTA (or use the bundled sample) and watch the pipeline
validate every record against the reference, normalize it the way `bcftools norm` would,
load it into PostgreSQL in parallel chunks and serve statistics and region queries.

Source, architecture and benchmarks: <https://github.com/aleksandrahodzzik/genomic-etl-pipeline>

This Space runs every service (PostgreSQL, Redis, MinIO, the API, two Celery workers and an
nginx proxy) in one container, built from `Dockerfile.space`. Limits: files up to 20 MB, at
most 2 jobs at a time, and everything except the sample is deleted after an hour. The Space
sleeps when idle, so the first visit can take about a minute.
