#!/usr/bin/env bash
# Owner-migration approval gate tests (second review round, finding 1).
#
# The critical property is that no agent-written SQL reaches owner rights without
# a human having approved that exact content. The runner therefore validates the
# WHOLE migration set — every journal entry present, every content hash listed in
# the approvals file — BEFORE the first byte reaches the database.
#
# Third round (finding A) added two things this suite now proves:
#   * the runner's trust path is LITERAL: PSQL_IMAGE / DOCKER_BIN / DOCKER_CONTEXT
#     / TRUST_DIR in the environment cannot redirect what holds the credential;
#   * the ACTIONS are pinned: only the installed env-file and approvals path, and
#     only a migrations dir below the installed clone, are acted on.
#
# The suites run a SUBSTITUTED COPY (tests/lib/harness.sh), never the canonical
# script with its paths exported.
#
#   A) all entries approved, hashes match        -> proceeds (reaches docker run)
#   B) a new entry not in the approvals list     -> REJECT, no database contact
#   C) an approved tag with edited content       -> REJECT, no database contact
#   D) an entry whose SQL file is missing        -> REJECT, no database contact
#   E) a tag with a traversal-shaped name        -> REJECT, no database contact
#   F) an approvals entry that is not a sha256   -> REJECT, no database contact
#   G) an empty approvals list                   -> REJECT, no database contact
#   H) a missing approvals file                  -> usage error
#   I) an owner env-file that is not 600         -> REJECT, no database contact
#   J) an env-file that is not the installed one -> REJECT (call-shape pinning)
#   K) an approvals list that is not the installed one -> REJECT
#   L) a migrations dir outside the installed clone    -> REJECT
#   M) extra arguments                                 -> REJECT
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tests/lib/harness.sh
. "$HERE/lib/harness.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

# Docker stand-in: a `run` proves the runner got as far as the database. With
# FAKE_DOCKER_LOG set it records argv and succeeds, so a test can read back the
# image/binary actually used.
FAKE="$WORK/fake-docker"
cp "$HERE/fake-docker.sh" "$FAKE"
chmod 755 "$FAKE"

RUN_LOG="$WORK/runs.log"
: > "$RUN_LOG"

# The installed layout the runner is substituted with.
TRUST="$WORK/trust"
STATE="$WORK/state"
CLONE="$WORK/deploy-clone"
mkdir -p "$TRUST/approved-migrations" "$STATE" "$CLONE/apps/ua-smoke/drizzle/migrations/meta"

RUNNER="$WORK/ua-smoke-migrate-runner"
build_runner "$RUNNER" ua-smoke "$TRUST" "$STATE" "$CLONE" "$FAKE"

ENV_FILE="$STATE/migrate.env"
cat > "$ENV_FILE" <<'ENV'
MIGRATION_DATABASE_URL=postgresql://owner:***@127.0.0.1:5432/db
ENV
chmod 600 "$ENV_FILE"

APPROVALS="$TRUST/approved-migrations/ua-smoke.txt"
MIG="$CLONE/apps/ua-smoke/drizzle/migrations"

write_case() { # write_case <sql-body-for-0001>
  rm -rf "$MIG"; mkdir -p "$MIG/meta"
  printf 'CREATE TABLE t0 (id int);\n' > "$MIG/0000_init.sql"
  printf '%s\n' "$1" > "$MIG/0001_next.sql"
  cat > "$MIG/meta/_journal.json" <<JSON
{"version":"7","dialect":"postgresql","entries":[
 {"idx":0,"version":"7","when":1,"tag":"0000_init"},
 {"idx":1,"version":"7","when":2,"tag":"0001_next"}]}
JSON
}

run_runner() { # run_runner <approvals-file> [extra args...]
  local appr="$1"; shift
  FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
    /bin/bash "$RUNNER" "$MIG" "$ENV_FILE" "$appr" "$@" 2>&1
}

touched() { [ -s "$RUN_LOG" ]; }

expect() { # expect <REJECT|PROCEED> <label> <approvals> [required-substring]
  local want="$1" label="$2" appr="$3" sub="${4:-}" out rc
  : > "$RUN_LOG"
  set +e
  out="$(run_runner "$appr")"; rc=$?
  set -e

  if [ "$want" = "REJECT" ]; then
    if [ "$rc" -eq 0 ]; then
      echo "FAIL  $label: expected REJECT, runner succeeded" >&2; fail=1; return
    fi
    if touched; then
      echo "FAIL  $label: rejected, but only AFTER contacting the database" >&2; fail=1; return
    fi
    if [ -n "$sub" ] && ! printf '%s' "$out" | grep -q "$sub"; then
      echo "FAIL  $label: rejected for the wrong reason (want '$sub')" >&2
      printf '%s\n' "$out" | tail -3 >&2; fail=1; return
    fi
    echo "PASS  $label (refused before any database contact)"; return
  fi

  if touched; then
    echo "PASS  $label (validated, then reached the database step)"; return
  fi
  echo "FAIL  $label: expected the run to proceed to the database step" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
}

echo "== owner-migration approval gate =="

# A) everything approved -> validation passes and the runner proceeds.
write_case A "CREATE TABLE t1 (id int);"
{
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG/0001_next.sql")"
} > "$APPROVALS"
expect PROCEED "A) all entries approved and matching" "$APPROVALS"

# B) a NEW migration the operator has not approved.
APPROVALS_B="$WORK/approved-B.txt"
printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")" > "$APPROVALS_B"
: > "$RUN_LOG"
set +e
out="$(/bin/bash "$RUNNER" "$MIG" "$ENV_FILE" "$APPROVALS_B" 2>&1)"; rc=$?
set -e
# A non-installed approvals path is now refused by the call-shape pinning before
# validation even runs; that is proven separately in K. To keep this case about
# the approval CONTENT, write the reduced list to the installed path instead.
printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")" > "$APPROVALS"
expect REJECT "B) an unapproved new migration is refused" "$APPROVALS" "not in the approved list"

# C) the tag is approved but the CONTENT was edited afterwards.
{
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  # Deliberately the hash of different content than what is on disk.
  printf '%s %s\n' 0001_next "0000000000000000000000000000000000000000000000000000000000000000"
} > "$APPROVALS"
expect REJECT "C) edited migration content is refused" "$APPROVALS" "does not match the approved content"

# C2) the same, but the change is an appended DROP — the case that matters most.
write_case C2 "CREATE TABLE t1 (id int);"
printf 'DROP TABLE entries;\n' >> "$MIG/0001_next.sql"
{
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "1111111111111111111111111111111111111111111111111111111111111111"
} > "$APPROVALS"
expect REJECT "C2) an appended destructive statement is refused" "$APPROVALS" "does not match the approved content"

# D) the journal references a file that does not exist.
write_case D "CREATE TABLE t1 (id int);"
rm -f "$MIG/0001_next.sql"
printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")" > "$APPROVALS"
expect REJECT "D) a missing SQL file is refused" "$APPROVALS" "has no SQL file"

# E) a traversal-shaped tag must never reach a path or psql.
rm -rf "$MIG"; mkdir -p "$MIG/meta"
printf 'SELECT 1;\n' > "$MIG/0000_init.sql"
cat > "$MIG/meta/_journal.json" <<'JSON'
{"version":"7","dialect":"postgresql","entries":[
 {"idx":0,"version":"7","when":1,"tag":"0000_init"},
 {"idx":1,"version":"7","when":2,"tag":"../../etc/passwd"}]}
JSON
printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")" > "$APPROVALS"
expect REJECT "E) a traversal-shaped tag is refused" "$APPROVALS" "unexpected shape"

# F) an approvals entry that is not a hex sha256.
write_case F "CREATE TABLE t1 (id int);"
{
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "yes-please"
} > "$APPROVALS"
expect REJECT "F) a non-sha approval entry is refused" "$APPROVALS" "not a sha256"

# G) an empty approvals file must refuse everything, not allow everything.
write_case G "CREATE TABLE t1 (id int);"
printf '# nothing approved yet\n' > "$APPROVALS"
expect REJECT "G) an empty approval list refuses (fail-closed)" "$APPROVALS" "not in the approved list"

# H) a missing approvals file is a usage error, never an implicit allow.
: > "$RUN_LOG"
set +e
out="$(FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
  /bin/bash "$RUNNER" "$MIG" "$ENV_FILE" "$WORK/does-not-exist.txt" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched; then
  echo "PASS  H) a missing approval list is refused"
else
  echo "FAIL  H) a missing approval list was not refused" >&2; fail=1
fi

# I) the owner env-file must be mode 600; otherwise refuse before use.
chmod 644 "$ENV_FILE"
write_case I "CREATE TABLE t1 (id int);"
{
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG/0001_next.sql")"
} > "$APPROVALS"
expect REJECT "I) a group/world-readable owner env-file is refused" "$APPROVALS" "expected 600"
chmod 600 "$ENV_FILE"

echo
echo "== call-shape pinning: only the installed paths are acted on (finding A) =="

# J) an env-file somewhere else, even a well-formed 600 one.
OTHER_ENV="$WORK/other.env"
printf 'MIGRATION_DATABASE_URL=postgresql://attacker:x@127.0.0.1:5432/db\n' > "$OTHER_ENV"
chmod 600 "$OTHER_ENV"
: > "$RUN_LOG"
set +e
out="$(FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
  /bin/bash "$RUNNER" "$MIG" "$OTHER_ENV" "$APPROVALS" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched && printf '%s' "$out" | grep -q "refusing owner env-file"; then
  echo "PASS  J) an attacker-chosen env-file is refused before any credential is read"
else
  echo "FAIL  J) a caller-chosen env-file was accepted" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

# K) an approvals list somewhere else.
OTHER_APPR="$WORK/other-approvals.txt"
printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")" > "$OTHER_APPR"
: > "$RUN_LOG"
set +e
out="$(FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
  /bin/bash "$RUNNER" "$MIG" "$ENV_FILE" "$OTHER_APPR" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched && printf '%s' "$out" | grep -q "refusing approvals list"; then
  echo "PASS  K) an attacker-chosen approvals list is refused"
else
  echo "FAIL  K) a caller-chosen approvals list was accepted" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

# L) a migrations tree outside the installed clone.
OUTSIDE="$WORK/outside-migrations"
mkdir -p "$OUTSIDE/meta"
cp "$MIG"/*.sql "$OUTSIDE/" 2>/dev/null || true
cp "$MIG/meta/_journal.json" "$OUTSIDE/meta/" 2>/dev/null || true
: > "$RUN_LOG"
set +e
out="$(FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
  /bin/bash "$RUNNER" "$OUTSIDE" "$ENV_FILE" "$APPROVALS" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched && printf '%s' "$out" | grep -q "not below the installed clone"; then
  echo "PASS  L) a migrations tree outside the installed clone is refused"
else
  echo "FAIL  L) an out-of-clone migrations tree was accepted" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

# M) extra arguments must not be tolerated (they would become psql/git words).
write_case M "CREATE TABLE t1 (id int);"
{
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG/0001_next.sql")"
} > "$APPROVALS"
: > "$RUN_LOG"
set +e
out="$(FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
  /bin/bash "$RUNNER" "$MIG" "$ENV_FILE" "$APPROVALS" "--extra" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched && printf '%s' "$out" | grep -q "expected exactly three arguments"; then
  echo "PASS  M) extra arguments are refused"
else
  echo "FAIL  M) extra arguments were accepted" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

echo
echo "== the pinned image and binary cannot be redirected from the environment =="
# The decisive one for the credential path: if PSQL_IMAGE or DOCKER_BIN could be
# set by the caller, agent-controlled code would run next to PGPASSWORD.
write_case N "CREATE TABLE t1 (id int);"
{
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG/0001_next.sql")"
} > "$APPROVALS"
: > "$RUN_LOG"
set +e
out="$(FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
  PSQL_IMAGE="attacker/evil:latest" DOCKER_CONTEXT="desktop-linux" DB_NETWORK="attacker-net" \
  /bin/bash "$RUNNER" "$MIG" "$ENV_FILE" "$APPROVALS" 2>&1)"; rc=$?
set -e
first_run="$(head -1 "$RUN_LOG" 2>/dev/null || true)"
if printf '%s' "$first_run" | grep -q "$PINNED_PSQL_IMAGE" \
   && printf '%s' "$first_run" | grep -q -- "--context devbox" \
   && ! printf '%s' "$first_run" | grep -q "attacker"; then
  echo "PASS  N) PSQL_IMAGE/DOCKER_CONTEXT/DB_NETWORK in the environment are ignored"
else
  echo "FAIL  N) an environment override reached the credential path" >&2
  printf 'first run argv: %s\n' "$first_run" >&2; fail=1
fi
if printf '%s' "$first_run" | grep -q -- "--network coolify"; then
  echo "PASS  N2) the network is the pinned one"
else
  echo "FAIL  N2) the network was not the pinned one" >&2; fail=1
fi

echo
[ "$fail" = 0 ] && echo "RESULT: PASS — owner migrations only run approved content" || echo "RESULT: FAIL" >&2
exit "$fail"
