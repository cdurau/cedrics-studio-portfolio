#!/usr/bin/env bash
# Print, for review, the tag + file + current sha256 of every migration entry in
# a checkout — the mechanical part of approving owner-migration SQL.
#
# Gate review 2026-09-21, third round — finding C.
#
# This is a REVIEW AID, not an authority. Its output is only worth what the
# human's own reading of the SQL is worth, because the script lives in the
# agent-writable planning repo. Two guards keep the trust chain honest:
#
#   * it prints the FILENAME next to each hash, so the diff and the hash can be
#     read side by side;
#   * the file it prints for installation carries a "# commit <sha>" line, and
#     the deploy gate refuses an approval list whose commit line is not the
#     commit being deployed.
#
# The independent check of the finished list is verify-approvals.sh, which
# recomputes every hash with /usr/bin/shasum; the human can equally well run
# `/usr/bin/shasum -a 256 *.sql` in the migrations directory and compare by eye.
#
# Usage: find-approvals.sh <path-to-checkout> <slug> [--for-install]
set -euo pipefail

PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

REPO="${1:-}"
SLUG="${2:-}"
MODE="${3:-}"
[ -n "$REPO" ] && [ -d "$REPO" ] || { echo "usage: $0 <path-to-checkout> <slug> [--for-install]" >&2; exit 2; }
[ -n "$SLUG" ] || { echo "usage: $0 <path-to-checkout> <slug> [--for-install]" >&2; exit 2; }

MIG_DIR="$REPO/apps/$SLUG/drizzle/migrations"
JOURNAL="$MIG_DIR/meta/_journal.json"
[ -r "$JOURNAL" ] || { echo "no migration journal at $JOURNAL" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq is required to read the migration journal" >&2; exit 1; }

COMMIT="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"

emit() { # emit <tag> <file>
  local tag="$1" file="$2"
  [ -r "$file" ] || { echo "journal entry $tag has no SQL file $file" >&2; exit 1; }
  printf '%s\t%s\t%s\n' "$tag" "$(basename "$file")" "$(/usr/bin/shasum -a 256 "$file" | awk '{print $1}')"
}

if [ "$MODE" = "--for-install" ]; then
  # The installable form. The commit line is what binds "a human read this SQL"
  # to the artifact the gate consumes.
  printf '# %s — approved migrations for %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SLUG"
  printf '# commit %s\n' "$COMMIT"
  printf '# review the SQL diff before installing: owner rights run exactly this content\n'
  jq -r '.entries[].tag' "$JOURNAL" | while IFS= read -r tag; do
    [ -n "$tag" ] || continue
    printf '%s %s\n' "$tag" "$(/usr/bin/shasum -a 256 "$MIG_DIR/$tag.sql" | awk '{print $1}')"
  done
  exit 0
fi

# Default: the review view — tag, filename and hash, plus what to read.
printf 'checkout: %s\ncommit:   %s\nsubject:  %s\n\n' \
  "$REPO" "$COMMIT" "$(git -C "$REPO" log -1 --format='%s' 2>/dev/null || echo '(unknown)')"
printf '%-38s %-34s %s\n' TAG FILE SHA256
jq -r '.entries[].tag' "$JOURNAL" | while IFS= read -r tag; do
  [ -n "$tag" ] || continue
  emit "$tag" "$MIG_DIR/$tag.sql"
done | awk -F'\t' '{printf "%-38s %-34s %s\n", $1, $2, $3}'
printf '\nread the SQL under %s before approving\n' "$MIG_DIR"
