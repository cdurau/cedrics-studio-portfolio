#!/usr/bin/env bash
# Trusted deploy gate — root-owned, lives OUTSIDE every agent-writable repo.
#
# Install target  : /usr/local/libexec/studio-deploy/<slug>-deploy-gate  (root:wheel 0755)
# Canonical source: platform/studio-deploy/deploy-gate.sh (planning repo)
#
# Gate review 2026-09-20, SECOND round — finding 1 (KRITISCH), 2/3/7 (HOCH/KLEIN).
# Gate review 2026-09-21, THIRD round  — finding A (KRITISCH/HOCH), C (HOCH/MITTEL).
#
# WHY THIS FILE IS NO LONGER IN THE RUNTIME REPO
# ---------------------------------------------
# The first remediation kept the gate at `apps/<slug>/scripts/deploy-gate.sh` —
# inside the repository the deploy agent writes. A commit there decides what runs
# next to the owner-migration credential, the Coolify token and the Docker
# socket, which is precisely the authority boundary S0 exists to draw. The gate
# now lives under /usr/local/libexec/ (root-owned, mode 0755): the agent may read
# and execute it, and cannot change it with a runtime-repo commit. The runtime
# repo is demoted to build/source input.
#
# THE PRODUCTION TRUST PATH IS LITERAL (third round, finding A)
# ------------------------------------------------------------
# The previous version accepted environment overrides for the trust path
# (TRUST_DIR, MIGRATE_RUNNER, APPROVALS_FILE, DOCKER_BIN, DOCKER_CONTEXT,
# STATE_DIR, GATE_REPO, ...). Convenient for tests, but on the production path a
# local caller who can set the environment could point MIGRATE_RUNNER at a
# script it wrote itself — and that script would then be handed the owner
# credential. Every path, binary and bound below is therefore a LITERAL that the
# installing owner substitutes exactly once; no environment variable is
# consulted for any security-relevant decision. An override is not honoured, and
# the run says so out loud instead of silently ignoring it.
#
# The test suites never exercise this file. They exercise a *substituted copy*
# (tests/lib/subst.sh), so test injection cannot be activated on the production
# path by any caller, env var or flag.
#
# WHAT IT DOES (deploy mode) — fail-closed at every step
#   1. bind the requested commit to the allowed ref (tip equality, see below)
#   2. require the owner's approval list to be bound to that same commit
#   3. migrate through the TRUSTED migration runner, which may only apply
#      migration files whose exact content hash the operator approved, and which
#      runs no agent build at all
#   4. trigger the Coolify deploy only AFTER a successful migration
#   5. poll the deployment UUID to a terminal state (bounded)
#   6. verify that a healthy container bound to THIS APP and built from the FULL
#      expected commit is serving
#
# Modes are arguments, never environment variables:
#   deploy-gate.sh <full-40-char-commit-sha>            full deploy
#   deploy-gate.sh --verify-only <sha>                  step 6 alone, mutates nothing
#   deploy-gate.sh --ref-check <sha>                    validation + ref binding, stops
#
# --verify-only reads the app UUID (not a secret) and no credential whatsoever,
# so it stays usable without one.
#
# State: <STATE_DIR>/{coolify_uuid.txt,coolify_app_id.txt,coolify_token,migrate.env}
set -euo pipefail

# --- the literal production trust path (substituted once at install) ---------
SLUG="__APP_SLUG__"
TRUST_DIR="__TRUST_DIR__"
STATE_DIR="__STATE_DIR__"
GATE_REPO="__GATE_REPO__"
DOCKER_BIN="__DOCKER_BIN__"
DEPLOY_TIMEOUT="__DEPLOY_TIMEOUT__"
HEALTH_TIMEOUT="__HEALTH_TIMEOUT__"
POLL_INTERVAL="__POLL_INTERVAL__"
COOLIFY_API="__COOLIFY_API__"

# Fixed constants. Not overridable, not derived from the caller.
DEFAULT_TRUST_DIR="/usr/local/libexec/studio-deploy"
GATE_REF="migration/coolify-v5"
DOCKER_CONTEXT="devbox"

MIGRATE_RUNNER="$TRUST_DIR/${SLUG}-migrate-runner"
APPROVALS_FILE="$TRUST_DIR/approved-migrations/${SLUG}.txt"
MIGRATE_ENV="$STATE_DIR/migrate.env"
APP_UUID_FILE="$STATE_DIR/coolify_uuid.txt"
APP_ID_FILE="$STATE_DIR/coolify_app_id.txt"
TOKEN_FILE="$STATE_DIR/coolify_token"

log() { printf '[deploy-gate %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { printf '[deploy-gate] FAIL: %s\n' "$*" >&2; exit 1; }

# A source file that still carries a placeholder was never installed properly.
# Refusing is the only safe reading: a placeholder TRUST_DIR silently means
# "not the production trust root".
case "$SLUG$TRUST_DIR$STATE_DIR$GATE_REPO$DOCKER_BIN$COOLIFY_API" in
  *__*) die "unsubstituted placeholder in the installed gate — this is the canonical source, not an installed component (see platform/studio-deploy/README.md)" ;;
esac

# Deterministic tool resolution and a home directory from the OS, never from the
# caller's environment. git is additionally pinned to the system config so a
# user-writable ~/.gitconfig cannot steer the fetch (core.sshCommand etc.).
PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH
GIT_CONFIG_NOSYSTEM=1
GIT_CONFIG_GLOBAL=/dev/null
GIT_SSH_COMMAND="/usr/bin/ssh -o BatchMode=yes"
export GIT_CONFIG_NOSYSTEM GIT_CONFIG_GLOBAL GIT_SSH_COMMAND

os_user() { id -un 2>/dev/null || printf '%s' "${USER:-unknown}"; }
os_home() {
  local u h
  u="$(os_user)"
  h="$(dscl . -read "/Users/$u" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
  [ -n "$h" ] || h="$(getent passwd "$u" 2>/dev/null | cut -d: -f6)"
  [ -n "$h" ] || h="${HOME:-/}"
  printf '%s' "$h"
}
HOME="$(os_home)"
export HOME

# Environment overrides are ignored, and that is reported rather than hidden:
# "nothing happened" must not be confusable with "the override worked".
OVERRIDE_NAMES="APP_SLUG TRUST_DIR MIGRATE_RUNNER APPROVALS_FILE PSQL_IMAGE DOCKER_BIN DOCKER_CONTEXT DB_NETWORK STATE_DIR GATE_REPO GATE_REF COOLIFY_API VERIFY_ONLY REF_CHECK_ONLY DEPLOY_TIMEOUT HEALTH_TIMEOUT POLL_INTERVAL"
seen=""
for n in $OVERRIDE_NAMES; do
  case "${!n:-}" in
    "") : ;;
    *) seen="$seen $n" ;;
  esac
done
if [ -n "$seen" ]; then
  # Names only — an override value could itself be sensitive.
  log "ignoring environment override(s) — the trust path is literal:$seen"
fi

json() { jq -r "$1 // empty" 2>/dev/null; }
file_mode() { stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"; }
file_owner() { stat -f '%u' "$1" 2>/dev/null || stat -c '%u' "$1"; }

# Docker goes through a function, never through a string that has to be
# word-split: "$DOCKER inspect ..." inside a command substitution silently became
# a single command word and reported every container as absent, which would have
# turned a real failure into a wrong-but-plausible message.
d() {
  "$DOCKER_BIN" --context "$DOCKER_CONTEXT" "$@"
}

# --- 0. mode and the requested commit, validated before anything uses them ---
MODE="deploy"
case "${1:-}" in
  --verify-only) MODE="verify"; shift ;;
  --ref-check)   MODE="refcheck"; shift ;;
  --help|-h)     echo "usage: $0 [--verify-only|--ref-check] <full-40-char-commit-sha>"; exit 0 ;;
  --*)           die "unknown option ${1}" ;;
esac
EXPECTED_SHA="${1:-}"
[ -n "$EXPECTED_SHA" ] || { echo "usage: $0 [--verify-only|--ref-check] <full-40-char-commit-sha>" >&2; exit 2; }
[ "$#" -eq 1 ] || die "expected exactly one argument (the commit sha), got $#"

# Only a full lowercase hex SHA is accepted. A ref name, a flag or an
# option-looking string must never reach git as an argument, and the short-SHA
# form is refused so that the very same string can be compared against a full
# object name without ambiguity.
case "$EXPECTED_SHA" in
  "" | *[!0-9a-f]*) die "expected a full lowercase hex commit sha" ;;
esac
[ "${#EXPECTED_SHA}" -eq 40 ] || die "expected a full 40-character sha, got ${#EXPECTED_SHA} characters"

# --- 1. ref binding (deploy and refcheck modes) -----------------------------
#
# The previous version proved only that the git OBJECT existed locally
# (`git cat-file -e "${SHA}^{commit}"`), and then reported "not reachable on
# $REF" — which that check never established. Any object already present in the
# clone satisfied it, including one that belongs to no deployed branch at all.
#
# Deployment model decision: this pipeline deploys the HEAD OF THE ALLOWED BRANCH.
# The workflow triggers on a push to $GATE_REF and passes that push's SHA, so the
# only correct commit is the tip of the ref as fetched *now*. Accepting arbitrary
# ancestors would let a caller re-deploy any older commit — including one that
# predates a security fix — which is the weakness being closed here. Hence
# tip equality, not ancestry.
#
# verify mode is exempt: it fetches nothing, builds nothing and mutates nothing,
# it only inspects already-running containers, so a ref binding would be
# meaningless there.
if [ "$MODE" != "verify" ]; then
  [ -d "$GATE_REPO/.git" ] || die "GATE_REPO is not a git checkout: $GATE_REPO"
  log "fetching $GATE_REF into $GATE_REPO"
  git -C "$GATE_REPO" fetch --quiet --prune origin "$GATE_REF" \
    || die "git fetch of $GATE_REF failed"
  # FETCH_HEAD is written by the fetch above and is the tip of $GATE_REF as of
  # just now. Comparing against it binds the requested commit to the ref.
  FETCHED_TIP="$(git -C "$GATE_REPO" rev-parse --verify --quiet 'FETCH_HEAD^{commit}' || true)"
  [ -n "$FETCHED_TIP" ] || die "could not resolve the tip of $GATE_REF after fetch"
  if [ "$EXPECTED_SHA" != "$FETCHED_TIP" ]; then
    die "$EXPECTED_SHA is not the tip of $GATE_REF (tip is $FETCHED_TIP) — refusing to deploy a commit that is not on the allowed ref"
  fi
  log "commit $EXPECTED_SHA is the current tip of $GATE_REF"
fi

if [ "$MODE" = "refcheck" ]; then
  log "PASS — commit is bound to the allowed ref"
  exit 0
fi

# --- 2. preflight -----------------------------------------------------------
#
# Only the files the chosen mode actually reads are demanded. Permissions are
# still enforced wherever a file IS read: an owner DB URL or API token that is
# group/world readable is a finding in its own right, so refuse rather than
# quietly widen the blast radius.
if [ "$MODE" = "verify" ]; then
  log "verify mode — credential files are not required and not read"
  [ -r "$APP_UUID_FILE" ] || die "missing app identity file $APP_UUID_FILE (needed to bind the check to the target app)"
  APP_UUID="$(tr -d '[:space:]' < "$APP_UUID_FILE")"
  [ -n "$APP_UUID" ] || die "app identity file $APP_UUID_FILE is empty"
  APP_ID=""
  if [ -r "$APP_ID_FILE" ]; then APP_ID="$(tr -d '[:space:]' < "$APP_ID_FILE")"; fi
else
  # Trusted components must not be replaceable by the deploy agent. On the
  # production path they must be root-owned and never writable by group/other:
  # that is the whole point, and a non-root-owned gate is not the boundary the
  # hook claims. TRUST_DIR is a literal, so "non-default" can only mean an
  # uninstalled/test copy — never a production run someone steered.
  for f in "$MIGRATE_RUNNER" "$APPROVALS_FILE"; do
    [ -e "$f" ] || die "missing trusted component $f (see platform/studio-deploy/README.md — installation is an owner step)"
    perms="$(file_mode "$f")"
    case "$perms" in
      *[2367]|*[2367]?) die "$f has mode $perms — must not be writable by group/other" ;;
    esac
    if [ "$TRUST_DIR" = "$DEFAULT_TRUST_DIR" ]; then
      owner="$(file_owner "$f")"
      [ "$owner" = "0" ] || die "$f is owned by uid $owner, expected root — refusing to trust it"
    else
      log "non-default TRUST_DIR=$TRUST_DIR — ownership check relaxed (group/other-write still refused)"
    fi
  done
  for f in "$MIGRATE_ENV" "$APP_UUID_FILE" "$TOKEN_FILE"; do
    [ -r "$f" ] || die "missing state file $f"
    perms="$(file_mode "$f")"
    [ "$perms" = "600" ] || die "$f has mode $perms, expected 600"
  done
  APP_UUID="$(tr -d '[:space:]' < "$APP_UUID_FILE")"
  [ -n "$APP_UUID" ] || die "app identity file $APP_UUID_FILE is empty"
  APP_ID=""
  if [ -r "$APP_ID_FILE" ]; then APP_ID="$(tr -d '[:space:]' < "$APP_ID_FILE")"; fi

  # The human approval must name the commit it approves (third round, finding C).
  # The approvals file is root-owned, so a same-host agent cannot strip this
  # line; requiring it binds "a human read this SQL for THIS commit" to the
  # artifact the gate consumes, instead of trusting a hash produced by
  # agent-writable helper code.
  approvals_commit="$(awk '$1=="#" && $2=="commit" {print $3; exit}' "$APPROVALS_FILE" 2>/dev/null || true)"
  [ -n "$approvals_commit" ] || die "$APPROVALS_FILE carries no '# commit <sha>' line — the approval list is not bound to a reviewed commit, refusing (see platform/studio-deploy/README.md)"
  case "$approvals_commit" in
    "" | *[!0-9a-f]*) die "the '# commit' line in $APPROVALS_FILE is not a lowercase hex sha" ;;
  esac
  [ "${#approvals_commit}" -eq 40 ] || die "the '# commit' line in $APPROVALS_FILE is not a full 40-character sha"
  [ "$approvals_commit" = "$EXPECTED_SHA" ] \
    || die "the approvals list is bound to commit $approvals_commit but $EXPECTED_SHA was requested — approve the SQL for the commit you are actually deploying"
  log "approval list is bound to commit $approvals_commit"
fi

api() { # api <method> <path> [curl args...]
  local method="$1" path="$2"; shift 2
  curl -sS -X "$method" "$COOLIFY_API$path" \
    -H "Authorization: Bearer $(tr -d '[:space:]' < "$TOKEN_FILE")" \
    -H 'Accept: application/json' "$@"
}

# --- 6. verification: target app AND commit AND health -----------------------
#
# The previous version accepted ANY container carrying any coolify.applicationId
# label whose image/commit string merely contained the 7-character SHA. In a
# monorepo several apps carry the same commit, so a healthy container of another
# app could turn a failed deploy of THIS app green. All three facts are now
# required together, and the FULL 40-character SHA is matched:
#   * coolify.name label == the app UUID from the app identity file
#   * coolify.applicationId label == the app id, when one is on file
#   * image tag or coolify.commit label carries the full expected SHA
#   * container health is "healthy"
verify_target_app() {
  log "verifying a healthy container for app $APP_UUID at $EXPECTED_SHA"
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  local found="" names c info health image commit label_name label_appid last=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    found=""
    names="$(d ps --filter "label=coolify.name=$APP_UUID" --format '{{.Names}}' 2>/dev/null || true)"
    for c in $names; do
      [ -n "$c" ] || continue
      info="$(d inspect -f '{{.State.Health.Status}}|{{.Config.Image}}|{{index .Config.Labels "coolify.commit"}}|{{index .Config.Labels "coolify.name"}}|{{index .Config.Labels "coolify.applicationId"}}' "$c" 2>/dev/null || echo 'unknown||||')"
      # Split on '|' without `read` and without a temporary IFS: empty labels are
      # meaningful here (a container may carry no commit label) and tab/newline
      # IFS handling otherwise shifts the fields.
      health="${info%%|*}"; info="${info#*|}"
      image="${info%%|*}";  info="${info#*|}"
      commit="${info%%|*}"; info="${info#*|}"
      label_name="${info%%|*}"; info="${info#*|}"
      label_appid="$info"
      # App binding: the UUID label must match, and so must the numeric id when
      # the operator put one on file.
      [ "$label_name" = "$APP_UUID" ] || continue
      if [ -n "$APP_ID" ] && [ "$label_appid" != "$APP_ID" ]; then continue; fi
      # Commit binding, full SHA only.
      case "$image$commit" in
        *"$EXPECTED_SHA"*) : ;;
        *) last="container $c is not built from $EXPECTED_SHA"; continue ;;
      esac
      if [ "$health" != "healthy" ]; then last="container $c health: $health"; continue; fi
      found="$c"
      break
    done
    if [ -n "$found" ]; then
      log "container $found is app $APP_UUID, built from $EXPECTED_SHA, healthy"
      return 0
    fi
    log "not there yet${last:+ ($last)}"
    sleep "$POLL_INTERVAL"
  done
  die "no healthy container for app $APP_UUID built from $EXPECTED_SHA — the deploy is not serving this app at this commit${last:+ ($last)}"
}

if [ "$MODE" = "verify" ]; then
  verify_target_app
  log "PASS — healthy container for expected app and commit is serving"
  exit 0
fi

# --- one deploy at a time ----------------------------------------------------
# Two interleaved runs share the migration and the checkout and can cut over
# against a half-applied schema. mkdir is atomic; no flock dependency.
LOCK_DIR="$STATE_DIR/deploy.lock"
mkdir "$LOCK_DIR" 2>/dev/null || die "another deploy is running ($LOCK_DIR exists) — refusing to interleave"
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

# --- checkout of the approved commit ----------------------------------------
log "checking out $EXPECTED_SHA"
git -C "$GATE_REPO" checkout --quiet --detach "$EXPECTED_SHA" \
  || die "git checkout of $EXPECTED_SHA failed"
[ "$(git -C "$GATE_REPO" rev-parse HEAD)" = "$EXPECTED_SHA" ] \
  || die "$GATE_REPO is not at $EXPECTED_SHA after checkout"

MIG_DIR="$GATE_REPO/apps/$SLUG/drizzle/migrations"
[ -d "$MIG_DIR" ] || die "no migrations directory $MIG_DIR in the checked-out tree"
[ -r "$MIG_DIR/meta/_journal.json" ] || die "no migration journal at $MIG_DIR/meta/_journal.json"

# --- 3. owner migration through the TRUSTED runner --------------------------
#
# This gate never reads the owner credential and never builds an image from the
# checkout. The runner is root-owned, needs no agent build (pinned psql image),
# and refuses every migration file whose content hash the operator has not
# approved. A new migration therefore does not run with owner rights until a
# human has approved it — fail-closed, which is the point.
log "running owner migration through the trusted runner (approval-gated)"
/bin/bash "$MIGRATE_RUNNER" "$MIG_DIR" "$MIGRATE_ENV" "$APPROVALS_FILE" \
  || die "migration failed or was not approved — NOT triggering a deploy (the old server keeps running against the old schema)"
log "migration ok"

# --- 4. trigger the deploy --------------------------------------------------
log "triggering Coolify deploy for $APP_UUID"
trigger="$(api GET "/deploy?uuid=$APP_UUID&force=false")" || die "deploy trigger call failed"
DEPLOY_UUID="$(printf '%s' "$trigger" | json '.deployments[0].deployment_uuid')"
[ -n "$DEPLOY_UUID" ] || die "no deployment uuid in trigger response (trigger accepted ≠ deploy started)"
log "deployment $DEPLOY_UUID"

# --- 5. poll to a terminal state --------------------------------------------
deadline=$(( $(date +%s) + DEPLOY_TIMEOUT ))
status=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  # Coolify has returned the deployment either flat or wrapped depending on
  # version; read whichever shape arrives rather than assuming one.
  body="$(api GET "/deployments/$DEPLOY_UUID")"
  status="$(printf '%s' "$body" | json '(.status // .deployments[0].status)')"
  case "$status" in
    finished)                        log "deployment finished"; break ;;
    failed|cancelled-by-force|error) die "deployment ended as '$status'" ;;
    "")                              die "deployment status unreadable" ;;
    *)                               log "status: $status"; sleep "$POLL_INTERVAL" ;;
  esac
done
[ "$status" = "finished" ] || die "deployment did not reach a terminal state within ${DEPLOY_TIMEOUT}s"

# --- 6. the part a green trigger never proves -------------------------------
verify_target_app

log "PASS — migrated, deployed, healthy, serving $EXPECTED_SHA"
