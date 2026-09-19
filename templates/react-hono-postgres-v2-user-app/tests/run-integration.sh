#!/usr/bin/env bash
# LOW-6 review hygiene: fail on unset vars and pipe failures — an empty
# HOST_PORT would otherwise flow into broken connection strings instead of
# aborting. Test containers are ephemeral here; productive Docker operations
# use `docker --context devbox` (AGENTS.md) — this script never touches prod.
set -eu -o pipefail

# Ephemeral Postgres + two-role bootstrap for user-app contract tests.
# The app runs with the RUNTIME role (RLS applies); migrations with OWNER.

CONTAINER_NAME="pg-test-$(openssl rand -hex 4)"

echo "Starting ephemeral postgres container: $CONTAINER_NAME..."
CONTAINER_ID=$(docker run -d --name "$CONTAINER_NAME" \
  -e POSTGRES_USER=testowner \
  -e POSTGRES_PASSWORD=testownerpass \
  -e POSTGRES_DB=testdb \
  -p 127.0.0.1::5432 \
  postgres:18-alpine)

cleanup() {
  echo "Cleaning up container $CONTAINER_NAME..."
  docker rm -f "$CONTAINER_NAME" > /dev/null || true
}
trap cleanup EXIT INT TERM

echo "Waiting for PostgreSQL to be ready..."
HOST_PORT=$(docker port "$CONTAINER_NAME" 5432 | head -n 1 | awk -F ':' '{print $NF}')
RETRIES=15
while [ $RETRIES -gt 0 ]; do
  if docker exec "$CONTAINER_NAME" pg_isready -U testowner -d testdb > /dev/null 2>&1; then
    break
  fi
  sleep 1
  RETRIES=$((RETRIES-1))
done
if [ $RETRIES -eq 0 ]; then
  echo "Database failed to become ready."
  exit 1
fi
echo "Database is ready on port $HOST_PORT."

export OWNER_URL="postgresql://testowner:testownerpass@127.0.0.1:${HOST_PORT}/testdb"

# Runtime role: login role, granted by migration 0002, no BYPASSRLS/ownership.
docker exec "$CONTAINER_NAME" psql -U testowner -d testdb -c \
  "CREATE ROLE app_runtime WITH LOGIN PASSWORD 'runtimepass';"

# Role creation must precede migrations (0002 grants reference app_runtime).
export TEST_DATABASE_URL="$OWNER_URL"
export TEST_RUNTIME_DATABASE_URL="postgresql://app_runtime:runtimepass@127.0.0.1:${HOST_PORT}/testdb"

echo "Running integration tests..."
npx vitest run tests/integration.test.ts