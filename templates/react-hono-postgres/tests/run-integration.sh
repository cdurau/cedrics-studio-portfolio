#!/usr/bin/env bash
set -e

# Generate a unique container name
CONTAINER_NAME="pg-test-$(openssl rand -hex 4)"

echo "Starting ephemeral postgres container: $CONTAINER_NAME..."
# Bind to 127.0.0.1 and a dynamically assigned port by letting Docker choose
CONTAINER_ID=$(docker run -d --name "$CONTAINER_NAME" \
  -e POSTGRES_USER=testuser \
  -e POSTGRES_PASSWORD=testpass \
  -e POSTGRES_DB=testdb \
  -p 127.0.0.1::5432 \
  postgres:18-alpine)

# Cleanup function
cleanup() {
  echo "Cleaning up container $CONTAINER_NAME..."
  docker rm -f "$CONTAINER_NAME" > /dev/null || true
}
trap cleanup EXIT INT TERM

echo "Waiting for PostgreSQL to be ready..."
# Find the dynamically assigned host port
HOST_PORT=$(docker port "$CONTAINER_NAME" 5432 | head -n 1 | awk -F ':' '{print $NF}')

# Wait for pg_isready
RETRIES=15
while [ $RETRIES -gt 0 ]; do
  if docker exec "$CONTAINER_NAME" pg_isready -U testuser -d testdb > /dev/null 2>&1; then
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
export TEST_DATABASE_URL="postgresql://testuser:testpass@127.0.0.1:${HOST_PORT}/testdb"

echo "Running integration tests..."
npx vitest run tests/integration.test.ts
