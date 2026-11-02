.PHONY: up down test lint data demo-data demo bench bench-clinvar gif pages space-build space-run space-smoke space-deploy

# Everything the demo, tests and benchmark talk to. Waits until all long-running services
# are healthy and the one-shot jobs (migrations, bucket creation) have finished.
up:
	docker compose up -d --build --wait

down:
	docker compose down -v

# Unit + oracle tests need nothing running; integration tests need `make up` first.
test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

# Full-size dataset (3 contigs x 5 Mb, 1M records) used by the benchmark.
data:
	uv run python scripts/generate_data.py --contigs 3 --contig-length 5_000_000 \
		--variants 1_000_000 --titv 2.0 --error-rate 0.005 --seed 42 --out-dir data/bench

# The small dataset (3 contigs x 2 Mb, 200k records) used by `make demo`.
demo-data:
	uv run python scripts/generate_data.py --contigs 3 --contig-length 2_000_000 \
		--variants 200_000 --titv 2.0 --error-rate 0.005 --seed 42 --out-dir data/demo

# Upload the small dataset, run a job, print the stats summary.
demo: demo-data
	uv run python scripts/demo.py --data-dir data/demo

bench: data
	uv run python scripts/benchmark.py --data-dir data/bench

# Real data: ClinVar GRCh38 (chromosomes 1, 2, 17) next to the synthetic set, measured in one
# session; writes docs/BENCHMARKS_CLINVAR.md. The first run downloads ~0.4 GB into data/clinvar.
bench-clinvar: data
	uv run python scripts/benchmark_clinvar.py

# Re-record docs/images/demo.gif from a running demo image (`make space-run`, or
# SPACE_URL=https://<space>.hf.space), driven by Playwright. Playwright and Pillow are
# fetched for this command only; they are not project dependencies.
gif:
	uv run --with playwright==1.64.1 playwright install chromium
	uv run --with playwright==1.64.1 --with pillow==13.0.0 python scripts/record_ui_gif.py \
		--url $${SPACE_URL:-http://localhost:7860}

# The project site (GitHub Pages) from a running stack (`make up`): pages/ plus a snapshot of the
# sample run in site/, then checked in Chromium against the API. Serve site/ with any static server.
pages:
	uv run python scripts/export_pages.py --out site
	uv run --with playwright==1.64.1 playwright install chromium
	uv run --with playwright==1.64.1 python scripts/pages_smoke.py --site site

# The public demo: every service in one container behind one port (Dockerfile.space).
space-build:
	docker build -f Dockerfile.space -t variant-etl-space .

space-run: space-build
	docker run --rm -p 7860:7860 variant-etl-space

# Against a running demo container (space-run), or SPACE_URL=https://<space>.hf.space
space-smoke:
	uv run python scripts/space_smoke.py --url $${SPACE_URL:-http://localhost:7860}

# Needs HF_TOKEN (write) in the environment or an earlier `hf auth login`.
space-deploy:
	uv run --with huggingface_hub python scripts/deploy_space.py
