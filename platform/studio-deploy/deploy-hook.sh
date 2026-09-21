#!/usr/bin/env bash
# Trusted SSH forced-command deploy hook — root-owned, outside every repo.
#
# Install target  : /usr/local/bin/<slug>-deploy-hook  (root:wheel 0755)
# Canonical source: platform/studio-deploy/deploy-hook.sh (planning repo)
#
# authorized_keys entry (single line, on the host, as the login user):
#   command="/usr/local/bin/<slug>-deploy-hook",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... ci@github
#
# Gate review 2026-09-20, SECOND round — finding 1 (KRITISCH) and 2 (HOCH).
#
# The hook is the bounded entry point. Its whole job is to accept exactly one
# full lowercase-hex commit SHA and hand it to the TRUSTED gate under
# /usr/local/libexec/studio-deploy/, which the deploy agent cannot modify with a
# runtime-repo commit.
#
# What it deliberately does NOT do: no fetch, no `git cat-file`. The first
# version checked object existence here and reported "not reachable on $REF",
# which that check never proved (finding 2). Ref binding now lives in the gate,
# where it is a real tip comparison against FETCH_HEAD, so it cannot be skipped
# by reaching the gate through another path.
set -euo pipefail

SLUG="${APP_SLUG:-__APP_SLUG__}"
# The trusted gate lives outside every repository. The production path is a
# literal so that no runtime-repo commit can redirect this hook at a binary it
# controls.
DEFAULT_GATE="/usr/local/libexec/studio-deploy/$SLUG-deploy-gate"
GATE="${GATE:-$DEFAULT_GATE}"
GATE_REPO="${GATE_REPO:-$HOME/.local/state/$SLUG/deploy-clone}"
GATE_REF="${GATE_REF:-migration/coolify-v5}"

msg() { printf '[deploy-hook] %s\n' "$*" >&2; }

# SSH_ORIGINAL_COMMAND is attacker-controlled input: validate before anything
# uses it, and exit on the spot. Exactly 40 lowercase hex characters, nothing
# else — no ref names, no flags, no second word, no leading dash.
CMD="${SSH_ORIGINAL_COMMAND:-}"
case "$CMD" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]\
[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]\
[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]\
[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) : ;;
  *)
    msg "refused: expected exactly one 40-character lowercase hex commit sha"
    exit 2
    ;;
esac
# Defence in depth: the pattern above already guarantees this, so a failure here
# means the pattern was edited wrongly.
[ "${#CMD}" -eq 40 ] || { msg "refused: expected a 40-character sha, got ${#CMD} characters"; exit 2; }

# The trusted gate must exist and must not be writable by the agent: a
# group/other-writable gate is not the boundary this hook claims. On the
# production path it must additionally be root-owned.
[ -x "$GATE" ] || { msg "no trusted deploy gate at $GATE"; exit 2; }
perms="$(stat -f '%Lp' "$GATE" 2>/dev/null || stat -c '%a' "$GATE")"
case "$perms" in
  *[2367]|*[2367]?) msg "refusing: $GATE has mode $perms, must not be writable by group/other"; exit 2 ;;
esac
if [ "$GATE" = "$DEFAULT_GATE" ]; then
  owner="$(stat -f '%u' "$GATE" 2>/dev/null || stat -c '%u' "$GATE")"
  [ "$owner" = "0" ] || { msg "refusing: $GATE is owned by uid $owner, expected root"; exit 2; }
fi

# A second, independent bound on the sha, so a bug in the pattern above cannot
# silently widen what reaches the gate.
[ "${#CMD}" -eq 40 ] || { msg "refused: sha length"; exit 2; }

msg "requesting deploy of $CMD"
# The gate re-validates the sha, binds it to $GATE_REF and fetches/checks out
# from the host's own clone, so the host decides what it builds.
exec env APP_SLUG="$SLUG" GATE_REPO="$GATE_REPO" GATE_REF="$GATE_REF" "$GATE" "$CMD"
