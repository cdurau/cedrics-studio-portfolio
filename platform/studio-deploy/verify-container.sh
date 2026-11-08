#!/usr/bin/env bash
# Trusted container-facts helper — root-owned, argument-FREE, read-only.
#
# Install target  : /usr/local/libexec/studio-deploy/<slug>-verify-container (root:wheel 0755)
# Canonical source: platform/studio-deploy/verify-container.sh (planning repo)
#
# WHY THIS EXISTS (S0-13, dedicated-deploy-user model)
# ---------------------------------------------------
# The gate now runs as the dedicated deploy user (`studio-deploy`) and holds NO
# Docker access at all. That is necessary and not a choice: the Docker socket is
# owned by the login user, mode 600, so giving the deploy user the socket would
# either hand it the login user's credentials or require adding it to a group that
# reaches an owner-capable daemon — i.e. it would re-flatten the uid boundary the
# deploy user exists to create.
#
# So exactly one Docker capability is delegated, through sudo, to this helper:
# READ-ONLY `ps` + `inspect` for the app identity pinned in the state directory.
# The owner-migration write is the separate <slug>-migrate-runner helper. Neither
# helper is the Docker socket, and neither accepts an argument.
#
# THE HELPER DECIDES NOTHING
# --------------------------
# It prints the facts (name, health, image, commit label, name label, app-id
# label) and exits. The app/commit/health binding — which is the part finding 3
# was about — stays in the gate. That split is why this helper can be
# argument-free: there is no expected SHA to pass it, so there is no argument to
# launder. A caller who can execute this file still cannot make it inspect
# anything other than the pinned app, and cannot make it return a verdict.
#
# It takes NO ARGUMENTS. The sudoers entry uses the empty-argument form
# (`... <slug>-verify-container ""`, sudoers(5): "the command may only be run
# with no arguments"), so sudo refuses any argv before this script is reached.
# The checks below are defence in depth, not the boundary.
#
# READ-ONLY BY CONSTRUCTION: only `ps` and `inspect` are ever invoked. There is
# no `run`, no `exec`, no `rm`, no `system`, and no subcommand is taken from
# input, so this helper cannot be turned into a mutation surface.
set -euo pipefail

# --- the literal trust path (substituted once at install) -------------------
SLUG="__APP_SLUG__"
TRUST_DIR="__TRUST_DIR__"
STATE_DIR="__STATE_DIR__"
DOCKER_BIN="__DOCKER_BIN__"

# The socket is pinned as an install-time LITERAL, passed with docker's -H flag
# (which overrides any DOCKER_HOST in the environment). A `--context devbox` pin
# resolves contexts under $HOME/.docker — under sudo that is the deploy user's
# home, where the production context does not exist (measured live 2026-09-27:
# `context "devbox": context not found`), and a context file there would be a
# deploy-user-writable input to a root process — the same defect class as the
# login-user-writable docker binary found on 2026-09-22.
DOCKER_HOST="__DOCKER_HOST__"
DEFAULT_TRUST_DIR="/usr/local/libexec/studio-deploy"

APP_UUID_FILE="$STATE_DIR/coolify_uuid.txt"
APP_ID_FILE="$STATE_DIR/coolify_app_id.txt"

die() { printf '[verify-container] FAIL: %s\n' "$*" >&2; exit 1; }

# --- trusted-path predicates (lstat-correct, symlink-free) ------------------
# `stat` uses lstat semantics: it measures the SYMLINK, not the file that is
# actually opened or executed. That made the previous ownership check
# bypassable on this very host — /usr/local/bin/docker is a root:wheel symlink
# whose payload lives in /Applications/Docker.app and is owned by the LOGIN
# USER, so `stat -f '%u'` returned 0 ("root-owned, trusted") while root would
# have executed an agent-writable binary. Measured 2026-09-22.
#
# Two independent corrections, because neither alone is the property wanted:
#   1. a trusted path must not BE a symlink — refused outright, there is no
#      legitimate reason for one on an installed trust path; and
#   2. every check uses `stat -L`, so if a link ever does appear, the property
#      measured is the one that decides what actually runs.
# GNU stat treats -f as --file-system and exits 0, so a BSD-format attempt
# cannot be the probe. Choose the format flag this stat accepts. Both forms
# follow links. A result that is not an octal mode or a numeric uid is a
# failed check, not a pass.
# Closing braces stay on the statement line. tests/trusted-path.sh treats the
# first line that is only "}" as the end of this whole copied block.
# Detect at call time. These scripts pin PATH after this block is defined, so
# a probe at load time can see a different stat than the check itself.
file_mode() {
  local mode
  if stat -c '%u' / >/dev/null 2>&1; then mode="$(stat -L -c '%a' "$1")" || die "cannot stat mode of $1"
  else mode="$(stat -L -f '%Lp' "$1")" || die "cannot stat mode of $1"; fi
  case "$mode" in ""|*[!0-7]*) die "stat did not return an octal mode for $1" ;; esac
  printf '%s\n' "$mode"; }
file_owner() {
  local owner
  if stat -c '%u' / >/dev/null 2>&1; then owner="$(stat -L -c '%u' "$1")" || die "cannot stat owner of $1"
  else owner="$(stat -L -f '%u' "$1")" || die "cannot stat owner of $1"; fi
  case "$owner" in ""|*[!0-9]*) die "stat did not return a numeric uid for $1" ;; esac
  printf '%s\n' "$owner"; }

# Ownership of the FILE is not enough: a root-owned file inside a directory the
# agent can write can be replaced wholesale (unlink + create), so the guarantee
# has to cover the whole chain from `/` down — every component root-owned and
# never group/other-writable. The same rule applies to the host's sudoers
# targets. A passwordless sudo rule must not point at a path the login user can replace.
assert_trusted_path() { # assert_trusted_path <absolute path>
  local p="$1" owner mode
  case "$p" in
    /*) : ;;
    *) die "trusted path must be absolute: $p" ;;
  esac
  while : ; do
    [ ! -h "$p" ] || die "$p is a symlink — a trusted path must not be one"
    owner="$(file_owner "$p")"
    mode="$(file_mode "$p")"
    [ "$owner" = "0" ] \
      || die "$p is owned by uid $owner, expected root — the trusted path is replaceable"
    case "$mode" in
      *[2367]|*[2367]?) die "$p has mode $mode — must not be writable by group/other" ;;
    esac
    [ "$p" != "/" ] || break
    p="${p%/*}"
    [ -n "$p" ] || p="/"
  done
}

# A source file that still carries a placeholder was never installed properly.
case "$SLUG$TRUST_DIR$STATE_DIR$DOCKER_BIN$DOCKER_HOST" in
  *__*) die "unsubstituted placeholder — this is the canonical source, not an installed component" ;;
esac

# --- no arguments, ever -----------------------------------------------------
# The installed sudoers form already forbids argv; this is the second lock. A
# helper that accepted a path, an image, a container name or a SHA would be an
# argument-laundering step for whoever can execute it — including the deploy user
# the gate itself runs as.
[ "$#" -eq 0 ] || die "this helper takes no arguments (got $#) — the app identity is pinned in $STATE_DIR"

# --- deterministic tools, no caller environment -----------------------------
# The trusted path must not depend on the caller's environment. PATH is fixed to
# the system directories; HOME is resolved from the OS. DOCKER_BIN is a literal.
PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

# A docker binary that is writable by a non-root user is not a trusted binary:
# replacing it would replace what this helper runs as root. MEASURED on this
# host (2026-09-22), neither installed docker CLI qualifies:
#   /usr/bin/docker  -> symlink, owned by the login user
#   /usr/local/bin/docker     -> root:wheel SYMLINK into /Applications/Docker.app,
#                                whose payload is owned by the login user and writable
# The second one is why the check below is not merely an ownership test: the old
# `stat -f '%u'` measured the link (uid 0) and PASSED, while root would have
# executed a login-user-writable binary. Hence the README pins a root-owned COPY
# installed under the trust directory, and the checks here refuse a symlink
# outright and use `stat -L`.
#
# The full path-chain and root-ownership assertion runs on the PRODUCTION path
# (TRUST_DIR at its literal default), for the same reason the gate relaxes its
# component-ownership check there: the offline suites run without root and
# cannot stage a root-owned stand-in. The symlink refusal and the group/other-
# WRITE refusal are unconditional, because those are real properties of any file
# the suite can build.
[ -x "$DOCKER_BIN" ] || die "no docker binary at $DOCKER_BIN"

# Unconditional, production path or not: a symlinked trusted binary is refused,
# because that is precisely the shape that defeated the previous check. Neither
# docker CLI on this host qualifies as a trusted binary on its own — the
# Homebrew one is login-user-owned, and the /usr/local one is a root symlink to
# a login-user-owned payload — so the README pins a ROOT-OWNED COPY installed
# under the trust directory instead.
[ ! -h "$DOCKER_BIN" ] \
  || die "$DOCKER_BIN is a symlink — pin the root-owned copy under $DEFAULT_TRUST_DIR (see the DOCKER_BIN section of the README)"
perms="$(file_mode "$DOCKER_BIN")"
case "$perms" in
  *[2367]|*[2367]?) die "$DOCKER_BIN has mode $perms — must not be writable by group/other" ;;
esac
if [ "$TRUST_DIR" = "$DEFAULT_TRUST_DIR" ]; then
  # Production path: the whole chain, not merely the last element.
  assert_trusted_path "$DOCKER_BIN"
fi

# --- the pinned app identity ------------------------------------------------
[ -r "$APP_UUID_FILE" ] || die "missing app identity file $APP_UUID_FILE"
APP_UUID="$(tr -d '[:space:]' < "$APP_UUID_FILE")"
[ -n "$APP_UUID" ] || die "app identity file $APP_UUID_FILE is empty"
# The UUID reaches a docker filter and a format string; keep it to a shape that
# cannot express anything else.
case "$APP_UUID" in
  *[!0-9a-zA-Z_-]*) die "app identity '$APP_UUID' has an unexpected shape — refusing" ;;
esac

APP_ID=""
if [ -r "$APP_ID_FILE" ]; then
  APP_ID="$(tr -d '[:space:]' < "$APP_ID_FILE")"
  case "$APP_ID" in
    *[!0-9]*) die "app id '$APP_ID' is not numeric — refusing (it is compared as a docker label)" ;;
  esac
fi

d() { "$DOCKER_BIN" -H "$DOCKER_HOST" "$@"; }

# --- print the facts, decide nothing ----------------------------------------
# Output: one line per container carrying the pinned app's coolify.name label.
#   <name>\t<health>\t<image>\t<commit-label>\t<name-label>\t<appid-label>
# Tab-separated so empty labels stay distinguishable; the gate splits on tab and
# applies the binding (name == UUID, appid == APP_ID when on file, FULL sha in
# image/commit label, health == healthy).
names="$(d ps --filter "label=coolify.name=$APP_UUID" --format '{{.Names}}' 2>/dev/null || true)"
found=0
for c in $names; do
  [ -n "$c" ] || continue
  info="$(d inspect -f '{{.State.Health.Status}}{{"\t"}}{{.Config.Image}}{{"\t"}}{{index .Config.Labels "coolify.commit"}}{{"\t"}}{{index .Config.Labels "coolify.name"}}{{"\t"}}{{index .Config.Labels "coolify.applicationId"}}' "$c" 2>/dev/null || true)"
  [ -n "$info" ] || continue
  printf '%s\t%s\n' "$c" "$info"
  found=1
done

[ "$found" -eq 1 ] || { echo "no container carries label coolify.name=$APP_UUID" >&2; exit 1; }
exit 0
