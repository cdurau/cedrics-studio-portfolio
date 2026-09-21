#!/usr/bin/env bash
# Convenience: print an installable approval list for a checkout.
#
# Gate review 2026-09-21, third round — finding C.
#
# This script is NOT the approval authority. It lives in the agent-writable
# planning repo, so its output must never be the thing a human signs off on
# without checking it. It is kept as a one-line convenience over find-approvals.sh
# and now emits the "# commit <sha>" line that binds the list to a commit.
#
# The honest owner procedure is:
#
#   ./platform/studio-deploy/find-approvals.sh <checkout> <slug>            # review view
#   $EDITOR <proposed.txt>                                                 # after reading the SQL diff
#   ./platform/studio-deploy/verify-approvals.sh <checkout> <slug> <proposed.txt>
#   sudo install -m 0644 -o root -g wheel <proposed.txt> \
#     /usr/local/libexec/studio-deploy/approved-migrations/<slug>.txt
#
# i.e. the human approves, an independent check recomputes the hashes, and only
# then is the root-owned file installed. `print-approvals | sudo tee` is no longer
# the procedure: it made agent-written code the source of the hashes that owner
# rights obey.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash "$HERE/find-approvals.sh" "$@" --for-install
