#!/usr/bin/env bash
# Owner-migration approval gate tests (second review round, finding 1).
#
# The critical property is that no agent-written SQL reaches owner rights without
# a human having approved that exact content. The runner therefore validates the
# WHOLE migration set — every journal entry present, every content hash listed in
# the approvals file — BEFORE the first byte reaches the database.
#
# These cases use a docker stand-in that exits 97 on any `run`, so "did anything
# try to touch the database?" is directly observable:
#
#   A) all entries approved, hashes match        -> proceeds (reaches docker run)
#   B) a new entry not in the approvals list     -> REJECT, no database contact
#   C) an approved tag with edited content       -> REJECT, no database contact
#   D) an entry whose SQL file is missing        -> REJECT, no database contact
#   E) a tag with a traversal-shaped name        -> REJECT, no database contact
#   F) an approvals entry that is not a sha256   -> REJECT, no database contact
#
# No database is contacted in any case.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="${RUNNER:-$HERE/../migrate-runner.sh}"
[ -r "$RUNNER" ] || { echo "missing runner at $RUNNER" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

# Docker stand-in: any `run` proves the runner got as far as the database.
FAKE="$WORK/fake-docker"
cat > "$FAKE" <<'STUB'
#!/usr/bin/env bash
echo "FAKE-DOCKER: database contact attempted" >&2
exit 97
STUB
chmod 755 "$FAKE"

ENV_FILE="$WORK/migrate.env"
cat > "$ENV_FILE" <<'ENV'
MIGRATION_DATABASE_URL=postgresql://owner:secret@127.0.0.1:5432/db
ENV
chmod 600 "$ENV_FILE"

MIG="$WORK/migrations"
mkdir -p "$MIG/meta"
write_case() { # write_case <name> <sql-body-for-0001>
  rm -rf "$MIG"; mkdir -p "$MIG/meta"
  printf 'CREATE TABLE t0 (id int);\n' > "$MIG/0000_init.sql"
  printf '%s\n' "$2" > "$MIG/0001_next.sql"
  cat > "$MIG/meta/_journal.json" <<JSON
{"version":"7","dialect":"postgresql","entries":[
 {"idx":0,"version":"7","when":1,"tag":"0000_init"},
 {"idx":1,"version":"7","when":2,"tag":"0001_next"}]}
JSON
}

digest() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi
}

run_runner() { # run_runner <approvals-file>
  FAKE_DOCKER_SCENARIO=/dev/null DOCKER_BIN="$FAKE" DOCKER_CONTEXT="" \
    bash "$RUNNER" "$MIG" "$ENV_FILE" "$1" 2>&1
}

expect() { # expect <REJECT|PROCEED> <label> <approvals> [required-substring]
  local want="$1" label="$2" appr="$3" sub="${4:-}" out rc
  set +e
  out="$(run_runner "$appr")"; rc=$?
  set -e
  local touched=0
  printf '%s' "$out" | grep -q "database contact attempted" && touched=1

  if [ "$want" = "REJECT" ]; then
    if [ "$rc" -eq 0 ]; then
      echo "FAIL  $label: expected REJECT, runner succeeded" >&2; fail=1; return
    fi
    if [ "$touched" -eq 1 ]; then
      echo "FAIL  $label: rejected, but only AFTER contacting the database" >&2; fail=1; return
    fi
    if [ -n "$sub" ] && ! printf '%s' "$out" | grep -q "$sub"; then
      echo "FAIL  $label: rejected for the wrong reason (want '$sub')" >&2
      printf '%s\n' "$out" | tail -3 >&2; fail=1; return
    fi
    echo "PASS  $label (refused before any database contact)"; return
  fi

  # PROCEED: the validation passed and the runner moved on to the database step.
  if [ "$touched" -eq 1 ]; then
    echo "PASS  $label (validated, then reached the database step)"; return
  fi
  echo "FAIL  $label: expected the run to proceed to the database step" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
}

echo "== owner-migration approval gate =="

# A) everything approved -> validation passes and the runner proceeds.
write_case A "CREATE TABLE t1 (id int);"
APPR="$WORK/approved-A.txt"
{
  printf '%s %s\n' 0000_init "$(digest "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(digest "$MIG/0001_next.sql")"
} > "$APPR"
expect PROCEED "A) all entries approved and matching" "$APPR"

# B) a NEW migration the operator has not approved.
APPR="$WORK/approved-B.txt"
printf '%s %s\n' 0000_init "$(digest "$MIG/0000_init.sql")" > "$APPR"
expect REJECT "B) an unapproved new migration is refused" "$APPR" "not in the approved list"

# C) the tag is approved but the CONTENT was edited afterwards.
APPR="$WORK/approved-C.txt"
{
  printf '%s %s\n' 0000_init "$(digest "$MIG/0000_init.sql")"
  # Deliberately the hash of different content than what is on disk.
  printf '%s %s\n' 0001_next "0000000000000000000000000000000000000000000000000000000000000000"
} > "$APPR"
expect REJECT "C) edited migration content is refused" "$APPR" "does not match the approved content"

# C2) the same, but the change is an appended DROP — the case that matters most.
write_case C2 "CREATE TABLE t1 (id int);"
printf 'DROP TABLE entries;\n' >> "$MIG/0001_next.sql"
APPR="$WORK/approved-C2.txt"
{
  printf '%s %s\n' 0000_init "$(digest "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "1111111111111111111111111111111111111111111111111111111111111111"
} > "$APPR"
expect REJECT "C2) an appended destructive statement is refused" "$APPR" "does not match the approved content"

# D) the journal references a file that does not exist.
write_case D "CREATE TABLE t1 (id int);"
rm -f "$MIG/0001_next.sql"
APPR="$WORK/approved-D.txt"
printf '%s %s\n' 0000_init "$(digest "$MIG/0000_init.sql")" > "$APPR"
expect REJECT "D) a missing SQL file is refused" "$APPR" "has no SQL file"

# E) a traversal-shaped tag must never reach a path or psql.
rm -rf "$MIG"; mkdir -p "$MIG/meta"
printf 'SELECT 1;\n' > "$MIG/0000_init.sql"
cat > "$MIG/meta/_journal.json" <<'JSON'
{"version":"7","dialect":"postgresql","entries":[
 {"idx":0,"version":"7","when":1,"tag":"0000_init"},
 {"idx":1,"version":"7","when":2,"tag":"../../etc/passwd"}]}
JSON
APPR="$WORK/approved-E.txt"
printf '%s %s\n' 0000_init "$(digest "$MIG/0000_init.sql")" > "$APPR"
expect REJECT "E) a traversal-shaped tag is refused" "$APPR" "unexpected shape"

# F) an approvals entry that is not a hex sha256.
write_case F "CREATE TABLE t1 (id int);"
APPR="$WORK/approved-F.txt"
{
  printf '%s %s\n' 0000_init "$(digest "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "yes-please"
} > "$APPR"
expect REJECT "F) a non-sha approval entry is refused" "$APPR" "not a sha256"

# G) an empty approvals file must refuse everything, not allow everything.
write_case G "CREATE TABLE t1 (id int);"
APPR="$WORK/approved-G.txt"
printf '# nothing approved yet\n' > "$APPR"
expect REJECT "G) an empty approval list refuses (fail-closed)" "$APPR" "not in the approved list"

# H) a missing approvals file is a usage error, never an implicit allow.
set +e
out="$(run_runner "$WORK/does-not-exist.txt")"; rc=$?
set -e
if [ "$rc" -ne 0 ] && ! printf '%s' "$out" | grep -q "database contact attempted"; then
  echo "PASS  H) a missing approval list is refused"
else
  echo "FAIL  H) a missing approval list was not refused" >&2; fail=1
fi

# I) the owner env-file must be mode 600; otherwise refuse before use.
chmod 644 "$ENV_FILE"
APPR="$WORK/approved-A.txt"
write_case I "CREATE TABLE t1 (id int);"
{
  printf '%s %s\n' 0000_init "$(digest "$MIG/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(digest "$MIG/0001_next.sql")"
} > "$APPR"
set +e
out="$(run_runner "$APPR")"; rc=$?
set -e
chmod 600 "$ENV_FILE"
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "expected 600"; then
  echo "PASS  I) a group/world-readable owner env-file is refused"
else
  echo "FAIL  I) an insecurely-permissioned owner env-file was not refused" >&2; fail=1
fi

echo
[ "$fail" = 0 ] && echo "RESULT: PASS — owner migrations only run approved content" || echo "RESULT: FAIL" >&2
exit "$fail"
