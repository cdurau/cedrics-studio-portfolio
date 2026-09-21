#!/usr/bin/env bash
# Trusted migration runner — root-owned, installed OUTSIDE any repository.
#
# Install target: /usr/local/libexec/studio-deploy/<slug>-migrate-runner
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
#     approved (third argument; see approved-migrations/<slug>.txt);
#   * it needs no agent build at all — the pinned postgres image provides psql,
#     so no agent-authored binary ever holds the owner credential.
#
# Order is deliberate: every journal entry is validated and every hash compared
# against the approval list BEFORE the first byte reaches the database. A
# rejected set therefore cannot have touched anything.
#
# Usage: migrate-runner.sh <migrations-dir> <owner-env-file> <approvals-file>
#   <migrations-dir>  the "drizzle/migrations" tree of the requested commit
#   <owner-env-file>  mode-600 file on this host containing MIGRATION_DATABASE_URL
#   <approvals-file>  root-owned list of approved migrations, "<tag> <sha256>"
#                     per line ('#' comments and blanks ignored)
#
# State written: rows in public.__drizzle_migrations (same shape as the app's
# migrate.ts, so an existing deployment upgrades in place).
set -euo pipefail

PSQL_IMAGE="${PSQL_IMAGE:-postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
DOCKER_CONTEXT="${DOCKER_CONTEXT-devbox}"
DB_NETWORK="${DB_NETWORK:-coolify}"

# Docker goes through a function rather than a "docker --context x" string that
# has to be word-split. A command string breaks wherever IFS has been changed for
# another purpose, which silently turns "docker inspect" into a single unknown
# command name.
d() {
  if [ -n "$DOCKER_CONTEXT" ]; then
    "$DOCKER_BIN" --context "$DOCKER_CONTEXT" "$@"
  else
    "$DOCKER_BIN" "$@"
  fi
}

MIG_DIR="${1:-}"
ENV_FILE="${2:-}"
APPROVALS="${3:-}"

log() { printf '[migrate-runner] %s\n' "$*"; }
die() { printf '[migrate-runner] FAIL: %s\n' "$*" >&2; exit 1; }

USAGE="usage: $0 <migrations-dir> <owner-env-file> <approvals-file>"
[ -n "$MIG_DIR" ] && [ -d "$MIG_DIR" ] || { echo "$USAGE" >&2; exit 2; }
[ -n "$ENV_FILE" ] && [ -r "$ENV_FILE" ] || { echo "$USAGE" >&2; exit 2; }
[ -n "$APPROVALS" ] && [ -r "$APPROVALS" ] || { echo "$USAGE" >&2; exit 2; }

JOURNAL="$MIG_DIR/meta/_journal.json"
[ -r "$JOURNAL" ] || die "missing migration journal at $JOURNAL"
command -v jq >/dev/null 2>&1 || die "jq is required to read the migration journal"

digest() { # digest <file>
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

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

perms="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")"
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
  d run --rm -i --network "$DB_NETWORK" \
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
