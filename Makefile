.PHONY: up down logs test generate-data benchmark

up:
	docker compose up --build -d

down:
	docker compose down -v

logs:
	docker compose logs -f

test:
	pytest -v

generate-data:
	python scripts/generate_sample_data.py

benchmark:
	python scripts/benchmark_etl.py
