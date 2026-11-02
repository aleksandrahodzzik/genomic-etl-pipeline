#!/usr/bin/env bash
# Entry point of the demo image (Dockerfile.space). On every boot: new random credentials
# for every store (never the defaults), a fresh database cluster, then supervisord runs the
# services in space/supervisord.conf. Nothing is persisted; a Space has no durable disk.
set -euo pipefail

export RUN="$HOME/run"
rm -rf "$RUN"
mkdir -p "$RUN/minio" "$RUN/redis" "$RUN/nginx" "$RUN/reference-cache"
umask 077 # credentials and data are readable by this user only

secret() { python -c 'import secrets; print(secrets.token_urlsafe(24))'; }
pg_password=$(secret)
redis_password=$(secret)

# The address browsers use. Hugging Face sets SPACE_HOST; locally it is the published port.
if [[ -z "${PUBLIC_URL:-}" ]]; then
    if [[ -n "${SPACE_HOST:-}" ]]; then PUBLIC_URL="https://$SPACE_HOST"; else PUBLIC_URL="http://localhost:7860"; fi
fi
signed_host="${PUBLIC_URL#*://}"
signed_host="${signed_host%%/*}"

# MinIO: bound to localhost, console off.
export MINIO_ROOT_USER="demo-$(python -c 'import secrets; print(secrets.token_hex(6))')"
export MINIO_ROOT_PASSWORD="$(secret)"
export MINIO_BROWSER=off

# The application's settings (src/variant_etl/config.py).
export DATABASE_URL="postgresql+psycopg://demo:${pg_password}@127.0.0.1:5432/variants"
export REDIS_URL="redis://:${redis_password}@127.0.0.1:6379/0"
export S3_ENDPOINT_URL="http://127.0.0.1:9000"
export S3_PUBLIC_ENDPOINT_URL="$PUBLIC_URL" # presigned URLs point at the public port...
export S3_ACCESS_KEY="$MINIO_ROOT_USER"
export S3_SECRET_KEY="$MINIO_ROOT_PASSWORD"
export S3_BUCKET="variant-etl" # ...and nginx.conf routes /variant-etl/ to MinIO
export REFERENCE_CACHE_DIR="$RUN/reference-cache"
export CHUNK_SIZE=25000 # so the 100k-record dataset runs as 4 chunks on the 2 workers
export MAX_ACTIVE_JOBS=2
export DEMO_MAX_UPLOAD_MB=20
export DEMO_RETENTION_MINUTES=60
export DEMO_SYNTHETIC_DIR=/app/synthetic
export DEMO_NOTICE="Public demo: files up to 20 MB, at most 2 jobs at a time; everything except the sample is deleted after an hour."
export REDISCLI_AUTH="$redis_password" # for the readiness check in bootstrap.sh

# PostgreSQL: a new cluster owned by this user, password auth, then the application database.
pg_bin=(/usr/lib/postgresql/*/bin)
export PATH="${pg_bin[0]}:$PATH"
export PGDATA="$RUN/pg"
pwfile="$RUN/pg-password"
printf '%s' "$pg_password" >"$pwfile"
initdb --pgdata="$PGDATA" --username=demo --pwfile="$pwfile" --auth=scram-sha-256 \
    --encoding=UTF8 --locale=C.UTF-8 >/dev/null
rm -f "$pwfile"
echo "CREATE DATABASE variants;" | postgres --single -D "$PGDATA" postgres >/dev/null

cat >"$RUN/redis/redis.conf" <<EOF
bind 127.0.0.1
port 6379
requirepass $redis_password
save ""
appendonly no
dir $RUN/redis
EOF

sed -e "s|__RUN__|$RUN|g" -e "s|__SIGNED_HOST__|$signed_host|g" \
    /app/space/nginx.conf >"$RUN/nginx/nginx.conf"

echo "start: services starting; public URL $PUBLIC_URL"
exec supervisord -c /app/space/supervisord.conf
