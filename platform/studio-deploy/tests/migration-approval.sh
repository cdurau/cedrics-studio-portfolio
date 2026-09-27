#!/usr/bin/env bash
# Owner-migration approval gate tests (second review round, finding 1).
#
# The critical property is that no agent-written SQL reaches owner rights without
# a human having approved that exact content. The runner therefore validates the
# WHOLE migration set — every journal entry present, every content hash listed in
# the approvals file — BEFORE the first byte reaches the database.
#
# Third round (finding A) added two things this suite proves:
#   * the runner's trust path is LITERAL: PSQL_IMAGE / DOCKER_BIN / DOCKER_CONTEXT
#     / TRUST_DIR in the environment cannot redirect what holds the credential;
#   * the ACTIONS are pinned: only the installed env-file and approvals path, and
#     only a migrations dir below the installed clone, are acted on.
#
# S0-13 (dedicated deploy user) went further and removed the arguments entirely:
# this runner is invoked as ROOT through `sudo -n` from a gate that runs as the
# deploy user, so the installed sudoers entry uses the empty-argument form
# (sudoers(5): "the command may only be run with no arguments"). Every input is
# now a literal substituted at install time. The old "call-shape pinning" cases
# (a caller-chosen env-file / approvals list / migrations tree) therefore collapse
# into one stronger fact: NO argument is accepted at all, which the cases below
# assert three ways.
#
# A second, independent binding was added: the runner checks on its own that the
# installed clone's HEAD is the commit the root-owned approval list names, so a
# swap of the tree between the gate's checkout and this call is caught (case O).
#
# The suites run a SUBSTITUTED COPY (tests/lib/harness.sh), never the canonical
# script with its paths exported.
#
#   A) all entries approved, hashes match        -> proceeds (reaches docker run)
#   B) a new entry not in the approvals list     -> REJECT, no database contact
#   C) an approved tag with edited content       -> REJECT, no database contact
#   C2) an appended destructive statement        -> REJECT, no database contact
#   D) an entry whose SQL file is missing        -> REJECT, no database contact
#   E) a tag with a traversal-shaped name        -> REJECT, no database contact
#   F) an approvals entry that is not a sha256   -> REJECT, no database contact
#   G) an empty approvals list                   -> REJECT, no database contact
#   H) a missing approvals file                  -> REJECT
#   I) an owner env-file that is not 600         -> REJECT, no database contact
#   I2) executable/non-data credential content   -> REJECT without evaluation
#   J) an env-file path as an argument            -> REJECT (no arguments accepted)
#   K) an approvals path as an argument           -> REJECT (no arguments accepted)
#   L) a migrations tree as an argument           -> REJECT (no arguments accepted)
#   M) any extra argument                         -> REJECT (no arguments accepted)
#   N) PSQL_IMAGE/DOCKER_CONTEXT/DB_NETWORK in the environment are ignored
#   N2) migration content is streamed, not bind-mounted
#   N3) each pending tag receives its own exact approved SQL body
#   O) the clone HEAD is not the approved commit  -> REJECT, no database contact
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tests/lib/harness.sh
. "$HERE/lib/harness.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

# Docker stand-in: a `run` proves the runner got as far as the database. With
# FAKE_DOCKER_LOG set it records argv and succeeds, so a test can read back the
# image/binary actually used. FAKE_DOCKER_HOST is the socket URI the suites
# substitute for __DOCKER_HOST__ (the -H pin); case N asserts the runner uses it
# over any caller-provided DOCKER_HOST.
FAKE_DOCKER_HOST="unix:///$WORK/fake-docker.sock"
FAKE="$WORK/fake-docker"
cp "$HERE/fake-docker.sh" "$FAKE"
chmod 755 "$FAKE"

RUN_LOG="$WORK/runs.log"
: > "$RUN_LOG"

# The installed layout the runner is substituted with. GATE_REPO must be a real
# git checkout: the runner checks its HEAD against the approved commit (case O).
TRUST="$WORK/trust"
STATE="$WORK/state"
CLONE="$WORK/deploy-clone"
mkdir -p "$TRUST/approved-migrations" "$TRUST/credentials" "$STATE" "$CLONE/apps/ua-smoke/drizzle/migrations/meta"

git -C "$CLONE" init -q
GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
  git -C "$CLONE" commit -q --allow-empty -m base
APPROVED_COMMIT="$(git -C "$CLONE" rev-parse HEAD)"

RUNNER="$WORK/ua-smoke-migrate-runner"
build_runner "$RUNNER" ua-smoke "$TRUST" "$STATE" "$CLONE" "$FAKE"

ENV_FILE="$TRUST/credentials/ua-smoke.env"
cat > "$ENV_FILE" <<'ENV'
MIGRATION_DATABASE_URL=postgresql://owner:***@127.0.0.1:5432/db
ENV
chmod 600 "$ENV_FILE"

APPROVALS="$TRUST/approved-migrations/ua-smoke.txt"
MIG="$CLONE/apps/ua-smoke/drizzle/migrations"

write_case() { # write_case <case-label> <sql-body-for-0001>
  local body="$2"
  rm -rf "$MIG"; mkdir -p "$MIG/meta"
  printf 'CREATE TABLE t0 (id int);\n' > "$MIG/0000_init.sql"
  printf '%s\n' "$body" > "$MIG/0001_next.sql"
  cat > "$MIG/meta/_journal.json" <<JSON
{"version":"7","dialect":"postgresql","entries":[
 {"idx":0,"version":"7","when":1,"tag":"0000_init"},
 {"idx":1,"version":"7","when":2,"tag":"0001_next"}]}
JSON
}

# Every approvals file carries the "# commit" binding line: the gate refuses a
# list without it, and the runner independently requires it too (case O).
approve_all() { # approve_all [commit]
  local c="${1:-$APPROVED_COMMIT}"
  {
    printf '# commit %s\n' "$c"
    printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
    printf '%s %s\n' 0001_next "$(sha256_file "$MIG/0001_next.sql")"
  } > "$APPROVALS"
}

# The runner takes NO arguments; this helper makes that visible at the call sites.
run_runner() { # run_runner [args...] — arguments are deliberately passable to prove refusal
  FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
    /bin/bash "$RUNNER" "$@" 2>&1
}

touched() { [ -s "$RUN_LOG" ]; }

expect() { # expect <REJECT|PROCEED> <label> [required-substring]
  local want="$1" label="$2" sub="${3:-}" out rc
  : > "$RUN_LOG"
  set +e
  out="$(run_runner)"; rc=$?
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
approve_all
expect PROCEED "A) all entries approved and matching"

# B) a NEW migration the operator has not approved.
write_case B "CREATE TABLE t1 (id int);"
{
  printf '# commit %s\n' "$APPROVED_COMMIT"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
} > "$APPROVALS"
expect REJECT "B) an unapproved new migration is refused" "not in the approved list"

# C) the tag is approved but the CONTENT was edited afterwards.
write_case C "CREATE TABLE t1 (id int);"
{
  printf '# commit %s\n' "$APPROVED_COMMIT"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  # Deliberately the hash of different content than what is on disk.
  printf '%s %s\n' 0001_next "0000000000000000000000000000000000000000000000000000000000000000"
} > "$APPROVALS"
expect REJECT "C) edited migration content is refused" "does not match the approved content"

# C2) the same, but the change is an appended DROP — the case that matters most.
write_case C2 "CREATE TABLE t1 (id int);"
printf 'DROP TABLE entries;\n' >> "$MIG/0001_next.sql"
{
  printf '# commit %s\n' "$APPROVED_COMMIT"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "1111111111111111111111111111111111111111111111111111111111111111"
} > "$APPROVALS"
expect REJECT "C2) an appended destructive statement is refused" "does not match the approved content"

# D) the journal references a file that does not exist.
write_case D "CREATE TABLE t1 (id int);"
rm -f "$MIG/0001_next.sql"
{
  printf '# commit %s\n' "$APPROVED_COMMIT"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
} > "$APPROVALS"
expect REJECT "D) a missing SQL file is refused" "has no SQL file"

# E) a traversal-shaped tag must never reach a path or psql.
rm -rf "$MIG"; mkdir -p "$MIG/meta"
printf 'SELECT 1;\n' > "$MIG/0000_init.sql"
cat > "$MIG/meta/_journal.json" <<'JSON'
{"version":"7","dialect":"postgresql","entries":[
 {"idx":0,"version":"7","when":1,"tag":"0000_init"},
 {"idx":1,"version":"7","when":2,"tag":"../../etc/passwd"}]}
JSON
{
  printf '# commit %s\n' "$APPROVED_COMMIT"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
} > "$APPROVALS"
expect REJECT "E) a traversal-shaped tag is refused" "unexpected shape"

# F) an approvals entry that is not a hex sha256.
write_case F "CREATE TABLE t1 (id int);"
{
  printf '# commit %s\n' "$APPROVED_COMMIT"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "yes-please"
} > "$APPROVALS"
expect REJECT "F) a non-sha approval entry is refused" "not a sha256"

# G) an empty approvals file must refuse everything, not allow everything.
write_case G "CREATE TABLE t1 (id int);"
printf '# commit %s\n# nothing approved yet\n' "$APPROVED_COMMIT" > "$APPROVALS"
expect REJECT "G) an empty approval list refuses (fail-closed)" "not in the approved list"

# H) a missing approvals file must refuse, never implicitly allow.
: > "$RUN_LOG"
rm -f "$APPROVALS"
set +e
out="$(run_runner)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched; then
  echo "PASS  H) a missing approval list is refused"
else
  echo "FAIL  H) a missing approval list was not refused" >&2; fail=1
fi

# I) the owner env-file must be mode 600; otherwise refuse before use.
chmod 644 "$ENV_FILE"
write_case I "CREATE TABLE t1 (id int);"
approve_all
expect REJECT "I) a group/world-readable owner env-file is refused" "expected 600"
chmod 600 "$ENV_FILE"

# I2) The root helper parses credential data; it must never source shell owned by
# another principal. An extra command-like record is refused and never executed.
MARKER="$WORK/must-not-exist"
printf 'MIGRATION_DATABASE_URL=postgresql://owner:***@127.0.0.1:5432/db\ntouch %s\n' "$MARKER" > "$ENV_FILE"
expect REJECT "I2) executable content in the credential file is refused" "unexpected executable or non-data content"
if [ -e "$MARKER" ]; then
  echo "FAIL  I2) credential content was evaluated as shell" >&2; fail=1
fi
printf 'MIGRATION_DATABASE_URL=postgresql://owner:***@127.0.0.1:5432/db\n' > "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo
echo "== the runner takes no arguments at all (S0-13) =="
# Under the deploy-user model the runner is reached as root through sudo with the
# empty-argument form. A caller-chosen env-file, approvals list or tree used to be
# an argument-laundering path into a process holding the owner credential; there
# is now no argument to launder. These three cases assert that directly.

# J) an env-file path as the first argument.
write_case J "CREATE TABLE t1 (id int);"
approve_all
: > "$RUN_LOG"
OTHER_ENV="$WORK/other.env"
printf 'MIGRATION_DATABASE_URL=postgresql://attacker:***@127.0.0.1:5432/db\n' > "$OTHER_ENV"
chmod 600 "$OTHER_ENV"
set +e
out="$(run_runner "$OTHER_ENV")"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched && printf '%s' "$out" | grep -q "takes no arguments"; then
  echo "PASS  J) no argument is accepted (a caller-chosen env-file cannot be passed)"
else
  echo "FAIL  J) a caller-chosen env-file was accepted" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

# K) an approvals path as an argument.
OTHER_APPR="$WORK/other-approvals.txt"
cp "$APPROVALS" "$OTHER_APPR"
: > "$RUN_LOG"
set +e
out="$(run_runner "$ENV_FILE" "$OTHER_APPR")"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched && printf '%s' "$out" | grep -q "takes no arguments"; then
  echo "PASS  K) no argument is accepted (a caller-chosen approvals list cannot be passed)"
else
  echo "FAIL  K) a caller-chosen approvals list was accepted" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

# L) a migrations tree as an argument.
OUTSIDE="$WORK/outside-migrations"
mkdir -p "$OUTSIDE/meta"
cp "$MIG"/*.sql "$OUTSIDE/" 2>/dev/null || true
cp "$MIG/meta/_journal.json" "$OUTSIDE/meta/" 2>/dev/null || true
: > "$RUN_LOG"
set +e
out="$(run_runner "$OUTSIDE")"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched && printf '%s' "$out" | grep -q "takes no arguments"; then
  echo "PASS  L) no argument is accepted (a migrations tree outside the clone cannot be passed)"
else
  echo "FAIL  L) an out-of-clone migrations tree was accepted" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

# M) any extra argument.
write_case M "CREATE TABLE t1 (id int);"
approve_all
: > "$RUN_LOG"
set +e
out="$(run_runner "--extra")"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! touched && printf '%s' "$out" | grep -q "takes no arguments"; then
  echo "PASS  M) any extra argument is refused"
else
  echo "FAIL  M) extra arguments were accepted" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

echo
echo "== the pinned image and binary cannot be redirected from the environment =="
# The decisive one for the credential path: if PSQL_IMAGE or DOCKER_BIN could be
# set by the caller, agent-controlled code would run next to PGPASSWORD.
write_case N "CREATE TABLE t1 (id int);"
approve_all
: > "$RUN_LOG"
set +e
out="$(FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$RUN_LOG" \
  PSQL_IMAGE="attacker/evil:latest" DOCKER_HOST="unix:///attacker.sock" DOCKER_CONTEXT="desktop-linux" DB_NETWORK="attacker-net" \
  /bin/bash "$RUNNER" 2>&1)"; rc=$?
set -e
first_run="$(head -1 "$RUN_LOG" 2>/dev/null || true)"
if printf '%s' "$first_run" | grep -q "$PINNED_PSQL_IMAGE" \
   && printf '%s' "$first_run" | grep -q -- "$FAKE_DOCKER_HOST" \
   && ! printf '%s' "$first_run" | grep -q "attacker"; then
  echo "PASS  N) PSQL_IMAGE/DOCKER_HOST/DOCKER_CONTEXT/DB_NETWORK in the environment are ignored"
else
  echo "FAIL  N) an environment override reached the credential path" >&2
  printf 'first run argv: %s\n' "$first_run" >&2; fail=1
fi

# N2) measured 2026-09-27: the guest may not see that host path, so a bind-
# mount under /var would arrive empty and the include step would fail closed;
# mounting the login user's home would expose the separated files to containers.
# The migration content therefore reaches psql through stdin — assert that the
# first credential-path docker run reaches the database step and carries neither
# the migrations tree nor a :/migrations mount point (psql's own -v variable
# flags are not volume mounts and are fine).
write_case N2 "CREATE TABLE t1 (id int);"
approve_all
: > "$RUN_LOG"
set +e
out="$(run_runner)"; rc=$?
set -e
first_run="$(head -1 "$RUN_LOG" 2>/dev/null || true)"
if [ "$rc" -ne 0 ] || [ ! -s "$RUN_LOG" ]; then
  echo "FAIL  N2) the runner did not reach the database step (rc=$rc)" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
elif printf '%s' "$first_run" | grep -qE ':/migrations|migrations:ro' \
     || printf '%s' "$first_run" | grep -qF -- "$MIG"; then
  echo "FAIL  N2) the migration docker run still bind-mounts the migrations tree" >&2
  printf 'first run argv: %s\n' "$first_run" >&2; fail=1
else
  echo "PASS  N2) migration content is streamed (no bind-mount in the credential-path argv)"
fi

# N3) Regression for the stdin conversion: the validation loop previously left
# `file` pointing at the final journal entry, so every pending tag received the
# last migration's SQL while its own tag/hash was journaled. Capture the exact
# stdin for two pending migrations and assert the tag -> body association.
write_case N3 "CREATE TABLE only_in_second (id int);"
approve_all
STDIN_DIR="$WORK/migration-stdin"
rm -rf "$STDIN_DIR"
: > "$RUN_LOG"
set +e
out="$(FAKE_DOCKER_STDIN_DIR="$STDIN_DIR" run_runner)"; rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  echo "FAIL  N3) runner failed while capturing migration stdin (rc=$rc)" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
elif [ ! -f "$STDIN_DIR/0000_init.sql" ] || [ ! -f "$STDIN_DIR/0001_next.sql" ]; then
  echo "FAIL  N3) did not capture one SQL stream per pending tag" >&2; fail=1
elif ! grep -q 'CREATE TABLE t0' "$STDIN_DIR/0000_init.sql" \
     || grep -q 'only_in_second' "$STDIN_DIR/0000_init.sql" \
     || ! grep -q 'CREATE TABLE only_in_second' "$STDIN_DIR/0001_next.sql" \
     || grep -q 'CREATE TABLE t0' "$STDIN_DIR/0001_next.sql"; then
  echo "FAIL  N3) a tag received another migration's SQL body" >&2; fail=1
else
  echo "PASS  N3) every pending tag receives its own exact approved SQL body"
fi

echo
echo "== the clone HEAD must be the commit the operator approved (S0-13) =="
# A second, independent binding: the gate checks out the requested commit, and the
# runner re-checks the tree it is about to read on its own. A swap of the clone
# between checkout and migration is therefore caught here, and the SQL that runs
# with owner rights is tied to one reviewed commit.
write_case O "CREATE TABLE t1 (id int);"
approve_all "$APPROVED_COMMIT"
# Move the clone's HEAD without moving the approval binding, then rebuild the
# approvals for the NEW tree so only the commit binding can be the cause.
GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
  git -C "$CLONE" commit -q --allow-empty -m move
MOVED_COMMIT="$(git -C "$CLONE" rev-parse HEAD)"
approve_all "$MOVED_COMMIT"
# Now bind the approval list back to the OLD commit while keeping valid hashes:
# HEAD no longer matches what the operator approved.
{
  printf '# commit %s\n' "$APPROVED_COMMIT"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG/0001_next.sql")"
} > "$APPROVALS"
expect REJECT "O) a clone whose HEAD is not the approved commit is refused" "is bound to"

echo
[ "$fail" = 0 ] \
  && echo "RESULT: PASS — owner migrations only run approved content" \
  || echo "RESULT: FAIL" >&2
exit "$fail"
