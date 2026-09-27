#!/usr/bin/env bash
# Test harness for the trusted studio-deploy components.
#
# WHY THE TESTS NO LONGER SET ENVIRONMENT VARIABLES ON THE REAL SCRIPT
# -------------------------------------------------------------------
# Third review round, finding A. The production gate and migration runner now use
# a LITERAL trust path: every path, binary and image is a placeholder that the
# installing owner substitutes exactly once, and no environment variable is
# consulted for any security-relevant decision. That is only a real property if
# the test suites stop relying on the override they used to depend on.
#
# So the suites never execute the canonical source. They execute a SUBSTITUTED
# COPY (this file's subst_script) with the placeholders filled in. Test injection
# is therefore a property of the copy, and cannot be activated on the production
# path by any caller, environment variable or flag — the installed file simply no
# longer reads one.
#
# subst_script refuses to produce a copy that still contains a placeholder, so a
# new placeholder added to a component without updating this harness fails loudly
# instead of silently testing an unsubstituted script.
#
# S0-13 (dedicated deploy user): the gate now runs as the deploy account and holds
# no Docker access; it reaches the two root-owned helpers through `sudo -n`. The
# suites substitute DEPLOY_USER with the account name the test actually runs as
# (so the gate's identity check can be exercised both ways) and SUDO_BIN with the
# fake-sudo stand-in below.
#
# The identity is a NAME, not a number. No uid is hard-coded in the components or
# in these suites: the uid is resolved by the OS at install time, so a suite that
# depended on a recorded uid would be asserting an install-time choice that may change. A
# test proves refusal by substituting a DIFFERENT account name, which is the same
# check in uid-agnostic form.
set -euo pipefail

HARNESS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STUDIO_DEPLOY_DIR="$(cd "$HARNESS_LIB_DIR/../.." && pwd)"

SRC_GATE="$STUDIO_DEPLOY_DIR/deploy-gate.sh"
SRC_HOOK="$STUDIO_DEPLOY_DIR/deploy-hook.sh"
SRC_RUNNER="$STUDIO_DEPLOY_DIR/migrate-runner.sh"
SRC_VERIFY="$STUDIO_DEPLOY_DIR/verify-container.sh"
SRC_PRINT_APPROVALS="$STUDIO_DEPLOY_DIR/print-approvals.sh"

# The digest-pinned postgres image the runner ships with. Tests substitute it so
# no live pull is ever needed; the value is only ever compared as a string.
PINNED_PSQL_IMAGE="postgres:18-alpine@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"

# The account the suites run as. Substituted for DEPLOY_USER so a test can prove
# the gate's identity check (and then override it to a different name to prove the
# refusal).
CURRENT_UID="$(id -u)"
CURRENT_USER="$(id -un)"

subst_script() { # subst_script <src> <dst> <KEY=value>...
  local src="$1" dst="$2"; shift 2
  [ -r "$src" ] || { printf 'harness: missing source %s\n' "$src" >&2; return 2; }
  cp "$src" "$dst"
  local pair key val esc tmp
  for pair in "$@"; do
    key="${pair%%=*}"
    val="${pair#*=}"
    # Escape the sed replacement metacharacters and the delimiter.
    esc="$(printf '%s' "$val" | sed -e 's/[\\&|]/\\&/g')"
    tmp="$dst.tmp.$$"
    sed -e "s|__${key}__|${esc}|g" "$dst" > "$tmp" || { rm -f "$tmp"; return 1; }
    mv "$tmp" "$dst"
  done
  local remaining
  remaining="$(grep -o '__[A-Z][A-Z_]*__' "$dst" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
  if [ -n "$remaining" ]; then
    printf 'harness: unsubstituted placeholder(s) in %s: %s\n' "$dst" "$remaining" >&2
    return 1
  fi
  chmod 755 "$dst"
}

# COOLIFY_API defaults to a dead local port for the suites. No test may ever
# contact the live Coolify API: on the host it listens on localhost:8000, so a
# test that reached it would trigger a REAL deployment of the branch tip.
DEAD_COOLIFY_API="http://127.0.0.1:1/api/v1"

# The sudo stand-in: it drops `-n` and execs the rest, so the suites exercise the
# helper-invocation shape without needing root. A test can point SUDO_FAKE_REFUSE
# at a file to make it refuse like an unpermitted sudoers entry would.
stage_fake_sudo() { # stage_fake_sudo <dst>
  cp "$HARNESS_LIB_DIR/fake-sudo.sh" "$1"
  chmod 755 "$1"
}

build_gate() { # build_gate <dst> <slug> <trust_dir> <state_dir> <clone> [docker_bin] [h] [p] [api] [deploy_user] [sudo]
  local dst="$1" slug="$2" trust="$3" state="$4" clone="$5"
  local sudo_bin="${11:-}"
  if [ -z "$sudo_bin" ]; then
    sudo_bin="${dst}.fake-sudo"
    stage_fake_sudo "$sudo_bin"
  fi
  # The docker_bin parameter is retained for call-site compatibility. The gate no
  # longer invokes Docker at all — it reaches Docker only through the helpers — so
  # the value is deliberately unused here.
  subst_script "$SRC_GATE" "$dst" \
    "APP_SLUG=$slug" "TRUST_DIR=$trust" "STATE_DIR=$state" "GATE_REPO=$clone" \
    "DEPLOY_USER=${10:-$CURRENT_USER}" "SUDO_BIN=$sudo_bin" \
    "DEPLOY_TIMEOUT=900" "HEALTH_TIMEOUT=${7:-1}" "POLL_INTERVAL=${8:-1}" \
    "COOLIFY_API=${9:-$DEAD_COOLIFY_API}"
}

build_runner() { # build_runner <dst> <slug> <trust_dir> <state_dir> <clone> <docker_bin> [image]
  subst_script "$SRC_RUNNER" "$1" \
    "APP_SLUG=$2" "TRUST_DIR=$3" "STATE_DIR=$4" "GATE_REPO=$5" "DOCKER_BIN=$6" \
    "DOCKER_HOST=${FAKE_DOCKER_HOST:-unix:///run/fake-docker.sock}" \
    "PSQL_IMAGE=${7:-$PINNED_PSQL_IMAGE}"
}

build_verify_container() { # build_verify_container <dst> <slug> <trust_dir> <state_dir> <docker_bin>
  subst_script "$SRC_VERIFY" "$1" \
    "APP_SLUG=$2" "TRUST_DIR=$3" "STATE_DIR=$4" "DOCKER_BIN=$5" \
    "DOCKER_HOST=${FAKE_DOCKER_HOST:-unix:///fake.sock}"
}

build_hook() { # build_hook <dst> <slug> <trust_dir> <clone> [deploy_user]
  local dst="$1" slug="$2" trust="$3" clone="$4"
  local user="${5:-$CURRENT_USER}"
  subst_script "$SRC_HOOK" "$dst" \
    "APP_SLUG=$slug" "TRUST_DIR=$trust" "GATE_REPO=$clone" "DEPLOY_USER=$user"
}

sha256_file() { # sha256_file <file>
  /usr/bin/shasum -a 256 "$1" | awk '{print $1}'
}
