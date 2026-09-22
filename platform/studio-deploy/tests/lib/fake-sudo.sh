#!/usr/bin/env bash
# `sudo` stand-in for the studio-deploy suites — TEST DOUBLE ONLY.
#
# The installed gate reaches its two root-owned helpers through `sudo -n`:
#   sudo -n <slug>-migrate-runner      (no arguments)
#   sudo -n <slug>-verify-container    (no arguments)
#
# The suites must exercise that call shape without needing root, and must also be
# able to prove what happens when sudo REFUSES (an unpermitted entry is the
# realistic misconfiguration). So this stand-in drops the `-n` flag and execs the
# remaining command, unless SUDO_FAKE_REFUSE points at a file — in that case it
# behaves like a denied sudoers entry: refuse, explain, non-zero.
#
# It never elevates anything. It is not on any production path: the gate reads
# SUDO_BIN from a literal that the installing owner substitutes with /usr/bin/sudo.
set -euo pipefail

REFUSE="${SUDO_FAKE_REFUSE:-}"

while [ "${1:-}" = "-n" ] || [ "${1:-}" = "-H" ]; do
  shift
done

if [ -n "$REFUSE" ]; then
  printf 'sudo: a password is required (stand-in refusing per %s)\n' "$REFUSE" >&2
  exit 1
fi

[ "$#" -ge 1 ] || { echo "fake-sudo: nothing to run" >&2; exit 1; }
exec "$@"
