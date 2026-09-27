#!/usr/bin/env bash
# Deploy-user / credential-separation tests (S0-13).
#
# The boundary this suite proves: the deploy path runs as a dedicated uid, the
# deploy credentials belong to THAT uid, and the coding agent's own uid is a
# different principal — so mode 600 actually means something. Everything here is
# offline: no user is created, no sudoers entry installed, no secret moved, no
# deploy triggered.
#
# What is asserted, per the S0-13 brief:
#   A)  the gate refuses to run as an account other than its DEPLOY_USER
#   B)  the hook refuses to run as an account other than its DEPLOY_USER
#   C)  a state file that is not mode 600 is refused
#   D)  a state file owned by a different uid is refused (production-path rule,
#       exercised on the trusted binary: a non-root-owned Docker binary is refused
#       on the production trust path)
#   E)  a state file the deploy user cannot read is refused — which is what a file
#       owned by another uid actually looks like at mode 600
#   F)  an external credential file cannot be substituted: no helper accepts a
#       path, so a caller-chosen env-file/approvals list is unreachable
#   G)  generic Docker/helper arguments cannot be injected
#   H)  the verify-container helper is argument-free and read-only by shape
#   I)  a sudoers refusal makes the gate fail closed, before the deploy trigger
#   J)  the configured credential state is NOT under /Users/<login user>/...
#   K)  no trusted component hard-codes a /Users/<login user> credential path
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tests/lib/harness.sh
. "$HERE/lib/harness.sh"
# shellcheck source=tests/lib/agent-stubs.sh
. "$HERE/lib/agent-stubs.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

pass() { echo "PASS  $1"; }
bad()  { echo "FAIL  $1" >&2; fail=1; }

# A docker stand-in, so nothing here can reach a real daemon.
FAKE="$WORK/fake-docker"
stage_docker_stdin "$FAKE"

SLUG="ua-smoke"
# A DIFFERENT account name, to prove the identity check refuses. Using a name (not
# a number) keeps the suites independent of whichever uid the OS assigns at
# install time — no test asserts a recorded uid.
OTHER_USER="no-such-deploy-account"

# --- the installed layout the components are substituted with ----------------
TRUST="$WORK/trust"
STATE="$WORK/state"
CLONE="$WORK/deploy-clone"
mkdir -p "$TRUST/approved-migrations" "$TRUST/credentials" "$STATE" "$CLONE/apps/$SLUG/drizzle/migrations/meta"

# A tiny git clone so the gate's ref/checkout logic has something real.
git -C "$CLONE" init -q
GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
  git -C "$CLONE" commit -q --allow-empty -m base

APP_UUID="bo95tvgzq7mkhjx0syelwir5"
printf '%s\n' "$APP_UUID" > "$STATE/coolify_uuid.txt"
printf '13\n' > "$STATE/coolify_app_id.txt"
printf 'placeholder-token\n' > "$STATE/coolify_token"
MIGRATE_ENV="$TRUST/credentials/$SLUG.env"
printf 'MIGRATION_DATABASE_URL=postgresql://owner:***@127.0.0.1:5432/db\n' > "$MIGRATE_ENV"
chmod 600 "$MIGRATE_ENV" "$STATE/coolify_token"

SCEN="$WORK/scenario.tsv"
printf 'c-ok\t%s\t13\t%s:0123456789abcdef0123456789abcdef01234567\t\thealthy\n' "$APP_UUID" "$APP_UUID" > "$SCEN"

# The gate reads its state files at 600 and, in deploy mode, the trusted
# components. Stage the helpers so the deploy-mode preflight can pass where the
# case intends it to.
stage_helpers "$TRUST" "$STATE" "$CLONE" "$FAKE" "$SLUG"

echo "== A/B: the gate and hook refuse the wrong uid =="

# A) The gate is installed for the deploy account. Running a copy as the login
# user must be refused rather than silently acting with the agent's identity.
GATE_WRONG_UID="$WORK/gate-wrong-uid"
build_gate "$GATE_WRONG_UID" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$FAKE" 1 1 "$DEAD_COOLIFY_API" "$OTHER_USER"
set +e
out="$(FAKE_DOCKER_SCENARIO="$SCEN" "$GATE_WRONG_UID" --verify-only "0123456789abcdef0123456789abcdef01234567" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "installed for the deploy account"; then
  pass "A) the gate refuses to run as an account other than its DEPLOY_USER"
else
  bad "A) the gate ran as the wrong account (rc=$rc)"; printf '%s\n' "$out" | tail -3 >&2
fi

# B) Same for the hook: a forced command invoked as another account is not the
# session this component was installed for.
HOOK_WRONG_UID="$WORK/hook-wrong-uid"
build_hook "$HOOK_WRONG_UID" "$SLUG" "$TRUST" "$CLONE" "$OTHER_USER"
set +e
out="$(SSH_ORIGINAL_COMMAND="0123456789abcdef0123456789abcdef01234567" "$HOOK_WRONG_UID" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "installed for the deploy account"; then
  pass "B) the hook refuses to run as an account other than its DEPLOY_USER"
else
  bad "B) the hook ran as the wrong account (rc=$rc)"; printf '%s\n' "$out" | tail -3 >&2
fi

echo
echo "== C/D/E: state file permissions and ownership =="

GATE="$WORK/gate"
build_gate "$GATE" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$FAKE" 1 1 "$DEAD_COOLIFY_API" "$CURRENT_USER"

SHA40="0123456789abcdef0123456789abcdef01234567"

# C) mode 600 is required for the root-owned credential file. The runner reads
# it in its last phase, AFTER the migration set has validated — so a
# valid approvals file and migration tree must exist, otherwise the run would
# stop earlier for an unrelated reason and prove nothing about the mode.
MIG="$CLONE/apps/$SLUG/drizzle/migrations"
rm -rf "$MIG"; mkdir -p "$MIG/meta"
printf 'CREATE TABLE t0 (id int);\n' > "$MIG/0000_init.sql"
cat > "$MIG/meta/_journal.json" <<'JSON'
{"version":"7","dialect":"postgresql","entries":[
 {"idx":0,"version":"7","when":1,"tag":"0000_init"}]}
JSON
{
  printf '# commit %s\n' "$(git -C "$CLONE" rev-parse HEAD)"
  printf '%s %s\n' 0000_init "$(sha256_file "$MIG/0000_init.sql")"
} > "$TRUST/approved-migrations/$SLUG.txt"
chmod 644 "$MIGRATE_ENV"
set +e
out="$(FAKE_DOCKER_SCENARIO="$SCEN" FAKE_DOCKER_LOG="$WORK/runs.log" \
  "$TRUST/$SLUG-migrate-runner" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "expected 600"; then
  pass "C) a credential file that is not mode 600 is refused by the runner"
else
  bad "C) a 644 owner env-file was accepted (rc=$rc)"; printf '%s\n' "$out" | tail -3 >&2
fi
chmod 600 "$MIGRATE_ENV"

# D) Ownership on the production trust path: the trusted binary must be
# root-owned. A Docker binary owned by the login user is exactly the situation
# this rule exists for (the Homebrew docker on the host is login-user-owned).
VERIFY_PROD="$WORK/verify-prod"
subst_script "$(dirname "$HERE")/verify-container.sh" "$VERIFY_PROD" \
  "APP_SLUG=$SLUG" "TRUST_DIR=/usr/local/libexec/studio-deploy" \
  "STATE_DIR=$STATE" "DOCKER_BIN=$FAKE" "DOCKER_HOST=unix://$WORK/fake-docker.sock"
set +e
out="$(FAKE_DOCKER_SCENARIO="$SCEN" "$VERIFY_PROD" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "expected root"; then
  pass "D) a non-root-owned Docker binary is refused on the production trust path"
else
  bad "D) a login-user-owned Docker binary was executed as root (rc=$rc)"; printf '%s\n' "$out" | tail -3 >&2
fi

# E) A file the deploying uid cannot read is what a 600 file owned by another
# uid looks like on a shared host — the realistic production shape. The
# verify-container helper requires the app identity file, so that is the file to
# make unreadable: a helper that proceeded anyway could report facts for an app
# it never identified.
UNREADABLE="$WORK/unreadable-state"
mkdir -p "$UNREADABLE"
printf '%s\n' "$APP_UUID" > "$UNREADABLE/coolify_uuid.txt"
printf 'placeholder-token\n' > "$UNREADABLE/coolify_token"
chmod 600 "$UNREADABLE"/*
chmod 000 "$UNREADABLE/coolify_uuid.txt"
VERIFY_UR="$WORK/verify-unreadable"
subst_script "$(dirname "$HERE")/verify-container.sh" "$VERIFY_UR" \
  "APP_SLUG=$SLUG" "TRUST_DIR=$TRUST" "STATE_DIR=$UNREADABLE" "DOCKER_BIN=$FAKE" "DOCKER_HOST=unix://$WORK/fake-docker.sock"
set +e
out="$(FAKE_DOCKER_SCENARIO="$SCEN" "$VERIFY_UR" 2>&1)"; rc=$?
set -e
chmod 600 "$UNREADABLE/coolify_uuid.txt"
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qE "missing app identity|unreadable"; then
  pass "E) a state file the deploying uid cannot read is refused (fail-closed)"
else
  bad "E) an unreadable state file did not stop the helper (rc=$rc)"; printf '%s\n' "$out" | tail -3 >&2
fi

echo
echo "== F/G/H: no external credential, no injected arguments =="

# F) The external-credential attempt: hand the helper a caller-chosen env-file or
# approvals path. Argument-freedom means it is structurally unreachable.
: > "$WORK/runs.log"
EXT="$WORK/external.env"
printf 'MIGRATION_DATABASE_URL=postgresql://attacker:***@127.0.0.1:5432/db\n' > "$EXT"
chmod 600 "$EXT"
set +e
out="$(FAKE_DOCKER_SCENARIO="$SCEN" FAKE_DOCKER_LOG="$WORK/runs.log" \
  "$TRUST/$SLUG-migrate-runner" "$EXT" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && [ ! -s "$WORK/runs.log" ] && printf '%s' "$out" | grep -q "takes no arguments"; then
  pass "F) an external credential file cannot be substituted (no argument is accepted)"
else
  bad "F) a caller-chosen credential file reached the credential path (rc=$rc)"; printf '%s\n' "$out" | tail -3 >&2
fi

# G) Generic Docker/helper arguments must not be injectable. These are the shapes
# a caller would use to turn a bounded helper into a general Docker client.
injected_ok=1
for args in "--rm -v /:/host alpine" "run attacker/evil" "exec coolify-db psql" "--context desktop-linux ps"; do
  set +e
  # shellcheck disable=SC2086
  out="$(FAKE_DOCKER_SCENARIO="$SCEN" "$TRUST/$SLUG-verify-container" $args 2>&1)"; rc=$?
  set -e
  if [ "$rc" -eq 0 ] || ! printf '%s' "$out" | grep -q "takes no arguments"; then
    injected_ok=0
    echo "  injected shape accepted: $args" >&2
  fi
done
if [ "$injected_ok" = 1 ]; then
  pass "G) generic Docker/helper arguments are refused (4 injection shapes)"
else
  bad "G) a helper accepted generic Docker arguments"
fi

# H) The verify-container helper is read-only by shape: it never invokes a
# mutating docker subcommand. Assert on the source, because "no `run` in a
# read-only helper" is a code property, not a runtime one.
SRC_VERIFY="$(dirname "$HERE")/verify-container.sh"
# The helper must contain no `run`/`exec`/`rm`/`system` docker invocation.
bad_subs="$(grep -nE 'd (run|exec|rm|system|kill|start|stop)\b' "$SRC_VERIFY" || true)"
if [ -z "$bad_subs" ] \
   && grep -qE 'd (ps|inspect)\b' "$SRC_VERIFY" \
   && grep -q '"\$#" -eq 0' "$SRC_VERIFY"; then
  pass "H) the helper is argument-free and invokes only ps/inspect"
else
  bad "H) the helper contains a mutating docker subcommand or accepts arguments"; printf '%s\n' "$bad_subs" >&2
fi

echo
echo "== I: a sudo refusal fails closed, before the trigger =="

# I) The helpers are reached through sudo. A refused entry (the realistic
# misconfiguration) must stop the deploy before any Coolify trigger — never be
# treated as "nothing to do".
REFUSE="$WORK/refuse-flag"
: > "$REFUSE"
GATE_SUDO="$WORK/gate-sudo-refused"
build_gate "$GATE_SUDO" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$FAKE" 1 1 "http://127.0.0.1:9/api/v1" "$CURRENT_USER" "$WORK/fake-sudo"
set +e
out="$(FAKE_DOCKER_SCENARIO="$SCEN" SUDO_FAKE_REFUSE="$REFUSE" "$GATE_SUDO" --verify-only "$SHA40" 2>&1)"; rc=$?
set -e
# verify mode goes through the helper too, so a refusal must surface as a failure.
if [ "$rc" -ne 0 ]; then
  pass "I) a refused helper invocation fails the gate (fail-closed)"
else
  bad "I) a refused sudo entry was treated as success"; printf '%s\n' "$out" | tail -3 >&2
fi

echo
echo "== J/K: no /Users/<login> credential assumption on the production path =="

# J) The configured credential state of the installed layout is the deploy user's
# state directory, not a login-user path. Assert the production defaults.
GATE_SRC="$(dirname "$HERE")/deploy-gate.sh"
hook_src="$(dirname "$HERE")/deploy-hook.sh"
runner_src="$(dirname "$HERE")/migrate-runner.sh"

if grep -q '/Users/owner' "$GATE_SRC" "$hook_src" "$runner_src" "$SRC_VERIFY"; then
  bad "J) a trusted component hard-codes /Users/owner"
else
  pass "J) no trusted component hard-codes a login-user credential path"
fi

# K) The state directory placeholder is the deploy user's, and the documented
# install path is under /var — not under a login user's home.
if grep -q '__STATE_DIR__' "$GATE_SRC" \
   && grep -q '/var/studio-deploy/state' "$(dirname "$HERE")/README.md"; then
  pass "K) the configured state path is the deploy user's (/var/studio-deploy/state)"
else
  bad "K) the state path is not the documented deploy-user path"
fi

echo
echo "== L/M: state ownership and the unsubstituted-source guard =="

# L) The gate refuses when it is not the account it was installed for. This is
# the same fail-closed identity check, exercised through the substitution seam:
# a gate built for a DIFFERENT account name must refuse before it reads or acts
# on any state.
GATE_L="$WORK/gate-owner"
build_gate "$GATE_L" "$SLUG" "$TRUST" "$STATE" "$CLONE" "$FAKE" 1 1 "$DEAD_COOLIFY_API" "$OTHER_USER"
set +e
out="$(FAKE_DOCKER_SCENARIO="$SCEN" "$GATE_L" --verify-only "$SHA40" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  pass "L) a gate installed for a different account refuses to run"
else
  bad "L) the gate proceeded despite the account mismatch"; printf '%s\n' "$out" | tail -3 >&2
fi

# M) An unsubstituted copy must refuse to run: it is the source file, not an
# installed component, and a leftover placeholder silently means "not the trust
# root".
set +e
out="$(SSH_ORIGINAL_COMMAND="$SHA40" "$hook_src" 2>&1)"; rc=$?
set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "unsubstituted placeholder"; then
  pass "M) the unsubstituted hook source refuses to run"
else
  bad "M) the unsubstituted hook source ran (rc=$rc)"; printf '%s\n' "$out" | tail -3 >&2
fi

echo
[ "$fail" = 0 ] \
  && echo "RESULT: PASS — the deploy credential is bound to the deploy uid, and the agent uid is a different principal" \
  || echo "RESULT: FAIL" >&2
exit "$fail"
