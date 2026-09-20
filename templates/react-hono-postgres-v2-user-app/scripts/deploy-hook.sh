#!/usr/bin/env bash
# SSH forced-command deploy hook — the bounded entry point for the deploy gate.
#
# Gate review 2026-09-20 (WR-01). This is the cheaper form of the "bounded hook"
# option, and it replaces the self-hosted-runner variant in
# `ci/deploy-__APP_SLUG__.yml` for this repository.
#
# Why not a self-hosted runner: the runtime repo is the bot-written mirror
# repository. A runner registered there executes the workflow definition of the
# branch that triggered it — and that definition is written by the agent. Anyone
# who can land a branch would therefore run arbitrary commands on the host as
# the runner user, with read access to ~/.local/state/*/secrets.env (owner DB
# password, Google client secret, Better-Auth secrets), the Coolify token and the
# Docker socket via the local VM. A branch guard inside the workflow does not help,
# because it lives in the same document the attacker controls. That is exactly
# the authority boundary S0 exists to draw: "the general deploy agent holds no
# unbounded credential". A runner would flip it.
#
# INSTALL (on the host, as the login user):
#   1. place this script at /usr/local/bin/<slug>-deploy-hook, owned by root,
#      mode 0755, and make sure the login user can read it but not edit it
#   2. generate a dedicated key pair for CI; put THIS line in ~/.ssh/authorized_keys,
#      with the command path and the public key of that pair:
#        command="/usr/local/bin/<slug>-deploy-hook",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... ci@github
#   3. store the PRIVATE key as the GitHub secret COOLIFY_DEPLOY_SSH_KEY
#   4. the workflow then runs: ssh -i <key> ci@<mini> <40-char-sha>
#
# What it accepts: exactly one full lowercase-hex commit SHA, arriving as
# SSH_ORIGINAL_COMMAND. Nothing else. No ref names, no flags, no extra words —
# sshd passes the client's command verbatim, so an unvalidated value would land
# in git/argv as the caller chose.
set -euo pipefail

SLUG="${APP_SLUG:-__APP_SLUG__}"
GATE_REPO="${GATE_REPO:-$HOME/.local/state/$SLUG/deploy-clone}"
GATE="$GATE_REPO/apps/$SLUG/scripts/deploy-gate.sh"
REF="${GATE_REF:-migration/coolify-v5}"

msg() { printf '[deploy-hook] %s\n' "$*" >&2; }

# SSH_ORIGINAL_COMMAND is attacker-controlled input: treat it as such.
# Validate BEFORE anything else uses it, and exit on the spot — a first pass that
# only prints a message would let a 40-character non-hex value through the
# length check below and reach git/argv intact.
CMD="${SSH_ORIGINAL_COMMAND:-}"
case "$CMD" in
  # exactly 40 lowercase hex characters, nothing else. No ref names, no flags,
  # no second word, no leading dash.
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]\
[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]\
[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]\
[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) : ;;
  *)
    msg "refused: expected exactly one 40-character lowercase hex commit sha"
    exit 2
    ;;
esac
# Defence in depth: the pattern above already guarantees this, so a failure
# here means the pattern was edited wrongly.
[ "${#CMD}" -eq 40 ] || { msg "refused: expected a 40-character sha, got ${#CMD} characters"; exit 2; }

# The clone is the host's own; the caller never supplies a path or a ref.
[ -d "$GATE_REPO/.git" ] || { msg "no deploy clone at $GATE_REPO"; exit 2; }
[ -x "$GATE" ] || { msg "no deploy gate at $GATE"; exit 2; }

msg "deploying $CMD from $GATE_REPO"
msg "fetching $REF"
git -C "$GATE_REPO" fetch --quiet --prune origin "$REF" || { msg "fetch failed"; exit 1; }

git -C "$GATE_REPO" cat-file -e "${CMD}^{commit}" 2>/dev/null || {
  msg "commit $CMD is not reachable on $REF — refusing to build something else"
  exit 1
}

msg "running the deploy gate"
# The gate re-checks the sha, fetches, checks out and builds from its own clone
# (GATE_REPO mode), so the host decides what it builds rather than the caller.
exec env GATE_REPO="$GATE_REPO" APP_SLUG="$SLUG" GATE_REF="$REF" \
  "$GATE" "$CMD"
