#!/usr/bin/env bash
# Forced-command hook validation tests.
#
# The hook is the bounded entry point: whatever a caller can put into
# SSH_ORIGINAL_COMMAND is what it must refuse unless it is exactly one full
# lowercase-hex commit SHA. An unvalidated value would reach git/argv as the
# caller chose.
#
# The hook no longer fetches or runs `git cat-file` itself (finding 2 moved ref
# binding into the trusted gate, where it is a real tip comparison). These cases
# therefore assert (a) the input validation and (b) that a valid SHA is handed to
# the trusted gate unchanged.
#
# No SSH and no live system are involved: SSH_ORIGINAL_COMMAND is set directly.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="${HOOK:-$HERE/../deploy-hook.sh}"
[ -x "$HOOK" ] || { echo "missing hook at $HOOK" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
fail=0

# A stand-in gate that records what it was asked to deploy, so the "valid input
# reaches the gate unchanged" half of the contract is observable.
GATE_STUB="$WORK/deploy-gate"
cat > "$GATE_STUB" <<'STUB'
#!/usr/bin/env bash
printf 'GATE-INVOKED argv1=%s APP_SLUG=%s GATE_REPO=%s GATE_REF=%s\n' \
  "$1" "${APP_SLUG:-}" "${GATE_REPO:-}" "${GATE_REF:-}"
exit 0
STUB
chmod 755 "$GATE_STUB"

GOOD_SHA="07759121ba315a6c6a4f9dc3ec1f95d07b4059a9"

run_hook() { # run_hook <SSH_ORIGINAL_COMMAND>
  SSH_ORIGINAL_COMMAND="$1" GATE="$GATE_STUB" APP_SLUG=ua-smoke \
    GATE_REPO="$WORK/clone" GATE_REF="migration/coolify-v5" \
    "$HOOK" 2>&1
}

expect() { # expect <ACCEPT|REJECT> <label> <command>
  local want="$1" label="$2" cmd="$3" out rc
  set +e
  out="$(run_hook "$cmd")"; rc=$?
  set -e
  if [ "$want" = "ACCEPT" ]; then
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "GATE-INVOKED"; then
      echo "PASS  $label"; return
    fi
    echo "FAIL  $label: expected ACCEPT, got exit $rc" >&2
    printf '%s\n' "$out" | tail -3 >&2; fail=1; return
  fi
  if [ "$rc" -eq 0 ]; then
    echo "FAIL  $label: expected REJECT, but the hook accepted it" >&2; fail=1; return
  fi
  if printf '%s' "$out" | grep -q "GATE-INVOKED"; then
    echo "FAIL  $label: rejected, but the gate was still invoked" >&2; fail=1; return
  fi
  echo "PASS  $label"
}

echo "== accepted =="
expect ACCEPT "A) exactly one full lowercase-hex sha" "$GOOD_SHA"

echo
echo "== rejected: shape of the value =="
expect REJECT "B) empty command" ""
expect REJECT "C) uppercase sha" "$(printf '%s' "$GOOD_SHA" | tr 'a-f' 'A-F')"
expect REJECT "D) 39 characters" "${GOOD_SHA:0:39}"
expect REJECT "E) 41 characters" "${GOOD_SHA}0"
expect REJECT "F) short (7-character) sha" "${GOOD_SHA:0:7}"
expect REJECT "G) non-hex character inside the sha" "${GOOD_SHA%?}g"

echo
echo "== rejected: nothing but the sha =="
expect REJECT "H) a ref name" "migration/coolify-v5"
expect REJECT "I) refs/heads/... form" "refs/heads/main"
expect REJECT "J) leading dash (option-looking)" "-n${GOOD_SHA}"
expect REJECT "K) sha with a trailing word" "${GOOD_SHA} main"
expect REJECT "L) two shas" "$GOOD_SHA $GOOD_SHA"

echo
echo "== rejected: shell / git injection attempts =="
expect REJECT "M) command chaining" "${GOOD_SHA}; id"
expect REJECT "N) command substitution" "\$(id)"
expect REJECT "O) backticks" "\`id\`"
expect REJECT "P) pipe" "${GOOD_SHA}|tee /tmp/pwned"
expect REJECT "Q) upload-pack option injection" "${GOOD_SHA} --upload-pack=/bin/sh"
expect REJECT "R) newline smuggling" "$(printf '%s\nid' "$GOOD_SHA")"
expect REJECT "S) embedded NUL-free whitespace tricks" "$(printf '%s\t' "$GOOD_SHA")"

echo
echo "== the valid sha reaches the gate unchanged =="
out="$(run_hook "$GOOD_SHA")"
if printf '%s' "$out" | grep -q "argv1=$GOOD_SHA"; then
  echo "PASS  the gate receives the requested commit as its single argument"
else
  echo "FAIL  the gate did not receive the expected commit" >&2
  printf '%s\n' "$out" >&2; fail=1
fi

echo
echo "== the gate must not be a group/other-writable binary =="
chmod 777 "$GATE_STUB"
set +e
out="$(run_hook "$GOOD_SHA")"; rc=$?
set -e
chmod 755 "$GATE_STUB"
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "must not be writable by group/other"; then
  echo "PASS  a world-writable gate is refused"
else
  echo "FAIL  a world-writable gate was accepted" >&2; fail=1
fi

echo
[ "$fail" = 0 ] && echo "RESULT: PASS — only a single full sha reaches the trusted gate" || echo "RESULT: FAIL" >&2
exit "$fail"
