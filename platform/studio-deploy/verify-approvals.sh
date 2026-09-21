#!/usr/bin/env bash
# Independently VERIFY an owner-approval list against a checkout.
#
# Gate review 2026-09-21, third round — finding C (HOCH/MITTEL).
#
# The old owner step was `print-approvals.sh <checkout> <slug> | sudo tee ...`.
# print-approvals.sh lives in the AGENT-WRITABLE planning repo, so its output
# could not stand for a human's judgement: whatever the agent put in that script
# would become "the approved hashes" the owner credential obeys. The trust chain
# has to terminate in something a human can check without trusting agent code.
#
# This script is that check. It recomputes every hash from the files on disk with
# the SYSTEM tool (/usr/bin/shasum, root-owned on macOS), compares the result
# against the *proposed* approval file, and exits non-zero on any mismatch. It
# contains no hash logic of its own and reads no credential — so it is useful even
# though it lives in the same repo, because the human no longer has to believe a
# hash that agent-written code produced: they can run the one-liner below and
# compare by eye.
#
# The review the human is asked to perform, in full:
#
#   1. git -C <checkout> log -1 --format='%H %s'      # which commit, and why
#   2. git -C <checkout> diff <their-base>..HEAD -- apps/<slug>/drizzle/migrations
#   3. For each changed file, read the SQL itself and the hash printed here.
#   4. Optionally, by hand, without any project script:
#        (cd <checkout>/apps/<slug>/drizzle/migrations && /usr/bin/shasum -a 256 *.sql)
#
# Usage: verify-approvals.sh <path-to-checkout> <slug> <proposed-approvals-file>
#
# Exit: 0 = the proposed list matches the checkout exactly (commit + every hash).
set -euo pipefail

PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

REPO="${1:-}"
SLUG="${2:-}"
PROPOSED="${3:-}"
[ -n "$REPO" ] && [ -d "$REPO" ] || { echo "usage: $0 <path-to-checkout> <slug> <proposed-approvals-file>" >&2; exit 2; }
[ -n "$SLUG" ] || { echo "usage: $0 <path-to-checkout> <slug> <proposed-approvals-file>" >&2; exit 2; }
[ -n "$PROPOSED" ] && [ -r "$PROPOSED" ] || { echo "usage: $0 <path-to-checkout> <slug> <proposed-approvals-file>" >&2; exit 2; }

MIG_DIR="$REPO/apps/$SLUG/drizzle/migrations"
JOURNAL="$MIG_DIR/meta/_journal.json"
[ -r "$JOURNAL" ] || { echo "no migration journal at $JOURNAL" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq is required to read the migration journal" >&2; exit 1; }

fail=0
note() { printf '%s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*" >&2; fail=1; }

# The commit the checkout is actually at — read from git, never from the file
# under review, so a wrong commit line cannot pass by agreeing with itself.
HEAD_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
[ -n "$HEAD_SHA" ] || { echo "not a git checkout: $REPO" >&2; exit 1; }

note "checkout commit: $HEAD_SHA"
note "checkout subject: $(git -C "$REPO" log -1 --format='%s' 2>/dev/null || echo '(unknown)')"
note ""

# --- the commit binding ------------------------------------------------------
want_commit="$(awk '$1=="#" && $2=="commit" {print $3; exit}' "$PROPOSED")"
if [ -z "$want_commit" ]; then
  bad "the proposed list has no '# commit <sha>' line — it is not bound to a reviewed commit"
elif [ "$want_commit" != "$HEAD_SHA" ]; then
  bad "the proposed list is bound to commit $want_commit but the checkout is at $HEAD_SHA"
else
  note "PASS  the proposed list is bound to the checkout commit"
fi

# --- hashes, recomputed from disk with the system tool ----------------------
n_ok=0
while IFS= read -r tag; do
  [ -n "$tag" ] || continue
  file="$MIG_DIR/$tag.sql"
  if [ ! -r "$file" ]; then
    bad "journal entry $tag has no SQL file at $file"
    continue
  fi
  got="$(/usr/bin/shasum -a 256 "$file" | awk '{print $1}')"
  want="$(awk -v t="$tag" '!/^[[:space:]]*(#|$)/ && $1==t {print $2; exit}' "$PROPOSED")"
  if [ -z "$want" ]; then
    bad "migration $tag is missing from the proposed list"
  elif [ "$want" != "$got" ]; then
    bad "migration $tag hash mismatch — proposed $want, file is $got (content changed after approval?)"
  else
    note "PASS  $tag  $got"
    n_ok=$((n_ok + 1))
  fi
done <<EOF
$(jq -r '.entries[].tag' "$JOURNAL")
EOF

# --- entries in the list that the journal does not know about ---------------
while read -r tag _; do
  [ -n "$tag" ] || continue
  if ! jq -e --arg t "$tag" '.entries[] | select(.tag==$t)' "$JOURNAL" >/dev/null 2>&1; then
    # Harmless (the runner only reads journal tags) but worth saying out loud:
    # a stale line usually means the list was pasted from a different commit.
    note "WARN  '$tag' is in the proposed list but not in the journal"
  fi
done < <(awk '!/^[[:space:]]*(#|$)/ {print $1}' "$PROPOSED")

note ""
if [ "$fail" -eq 0 ]; then
  note "RESULT: PASS — $n_ok migration(s) match the checkout and the list is bound to $HEAD_SHA"
  note ""
  note "Read the SQL diff before installing this list: owner rights run exactly this content."
  note "  git -C $REPO diff HEAD~1..HEAD -- apps/$SLUG/drizzle/migrations"
else
  printf 'RESULT: FAIL — do not install this approval list\n' >&2
fi
exit "$fail"
