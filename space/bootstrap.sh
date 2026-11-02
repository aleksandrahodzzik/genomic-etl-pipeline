#!/usr/bin/env bash
# Runs once per boot, under supervisord, after the stores have been started: wait until they
# answer, migrate, create the bucket, start the API, workers and janitor, queue the
# 3,000-record sample, and only then open the public port.
set -euo pipefail
cd /app

log() { echo "bootstrap: $*"; }
ctl() { supervisorctl -c /app/space/supervisord.conf "$@"; }
wait_for() {
    local what=$1
    shift
    for _ in $(seq 1 240); do
        if "$@" >/dev/null 2>&1; then
            log "$what is up"
            return 0
        fi
        sleep 0.5
    done
    log "$what did not come up"
    return 1
}
http_ok() { python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)" "$1"; }

wait_for postgres pg_isready -h 127.0.0.1 -p 5432 -d variants
wait_for redis redis-cli -h 127.0.0.1 -p 6379 ping
wait_for minio http_ok http://127.0.0.1:9000/minio/health/live

alembic upgrade head
python -m variant_etl.storage.init_bucket

ctl start api worker1 worker2 janitor
wait_for api http_ok http://127.0.0.1:8000/health

python -m variant_etl.demo # the sample job; the workers pick it up straight away

ctl start nginx
wait_for "public port" http_ok http://127.0.0.1:7860/health
log "demo is ready"
