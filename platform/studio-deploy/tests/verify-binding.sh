#!/usr/bin/env bash
# Target-app / commit / health binding tests for the deploy gate's verification
# step (second review round, finding 3).
#
# The previous version picked any container carrying any coolify.applicationId
# label whose image string merely CONTAINED the 7-character short SHA. In a
# monorepo several apps carry the same commit, so a healthy container of ANOTHER
# app could turn a failed deploy of this app green.
#
# These cases run the gate in verify mode against a synthetic container world
# (tests/fake-docker.sh), so they need no live deployment and can deterministically
# present "another app, same commit" — which a live test cannot.
#
# Third round (finding A): the suite runs a SUBSTITUTED COPY of the gate built by
# tests/lib/harness.sh, not the canonical script with STATE_DIR/TRUST_DIR exported.
#
# S0-13: the gate no longer talks to Docker at all. It reaches a root-owned,
# ARGUMENT-FREE helper through `sudo -n` and does the binding itself. This suite
# therefore stages that helper (the real verify-container script, with the docker
# stand-in substituted for DOCKER_BIN) and a sudo stand-in — so the call shape,
# the argument-free contract and the binding are all really exercised.
#
#   A) target app, full commit, healthy               -> PASS
#   B) same commit but a DIFFERENT app                -> FAIL (the false-green)
#   C) target app but a different commit              -> FAIL
#   D) target app + commit but unhealthy              -> FAIL
#   E) commit matches only by prefix (7 chars)        -> FAIL
#   F) target app healthy, another app shares commit   -> PASS (no false negative)
#   G) full commit only in the coolify.commit label    -> PASS
#
# Every FAIL case also asserts the REASON, so a rejection from the wrong branch
# of the logic cannot be mistaken for a pass.
#
# Verify mode must read no owner credential and no Coolify token: the state dir
# has neither, and the docker stand-in exits loudly on any `run`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tests/lib/harness.sh
. "$HERE/lib/harness.sh"
# shellcheck source=tests/lib/agent-stubs.sh
. "$HERE/lib/agent-stubs.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

# The gate runs the root-owned verify-container helper; stage the real script with
# the docker stand-in substituted in, exactly as the installed component would run
# it. The stand-in is an executable copy: a rewrite of the source file must not be
# able to turn every case into a vacuous pass (writing a 0600 file once made the
# helper's ps/inspect fail silently and made the FAIL cases "pass" for the wrong
# reason).
FAKE="$WORK/fake-docker"
stage_docker_stdin "$FAKE"

APP_UUID="bo95tvgzq7mkhjx0syelwir5"
APP_ID="13"
OTHER_UUID="z1dvf74j9vltpjapbieaiihx"
OTHER_ID="12"
# Full 40-char commits. OTHER_COMMIT shares the first 7 characters with FULL_SHA
# on purpose: that prefix identity is what the old match could not distinguish.
FULL_SHA="07759121ba315a6c6a4f9dc3ec1f95d07b4059a9"
SHORT_SHA="${FULL_SHA:0:7}"
OTHER_COMMIT="0775912fffffffffffffffffffffffffffffffff"

SCEN="$WORK/scenario.tsv"

# Prove the stand-in actually answers before trusting any result below: if it
# cannot be executed, every REJECT case would pass for the wrong reason.
scenario() { printf '%s' "$1" > "$SCEN"; }
scenario "$(printf 'c-ok\t%s\t%s\t%s:%s\t\thealthy\n' "$APP_UUID" "$APP_ID" "$APP_UUID" "$FULL_SHA")"
seen="$(FAKE_DOCKER_SCENARIO="$SCEN" "$FAKE" --context devbox ps --filter "label=coolify.name=$APP_UUID" --format '{{.Names}}' 2>&1)" \
  || { echo "docker stand-in failed to run: $seen" >&2; exit 2; }
[ "$seen" = "c-ok" ] || { echo "docker stand-in did not report the scenario container (got '$seen')" >&2; exit 2; }

# App identity files: NOT secrets, and verify mode is allowed to read them. The
# directory deliberately lacks coolify_token and migrate.env, so a verify run that
# suddenly demanded a credential would fail here.
STATE="$WORK/state"
mkdir -p "$STATE"
printf '%s\n' "$APP_UUID" > "$STATE/coolify_uuid.txt"
printf '%s\n' "$APP_ID"   > "$STATE/coolify_app_id.txt"

# verify mode does not read the trusted runner/approvals; the helper set is staged
# anyway so a regression that starts demanding them is visible, and so the gate's
# own component-existence checks are exercised.
TRUST="$WORK/trust"
mkdir -p "$TRUST"
stage_helpers "$TRUST" "$STATE" "$WORK/deploy-clone" "$FAKE" ua-smoke

GATE="$WORK/ua-smoke-deploy-gate"
build_gate "$GATE" ua-smoke "$TRUST" "$STATE" "$WORK/deploy-clone" "$FAKE" 1 1

run_verify() { # run_verify <sha> — the docker world is injected via the stand-in
  # FAKE_DOCKER_SCENARIO is a property of the *test double* the helper runs, not of
  # the gate: the gate never learns a scenario, it only invokes the helper.
  FAKE_DOCKER_SCENARIO="$SCEN" "$GATE" --verify-only "$1" 2>&1
}

expect() { # expect <PASS|FAIL> <label> <sha> [required-substring]
  local want="$1" label="$2" sha="$3" sub="${4:-}" out rc
  set +e
  out="$(run_verify "$sha")"; rc=$?
  set -e
  if [ "$want" = "PASS" ]; then
    if [ "$rc" -eq 0 ]; then
      if [ -n "$sub" ] && ! printf '%s' "$out" | grep -q "$sub"; then
        echo "FAIL  $label: succeeded without the expected message" >&2; fail=1; return
      fi
      echo "PASS  $label"; return
    fi
    echo "FAIL  $label: expected PASS, got exit $rc" >&2
    printf '%s\n' "$out" | tail -3 >&2; fail=1; return
  fi
  if [ "$rc" -eq 0 ]; then
    echo "FAIL  $label: expected FAIL, but the gate passed" >&2; fail=1; return
  fi
  if [ -n "$sub" ] && ! printf '%s' "$out" | grep -q "$sub"; then
    echo "FAIL  $label: rejected, but for the wrong reason (want '$sub')" >&2
    printf '%s\n' "$out" | tail -3 >&2; fail=1; return
  fi
  echo "PASS  $label"
}

echo "== verification binding (verify mode) =="

# A) the target app at the full commit, healthy.
scenario "$(printf 'c-ok\t%s\t%s\t%s:%s\t\thealthy\n' "$APP_UUID" "$APP_ID" "$APP_UUID" "$FULL_SHA")"
expect PASS "A) target app + full commit + healthy" "$FULL_SHA" \
  "PASS — healthy container for expected app and commit is serving"

# B) THE false-green: another app at the same commit, healthy.
scenario "$(printf 'c-other\t%s\t%s\t%s:%s\t\thealthy\n' "$OTHER_UUID" "$OTHER_ID" "$OTHER_UUID" "$FULL_SHA")"
expect FAIL "B) another app at the same commit does NOT turn the gate green" "$FULL_SHA" \
  "no healthy container for app $APP_UUID"

# C) target app IS found, but serving a different commit — the reason must come
# from the commit comparison, not from finding nothing.
scenario "$(printf 'c-ok\t%s\t%s\t%s:%s\t\thealthy\n' "$APP_UUID" "$APP_ID" "$APP_UUID" "$OTHER_COMMIT")"
expect FAIL "C) target app at the wrong commit" "$FULL_SHA" "is not built from $FULL_SHA"

# D) target app + commit but not healthy — the reason must cite the health state.
scenario "$(printf 'c-ok\t%s\t%s\t%s:%s\t\tstarting\n' "$APP_UUID" "$APP_ID" "$APP_UUID" "$FULL_SHA")"
expect FAIL "D) target app + commit but unhealthy" "$FULL_SHA" "health: starting"

# E) only a 7-character prefix matches; the full commit appears nowhere.
scenario "$(printf 'c-ok\t%s\t%s\t%s:%s\t\thealthy\n' "$APP_UUID" "$APP_ID" "$APP_UUID" "$SHORT_SHA")"
expect FAIL "E) a short-sha-only match is not enough" "$FULL_SHA" "is not built from $FULL_SHA"

# F) target app healthy AND another app shares the commit: still PASS, so the
# stronger binding did not become a false negative.
scenario "$(printf 'c-ok\t%s\t%s\t%s:%s\t\thealthy\nc-other\t%s\t%s\t%s:%s\t\thealthy\n' \
  "$APP_UUID" "$APP_ID" "$APP_UUID" "$FULL_SHA" "$OTHER_UUID" "$OTHER_ID" "$OTHER_UUID" "$FULL_SHA")"
expect PASS "F) target app found even when another app shares the commit" "$FULL_SHA"

# G) the full commit sits only in the coolify.commit label, not the image tag.
scenario "$(printf 'c-ok\t%s\t%s\t%s:untagged\t%s\thealthy\n' "$APP_UUID" "$APP_ID" "$APP_UUID" "$FULL_SHA")"
expect PASS "G) full commit satisfied by the commit label alone" "$FULL_SHA"

echo
echo "== verify mode reads no credential and mutates nothing =="
# The docker stand-in exits 97 on any `run`, so a migration attempt inside verify
# mode cannot go unnoticed; and the state dir holds no token or env file.
scenario "$(printf 'c-ok\t%s\t%s\t%s:%s\t\thealthy\n' "$APP_UUID" "$APP_ID" "$APP_UUID" "$FULL_SHA")"
out="$(run_verify "$FULL_SHA")" || true
if printf '%s' "$out" | grep -q "credential files are not required and not read"; then
  echo "PASS  the run states that no credential file is read"
else
  echo "FAIL  the run did not confirm credential-free operation" >&2; fail=1
fi
if printf '%s' "$out" | grep -q "FAKE-DOCKER: unexpected 'run'"; then
  echo "FAIL  verify mode tried to reach the database" >&2; fail=1
else
  echo "PASS  no database contact occurred"
fi

# H) a missing app identity file must be a hard failure, not a silent skip.
mv "$STATE/coolify_uuid.txt" "$STATE/coolify_uuid.txt.off"
set +e
out="$(run_verify "$FULL_SHA")"; rc=$?
set -e
mv "$STATE/coolify_uuid.txt.off" "$STATE/coolify_uuid.txt"
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "missing app identity file"; then
  echo "PASS  H) a missing app identity is refused"
else
  echo "FAIL  H) missing app identity was not refused" >&2; fail=1
fi

echo
[ "$fail" = 0 ] \
  && echo "RESULT: PASS — health verification is bound to app, commit and state" \
  || echo "RESULT: FAIL" >&2
exit "$fail"
