#!/usr/bin/env bash
# Trusted SSH forced-command deploy hook — root-owned, outside every repo.
#
# Install target  : /usr/local/bin/<slug>-deploy-hook  (root:wheel 0755)
# Canonical source: platform/studio-deploy/deploy-hook.sh (planning repo)
#
# authorized_keys entry (single line, on the host, as the login user):
#   command="/usr/local/bin/<slug>-deploy-hook",restrict ssh-ed25519 AAAA... ci@github
#
# Gate review 2026-09-20, SECOND round — finding 1 (KRITISCH) and 2 (HOCH).
# Gate review 2026-09-21, THIRD round  — finding A (KRITISCH/HOCH) and B (HOCH).
#
# WHAT IT DOES
# The hook is the bounded entry point. Its whole job is to accept exactly one
# full lowercase-hex commit SHA and hand it to the TRUSTED gate under
# /usr/local/libexec/studio-deploy/, which the deploy agent cannot modify with a
# runtime-repo commit. It deliberately does not fetch, does not run
# `git cat-file`, and consults no environment variable: ref binding lives in the
# gate, where it is a real tip comparison against FETCH_HEAD.
#
# (finding 2) The first version checked object existence here and reported "not
# reachable on $REF", which that check never proved.
#
# (finding A) The gate path, clone and ref are now LITERALS substituted at
# install time. The previous version read GATE/GATE_REPO/GATE_REF from the
# environment, so a caller who could set the environment of this process could
# point the forced command at a gate of its own — which is exactly the
# authority boundary this hook exists to hold.
#
# (finding B) `no-user-rc` / `restrict`: with only no-pty,no-port-forwarding,
# no-agent-forwarding,no-X11-forwarding, sshd still executes ~/.ssh/rc before the
# forced command if that file exists. On the host the login account is the same
# uid that runs the runtime repo and the agent processes, so ~/.ssh/rc is a path
# an agent can create and get executed next to the deploy credential. The
# documented entry therefore uses `restrict`, which disables port/agent/X11
# forwarding, PTY allocation AND ~/.ssh/rc — and, per sshd(8), any future
# restriction capability added to authorized_keys. `no-user-rc` is the narrower
# equivalent if forwarding options are wanted individually.
set -euo pipefail

# --- the literal production trust path (substituted once at install) ---------
SLUG="__APP_SLUG__"
TRUST_DIR="__TRUST_DIR__"
GATE_REPO="__GATE_REPO__"

DEFAULT_TRUST_DIR="/usr/local/libexec/studio-deploy"
GATE="$TRUST_DIR/${SLUG}-deploy-gate"
GATE_REF="migration/coolify-v5"

msg() { printf '[deploy-hook] %s\n' "$*" >&2; }

case "$SLUG$TRUST_DIR$GATE_REPO" in
  *__*) msg "refused: unsubstituted placeholder — this is the canonical source, not an installed component"; exit 2 ;;
esac

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
if [ "$TRUST_DIR" = "$DEFAULT_TRUST_DIR" ]; then
  owner="$(stat -f '%u' "$GATE" 2>/dev/null || stat -c '%u' "$GATE")"
  [ "$owner" = "0" ] || { msg "refusing: $GATE is owned by uid $owner, expected root"; exit 2; }
fi

# A second, independent bound on the sha, so a bug in the pattern above cannot
# silently widen what reaches the gate.
[ "${#CMD}" -eq 40 ] || { msg "refused: sha length"; exit 2; }

msg "requesting deploy of $CMD"

# Sanitised environment: the gate is self-sufficient (it resolves its own PATH
# and home from the OS and reads no override), so nothing from this session —
# including any variable a forced-command session might carry — is passed on.
# Defence in depth on top of the gate's own literal trust path.
exec /usr/bin/env -i \
  PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  LANG="${LANG:-en_US.UTF-8}" \
  "$GATE" "$CMD"
