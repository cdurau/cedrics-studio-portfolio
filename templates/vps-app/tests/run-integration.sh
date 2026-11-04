#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=(docker)
if [[ "$(uname -s)" == Darwin ]]; then DOCKER=(docker --context devbox); fi
CONTAINER_NAME="app-template-test-$(openssl rand -hex 6)"
cleanup() { "${DOCKER[@]}" rm -fv "$CONTAINER_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"${DOCKER[@]}" run -d --name "$CONTAINER_NAME" --label studio.test=vps-template \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=testownerpass -e POSTGRES_DB=postgres \
  -p 127.0.0.1::5432 postgres:18-alpine@sha256:10a5c69a83035ab5239f5620d708520e8b28f5aa1f2b20143e6024532edadfb5 >/dev/null
ready=0
for ((i=0; i<30; i++)); do
  if "${DOCKER[@]}" exec "$CONTAINER_NAME" pg_isready -U postgres -d postgres >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo 'Isolated test DB unavailable' >&2; exit 1; }
HOST_PORT=$("${DOCKER[@]}" port "$CONTAINER_NAME" 5432 | awk -F: 'NR==1 {print $NF}')
export TEST_DATABASE_URL="postgresql://postgres:testownerpass@127.0.0.1:${HOST_PORT}/postgres"
export TEST_RUNTIME_DATABASE_URL="postgresql://app_runtime:$(printf '%064d' 1)@127.0.0.1:${HOST_PORT}/postgres"
# Bootstrap is exercised before baseline tests; empty DB admin seed is tested separately.
npx vitest run tests/bootstrap.test.ts
npx vitest run tests/integration.test.ts
