#!/usr/bin/env bash
# Print the exact approval lines for a checkout, so reviewing a new migration is
# mechanical instead of error-prone.
#
#   scripts/print-approvals.sh <path-to-checkout> <slug>
#
# Emits "<tag> <sha256>" for every journal entry of
# <checkout>/apps/<slug>/drizzle/migrations, in journal order — paste the result
# into /usr/local/libexec/studio-deploy/approved-migrations/<slug>.txt after
# reading the SQL diff. Nothing is written automatically and no credential is
# read; this only prints hashes of files that are already on disk.
set -euo pipefail

REPO="${1:-}"
SLUG="${2:-}"
[ -n "$REPO" ] && [ -d "$REPO" ] || { echo "usage: $0 <path-to-checkout> <slug>" >&2; exit 2; }
[ -n "$SLUG" ] || { echo "usage: $0 <path-to-checkout> <slug>" >&2; exit 2; }

MIG_DIR="$REPO/apps/$SLUG/drizzle/migrations"
JOURNAL="$MIG_DIR/meta/_journal.json"
[ -r "$JOURNAL" ] || { echo "no migration journal at $JOURNAL" >&2; exit 1; }

digest() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi
}

printf '# %s — approved migrations for %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SLUG"
printf '# review the SQL diff before pasting: owner rights run exactly this content\n'
jq -r '.entries[].tag' "$JOURNAL" | while IFS= read -r tag; do
  file="$MIG_DIR/$tag.sql"
  [ -r "$file" ] || { echo "journal entry $tag has no SQL file $file" >&2; exit 1; }
  printf '%s %s\n' "$tag" "$(digest "$file")"
done
