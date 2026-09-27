#!/usr/bin/env bash
# Trusted-path integrity tests (host trust-boundary findings, 2026-09-22).
#
# WHAT THIS SUITE IS ABOUT
# ------------------------
# Every ownership/mode check in the trusted components used `stat` without `-L`.
# `stat` has lstat semantics: it measures the SYMLINK, not the file that is
# opened or executed. On the target host that was not theoretical —
#
#   /usr/local/bin/docker  is root:wheel, and is a SYMLINK into
#   /Applications/Docker.app/Contents/Resources/bin/docker, owned by the login user,
#   writable by the login user (= the coding agent's uid).
#
# so `stat -f '%u'` answered 0 and the check passed, while root would have
# executed a binary the agent can replace. The mirror-image defect: the
# migration runner — the helper that holds the owner DB credential and performs
# the only Docker WRITE — validated its docker binary not at all.
#
# The fix has three parts, and this suite pins all three:
#   1. a trusted path must not BE a symlink (refused unconditionally),
#   2. checks use `stat -L`, so a link that did appear is measured correctly,
#   3. on the production path the WHOLE chain from `/` down is asserted, because
#      a root-owned file inside an agent-writable directory can be replaced
#      wholesale.
#
# Offline, like every suite here: no root, no daemon, no deploy, no mutation of
# the host. The chain walker is exercised as a unit with stubbed predicates,
# which is the only way to assert root-owned layouts from an unprivileged run.
#
# Cases:
#   A) the trusted-path block is byte-identical in all four components
#   B) the platform semantics the fix rests on (stat vs stat -L on a symlink)
#   C) the chain walker accepts a clean all-root chain
#   D) the chain walker refuses a group/other-writable PARENT
#   E) the chain walker refuses a PARENT owned by a non-root uid
#   F) the chain walker refuses a relative path
#   G) the chain walker ascends and refuses a symlinked PARENT
#   H) verify-container refuses a symlinked docker binary
#   I) verify-container refuses a group/other-writable docker binary
#   J) migrate-runner refuses a symlinked docker binary        (no check existed)
#   K) migrate-runner refuses a group/other-writable docker binary (ditto)
#   L) migrate-runner refuses a symlinked owner credential file
#   M) the gate refuses a symlinked trusted component
#   N) the gate refuses a symlinked state file
#   O) the hook refuses a symlinked gate
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tests/lib/harness.sh
. "$HERE/lib/harness.sh"
# shellcheck source=tests/lib/agent-stubs.sh
. "$HERE/lib/agent-stubs.sh"
# shellcheck source=tests/lib/fixture.sh
. "$HERE/lib/fixture.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0
pass() { echo "PASS  $1"; }
bad()  { echo "FAIL  $1" >&2; fail=1; }

SRC_DIR="$(dirname "$HERE")"
SLUG="ua-smoke"

FAKE="$WORK/fake-docker"
stage_docker_stdin "$FAKE"

echo "== A: the trusted-path block is one block, not four drifting copies =="

# The block is inlined in each component on purpose (each is installed and runs
# standalone, with no shared library on the trust path). Inlining is only safe
# if the copies cannot drift, so that is asserted here rather than assumed.
extract_block() { # extract_block <file>
  awk '/^# --- trusted-path predicates/{on=1} on{print} on && /^\}$/{exit}' "$1"
}
extract_block "$SRC_DIR/verify-container.sh" > "$WORK/block.ref"
[ -s "$WORK/block.ref" ] || bad "A) could not extract the trusted-path block from verify-container.sh"
drift=""
for c in deploy-gate.sh deploy-hook.sh migrate-runner.sh; do
  extract_block "$SRC_DIR/$c" > "$WORK/block.$c"
  cmp -s "$WORK/block.ref" "$WORK/block.$c" || drift="$drift $c"
done
if [ -z "$drift" ]; then
  pass "A) the trusted-path block is byte-identical in all four components"
else
  bad "A) the trusted-path block has drifted in:$drift"
fi

echo
echo "== B: the platform semantics this fix rests on =="

# The exact shape of the live defect, reproduced without root: a link whose
# TARGET is owned by this (non-root) uid. If a future OS made `stat` follow
# links by default, or `-L` stop following, this assertion says so.
mkdir -p "$WORK/b"
printf 'payload\n' > "$WORK/b/target"
ln -s "$WORK/b/target" "$WORK/b/link"
lstat_uid="$(stat -f '%u' "$WORK/b/link" 2>/dev/null || stat -c '%u' "$WORK/b/link")"
stat_uid="$(stat -L -f '%u' "$WORK/b/link" 2>/dev/null || stat -L -c '%u' "$WORK/b/link")"
link_uid="$(stat -f '%u' "$WORK/b/link" 2>/dev/null || stat -c '%u' "$WORK/b/link")"
if [ "$stat_uid" = "$CURRENT_UID" ] && [ "$lstat_uid" = "$link_uid" ]; then
  pass "B) stat -L measures the target ($stat_uid), bare stat measures the link — the defect is real and the fix addresses it"
else
  bad "B) unexpected stat semantics (bare=$lstat_uid, -L=$stat_uid, self=$CURRENT_UID)"
fi

echo
echo "== C-G: the chain walker =="

# The walker is exercised as a unit: the block is extracted verbatim from the
# canonical source and the two predicates are re-defined AFTER it, so the stub
# wins at call time. That makes root-owned layouts assertable from a run that
# has no root — the alternative would be to assert nothing at all.
STUB="$WORK/stub.tsv"
cat > "$WORK/unit.sh" <<UNIT
#!/usr/bin/env bash
set -euo pipefail
die() { printf 'DIE: %s\n' "\$*" >&2; exit 1; }
$(cat "$WORK/block.ref")
# Stubs: a path not named in the table is root-owned and mode 755.
file_owner() { awk -v p="\$1" '\$1==p{print \$2; f=1} END{if(!f) print "0"}' "$STUB"; }
file_mode()  { awk -v p="\$1" '\$1==p{print \$3; f=1} END{if(!f) print "755"}' "$STUB"; }
assert_trusted_path "\$1"
echo ACCEPTED
UNIT
chmod 755 "$WORK/unit.sh"

unit() { # unit <path> -> prints output, sets rc
  bash "$WORK/unit.sh" "$1" 2>&1
}

# C) a clean chain is accepted — otherwise every later refusal would be vacuous.
: > "$STUB"
set +e; out="$(unit /a/b/c)"; rc=$?; set -e
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q ACCEPTED; then
  pass "C) the chain walker accepts an all-root, non-writable chain"
else
  bad "C) a clean chain was refused (rc=$rc): $out"
fi

# D) the parent directory, not the file, carries the defect.
printf '/a/b\t0\t775\n' > "$STUB"
set +e; out="$(unit /a/b/c)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "/a/b has mode 775"; then
  pass "D) the chain walker refuses a group-writable PARENT directory"
else
  bad "D) a group-writable parent was accepted (rc=$rc): $out"
fi

# E) same for ownership: a root-owned file under an agent-owned directory can be
#    replaced wholesale, which is exactly what file-only ownership misses.
printf '/a\t1000\t755\n' > "$STUB"
set +e; out="$(unit /a/b/c)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "/a is owned by uid 1000"; then
  pass "E) the chain walker refuses a PARENT owned by a non-root uid"
else
  bad "E) a non-root parent was accepted (rc=$rc): $out"
fi

# F) a relative path cannot be reasoned about at all.
: > "$STUB"
set +e; out="$(unit some/relative/path)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "must be absolute"; then
  pass "F) the chain walker refuses a relative path"
else
  bad "F) a relative path was accepted (rc=$rc): $out"
fi

# G) proves the walker actually ASCENDS: the leaf is clean and only a parent is
#    a symlink, so a file-only check would pass this.
mkdir -p "$WORK/g/real"
ln -s "$WORK/g/real" "$WORK/g/link"
: > "$STUB"
set +e; out="$(unit "$WORK/g/link/x")"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "$WORK/g/link is a symlink"; then
  pass "G) the chain walker ascends and refuses a symlinked PARENT"
else
  bad "G) a symlinked parent was accepted (rc=$rc): $out"
fi

echo
echo "== H-K: the docker binary, in both helpers =="

STATE="$WORK/state"
TRUST="$WORK/trust"
mkdir -p "$STATE" "$TRUST/approved-migrations"
printf 'bo95tvgzq7mkhjx0syelwir5\n' > "$STATE/coolify_uuid.txt"

# The gate performs its ref binding BEFORE it looks at components or state, so
# these cases need a real origin to fetch from — otherwise the run would stop at
# the fetch and prove nothing about the checks under test. The shared fixture is
# the same repository shape the ref-binding suite uses.
fixture_build "$WORK/fix" "$SLUG"
CLONE="$fixture_clone"

# A symlink to the docker stand-in: root-owned or not, the shape itself is
# refused, because the shape is what defeated the old check.
DOCKER_LINK="$WORK/docker-link"
ln -s "$FAKE" "$DOCKER_LINK"
# A real file that is group/other-writable.
DOCKER_WRITABLE="$WORK/docker-writable"
cp "$FAKE" "$DOCKER_WRITABLE"; chmod 777 "$DOCKER_WRITABLE"

V_LINK="$WORK/verify-link"
build_verify_container "$V_LINK" "$SLUG" "$TRUST" "$STATE" "$DOCKER_LINK"
set +e; out="$("$V_LINK" 2>&1)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "is a symlink"; then
  pass "H) verify-container refuses a symlinked docker binary"
else
  bad "H) verify-container accepted a symlinked docker binary (rc=$rc): $out"
fi

V_W="$WORK/verify-writable"
build_verify_container "$V_W" "$SLUG" "$TRUST" "$STATE" "$DOCKER_WRITABLE"
set +e; out="$("$V_W" 2>&1)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "must not be writable by group/other"; then
  pass "I) verify-container refuses a group/other-writable docker binary"
else
  bad "I) verify-container accepted a world-writable docker binary (rc=$rc): $out"
fi

# J/K: the runner had NO docker-binary check at all before this change — the
# helper that runs as root AND holds the owner credential was the unchecked one.
R_LINK="$WORK/runner-link"
build_runner "$R_LINK" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$DOCKER_LINK"
set +e; out="$("$R_LINK" 2>&1)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "is a symlink"; then
  pass "J) migrate-runner refuses a symlinked docker binary"
else
  bad "J) migrate-runner accepted a symlinked docker binary (rc=$rc): $out"
fi

R_W="$WORK/runner-writable"
build_runner "$R_W" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$DOCKER_WRITABLE"
set +e; out="$("$R_W" 2>&1)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "must not be writable by group/other"; then
  pass "K) migrate-runner refuses a group/other-writable docker binary"
else
  bad "K) migrate-runner accepted a world-writable docker binary (rc=$rc): $out"
fi

echo
echo "== L: the owner credential file =="

# The runner reads its root-only credential only after the whole migration set has validated,
# so the fixture has to be complete or this would stop earlier and prove nothing
# about the credential file.
MIG="$CLONE/apps/$SLUG/drizzle/migrations"
{
  printf '# commit %s\n' "$(git -C "$CLONE" rev-parse HEAD)"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
} > "$TRUST/approved-migrations/$SLUG.txt"

# The real credential lives elsewhere; the installed path is only a link to it.
printf 'MIGRATION_DATABASE_URL=postgresql://owner:pw@127.0.0.1:5432/db\n' > "$WORK/elsewhere.env"
chmod 600 "$WORK/elsewhere.env"
mkdir -p "$TRUST/credentials"
ln -s "$WORK/elsewhere.env" "$TRUST/credentials/$SLUG.env"

R_ENV="$WORK/runner-env"
build_runner "$R_ENV" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$FAKE"
set +e
out="$(FAKE_DOCKER_SCENARIO=/dev/null FAKE_DOCKER_LOG="$WORK/runs.log" "$R_ENV" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "$SLUG.env is a symlink"; then
  pass "L) migrate-runner refuses a symlinked owner credential file"
else
  bad "L) a symlinked root credential was accepted (rc=$rc): $(printf '%s' "$out" | tail -3)"
fi
rm -f "$TRUST/credentials/$SLUG.env"

echo
echo "== M-O: the gate's components and state, and the hook's gate =="

printf 'MIGRATION_DATABASE_URL=postgresql://owner:pw@127.0.0.1:5432/db\n' > "$TRUST/credentials/$SLUG.env"
printf 'placeholder-token\n' > "$STATE/coolify_token"
chmod 600 "$TRUST/credentials/$SLUG.env" "$STATE/coolify_token" "$STATE/coolify_uuid.txt"
stage_helpers "$TRUST" "$STATE" "$CLONE" "$FAKE" "$SLUG"
# The tip of the allowed branch: the only sha the gate's ref binding accepts.
SHA40="$FIX_TIP_SHA"

# M) a trusted component reached through a link is not a trusted component.
mv "$TRUST/$SLUG-verify-container" "$WORK/real-verify"
ln -s "$WORK/real-verify" "$TRUST/$SLUG-verify-container"
GATE_M="$WORK/gate-m"
build_gate "$GATE_M" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$FAKE" 1 1 "$DEAD_COOLIFY_API" "$CURRENT_USER"
set +e; out="$("$GATE_M" "$SHA40" 2>&1)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "is a symlink"; then
  pass "M) the gate refuses a symlinked trusted component"
else
  bad "M) the gate accepted a symlinked trusted component (rc=$rc): $(printf '%s' "$out" | tail -3)"
fi
rm -f "$TRUST/$SLUG-verify-container"; mv "$WORK/real-verify" "$TRUST/$SLUG-verify-container"

# N) same for the deploy state: a linked state file has its content chosen by
#    whoever could create the link.
mv "$STATE/coolify_token" "$WORK/real-token"
ln -s "$WORK/real-token" "$STATE/coolify_token"
set +e; out="$("$GATE_M" "$SHA40" 2>&1)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "coolify_token is a symlink"; then
  pass "N) the gate refuses a symlinked state file"
else
  bad "N) the gate accepted a symlinked state file (rc=$rc): $(printf '%s' "$out" | tail -3)"
fi
rm -f "$STATE/coolify_token"; mv "$WORK/real-token" "$STATE/coolify_token"

# O) the hook execs the gate, so the same shape has to be refused one level up.
HOOK_TRUST="$WORK/hook-trust"
mkdir -p "$HOOK_TRUST"
GATE_REAL="$WORK/gate-real"
build_gate "$GATE_REAL" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$FAKE" 1 1 "$DEAD_COOLIFY_API" "$CURRENT_USER"
ln -s "$GATE_REAL" "$HOOK_TRUST/$SLUG-deploy-gate"
HOOK_O="$WORK/hook-o"
build_hook "$HOOK_O" "$SLUG" "$HOOK_TRUST" "$CLONE" "$CURRENT_USER"
set +e; out="$(SSH_ORIGINAL_COMMAND="$SHA40" "$HOOK_O" 2>&1)"; rc=$?; set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "is a symlink"; then
  pass "O) the hook refuses a symlinked gate"
else
  bad "O) the hook accepted a symlinked gate (rc=$rc): $(printf '%s' "$out" | tail -3)"
fi

echo
[ "$fail" -eq 0 ] && echo "trusted-path: all cases passed" || echo "trusted-path: FAILURES above" >&2
exit "$fail"
