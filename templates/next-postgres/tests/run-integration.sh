#!/bin/bash
set -euo pipefail

RUN_ID="$$$RANDOM"
IMAGE_NAME="test-next-postgres-${RUN_ID}"
NETWORK_NAME="test-net-next-${RUN_ID}"
PG_CONTAINER="test-pg-next-${RUN_ID}"
APP_CONTAINER="test-app-next-${RUN_ID}"

cleanup() {
  echo "Cleaning up..."
  docker rm -f "$APP_CONTAINER" 2>/dev/null || true
  docker rm -f "$PG_CONTAINER" 2>/dev/null || true
  docker network rm "$NETWORK_NAME" 2>/dev/null || true
  docker rmi "$IMAGE_NAME" 2>/dev/null || true
}
trap cleanup EXIT

echo "Creating private network..."
docker network create "$NETWORK_NAME"

echo "Starting ephemeral postgres for integration tests..."
docker run -d --name "$PG_CONTAINER" --network "$NETWORK_NAME" -p 127.0.0.1::5432 -e POSTGRES_PASSWORD=test -e POSTGRES_USER=test -e POSTGRES_DB=testdb postgres:18-alpine

echo "Getting postgres dynamic port..."
PG_TEST_PORT=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' "$PG_CONTAINER")
PG_HOST_IP=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostIp}}' "$PG_CONTAINER")
if [ "$PG_HOST_IP" != "127.0.0.1" ]; then
  echo "PG container port bound to $PG_HOST_IP, expected 127.0.0.1"
  exit 1
fi

echo "Waiting for postgres to be ready..."
for i in {1..30}; do
  if docker exec "$PG_CONTAINER" pg_isready -U test; then
    break
  fi
  if [ $i -eq 30 ]; then
    echo "Postgres failed to start"
    exit 1
  fi
  sleep 1
done

export TEST_DATABASE_URL="postgres://test:test@127.0.0.1:$PG_TEST_PORT/testdb"

echo "Running migration integration tests..."
npx vitest run tests/migrate.integration.test.ts

echo "Building next-postgres Docker image..."
docker build -t "$IMAGE_NAME" .

echo "Starting next container..."
docker run -d --name "$APP_CONTAINER" --network "$NETWORK_NAME" -p 127.0.0.1::3000 -e DATABASE_URL="postgres://test:test@$PG_CONTAINER:5432/testdb" "$IMAGE_NAME"

echo "Getting app dynamic port..."
HOST_PORT=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "3000/tcp") 0).HostPort}}' "$APP_CONTAINER")
APP_HOST_IP=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "3000/tcp") 0).HostIp}}' "$APP_CONTAINER")
if [ "$APP_HOST_IP" != "127.0.0.1" ]; then
  echo "App container port bound to $APP_HOST_IP, expected 127.0.0.1"
  exit 1
fi

echo "Waiting for app to be ready..."
for i in {1..30}; do
  if curl -s "http://127.0.0.1:$HOST_PORT/healthz" | grep -q '{"ok":true}'; then
    break
  fi
  if [ $i -eq 30 ]; then
    echo "App container failed to start"
    exit 1
  fi
  sleep 1
done

echo "Testing /healthz..."
HEALTH=$(curl -s "http://127.0.0.1:$HOST_PORT/healthz")
if [ "$HEALTH" != '{"ok":true}' ]; then
  echo "Health endpoint failed: $HEALTH"
  exit 1
fi

echo "Testing root page..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$HOST_PORT/")
if [ "$HTTP_CODE" != "200" ]; then
  echo "Root endpoint failed"
  exit 1
fi

echo "Testing CRUD..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d '{"content":"TestEntry"}' "http://127.0.0.1:$HOST_PORT/api/entries" || echo "000")
if [ "$HTTP_CODE" == "200" ] || [ "$HTTP_CODE" == "201" ]; then
  PAGE_CONTENT=$(curl -s "http://127.0.0.1:$HOST_PORT/api/entries")
  if [[ "$PAGE_CONTENT" != *"TestEntry"* ]]; then
    echo "CRUD read test failed"
    exit 1
  fi
fi

echo "Testing /_next static asset..."
ASSET_URL=$(curl -s "http://127.0.0.1:$HOST_PORT/" | grep -o '/_next/static/[^"]*\.js' | head -n 1 || true)
if [ -z "$ASSET_URL" ]; then
  echo "Could not find a /_next/static asset to test"
  exit 1
fi

ASSET_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$HOST_PORT$ASSET_URL")
if [ "$ASSET_CODE" != "200" ]; then
  echo "Asset $ASSET_URL test failed with $ASSET_CODE"
  exit 1
fi

echo "Inspecting non-root user..."
USER=$(docker inspect --format='{{.Config.User}}' "$IMAGE_NAME")
if [ "$USER" = "root" ] || [ "$USER" = "0" ] || [ -z "$USER" ]; then
  echo "User is root, expected non-root"
  exit 1
fi

echo "Inspecting exposed port..."
EXPOSED_PORTS=$(docker inspect --format='{{range $p, $conf := .Config.ExposedPorts}}{{$p}}{{end}}' "$IMAGE_NAME")
if [[ "$EXPOSED_PORTS" != *"3000/tcp"* ]]; then
  echo "Port 3000 not exposed"
  exit 1
fi

echo "Inspecting health CMD..."
HEALTH_CMD=$(docker inspect --format='{{json .Config.Healthcheck.Test}}' "$IMAGE_NAME")
if [[ "$HEALTH_CMD" != *"wget"* ]] || [[ "$HEALTH_CMD" != *"healthz"* ]]; then
  echo "Health CMD is missing or incorrect: $HEALTH_CMD"
  exit 1
fi

echo "Inspecting CMD migration before server..."
CMD=$(docker inspect --format='{{json .Config.Cmd}}' "$IMAGE_NAME")
if [[ "$CMD" != *"node dist/migrate.js && node server.js"* ]] && [[ "$CMD" != *"node","dist/migrate.js","&&","node","server.js"* ]]; then
  # the json might be an array or string. Since Dockerfile uses `CMD node dist/migrate.js && node server.js`
  # we can just use `docker inspect --format='{{.Config.Cmd}}'` which prints an array.
  CMD_STR=$(docker inspect --format='{{.Config.Cmd}}' "$IMAGE_NAME")
  if [[ "$CMD_STR" != *"node dist/migrate.js && node server.js"* ]]; then
    echo "Migration CMD is missing or incorrect: $CMD_STR"
    exit 1
  fi
fi

echo "Inspecting for mounts..."
MOUNTS=$(docker inspect --format='{{json .Mounts}}' "$APP_CONTAINER")
if [ "$MOUNTS" != "[]" ] && [ "$MOUNTS" != "null" ]; then
  echo "Container has unexpected mounts: $MOUNTS"
  exit 1
fi

echo "Integration tests passed successfully!"
