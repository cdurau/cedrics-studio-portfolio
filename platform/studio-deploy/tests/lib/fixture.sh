#!/usr/bin/env bash
# Shared git fixture for the deploy-gate suites.
#
# Builds a small origin with the allowed branch plus the two decoys the ref
# binding must reject: a commit that exists locally on another branch, and one
# that is in no branch at all. Sourcing this file gives the caller:
#
#   fixture_origin, fixture_clone   absolute paths
#   FIX_TIP_SHA                     tip of migration/coolify-v5
#   FIX_OLD_SHA                     older commit that IS in the clone
#   FIX_SIDE_SHA                    commit on an unrelated branch
#   FIX_DANGLING_SHA                commit in no branch
#
# Written as a library so the ref-binding and trust-path suites exercise the same
# repository shape; a fixture that drifted between suites would make one of them
# prove something about a world the other never built.
set -euo pipefail

fixture_build() { # fixture_build <workdir> <slug> [with-migration-tree]
  local work="$1" slug="$2" with_mig="${3:-yes}"
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

  fixture_origin="$work/origin.git"
  local seed="$work/seed"
  git init -q --bare "$fixture_origin"
  git init -q "$seed"
  cd "$seed"

  mkdir -p "apps/$slug"
  printf 'v1\n' > "apps/$slug/file.txt"
  if [ "$with_mig" = "yes" ]; then
    mkdir -p "apps/$slug/drizzle/migrations/meta"
    printf 'CREATE TABLE t1 (id int);\n' > "apps/$slug/drizzle/migrations/0000_init.sql"
    cat > "apps/$slug/drizzle/migrations/meta/_journal.json" <<'JSON'
{"version":"7","dialect":"postgresql","entries":[{"idx":0,"version":"7","when":1,"tag":"0000_init"}]}
JSON
  fi
  git add -A && git commit -qm c1
  FIX_OLD_SHA="$(git rev-parse HEAD)"

  printf 'v2\n' > "apps/$slug/file.txt"
  git add -A && git commit -qm c2
  FIX_TIP_SHA="$(git rev-parse HEAD)"

  git branch -m migration/coolify-v5
  git remote add origin "$fixture_origin"
  git push -q origin migration/coolify-v5

  # A commit that exists only on an unrelated branch.
  git checkout -q -b side-branch
  printf 'side\n' > "apps/$slug/side.txt"
  git add -A && git commit -qm side
  FIX_SIDE_SHA="$(git rev-parse HEAD)"
  git push -q origin side-branch
  git checkout -q migration/coolify-v5

  # A commit that is in no branch at all (dangling after a reset).
  printf 'detached\n' > "apps/$slug/file.txt"
  git add -A && git commit -qm dangling
  FIX_DANGLING_SHA="$(git rev-parse HEAD)"
  git reset -q --hard "$FIX_TIP_SHA"

  # --- clone the way the gate expects (its own clone, detached-checkoutable) --
  fixture_clone="$work/deploy-clone"
  git clone -q --branch migration/coolify-v5 "$fixture_origin" "$fixture_clone"
  cd "$work"
}
