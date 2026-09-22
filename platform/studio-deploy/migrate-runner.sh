#!/usr/bin/env bash
# Trusted migration runner — root-owned, argument-FREE.
#
# Install target: /usr/local/libexec/studio-deploy/<slug>-migrate-runner (root:wheel 0755)
# Canonical source: platform/studio-deploy/migrate-runner.sh (planning repo)
#                  Installation is an owner step.
#
# Why this exists (gate review 2026-09-20, second round — finding 1, KRITISCH)
# ---------------------------------------------------------------------------
# The first remediation ran the owner migration by BUILDING A MIGRATION IMAGE
# FROM THE AGENT-WRITTEN RUNTIME REPO and running it with MIGRATION_DATABASE_URL.
# That hands the owner credential to agent-controlled code: any file in that
# repository lands in the image and executes with owner rights on the production
# database. Approving SQL *text* alone would not have closed it either — in that
# design the runner itself was agent code.
#
# So the runner is trusted and the SQL is data:
#   * this script is root-owned and outside every repo an agent can write;
#   * it applies ONLY migration files whose exact content hash the operator has
#     approved (approved-migrations/<slug>.txt);
#   * it needs no agent build at all — the pinned postgres image provides psql,
#     so no agent-authored binary ever holds the owner credential.
#
# NO ENVIRONMENT OVERRIDE REACHES THE TRUST PATH (third round, finding A)
# ---------------------------------------------------------------------
# This script reads the owner credential, exports PGPASSWORD and then starts a
# container. That makes every path and image it uses part of the credential
# path: if PSQL_IMAGE, DOCKER_BIN or DOCKER_CONTEXT could be set by an untrusted
# caller, agent-controlled code would get the owner credential back. They are
# therefore literals.
#
# ARGUMENT-FREE (S0-13, dedicated-deploy-user model)
# --------------------------------------------------
# This helper is invoked as ROOT, via sudo, from the deploy gate — which runs as
# the dedicated deploy user. The sudoers entry uses the empty-argument form
# (`... <slug>-migrate-runner ""`, sudoers(5): "the command may only be run with
# no arguments"), so sudo refuses any argv before this script is reached.
#
# The previous version took three arguments (migrations dir, env-file, approvals
# file) and pinned their shapes. Removing the arguments is strictly stronger: a
# caller who can execute this file — including the deploy user the gate itself
# runs as — can no longer pass a path, an image or a tree at all. Every input is
# now a literal substituted at install time, and the checks below are defence in
# depth rather than the boundary.
#
# Order is deliberate: the WHOLE migration set is validated, and the clone's HEAD
# is confirmed to be the commit the operator approved, BEFORE the first byte
# reaches the database. A rejected set therefore cannot have touched anything.
#
# Usage: <slug>-migrate-runner            (no arguments)
#
# State written: rows in public.__drizzle_migrations (same shape as the app's
# migrate.ts, so an existing deployment upgrades in place).
set -euo pipefail

# --- the literal trust path (substituted once at install) -------------------
SLUG="__APP_SLUG__"
TRUST_DIR="__TRUST_DIR__"
STATE_DIR="__STATE_DIR__"
GATE_REPO="__GATE_REPO__"
DOCKER_BIN="__DOCKER_BIN__"

PSQL_IMAGE="__PSQL_IMAGE__"
DOCKER_CONTEXT="devbox"
DB_NETWORK="coolify"
DEFAULT_TRUST_DIR="/usr/local/libexec/studio-deploy"

EXPECTED_ENV_FILE="$STATE_DIR/migrate.env"
EXPECTED_APPROVALS_FILE="$TRUST_DIR/approved-migrations/${SLUG}.txt"
MIG_DIR="$GATE_REPO/apps/$SLUG/drizzle/migrations"

log() { printf '[migrate-runner] %s\n' "$*"; }
die() { printf '[migrate-runner] FAIL: %s\n' "$*" >&2; exit 1; }

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


case "$SLUG$TRUST_DIR$STATE_DIR$GATE_REPO$DOCKER_BIN$PSQL_IMAGE" in
  *__*) die "unsubstituted placeholder in the installed runner — this is the canonical source, not an installed component" ;;
esac

# --- no arguments, ever -----------------------------------------------------
# The installed sudoers form already forbids argv; this is the second lock. A
# caller-chosen env-file, approvals list or tree was the "argument laundering"
# path into a process that holds the owner credential, and it is gone.
[ "$#" -eq 0 ] || die "this helper takes no arguments (got $#) — every input is pinned at install time"

# --- deterministic tool resolution ------------------------------------------
PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

ENV_FILE="$EXPECTED_ENV_FILE"
APPROVALS="$EXPECTED_APPROVALS_FILE"

# The docker binary this helper runs AS ROOT must satisfy the same trusted-path
# rule as the read-only helper. The previous version checked nothing here at
# all, which was the wrong way round: this is the helper that holds the owner
# credential and performs the only Docker WRITE.
[ -x "$DOCKER_BIN" ] || die "no docker binary at $DOCKER_BIN"
[ ! -h "$DOCKER_BIN" ] \
  || die "$DOCKER_BIN is a symlink — pin the root-owned copy under $DEFAULT_TRUST_DIR (see the DOCKER_BIN section of the README)"
perms="$(file_mode "$DOCKER_BIN")"
case "$perms" in
  *[2367]|*[2367]?) die "$DOCKER_BIN has mode $perms — must not be writable by group/other" ;;
esac
if [ "$TRUST_DIR" = "$DEFAULT_TRUST_DIR" ]; then
  assert_trusted_path "$DOCKER_BIN"
fi

# The installed paths must exist before anything else is attempted. These are
# literals, so a missing path is an installation defect, not a caller mistake.
[ -d "$MIG_DIR" ] || die "no migrations directory $MIG_DIR in the installed clone"
[ -r "$ENV_FILE" ] || die "missing owner env-file $ENV_FILE"
[ -r "$APPROVALS" ] || die "missing approvals list $APPROVALS"

JOURNAL="$MIG_DIR/meta/_journal.json"
[ -r "$JOURNAL" ] || die "missing migration journal at $JOURNAL"
command -v jq >/dev/null 2>&1 || die "jq is required to read the migration journal"

digest() { # digest <file>
  shasum -a 256 "$1" | awk '{print $1}'
}

# --- phase 0: the clone's HEAD must be the commit the operator approved ------
# The gate checks out the requested commit and binds it to the allowed ref before
# calling this runner. This is the runner's OWN, independent check that the tree
# it is about to read is the commit the root-owned approvals file names — so a
# swap of the clone between the gate's checkout and this call is caught here, and
# the migration content that runs with owner rights is tied to one reviewed
# commit.
HEAD_SHA="$(git -C "$GATE_REPO" rev-parse --verify --quiet 'HEAD^{commit}' || true)"
[ -n "$HEAD_SHA" ] || die "could not resolve HEAD in $GATE_REPO"
case "$HEAD_SHA" in
  "" | *[!0-9a-f]*) die "HEAD in $GATE_REPO is not a lowercase hex sha" ;;
esac
[ "${#HEAD_SHA}" -eq 40 ] || die "HEAD in $GATE_REPO is not a full 40-character sha"

approvals_commit="$(awk '$1=="#" && $2=="commit" {print $3; exit}' "$APPROVALS" 2>/dev/null || true)"
[ -n "$approvals_commit" ] || die "$APPROVALS carries no '# commit <sha>' line — refusing to run unreviewed SQL with owner rights"
[ "${#approvals_commit}" -eq 40 ] || die "the '# commit' line in $APPROVALS is not a full 40-character sha"
case "$approvals_commit" in
  *[!0-9a-f]*) die "the '# commit' line in $APPROVALS is not a lowercase hex sha" ;;
esac
[ "$HEAD_SHA" = "$approvals_commit" ] \
  || die "the installed clone is at $HEAD_SHA but the approved migration set is bound to $approvals_commit — refusing (see platform/studio-deploy/README.md)"
log "clone HEAD $HEAD_SHA matches the approved commit"

# --- phase 1: validate the whole set before anything touches the database ----

TAGS="$(jq -r '.entries[].tag' "$JOURNAL")"
[ -n "$TAGS" ] || die "journal contains no entries"

validated=""   # "<tag> <hash>" lines, journal order
while IFS= read -r tag; do
  [ -n "$tag" ] || continue
  # The tag reaches a file path and a psql \i inside the container; keep it to a
  # shape that cannot express traversal or option injection.
  case "$tag" in
    *[!0-9a-zA-Z_]*) die "journal tag '$tag' has an unexpected shape — refusing" ;;
  esac
  file="$MIG_DIR/$tag.sql"
  [ -r "$file" ] || die "journal entry $tag has no SQL file at $file"
  hash="$(digest "$file")"

  # Owner boundary: the operator approves exact content, never just a name. An
  # agent that edits a migration, adds a new one, or swaps in an older body
  # changes the hash and is refused before any SQL runs with owner rights.
  want="$(awk -v t="$tag" '!/^[[:space:]]*(#|$)/ && $1==t { print $2; exit }' "$APPROVALS")"
  [ -n "$want" ] || die "migration $tag is not in the approved list ($APPROVALS) — refusing to run it with owner rights"
  case "$want" in
    *[!0-9a-f]*) die "approval entry for $tag is not a sha256" ;;
  esac
  [ "$want" = "$hash" ] || die "migration $tag does not match the approved content — refusing to run unreviewed SQL with owner rights"

  validated="$validated$tag $hash
"
done <<EOF
$TAGS
EOF

log "validated $(printf '%s' "$validated" | grep -c .) migration(s) against the approval list"

# --- phase 2: owner credential, owner-only file on this host ----------------

# A symlinked credential file would pass a mode check while its CONTENT comes
# from wherever the link points — i.e. the owner DB URL this helper is about to
# use could be chosen by whoever could create the link.
[ ! -h "$ENV_FILE" ] || die "$ENV_FILE is a symlink — the owner credential must be a real file"
perms="$(file_mode "$ENV_FILE")"
[ "$perms" = "600" ] || die "$ENV_FILE has mode $perms, expected 600"

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
URL="${MIGRATION_DATABASE_URL:-}"
[ -n "$URL" ] || die "MIGRATION_DATABASE_URL missing in $ENV_FILE"
case "$URL" in
  postgresql://*|postgres://*) : ;;
  *) die "MIGRATION_DATABASE_URL is not a postgres:// URL" ;;
esac

# Split into libpq variables so the URL never reaches a container argv.
rest="${URL#*://}"
creds="${rest%%@*}"
hostpart="${rest#*@}"
export PGUSER="${creds%%:*}"
export PGPASSWORD="${creds#*:}"
hostport="${hostpart%%/*}"
export PGDATABASE="${hostpart#*/}"
export PGHOST="${hostport%%:*}"
export PGPORT="${hostport#*:}"
[ "$PGPORT" = "$PGHOST" ] && export PGPORT=5432
export PGDATABASE="${PGDATABASE%%\?*}"
[ -n "$PGUSER" ] && [ -n "$PGHOST" ] && [ -n "$PGDATABASE" ] \
  || die "could not parse MIGRATION_DATABASE_URL (need user:pass@host:port/db)"

psql_in() { # psql_in [psql args...] — SQL arrives on stdin when needed
  "$DOCKER_BIN" --context "$DOCKER_CONTEXT" run --rm -i --network "$DB_NETWORK" \
    -e PGHOST -e PGPORT -e PGUSER -e PGPASSWORD -e PGDATABASE \
    -v "$MIG_DIR":/migrations:ro \
    --entrypoint psql "$PSQL_IMAGE" -v ON_ERROR_STOP=1 "$@"
}

# --- phase 3: apply ----------------------------------------------------------

log "ensuring the migration journal table (owner role)"
psql_in -q -c '
  CREATE TABLE IF NOT EXISTS public.__drizzle_migrations (
    id serial PRIMARY KEY,
    hash text NOT NULL,
    tag text,
    created_at bigint
  );
  ALTER TABLE public.__drizzle_migrations ADD COLUMN IF NOT EXISTS tag text;
  CREATE UNIQUE INDEX IF NOT EXISTS __drizzle_migrations_tag_uidx
    ON public.__drizzle_migrations (tag) WHERE tag IS NOT NULL;
' >/dev/null || die "could not ensure public.__drizzle_migrations"

done_pairs="$(psql_in -At -F $'\t' -c \
  'SELECT tag, hash FROM public.__drizzle_migrations WHERE tag IS NOT NULL')" \
  || die "could not read applied migrations"

applied=""
skipped=""
while IFS=' ' read -r tag hash; do
  [ -n "$tag" ] || continue
  prev="$(printf '%s\n' "$done_pairs" | awk -F '\t' -v t="$tag" '$1==t{print $2; exit}')"
  if [ -n "$prev" ]; then
    # Mirrors migrate.ts: a re-used tag with different content is a hard failure,
    # never a silent re-apply.
    [ "$prev" = "$hash" ] || die "Migration hash mismatch for $tag. The migration file has been modified."
    skipped="$skipped $tag"
    continue
  fi

  log "applying $tag"
  # The migration text is \i-included inside the transaction; the bookkeeping
  # values are passed as psql variables, so no migration content is ever
  # interpolated into a command line.
  psql_in -q -v tag="$tag" -v hash="$hash" -v created_at="$(date +%s)000" <<SQL \
    || die "migration $tag failed"
BEGIN;
\i /migrations/$tag.sql
INSERT INTO public.__drizzle_migrations (hash, tag, created_at)
  VALUES (:'hash', :'tag', :'created_at'::bigint);
COMMIT;
SQL
  applied="$applied $tag"
done <<EOF
$validated
EOF

log "Migration complete { applied: [$applied], skipped: [$skipped] }"
