#!/usr/bin/env bash
# Trusted deploy gate — root-owned, lives OUTSIDE every agent-writable repo.
#
# Install target  : /usr/local/libexec/studio-deploy/<slug>-deploy-gate  (root:wheel 0755)
# Canonical source: platform/studio-deploy/deploy-gate.sh (planning repo)
#
# Gate review 2026-09-20, SECOND round — finding 1 (KRITISCH), 2/3/7 (HOCH/KLEIN).
# Gate review 2026-09-21, THIRD round  — finding A (KRITISCH/HOCH), C (HOCH/MITTEL).
# S0-13 (2026-09-22)                   — dedicated deploy user.
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
# THIS GATE RUNS AS THE DEDICATED DEPLOY USER AND HOLDS NO DOCKER ACCESS
# ---------------------------------------------------------------------
# S0-13, the dedicated-deploy-user model. The gate's uid is the forced command's
# uid — the hidden service account `studio-deploy`, NOT the login user the coding
# agent runs as. That is what separates the deploy credential from the agent: the
# agent's processes are a different uid, so mode-600 files owned by the deploy
# user are unreadable to them. The gate refuses to run at all if its effective
# account is not the installed DEPLOY_USER, so a copy invoked as the login user
# cannot silently act with the agent's identity. The identity is the account NAME:
# the numeric uid is whatever the OS assigned at install time and is never
# hard-coded, so installing with a different free uid cannot stale this check.
#
# The gate itself never touches the Docker socket. On this host the socket is
# `login-user:login-group` mode 600, so reaching it here would hand the gate the login
# user's authority and undo the separation. Instead exactly two operations are
# delegated through `sudo -n` to root-owned, ARGUMENT-FREE helpers:
#   * <slug>-migrate-runner   — the ONE Docker write (the owner migration)
#   * <slug>-verify-container — READ-ONLY container ps/inspect, facts only
# Both are pinned in /etc/sudoers.d with the empty-argument form, so argv is
# refused by sudo itself. The binding decisions (which commit, which app, which
# health) stay HERE; the helper only reports facts, which is why it needs no
# arguments and why the deploy user cannot launder one through it.
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
# WHAT IT DOES (deploy mode) — fail-closed at every step
#   1. bind the requested commit to the allowed ref (tip equality, see below)
#   2. require the owner's approval list to be bound to that same commit
#   3. migrate through the TRUSTED migration runner (sudo, no arguments), which
#      may only apply migration files whose exact content hash the operator
#      approved, and which runs no agent build at all
#   4. trigger the Coolify deploy only AFTER a successful migration
#   5. poll the deployment UUID to a terminal state (bounded)
#   6. verify that a healthy container bound to THIS APP and built from the FULL
#      expected commit is serving (via the read-only helper)
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
DEPLOY_USER="__DEPLOY_USER__"
SUDO_BIN="__SUDO_BIN__"
DEPLOY_TIMEOUT="__DEPLOY_TIMEOUT__"
HEALTH_TIMEOUT="__HEALTH_TIMEOUT__"
POLL_INTERVAL="__POLL_INTERVAL__"
COOLIFY_API="__COOLIFY_API__"

# Fixed constants. Not overridable, not derived from the caller.
DEFAULT_TRUST_DIR="/usr/local/libexec/studio-deploy"
GATE_REF="migration/coolify-v5"

MIGRATE_RUNNER="$TRUST_DIR/${SLUG}-migrate-runner"
VERIFY_CONTAINER="$TRUST_DIR/${SLUG}-verify-container"
APPROVALS_FILE="$TRUST_DIR/approved-migrations/${SLUG}.txt"
MIGRATE_ENV="$STATE_DIR/migrate.env"
APP_UUID_FILE="$STATE_DIR/coolify_uuid.txt"
APP_ID_FILE="$STATE_DIR/coolify_app_id.txt"
TOKEN_FILE="$STATE_DIR/coolify_token"

log() { printf '[deploy-gate %s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
die() { printf '[deploy-gate] FAIL: %s\n' "$*" >&2; exit 1; }

# --- trusted-path predicates (lstat-correct, symlink-free) ------------------
# `stat` uses lstat semantics: it measures the SYMLINK, not the file that is
# actually opened or executed. That made the previous ownership check
# bypassable on this very host — /usr/local/bin/docker is a root:wheel symlink
# whose payload lives in /Applications/Docker.app and is owned by the LOGIN
# USER, so `stat -f '%u'` returned 0 ("root-owned, trusted") while root would
# have executed an agent-writable binary. Measured 2026-09-22.
#
# Two independent corrections, because neither alone is the property wanted:
#   1. a trusted path must not BE a symlink — refused outright, there is no
#      legitimate reason for one on an installed trust path; and
#   2. every check uses `stat -L`, so if a link ever does appear, the property
#      measured is the one that decides what actually runs.
file_mode()  { stat -L -f '%Lp' "$1" 2>/dev/null || stat -L -c '%a' "$1"; }
file_owner() { stat -L -f '%u'  "$1" 2>/dev/null || stat -L -c '%u'  "$1"; }

# Ownership of the FILE is not enough: a root-owned file inside a directory the
# agent can write can be replaced wholesale (unlink + create), so the guarantee
# has to cover the whole chain from `/` down — every component root-owned and
# never group/other-writable. The same rule applies to the host's sudoers
# targets; see the unpublished host-trust review.
assert_trusted_path() { # assert_trusted_path <absolute path>
  local p="$1" owner mode
  case "$p" in
    /*) : ;;
    *) die "trusted path must be absolute: $p" ;;
  esac
  while : ; do
    [ ! -h "$p" ] || die "$p is a symlink — a trusted path must not be one"
    owner="$(file_owner "$p")"
    mode="$(file_mode "$p")"
    [ "$owner" = "0" ] \
      || die "$p is owned by uid $owner, expected root — the trusted path is replaceable"
    case "$mode" in
      *[2367]|*[2367]?) die "$p has mode $mode — must not be writable by group/other" ;;
    esac
    [ "$p" != "/" ] || break
    p="${p%/*}"
    [ -n "$p" ] || p="/"
  done
}


# A source file that still carries a placeholder was never installed properly.
# Refusing is the only safe reading: a placeholder TRUST_DIR silently means
# "not the production trust root".
case "$SLUG$TRUST_DIR$STATE_DIR$GATE_REPO$DEPLOY_USER$SUDO_BIN$COOLIFY_API" in
  *__*) die "unsubstituted placeholder in the installed gate — this is the canonical source, not an installed component (see platform/studio-deploy/README.md)" ;;
esac

# --- the gate's own identity -------------------------------------------------
# The identity is the ACCOUNT NAME, not a number. The uid is resolved by the OS at
# install time and can differ from any value written in a document, so the name is
# what stays true; nothing here hard-codes a numeric uid. Ownership is compared
# against the uid this process actually is (below), so the check is uid-agnostic.
#
# The authority this gate exercises is the DEPLOY USER's, so running it as any
# other account is a configuration error and not something to work around: a copy
# invoked as the login user would read the login user's state directory and act
# with the agent's identity, which is the separation this model removes.
case "$DEPLOY_USER" in
  "" | *[!0-9a-zA-Z_.-]*) die "DEPLOY_USER is not a plausible account name in the installed gate" ;;
esac
SELF_USER="$(id -un)"
[ "$SELF_USER" = "$DEPLOY_USER" ] \
  || die "refusing to run as user '$SELF_USER' — this gate is installed for the deploy account '$DEPLOY_USER'"
SELF_UID="$(id -u)"

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
OVERRIDE_NAMES="APP_SLUG TRUST_DIR MIGRATE_RUNNER VERIFY_CONTAINER APPROVALS_FILE PSQL_IMAGE DOCKER_BIN DOCKER_CONTEXT DB_NETWORK STATE_DIR GATE_REPO GATE_REF COOLIFY_API VERIFY_ONLY REF_CHECK_ONLY DEPLOY_TIMEOUT HEALTH_TIMEOUT POLL_INTERVAL DEPLOY_USER SUDO_BIN"
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
# file_mode/file_owner are defined once, near die(), in their lstat-correct form.

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
  for f in "$MIGRATE_RUNNER" "$VERIFY_CONTAINER" "$APPROVALS_FILE"; do
    [ -e "$f" ] || die "missing trusted component $f (see platform/studio-deploy/README.md — installation is an owner step)"
    # A symlinked component is refused everywhere: `stat` measures the link, so
    # a root-owned link to an agent-writable file would otherwise pass as
    # "root-owned". This is the same defect class that was found in the docker
    # binary check on 2026-09-22.
    [ ! -h "$f" ] || die "$f is a symlink — a trusted component must be a real root-owned file"
    perms="$(file_mode "$f")"
    case "$perms" in
      *[2367]|*[2367]?) die "$f has mode $perms — must not be writable by group/other" ;;
    esac
    if [ "$TRUST_DIR" = "$DEFAULT_TRUST_DIR" ]; then
      # Whole chain: a root-owned component inside an agent-writable directory
      # can be replaced wholesale, so file ownership alone is not the property.
      assert_trusted_path "$f"
    else
      log "non-default TRUST_DIR=$TRUST_DIR — ownership check relaxed (group/other-write still refused)"
    fi
  done
  # The state directory belongs to the deploy user: its files must be owned by the
  # uid this gate runs as, not merely mode-600. A 600 file owned by another uid is
  # either unreadable (and then the run must fail loudly) or, worse, a hint that
  # the state was moved from a different account without re-owning it.
  for f in "$MIGRATE_ENV" "$APP_UUID_FILE" "$TOKEN_FILE"; do
    [ -r "$f" ] || die "missing or unreadable state file $f"
    # Ownership and mode describe the LINK unless the link is refused: a state
    # file that is a symlink has its content chosen by whoever made the link.
    [ ! -h "$f" ] || die "$f is a symlink — deploy state must be a real file owned by the deploy user"
    perms="$(file_mode "$f")"
    [ "$perms" = "600" ] || die "$f has mode $perms, expected 600"
    owner="$(file_owner "$f")"
    [ "$owner" = "$SELF_UID" ] \
      || die "$f is owned by uid $owner but this gate runs as uid $SELF_UID — the deploy state must belong to the deploy user"
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
#
# The docker facts come from the root-owned READ-ONLY helper, called through
# sudo with NO arguments. The binding below is deliberately kept here, in the
# gate — a helper that returned a verdict would need the expected SHA as an
# argument, and an argument is exactly what the deploy user could launder.
verify_target_app() {
  log "verifying a healthy container for app $APP_UUID at $EXPECTED_SHA"
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  local found="" line c health image commit label_name label_appid rest last=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    found=""
    if ! listing="$("$SUDO_BIN" -n "$VERIFY_CONTAINER" 2>/dev/null)"; then
      listing=""
      last="the container-facts helper did not return a listing"
    fi
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      # "<name>\t<health>\t<image>\t<commit>\t<name-label>\t<appid-label>"
      c="${line%%$'\t'*}";        rest="${line#*$'\t'}"
      health="${rest%%$'\t'*}";   rest="${rest#*$'\t'}"
      image="${rest%%$'\t'*}";    rest="${rest#*$'\t'}"
      commit="${rest%%$'\t'*}";   rest="${rest#*$'\t'}"
      label_name="${rest%%$'\t'*}"; rest="${rest#*$'\t'}"
      label_appid="$rest"
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
    done <<EOF
$listing
EOF
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
# This gate never reads the owner credential, never builds an image from the
# checkout and never talks to Docker. The runner is root-owned, is reached
# through `sudo -n` with NO arguments (so argv is refused by sudo before the
# script runs), takes every path from literals, needs no agent build (pinned psql
# image), and refuses every migration file whose content hash the operator has
# not approved. The runner additionally re-checks on its own that the clone's
# HEAD is the commit the approval list names.
log "running owner migration through the trusted runner (approval-gated, sudo, no arguments)"
"$SUDO_BIN" -n "$MIGRATE_RUNNER" \
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
