#!/usr/bin/env bash
set -euo pipefail

echo "Running integration unit tests..."
npx vitest run tests/integration.test.ts

APP_NAME="sqlite-test-$(openssl rand -hex 4)"
VOLUME_NAME="sqlite-data-${APP_NAME}"
IMAGE_NAME="test-sqlite-app-${APP_NAME}"

# Cleanup function
cleanup() {
  echo "Cleaning up container $APP_NAME, volume $VOLUME_NAME, image $IMAGE_NAME..."
  docker rm -f "$APP_NAME" > /dev/null 2>&1 || true
  docker volume rm "$VOLUME_NAME" > /dev/null 2>&1 || true
  docker rmi "$IMAGE_NAME" > /dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "Building test image..."
docker build -t "$IMAGE_NAME" .

echo "Starting container A to create record..."
CONTAINER_A_ID=$(docker run -d --name "$APP_NAME" \
  -v "${VOLUME_NAME}:/data" \
  -p 127.0.0.1::3000 \
  "$IMAGE_NAME")

echo "Waiting for app to be healthy..."
HOST_PORT=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "3000/tcp") 0).HostPort}}' "$APP_NAME")

RETRIES=30
while [ $RETRIES -gt 0 ]; do
  if curl -s http://127.0.0.1:${HOST_PORT}/healthz | grep -q '"ok":true'; then
    break
  fi
  sleep 1
  RETRIES=$((RETRIES-1))
done

if [ $RETRIES -eq 0 ]; then
  echo "App failed to become ready."
  docker logs "$APP_NAME"
  exit 1
fi

echo "Verifying SPA nested-route reload returns index..."
INDEX_HTML=$(curl -s "http://127.0.0.1:${HOST_PORT}/some/nested/route")
if ! echo "$INDEX_HTML" | grep -q '<div id="root"></div>'; then
  echo "SPA fallback failed."
  exit 1
fi

echo "App is ready on port $HOST_PORT. Creating a record..."
curl -s -X POST -H "Content-Type: application/json" -d '{"content":"hello test"}' "http://127.0.0.1:${HOST_PORT}/api/entries" > /dev/null

echo "Reading PRAGMA values from container A..."
PRAGMAS=$(docker exec "$APP_NAME" node -e "
const db = require('better-sqlite3')('/data/app.db');
console.log(JSON.stringify({
  journal_mode: db.pragma('journal_mode', {simple:true}),
  foreign_keys: db.pragma('foreign_keys', {simple:true}),
  busy_timeout: db.pragma('busy_timeout', {simple:true})
}));
")

if ! echo "$PRAGMAS" | grep -q '"journal_mode":"wal"'; then
  echo "journal_mode is not WAL: $PRAGMAS"
  exit 1
fi

if ! echo "$PRAGMAS" | grep -q '"foreign_keys":1'; then
  echo "foreign_keys is not ON: $PRAGMAS"
  exit 1
fi

if ! echo "$PRAGMAS" | grep -q '"busy_timeout":5000'; then
  echo "busy_timeout is not 5000: $PRAGMAS"
  exit 1
fi

echo "Removing container A..."
docker rm -f "$APP_NAME" > /dev/null

echo "Starting container B with the same volume..."
CONTAINER_B_ID=$(docker run -d --name "$APP_NAME" \
  -v "${VOLUME_NAME}:/data" \
  -p 127.0.0.1::3000 \
  "$IMAGE_NAME")

HOST_PORT_B=$(docker port "$APP_NAME" 3000 | awk -F ':' '{print $NF}' | head -n 1)

RETRIES=30
while [ $RETRIES -gt 0 ]; do
  if curl -s http://127.0.0.1:${HOST_PORT_B}/healthz | grep -q '"ok":true'; then
    break
  fi
  sleep 1
  RETRIES=$((RETRIES-1))
done

if [ $RETRIES -eq 0 ]; then
  echo "Container B failed to become ready."
  docker logs "$APP_NAME"
  exit 1
fi

echo "Reading records from container B..."
RESPONSE=$(curl -s "http://127.0.0.1:${HOST_PORT_B}/api/entries")
if echo "$RESPONSE" | grep -q '"hello test"'; then
  echo "Record persisted successfully across container replacement!"
else
  echo "Record did NOT persist! Response: $RESPONSE"
  exit 1
fi

echo "Integration tests passed."
