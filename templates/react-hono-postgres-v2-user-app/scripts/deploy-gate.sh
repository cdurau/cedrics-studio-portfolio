#!/usr/bin/env bash
# Bounded deploy gate — runs ON THE MINI, never in GitHub Actions.
#
# Gate review 2026-09-20 (WR-01). The previous pipeline sent
# `POST /api/v1/deploy` and graded itself on that call's HTTP status. A 200
# there means "trigger accepted" and nothing else: the project's own evidence
# contains a deploy whose trigger succeeded and whose container never booted.
# Worse, the separate owner-role migration documented in the README and the
# Dockerfile was never executed by the pipeline at all — so the next schema
# change would start a new server against an old schema.
#
# This script is the whole deploy, in order, and it fails closed at every step:
#
#   1. migrate with the OWNER role, from an owner-only env-file on this host
#   2. only then trigger the Coolify deploy
#   3. poll the deployment UUID until a terminal state (bounded)
#   4. verify the running container is healthy AND built from the expected commit
#
# The owner credential is read from a mode-600 file on this machine and never
# leaves it. GitHub only supplies the commit SHA — it never holds, sees or
# forwards the migration credential.
#
# Usage:  deploy-gate.sh <expected-commit-sha>
# State:  ~/.local/state/<slug>/{migrate.env,coolify_uuid.txt,coolify_token}
set -euo pipefail

SLUG="${APP_SLUG:-__APP_SLUG__}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/$SLUG}"
MIGRATE_ENV="$STATE_DIR/migrate.env"
APP_UUID_FILE="$STATE_DIR/coolify_uuid.txt"
TOKEN_FILE="$STATE_DIR/coolify_token"
COOLIFY_API="${COOLIFY_API:-http://localhost:8000/api/v1}"
DOCKER="docker --context ${DOCKER_CONTEXT:-devbox}"

DEPLOY_TIMEOUT="${DEPLOY_TIMEOUT:-900}"   # seconds, bounded — never poll forever
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
POLL_INTERVAL="${POLL_INTERVAL:-10}"

EXPECTED_SHA="${1:-}"
[ -n "$EXPECTED_SHA" ] || { echo "usage: $0 <expected-commit-sha>" >&2; exit 2; }
SHORT_SHA="${EXPECTED_SHA:0:7}"

log() { printf '[deploy-gate %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { printf '[deploy-gate] FAIL: %s\n' "$*" >&2; exit 1; }

# VERIFY_ONLY=1 runs step 4 alone: no migration, no trigger, nothing mutated.
# It exists so the negative test can exercise the verification logic without
# deploying to production — and so an operator can ask "is what is running
# actually this commit?" at any time. It reads no credential, so it must not
# demand one: requiring the owner env-file here made the read-only check
# unusable whenever no owner credential was at hand, and it let the negative
# test "pass" by aborting in preflight instead of in the logic under test.
VERIFY_ONLY="${VERIFY_ONLY:-0}"

# The preflight guards what the chosen mode actually uses. MIGRATE_ENV and the
# Coolify token are consumed by steps 1–3 only; the docker inspection in step 4
# needs no credential. Permissions are still enforced wherever a file is read —
# an owner DB URL or API token that is group/world readable is a finding in its
# own right, so refuse rather than quietly widen the blast radius.
if [ "$VERIFY_ONLY" = "1" ]; then
  log "VERIFY_ONLY=1 — credential files are not required and not read"
else
  for f in "$MIGRATE_ENV" "$APP_UUID_FILE" "$TOKEN_FILE"; do
    [ -r "$f" ] || die "missing state file $f"
    perms="$(stat -f '%Lp' "$f" 2>/dev/null || stat -c '%a' "$f")"
    [ "$perms" = "600" ] || die "$f has mode $perms, expected 600"
  done

  APP_UUID="$(tr -d '[:space:]' < "$APP_UUID_FILE")"
fi

api() { # api <method> <path> [curl args...]
  local method="$1" path="$2"; shift 2
  curl -sS -X "$method" "$COOLIFY_API$path" \
    -H "Authorization: Bearer $(tr -d '[:space:]' < "$TOKEN_FILE")" \
    -H 'Accept: application/json' "$@"
}

json() { # json <key> — minimal extractor, avoids a jq dependency on the host
  python3 -c 'import json,sys;d=json.load(sys.stdin);k=sys.argv[1].split(".")
o=d
for p in k:
    o=o[int(p)] if p.isdigit() else o.get(p) if isinstance(o,dict) else None
    if o is None: break
print(o if o is not None else "")' "$1"
}

if [ "$VERIFY_ONLY" != "1" ]; then

# --- 1. migration, owner role, before any cutover -----------------------
#
# The image must exist before it can migrate. Coolify builds it during deploy,
# so we build the migration image here, from the tree this script is told to
# trust, and throw the result away afterwards.
#
# Two ways to arrive here, decided by GATE_REPO:
#   * unset  — a CI checkout: the caller already materialised the commit, so the
#              build context is the current directory.
#   * set    — the SSH forced-command hook: the caller passed ONLY a SHA, so the
#              mini fetches that commit into its own clone and builds from
#              there. The builder then decides what it builds, not the caller.
BUILD_DIR="."
if [ -n "${GATE_REPO:-}" ]; then
  [ -d "$GATE_REPO/.git" ] || die "GATE_REPO is not a git checkout: $GATE_REPO"
  # Only a full lowercase hex SHA is accepted here; anything else (a ref name,
  # a flag, an option-looking string) must never reach git as an argument.
  case "$EXPECTED_SHA" in
    "" | *[!0-9a-f]*) die "GATE_REPO mode requires a full lowercase hex commit sha" ;;
  esac
  [ "${#EXPECTED_SHA}" -eq 40 ] || die "GATE_REPO mode requires a full 40-character sha"
  GATE_REF="${GATE_REF:-migration/coolify-v5}"
  log "fetching $GATE_REF into $GATE_REPO"
  git -C "$GATE_REPO" fetch --quiet --prune origin "$GATE_REF" \
    || die "git fetch of $GATE_REF failed"
  # Fetch by ref, then require the commit to be present locally: fetching a bare
  # SHA depends on server-side config, and a silent miss here would build the
  # wrong tree instead of failing.
  git -C "$GATE_REPO" cat-file -e "${EXPECTED_SHA}^{commit}" 2>/dev/null \
    || die "commit $SHORT_SHA is not present in $GATE_REPO after fetch (not on $GATE_REF?)"
  log "checking out $SHORT_SHA"
  git -C "$GATE_REPO" checkout --quiet --detach "$EXPECTED_SHA" \
    || die "git checkout of $SHORT_SHA failed"
  [ "$(git -C "$GATE_REPO" rev-parse HEAD)" = "$EXPECTED_SHA" ] \
    || die "$GATE_REPO is not at $SHORT_SHA after checkout"
  BUILD_DIR="$GATE_REPO/apps/$SLUG"
  [ -d "$BUILD_DIR" ] || die "no app directory $BUILD_DIR in the fetched tree"
fi

log "building migration image for $SHORT_SHA"
MIG_IMAGE="$SLUG-migrate:$SHORT_SHA"
$DOCKER build -q -t "$MIG_IMAGE" "$BUILD_DIR" >/dev/null || die "migration image build failed"

log "running owner migration (credential stays on this host)"
if ! $DOCKER run --rm --network coolify --env-file "$MIGRATE_ENV" \
      "$MIG_IMAGE" node dist/server/server/migrate.js; then
  $DOCKER image rm -f "$MIG_IMAGE" >/dev/null 2>&1 || true
  die "migration failed — NOT triggering a deploy (the old server keeps running against the old schema)"
fi
$DOCKER image rm -f "$MIG_IMAGE" >/dev/null 2>&1 || true
log "migration ok"

# --- 2. trigger the deploy ----------------------------------------------
log "triggering Coolify deploy for $APP_UUID"
trigger="$(api GET "/deploy?uuid=$APP_UUID&force=false")" || die "deploy trigger call failed"
DEPLOY_UUID="$(printf '%s' "$trigger" | json 'deployments.0.deployment_uuid')"
[ -n "$DEPLOY_UUID" ] || die "no deployment uuid in trigger response (trigger accepted ≠ deploy started)"
log "deployment $DEPLOY_UUID"

# --- 3. poll to a terminal state ----------------------------------------
deadline=$(( $(date +%s) + DEPLOY_TIMEOUT ))
status=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  # Coolify has returned the deployment either flat or wrapped depending on
  # version; read whichever shape arrives rather than assuming one.
  body="$(api GET "/deployments/$DEPLOY_UUID")"
  status="$(printf '%s' "$body" | json 'status')"
  [ -n "$status" ] || status="$(printf '%s' "$body" | json 'deployments.0.status')"
  case "$status" in
    finished)                      log "deployment finished"; break ;;
    failed|cancelled-by-force|error) die "deployment ended as '$status'" ;;
    "")                            die "deployment status unreadable" ;;
    *)                             log "status: $status"; sleep "$POLL_INTERVAL" ;;
  esac
done
[ "$status" = "finished" ] || die "deployment did not reach a terminal state within ${DEPLOY_TIMEOUT}s"

else
  log "VERIFY_ONLY=1 — skipping migration and deploy, verifying the running container only"
fi

# --- 4. the part a green trigger never proves ---------------------------
log "verifying running container"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
container=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  # Match on the image tag (Coolify tags images <app-uuid>:<commit>) and fall
  # back to the commit label, so a retagged or pruned image does not silently
  # turn "not this commit" into "no container found".
  container="$($DOCKER ps --filter "label=coolify.applicationId" \
    --format '{{.Names}} {{.Image}} {{.Label "coolify.commit"}}' \
    | grep -F "$SHORT_SHA" | head -n1 | awk '{print $1}' || true)"
  if [ -n "$container" ]; then
    health="$($DOCKER inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo unknown)"
    [ "$health" = "healthy" ] && { log "container $container healthy"; break; }
    log "health: $health"
  else
    log "no container from $SHORT_SHA yet"
  fi
  sleep "$POLL_INTERVAL"
  container=""
done
[ -n "$container" ] || die "no healthy container built from $SHORT_SHA — the deploy reported success but the app is not serving it"

image="$($DOCKER inspect -f '{{.Config.Image}}' "$container")"
commit_label="$($DOCKER inspect -f '{{index .Config.Labels "coolify.commit"}}' "$container" 2>/dev/null || true)"
case "$image$commit_label" in
  *"$SHORT_SHA"*) log "running container is built from $SHORT_SHA" ;;
  *)              die "running image/label does not carry $SHORT_SHA (image=$image)" ;;
esac

log "PASS — migrated, deployed, healthy, serving $SHORT_SHA"
