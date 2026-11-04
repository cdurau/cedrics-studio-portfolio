#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=(docker)
if [[ "$(uname -s)" == Darwin ]]; then DOCKER=(docker --context devbox); fi
prefix="app-image-test-$(openssl rand -hex 6)"
image="$prefix:check"
cleanup() {
  "${DOCKER[@]}" rm -fv "$prefix-app" "$prefix-db" >/dev/null 2>&1 || true
  "${DOCKER[@]}" network rm "$prefix" >/dev/null 2>&1 || true
  "${DOCKER[@]}" image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
sha=1111111111111111111111111111111111111111
"${DOCKER[@]}" build --platform linux/amd64 --build-arg "APP_COMMIT_SHA=$sha" --tag "$image" .
"${DOCKER[@]}" network create --internal --label studio.test=vps-template "$prefix" >/dev/null
"${DOCKER[@]}" run -d --name "$prefix-db" --label studio.test=vps-template --network "$prefix" --network-alias db \
  -e POSTGRES_PASSWORD=testownerpass \
  postgres:18-alpine@sha256:10a5c69a83035ab5239f5620d708520e8b28f5aa1f2b20143e6024532edadfb5 >/dev/null
ready=0
for ((i=0; i<30; i++)); do
  if "${DOCKER[@]}" exec "$prefix-db" pg_isready -U postgres >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]] || exit 1
"${DOCKER[@]}" run -d --platform linux/amd64 --name "$prefix-app" --label studio.test=vps-template --network "$prefix" \
  -e "DATABASE_URL=postgresql://app_runtime:$(printf '%064d' 1)@db:5432/postgres" \
  -e MIGRATION_DATABASE_URL=postgresql://postgres:testownerpass@db:5432/postgres \
  -e ADMIN_EMAIL=smoke@test.com -e ADMIN_PASSWORD=smoke-test-password \
  -e BETTER_AUTH_SECRETS=1:smoke-test-secret-0123456789abcdef0123456789abcdef \
  -e BETTER_AUTH_URL=https://smoke.studio.example \
  -e BETTER_AUTH_TRUSTED_PROXIES=192.0.2.1/32 \
  -e APP_COMMIT_SHA=2222222222222222222222222222222222222222 "$image" >/dev/null
ready=0
for ((i=0; i<60; i++)); do
  if [[ "$("${DOCKER[@]}" inspect --format '{{.State.Running}}' "$prefix-app")" != true ]]; then
    echo 'IMAGE_SMOKE_CONTAINER_EXITED' >&2; break
  fi
  if "${DOCKER[@]}" exec "$prefix-app" node -e 'fetch("http://127.0.0.1:3000/readyz").then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))' >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo 'IMAGE_SMOKE_START_FAILED' >&2; exit 1; }
"${DOCKER[@]}" exec "$prefix-app" node --input-type=module -e '
  import { readFileSync } from "node:fs";
  import assert from "node:assert/strict";
  assert.notEqual(process.getuid(), 0);
  const env = readFileSync("/proc/1/environ", "utf8").split("\0");
  for (const name of ["MIGRATION_DATABASE_URL", "ADMIN_EMAIL", "ADMIN_PASSWORD"])
    assert(!env.some(value => value.startsWith(name+"=")), "server env contains bootstrap variable");
  const r = await fetch("http://127.0.0.1:3000/readyz");
  assert.equal(r.status,200);
  assert.deepEqual(await r.json(),{ok:true,commit:"1111111111111111111111111111111111111111"});
  assert.equal((await fetch("http://127.0.0.1:3000/api/deployment")).status,401);
  const login = await fetch("http://127.0.0.1:3000/api/auth/sign-in/email", {
    method:"POST",headers:{"Content-Type":"application/json","Origin":"https://smoke.studio.example","X-Forwarded-Proto":"https"},
    body:JSON.stringify({email:"smoke@test.com",password:"smoke-test-password"})
  });
  assert.equal(login.status,200);
  const cookie = login.headers.getSetCookie().map(s=>s.split(";")[0]).join("; ");
  assert(cookie);
  const probe=await fetch("http://127.0.0.1:3000/api/deployment",{headers:{cookie}});
  assert.equal(probe.status,200); assert.equal(probe.headers.get("cache-control"),"no-store");
  assert.deepEqual(await probe.json(),{ok:true,commit:"1111111111111111111111111111111111111111"});
  // Test cookie exists only inside the disposable container for outage proof.
  await (await import("node:fs/promises")).writeFile("/tmp/smoke-cookie",cookie,{mode:0o600});
'
"${DOCKER[@]}" stop "$prefix-db" >/dev/null
"${DOCKER[@]}" exec "$prefix-app" node --input-type=module -e '
  import assert from "node:assert/strict";
  assert.equal((await fetch("http://127.0.0.1:3000/healthz")).status,200);
  const r=await fetch("http://127.0.0.1:3000/readyz");
  assert.equal(r.status,503); assert.equal((await r.json()).ok,false);
  const cookie=await (await import("node:fs/promises")).readFile("/tmp/smoke-cookie","utf8");
  const probe=await fetch("http://127.0.0.1:3000/api/deployment",{headers:{cookie}});
  assert.equal(probe.status,503); assert.equal(probe.headers.get("cache-control"),"no-store");
  assert.deepEqual(await probe.json(),{ok:false,commit:null});

'
echo 'IMAGE_SMOKE_PASS'
