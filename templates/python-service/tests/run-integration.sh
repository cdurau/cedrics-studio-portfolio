#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
RUN_ID="$$-$RANDOM"
IMAGE="python-service-test-$RUN_ID"
CONTAINER="python-service-test-$RUN_ID"
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker build -t "$IMAGE" .
docker run -d --name "$CONTAINER" -p 127.0.0.1::3000 "$IMAGE" >/dev/null
PORT=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "3000/tcp") 0).HostPort}}' "$CONTAINER")
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
[ "$(curl -fsS "http://127.0.0.1:$PORT/healthz")" = '{"ok":true}' ]
curl -fsS "http://127.0.0.1:$PORT/api/info" | grep -q '"service":"python-service"'
[ "$(docker image inspect "$IMAGE" --format '{{.Config.User}}')" = 'app' ]
[ "$(docker inspect "$CONTAINER" --format '{{json .Mounts}}')" = '[]' ]
[ "$(docker inspect "$CONTAINER" --format '{{(index (index .HostConfig.PortBindings "3000/tcp") 0).HostIp}}')" = '127.0.0.1' ]
docker stop -t 10 "$CONTAINER" >/dev/null
docker logs "$CONTAINER" 2>&1 | grep -q 'SHUTDOWN_MARKER'
