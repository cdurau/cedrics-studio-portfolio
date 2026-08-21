#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

RUN_ID=$(date +%s)-$RANDOM
IMG="fastapi-test-${RUN_ID}"
CONT_A="fastapi-test-a-${RUN_ID}"
CONT_B="fastapi-test-b-${RUN_ID}"
VOL="fastapi-test-vol-${RUN_ID}"

trap 'echo "Cleaning up..."; docker rm -f $CONT_A $CONT_B 2>/dev/null || true; docker volume rm $VOL 2>/dev/null || true; docker rmi $IMG 2>/dev/null || true' EXIT

echo "Running pytest in local disposable venv..."
python3 -m venv .venv_integration
source .venv_integration/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. pytest tests/test_app.py tests/test_migrations.py

echo "Building Docker image..."
docker build -t $IMG .

echo "Inspecting Docker image..."
[ "$(docker image inspect "$IMG" --format '{{.Config.User}}')" = "app" ] || { echo "Missing nonroot app user"; exit 1; }
docker image inspect "$IMG" --format '{{json .Config.ExposedPorts}}' | grep -q '3000/tcp' || { echo "Missing exposed port 3000"; exit 1; }
docker image inspect "$IMG" --format '{{json .Config.Healthcheck.Test}}' | grep -q 'healthz' || { echo "Missing HEALTHCHECK"; exit 1; }
docker image inspect "$IMG" --format='{{json .Config.Cmd}}' | grep -q "alembic upgrade head && uvicorn" || { echo "Missing migration-before-uvicorn CMD"; exit 1; }

docker volume create $VOL

echo "Starting Container A..."
docker run -d --name $CONT_A -p 127.0.0.1:0:3000 -v $VOL:/data $IMG

echo "Waiting for healthcheck..."
for i in {1..30}; do
  STATUS=$(docker inspect --format='{{.State.Health.Status}}' $CONT_A)
  if [ "$STATUS" = "healthy" ]; then
    break
  fi
  sleep 1
done
if [ "$STATUS" != "healthy" ]; then
  echo "Container failed to become healthy"
  docker logs $CONT_A
  exit 1
fi

PORT=$(docker port $CONT_A 3000 | awk -F ':' '{print $2}')

echo "Testing POST/GET/UI..."
curl -s -X POST -H "Content-Type: application/json" -H "x-authentik-username: integratort" -d '{"content": "Persist me!"}' http://127.0.0.1:$PORT/api/entries > /dev/null
RES=$(curl -s http://127.0.0.1:$PORT/api/entries)
echo $RES | grep -q "integratort" || { echo "POST/GET failed"; exit 1; }

UI_RES=$(curl -s -H "x-authentik-name: UI Integrator" http://127.0.0.1:$PORT/)
echo "$UI_RES" | grep -q "Welcome, UI Integrator" || { echo "UI name failed"; exit 1; }
echo "$UI_RES" | grep -q "Persist me!" || { echo "UI content failed"; exit 1; }

echo "Removing Container A and starting Container B to test persistence..."
docker rm -f $CONT_A

docker run -d --name $CONT_B -p 127.0.0.1:0:3000 -v $VOL:/data $IMG
for i in {1..30}; do
  STATUS=$(docker inspect --format='{{.State.Health.Status}}' $CONT_B)
  if [ "$STATUS" = "healthy" ]; then
    break
  fi
  sleep 1
done
if [ "$STATUS" != "healthy" ]; then
  echo "Container B failed to become healthy"
  docker logs $CONT_B
  exit 1
fi

PORT_B=$(docker port $CONT_B 3000 | awk -F ':' '{print $2}')
RES_B=$(curl -s http://127.0.0.1:$PORT_B/api/entries)
echo $RES_B | grep -q "integratort" || { echo "Persistence failed"; exit 1; }

echo "Integration tests passed!"
