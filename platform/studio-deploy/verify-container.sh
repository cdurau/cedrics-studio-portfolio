#!/usr/bin/env bash
# Trusted container-facts helper — root-owned, argument-FREE, read-only.
#
# Install target  : /usr/local/libexec/studio-deploy/<slug>-verify-container (root:wheel 0755)
# Canonical source: platform/studio-deploy/verify-container.sh (planning repo)
#
# WHY THIS EXISTS (S0-13, dedicated-deploy-user model)
# ---------------------------------------------------
# The gate now runs as the dedicated deploy user (`studio-deploy`) and holds NO
# Docker access at all. On this host that is necessary and not a choice: the
# Docker socket is `login-user:login-group` mode 600, so giving the deploy user the socket
# would either hand it the login user's credentials or require adding it to a group that
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

DOCKER_CONTEXT="devbox"
DEFAULT_TRUST_DIR="/usr/local/libexec/studio-deploy"

APP_UUID_FILE="$STATE_DIR/coolify_uuid.txt"
APP_ID_FILE="$STATE_DIR/coolify_app_id.txt"

die() { printf '[verify-container] FAIL: %s\n' "$*" >&2; exit 1; }

# A source file that still carries a placeholder was never installed properly.
case "$SLUG$TRUST_DIR$STATE_DIR$DOCKER_BIN" in
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
# replacing it would replace what this helper runs as root. On this host
# /usr/bin/docker is owned by the login user, which is exactly why the
# literal is not allowed to be one — refuse loudly instead of running it.
#
# Ownership is enforced on the PRODUCTION path (TRUST_DIR at its literal default),
# for the same reason the gate relaxes its component-ownership check there: the
# offline suites run without root and cannot stage a root-owned stand-in, so
# root-ownership is an install-time property that only the installed layout can
# assert. Group/other-WRITE is refused unconditionally, because that is a real
# property of any file the suite can build.
[ -x "$DOCKER_BIN" ] || die "no docker binary at $DOCKER_BIN"
owner="$(stat -f '%u' "$DOCKER_BIN" 2>/dev/null || stat -c '%u' "$DOCKER_BIN")"
if [ "$TRUST_DIR" = "$DEFAULT_TRUST_DIR" ]; then
  [ "$owner" = "0" ] || die "$DOCKER_BIN is owned by uid $owner, expected root — refusing to execute a replaceable binary as root"
fi
perms="$(stat -f '%Lp' "$DOCKER_BIN" 2>/dev/null || stat -c '%a' "$DOCKER_BIN")"
case "$perms" in
  *[2367]|*[2367]?) die "$DOCKER_BIN has mode $perms — must not be writable by group/other" ;;
esac

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

d() { "$DOCKER_BIN" --context "$DOCKER_CONTEXT" "$@"; }

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
