#!/usr/bin/env bash
# Approval-verification and commit-binding tests.
#
# Gate review 2026-09-21, third round — finding C (HOCH/MITTEL).
#
# The approval list is what owner rights obey, and the old procedure was
# `print-approvals.sh | sudo tee`, where print-approvals.sh is agent-writable
# code. The chain now terminates in something a human can check WITHOUT trusting
# agent code:
#
#   * verify-approvals.sh recomputes every hash from disk with the root-owned
#     system tool (/usr/bin/shasum) and compares it against the proposed list;
#   * the list carries a "# commit <sha>" line, and the DEPLOY GATE refuses a list
#     whose commit line is not the commit being deployed.
#
# The second half matters most: a check that only a helper script performs can be
# skipped by skipping the helper. Binding the commit inside the artifact the gate
# consumes means "a human reviewed this SQL, for this commit" is enforced at deploy
# time.
#
#   verify-approvals.sh
#     V1) matching list and checkout            -> PASS
#     V2) content edited after approval          -> FAIL (hash mismatch)
#     V3) list bound to another commit           -> FAIL
#     V4) list with no commit line               -> FAIL
#     V5) entry missing from the list            -> FAIL
#     V6) journal entry with no SQL file         -> FAIL
#     V7) extra/stale entry in the list          -> PASS with a WARN
#   deploy gate
#     G1) approvals bound to another commit      -> REJECT, runner never invoked
#     G2) approvals with no commit line          -> REJECT, runner never invoked
#     G3) approvals bound to the deployed commit -> the runner IS invoked
#
# No database, no Coolify and no live system is contacted.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tests/lib/harness.sh
. "$HERE/lib/harness.sh"
# shellcheck source=tests/lib/fixture.sh
. "$HERE/lib/fixture.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0
SLUG=ua-smoke

fixture_build "$WORK" "$SLUG" yes
MIG_DIR="$fixture_clone/apps/$SLUG/drizzle/migrations"
JOURNAL="$MIG_DIR/meta/_journal.json"

# A second migration so "an entry missing from the list" is expressible.
printf 'CREATE TABLE t2 (id int);\n' > "$MIG_DIR/0001_next.sql"
cat > "$JOURNAL" <<'JSON'
{"version":"7","dialect":"postgresql","entries":[
 {"idx":0,"version":"7","when":1,"tag":"0000_init"},
 {"idx":1,"version":"7","when":2,"tag":"0001_next"}]}
JSON

VERIFY="$STUDIO_DEPLOY_DIR/verify-approvals.sh"
[ -x "$VERIFY" ] || { echo "missing $VERIFY" >&2; exit 2; }

# The review view must show the filename next to the hash: the human compares the
# diff against these lines, and a tag-only listing makes that needlessly indirect.
FIND="$STUDIO_DEPLOY_DIR/find-approvals.sh"
[ -x "$FIND" ] || { echo "missing $FIND" >&2; exit 2; }

make_list() { # make_list <file> [commit]
  local f="$1" commit="${2:-$(git -C "$fixture_clone" rev-parse HEAD)}"
  {
    printf '# approved migrations for %s\n' "$SLUG"
    printf '# commit %s\n' "$commit"
    printf '%s %s\n' 0000_init "$(sha256_file "$MIG_DIR/0000_init.sql")"
    printf '%s %s\n' 0001_next "$(sha256_file "$MIG_DIR/0001_next.sql")"
  } > "$f"
}

expect_verify() { # expect_verify <PASS|FAIL> <label> <list> [required-substring]
  local want="$1" label="$2" list="$3" sub="${4:-}" out rc
  set +e
  out="$(/bin/bash "$VERIFY" "$fixture_clone" "$SLUG" "$list" 2>&1)"; rc=$?
  set -e
  if [ "$want" = "PASS" ]; then
    if [ "$rc" -eq 0 ]; then
      if [ -n "$sub" ] && ! printf '%s' "$out" | grep -q "$sub"; then
        echo "FAIL  $label: passed without the expected output" >&2; fail=1; return
      fi
      echo "PASS  $label"; return
    fi
    echo "FAIL  $label: expected PASS, got exit $rc" >&2
    printf '%s\n' "$out" | grep '^FAIL' | head -3 >&2; fail=1; return
  fi
  if [ "$rc" -eq 0 ]; then
    echo "FAIL  $label: expected FAIL, but verification passed" >&2; fail=1; return
  fi
  if [ -n "$sub" ] && ! printf '%s' "$out" | grep -q "$sub"; then
    echo "FAIL  $label: failed for the wrong reason (want '$sub')" >&2
    printf '%s\n' "$out" | grep '^FAIL' | head -3 >&2; fail=1; return
  fi
  echo "PASS  $label"
}

echo "== independent approval verification (verify-approvals.sh) =="

GOOD="$WORK/good.txt"
make_list "$GOOD"
expect_verify PASS "V1) a matching list verifies and reports the bound commit" "$GOOD" \
  "PASS — 2 migration(s) match the checkout"

# V2) the SQL changed after the hash was recorded — the case that matters.
EDITED="$WORK/edited.txt"
make_list "$EDITED"
printf 'DROP TABLE entries;\n' >> "$MIG_DIR/0001_next.sql"
expect_verify FAIL "V2) content edited after approval is caught" "$EDITED" "hash mismatch"
# restore for the following cases
printf 'CREATE TABLE t2 (id int);\n' > "$MIG_DIR/0001_next.sql"

# V3) a list that belongs to a different commit.
WRONGCOMMIT="$WORK/wrongcommit.txt"
make_list "$WRONGCOMMIT" "07759121ba315a6c6a4f9dc3ec1f95d07b4059a9"
expect_verify FAIL "V3) a list bound to another commit is caught" "$WRONGCOMMIT" "bound to commit"

# V4) no commit binding at all — a list that cannot say what it approved.
NOCOMMIT="$WORK/nocommit.txt"
{
  printf '# approved migrations for %s\n' "$SLUG"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG_DIR/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG_DIR/0001_next.sql")"
} > "$NOCOMMIT"
expect_verify FAIL "V4) a list without a commit line is caught" "$NOCOMMIT" "no '# commit"

# V5) a journal entry the list forgot.
PARTIAL="$WORK/partial.txt"
{
  printf '# commit %s\n' "$(git -C "$fixture_clone" rev-parse HEAD)"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG_DIR/0000_init.sql")"
} > "$PARTIAL"
expect_verify FAIL "V5) a journal entry missing from the list is caught" "$PARTIAL" "missing from the proposed list"

# V6) the journal names a file that is not there.
mv "$MIG_DIR/0001_next.sql" "$MIG_DIR/0001_next.sql.off"
expect_verify FAIL "V6) a journal entry with no SQL file is caught" "$GOOD" "has no SQL file"
mv "$MIG_DIR/0001_next.sql.off" "$MIG_DIR/0001_next.sql"

# V7) an extra line for a tag the journal does not know: harmless for the runner,
# but a strong hint the list was pasted from elsewhere, so it must be reported.
STALE="$WORK/stale.txt"
make_list "$STALE"
printf '%s %s\n' 0009_from_another_commit "1111111111111111111111111111111111111111111111111111111111111111" >> "$STALE"
expect_verify PASS "V7) a stale extra entry verifies but is reported" "$STALE" "WARN"

echo
echo "== the review view and the installable form =="
out="$(/bin/bash "$FIND" "$fixture_clone" "$SLUG" 2>&1)"
if printf '%s' "$out" | grep -q "0001_next.sql" && printf '%s' "$out" | grep -q "commit:"; then
  echo "PASS  the review view shows the filename next to each hash"
else
  echo "FAIL  the review view hid the filename" >&2; printf '%s\n' "$out" | head -5 >&2; fail=1
fi
out="$(/bin/bash "$FIND" "$fixture_clone" "$SLUG" --for-install 2>&1)"
if printf '%s' "$out" | grep -q "^# commit $(git -C "$fixture_clone" rev-parse HEAD)$"; then
  echo "PASS  the installable form carries the '# commit' binding"
else
  echo "FAIL  the installable form has no commit binding" >&2; printf '%s\n' "$out" | head -5 >&2; fail=1
fi

echo
echo "== the deploy gate enforces the commit binding (finding C) =="

NODOCKER="$WORK/no-docker"
printf '#!/usr/bin/env bash\necho "GATE TOUCHED DOCKER" >&2\nexit 9\n' > "$NODOCKER"
chmod 755 "$NODOCKER"

TRUST="$WORK/trust"
STATE="$WORK/state"
mkdir -p "$TRUST/approved-migrations" "$STATE"
APPROVALS="$TRUST/approved-migrations/$SLUG.txt"

# Runner and container-facts stubs: reaching the runner proves the gate got past
# its preflight, including the commit binding. It logs and exits non-zero so the
# run stops there deterministically. The gate's preflight requires BOTH trusted
# helpers to exist (S0-13: the verification step now goes through the root-owned
# verify-container helper), so that one is staged too.
RUNNER_LOG="$WORK/runner.log"
: > "$RUNNER_LOG"
RUNNER="$TRUST/$SLUG-migrate-runner"
cat > "$RUNNER" <<STUB
#!/usr/bin/env bash
printf 'RUNNER-INVOKED args=%s\n' "\$#" >> "$RUNNER_LOG"
exit 1
STUB
chmod 755 "$RUNNER"

VERIFY_CONTAINER="$TRUST/$SLUG-verify-container"
cat > "$VERIFY_CONTAINER" <<'STUB'
#!/usr/bin/env bash
# Never reached in these cases (the runner stub fails first); present so the
# gate's component-existence preflight can pass.
exit 0
STUB
chmod 755 "$VERIFY_CONTAINER"

printf '%s\n' "bo95tvgzq7mkhjx0syelwir5" > "$STATE/coolify_uuid.txt"
: > "$STATE/coolify_token"
chmod 600 "$STATE/coolify_uuid.txt" "$STATE/coolify_token"

GATE="$WORK/$SLUG-deploy-gate"
build_gate "$GATE" "$SLUG" "$TRUST" "$STATE" "$fixture_clone" "$NODOCKER" 1 1

TIP_SHA="$(git -C "$fixture_clone" rev-parse HEAD)"

run_gate() { "$GATE" "$1" 2>&1; }

expect_gate() { # expect_gate <REJECT|PASS> <label> <sha> [substring] [runner-expected yes|no]
  local want="$1" label="$2" sha="$3" sub="${4:-}" runner_want="${5:-no}" out rc
  : > "$RUNNER_LOG"
  set +e
  out="$(run_gate "$sha")"; rc=$?
  set -e
  local ran=no; [ -s "$RUNNER_LOG" ] && ran=yes

  if [ "$want" = "REJECT" ]; then
    if [ "$rc" -eq 0 ]; then
      echo "FAIL  $label: expected REJECT, the gate succeeded" >&2; fail=1; return
    fi
    if [ "$ran" = "yes" ]; then
      echo "FAIL  $label: rejected, but the migration runner had already been invoked" >&2; fail=1; return
    fi
    if [ -n "$sub" ] && ! printf '%s' "$out" | grep -q "$sub"; then
      echo "FAIL  $label: rejected for the wrong reason (want '$sub')" >&2
      printf '%s\n' "$out" | tail -3 >&2; fail=1; return
    fi
    echo "PASS  $label (refused before the migration runner ran)"; return
  fi

  if [ "$ran" = "$runner_want" ]; then
    if [ -n "$sub" ] && ! printf '%s' "$out" | grep -q "$sub"; then
      echo "FAIL  $label: expected message '$sub' was not produced" >&2
      printf '%s\n' "$out" | tail -3 >&2; fail=1; return
    fi
    echo "PASS  $label"; return
  fi
  echo "FAIL  $label: runner invocation was '$ran', expected '$runner_want'" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
}

# G1) the list names another commit.
{
  printf '# commit 07759121ba315a6c6a4f9dc3ec1f95d07b4059a9\n'
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG_DIR/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG_DIR/0001_next.sql")"
} > "$APPROVALS"
expect_gate REJECT "G1) an approval list bound to another commit is refused" "$TIP_SHA" \
  "approvals list is bound to commit"

# G2) no commit binding in the list at all.
{
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG_DIR/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG_DIR/0001_next.sql")"
} > "$APPROVALS"
expect_gate REJECT "G2) an approval list with no commit line is refused" "$TIP_SHA" \
  "carries no '# commit"

# G3) correctly bound: the gate proceeds to the trusted runner (which then fails,
# which is the point — the binding check is behind us).
{
  printf '# commit %s\n' "$TIP_SHA"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG_DIR/0000_init.sql")"
  printf '%s %s\n' 0001_next "$(sha256_file "$MIG_DIR/0001_next.sql")"
} > "$APPROVALS"
expect_gate PASS "G3) a list bound to the deployed commit reaches the runner" "$TIP_SHA" \
  "migration failed or was not approved" yes

echo
[ "$fail" = 0 ] \
  && echo "RESULT: PASS — approval content is verified independently and bound to the deployed commit" \
  || echo "RESULT: FAIL" >&2
exit "$fail"
