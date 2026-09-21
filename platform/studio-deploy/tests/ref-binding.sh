#!/usr/bin/env bash
# Ref-binding tests for the trusted deploy gate (second review round, finding 2).
#
# The previous check proved only that the commit OBJECT existed locally and then
# called that "reachable on $REF" — which it never established. The gate now
# requires the requested commit to be the TIP of the allowed ref as fetched now.
#
# These cases run the real gate in REF_CHECK_ONLY mode, which performs exactly
# the production validation and ref binding and then stops. Nothing is migrated,
# deployed or mutated, and no credential is read.
#
#   A) tip of the allowed ref                     -> ACCEPT
#   B) an older commit that IS in the clone       -> REJECT (was the false-pass)
#   C) a commit on another branch, not the ref    -> REJECT
#   D) a commit present locally but on no branch  -> REJECT
#   E) short sha / uppercase / ref name           -> REJECT (input validation)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GATE="${GATE:-$HERE/../deploy-gate.sh}"
[ -x "$GATE" ] || { echo "missing gate at $GATE" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

# --- build a small origin with an allowed and a foreign branch ---------------
ORIGIN="$WORK/origin.git"
SEED="$WORK/seed"
git init -q --bare "$ORIGIN"
git init -q "$SEED"
cd "$SEED"
mkdir -p apps/ua-smoke
printf 'v1\n' > apps/ua-smoke/file.txt
git add -A && git commit -qm c1
OLD_SHA="$(git rev-parse HEAD)"
printf 'v2\n' > apps/ua-smoke/file.txt
git add -A && git commit -qm c2
TIP_SHA="$(git rev-parse HEAD)"
git branch -m migration/coolify-v5
git remote add origin "$ORIGIN"
git push -q origin migration/coolify-v5

# A commit that exists only on an unrelated branch.
git checkout -q -b side-branch
printf 'side\n' > apps/ua-smoke/side.txt
git add -A && git commit -qm side
SIDE_SHA="$(git rev-parse HEAD)"
git push -q origin side-branch
git checkout -q migration/coolify-v5

# A commit that is in no branch at all (dangling after a reset).
printf 'detached\n' > apps/ua-smoke/file.txt
git add -A && git commit -qm dangling
DANGLING_SHA="$(git rev-parse HEAD)"
git reset -q --hard "$TIP_SHA"

# --- clone the way the gate expects -----------------------------------------
CLONE="$WORK/deploy-clone"
git clone -q --branch migration/coolify-v5 "$ORIGIN" "$CLONE"
cd "$CLONE"

run_gate() { # run_gate <sha> -> output, exit status
  REF_CHECK_ONLY=1 GATE_REPO="$CLONE" GATE_REF="migration/coolify-v5" \
    APP_SLUG=ua-smoke "$GATE" "$1" 2>&1
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
    echo "PASS  $label (rejected: $(printf '%s' "$out" | grep -o 'is not the tip of[^—]*\|expected a full[^,]*\|has an unexpected shape' | head -1))"
    return
  fi
  echo "FAIL  $label: expected $want, got exit $rc" >&2
  printf '%s\n' "$out" | tail -3 >&2
  fail=1
}

echo "== ref binding =="
expect ACCEPT "A) tip of the allowed ref" "$TIP_SHA"
expect REJECT "B) older commit present in the clone" "$OLD_SHA"
expect REJECT "C) commit from another branch" "$SIDE_SHA"
expect REJECT "D) commit on no branch" "$DANGLING_SHA"

echo
echo "== input validation =="
expect REJECT "E1) 7-character short sha" "${TIP_SHA:0:7}"
expect REJECT "E2) uppercase sha" "$(printf '%s' "$TIP_SHA" | tr 'a-f' 'A-F')"
expect REJECT "E3) ref name, not a sha" "migration/coolify-v5"
expect REJECT "E4) 39 characters" "${TIP_SHA:0:39}"

echo
# B is the case the previous implementation wrongly accepted; state it explicitly
# so a future regression is unmistakable.
set +e
out="$(run_gate "$OLD_SHA")"; rc=$?
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
