#!/bin/bash
set -euo pipefail

IMAGE_NAME="test-static-vite-$RANDOM"
CONTAINER_NAME="test-static-vite-container-$RANDOM"

echo "Building docker image..."
docker build -t "$IMAGE_NAME" .

cleanup() {
  echo "Cleaning up..."
  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
  docker rmi "$IMAGE_NAME" 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting container..."
docker run -d --name "$CONTAINER_NAME" -p 127.0.0.1::3000 "$IMAGE_NAME"

echo "Getting app dynamic port..."
HOST_PORT=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "3000/tcp") 0).HostPort}}' "$CONTAINER_NAME")
APP_HOST_IP=$(docker inspect --format='{{(index (index .NetworkSettings.Ports "3000/tcp") 0).HostIp}}' "$CONTAINER_NAME")
if [ "$APP_HOST_IP" != "127.0.0.1" ]; then
  echo "App container port bound to $APP_HOST_IP, expected 127.0.0.1"
  exit 1
fi

echo "Waiting for container to be ready..."
for i in {1..30}; do
  if curl -s "http://127.0.0.1:$HOST_PORT/healthz" | grep -q '{"ok":true}'; then
    break
  fi
  if [ $i -eq 30 ]; then
    echo "Container failed to start"
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

echo "Testing root..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$HOST_PORT/")
if [ "$HTTP_CODE" != "200" ]; then
  echo "Root endpoint failed, got $HTTP_CODE"
  exit 1
fi

echo "Testing nested SPA reload..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$HOST_PORT/nested/route")
if [ "$HTTP_CODE" != "200" ]; then
  echo "Nested SPA reload failed, got $HTTP_CODE"
  exit 1
fi

echo "Testing index marker on nested route..."
if ! curl -s "http://127.0.0.1:$HOST_PORT/nested/route" | grep -q 'id="app"'; then
  echo "Nested SPA reload did not return index marker"
  exit 1
fi

echo "Testing real emitted asset URLs..."
# Vite build puts files in /usr/share/nginx/html/assets/
ASSET_FILE=$(docker exec "$CONTAINER_NAME" sh -c "ls -1 /usr/share/nginx/html/assets/*.js | head -n 1" | xargs basename)
if [ -z "$ASSET_FILE" ]; then
  echo "No built asset found to test!"
  exit 1
fi
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$HOST_PORT/assets/$ASSET_FILE")
if [ "$HTTP_CODE" != "200" ]; then
  echo "Real asset failed, got $HTTP_CODE instead of 200"
  exit 1
fi

echo "Testing missing asset 404..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$HOST_PORT/assets/missing.js")
if [ "$HTTP_CODE" != "404" ]; then
  echo "Missing asset failed, got $HTTP_CODE instead of 404"
  exit 1
fi

echo "Testing /api/... 404..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$HOST_PORT/api/test")
if [ "$HTTP_CODE" != "404" ]; then
  echo "/api/... failed, got $HTTP_CODE instead of 404"
  exit 1
fi

echo "Inspecting non-root user..."
USER=$(docker inspect --format='{{.Config.User}}' "$IMAGE_NAME")
if [ -z "$USER" ] || [ "$USER" = "root" ] || [ "$USER" = "0" ]; then
  echo "User might be root, expected non-root: $USER"
  exit 1
fi

echo "Inspecting exposed port..."
EXPOSED_PORTS=$(docker inspect --format='{{range $p, $conf := .Config.ExposedPorts}}{{$p}}{{end}}' "$IMAGE_NAME")
if [[ "$EXPOSED_PORTS" != *"3000/tcp"* ]]; then
  echo "Port 3000 not exposed"
  exit 1
fi

echo "Inspecting for mounts..."
MOUNTS=$(docker inspect --format='{{json .Mounts}}' "$CONTAINER_NAME")
if [ "$MOUNTS" != "[]" ] && [ "$MOUNTS" != "null" ]; then
  echo "Container has unexpected mounts: $MOUNTS"
  exit 1
fi

echo "Inspecting loopback-only PortBindings..."
BIND_IP=$(docker inspect --format='{{(index (index .HostConfig.PortBindings "3000/tcp") 0).HostIp}}' "$CONTAINER_NAME")
if [ "$BIND_IP" != "127.0.0.1" ]; then
  echo "Container port bound to $BIND_IP, expected 127.0.0.1"
  exit 1
fi

echo "Integration tests passed successfully!"
