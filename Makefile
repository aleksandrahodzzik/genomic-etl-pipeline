.PHONY: up down test lint data demo bench

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

# Upload a small generated dataset, run a job, print the stats summary.
demo:
	uv run python scripts/generate_data.py --contigs 3 --contig-length 2_000_000 \
		--variants 200_000 --titv 2.0 --error-rate 0.005 --seed 42 --out-dir data/demo
	uv run python scripts/demo.py --data-dir data/demo

bench: data
	uv run python scripts/benchmark.py --data-dir data/bench
