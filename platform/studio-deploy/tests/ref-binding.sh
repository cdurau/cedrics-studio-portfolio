#!/usr/bin/env bash
# Ref-binding tests for the trusted deploy gate (second review round, finding 2).
#
# The previous check proved only that the commit OBJECT existed locally and then
# called that "reachable on $REF" — which it never established. The gate now
# requires the requested commit to be the TIP of the allowed ref as fetched now.
#
# Third round (finding A): the suite no longer sets GATE_REPO/GATE_REF/APP_SLUG in
# the environment of the canonical script — the installed gate reads no such
# variable. It builds a substituted copy via tests/lib/harness.sh and runs that.
#
#   A) tip of the allowed ref                     -> ACCEPT
#   B) an older commit that IS in the clone       -> REJECT (was the false-pass)
#   C) a commit on another branch, not the ref    -> REJECT
#   D) a commit present locally but on no branch  -> REJECT
#   E) short sha / uppercase / ref name           -> REJECT (input validation)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=tests/lib/harness.sh
. "$HERE/lib/harness.sh"
# shellcheck source=tests/lib/fixture.sh
. "$HERE/lib/fixture.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

fixture_build "$WORK" ua-smoke yes

# A stand-in docker is never reached in ref-check mode, but the substituted script
# needs a value; point it at something that fails loudly if it ever runs.
NODOCKER="$WORK/no-docker"
printf '#!/usr/bin/env bash\necho "REF-CHECK TOUCHED DOCKER" >&2\nexit 9\n' > "$NODOCKER"
chmod 755 "$NODOCKER"

GATE="$WORK/ua-smoke-deploy-gate"
build_gate "$GATE" ua-smoke "$WORK/trust" "$WORK/state" "$fixture_clone" "$NODOCKER" 1 1

run_gate() { # run_gate <sha> -> output, exit status (nothing from the environment)
  "$GATE" --ref-check "$1" 2>&1
}

expect() { # expect <ACCEPT|REJECT> <label> <sha>
  local want="$1" label="$2" sha="$3" out rc
  set +e
  out="$(run_gate "$sha")"; rc=$?
  set -e
  if [ "$want" = "ACCEPT" ] && [ "$rc" -eq 0 ]; then
    echo "PASS  $label (accepted)"; return
  fi
  if [ "$want" = "REJECT" ] && [ "$rc" -ne 0 ]; then
    echo "PASS  $label (rejected: $(printf '%s' "$out" | grep -o 'is not the tip of[^—]*\|expected a full[^,]*\|has an unexpected shape\|expected exactly one argument' | head -1))"
    return
  fi
  echo "FAIL  $label: expected $want, got exit $rc" >&2
  printf '%s\n' "$out" | tail -3 >&2
  fail=1
}

echo "== ref binding =="
expect ACCEPT "A) tip of the allowed ref" "$FIX_TIP_SHA"
expect REJECT "B) older commit present in the clone" "$FIX_OLD_SHA"
expect REJECT "C) commit from another branch" "$FIX_SIDE_SHA"
expect REJECT "D) commit on no branch" "$FIX_DANGLING_SHA"

echo
echo "== input validation =="
expect REJECT "E1) 7-character short sha" "${FIX_TIP_SHA:0:7}"
expect REJECT "E2) uppercase sha" "$(printf '%s' "$FIX_TIP_SHA" | tr 'a-f' 'A-F')"
expect REJECT "E3) ref name, not a sha" "migration/coolify-v5"
expect REJECT "E4) 39 characters" "${FIX_TIP_SHA:0:39}"

echo
echo "== mode selection is an argument, never an environment variable =="
# The old gate decided its mode from VERIFY_ONLY/REF_CHECK_ONLY. If an env
# variable could still steer that, a caller could downgrade a real deploy to a
# no-op (or skip the migration) with a single exported var.
set +e
out="$("$GATE" "$FIX_OLD_SHA" 2>&1)"; rc=$?
set -e
# Without --ref-check this is a real deploy attempt: it must refuse the
# non-tip commit, i.e. the ref binding still applies.
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "is not the tip of"; then
  echo "PASS  a non-tip sha is refused on the deploy path too"
else
  echo "FAIL  the deploy path did not enforce ref binding (exit $rc)" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

set +e
VERIFY_ONLY=1 out="$("$GATE" "$FIX_TIP_SHA" 2>&1)"; rc=$?
set -e
if printf '%s' "$out" | grep -q "ignoring environment override"; then
  echo "PASS  a stray VERIFY_ONLY is announced as ignored"
else
  echo "FAIL  an environment override was not reported as ignored" >&2; fail=1
fi

echo
# B is the case the previous implementation wrongly accepted; state it explicitly
# so a future regression is unmistakable.
set +e
out="$(run_gate "$FIX_OLD_SHA")"; rc=$?
set -e
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "is not the tip of migration/coolify-v5"; then
  echo "PASS  the previous false-pass (object exists locally) is now a REJECT for the right reason"
else
  echo "FAIL  the object-exists case did not reject with the ref-binding message:" >&2
  printf '%s\n' "$out" | tail -3 >&2; fail=1
fi

echo
[ "$fail" = 0 ] && echo "RESULT: PASS — the commit must be the tip of the allowed ref" || echo "RESULT: FAIL" >&2
exit "$fail"
