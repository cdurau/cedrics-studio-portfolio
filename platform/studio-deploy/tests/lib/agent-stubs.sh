#!/usr/bin/env bash
# Shared wiring for the S0-13 suites: root-owned helper stand-ins.
#
# Under the dedicated-deploy-user model the gate holds NO Docker access. It
# reaches exactly two root-owned, ARGUMENT-FREE helpers through `sudo -n`:
#
#   sudo -n <slug>-migrate-runner      (one Docker write: the owner migration)
#   sudo -n <slug>-verify-container    (read-only container ps/inspect)
#
# The suites cannot install root-owned files, so they stage STAND-IN helpers with
# the same installed names and the same argument-free shape:
#
#   * the verify-container stand-in is the docker stand-in itself, substituted so
#     that DOCKER_BIN is the docker stand-in and the app identity is the suite's
#     state dir — i.e. the real script, really run, only with a stubbed Docker;
#   * the migrate-runner stand-in is the real runner, substituted the same way;
#   * the sudo stand-in (fake-sudo.sh) drops `-n` and execs the target, so the
#     call shape is exercised without root.
#
# Sourcing this file gives the caller:
#   stage_docker_stdin <dst>     the docker stand-in as an executable
#   stage_helpers <trust> <state> <clone> <docker_bin> <slug>
#   gate_env                     the environment assignment prefix for a run
set -euo pipefail

STUDIO_AGENT_HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STUDIO_AGENT_TESTS_DIR="$(cd "$STUDIO_AGENT_HARNESS_DIR/.." && pwd)"

# The docker stand-in, staged as an executable copy. A rewrite of the source file
# must not be able to turn every case into a vacuous pass, so the copy is checked
# executable before any result is trusted.
stage_docker_stdin() { # stage_docker_stdin <dst>
  cp "$STUDIO_AGENT_TESTS_DIR/fake-docker.sh" "$1"
  chmod 755 "$1"
  [ -x "$1" ] || { printf 'could not stage the docker stand-in at %s\n' "$1" >&2; return 2; }
}

# Build the two installed-name helpers under <trust>, each substituted against the
# docker stand-in and the suite's state dir.
stage_helpers() { # stage_helpers <trust> <state> <clone> <docker_bin> <slug>
  local trust="$1" state="$2" clone="$3" docker="$4" slug="$5"
  mkdir -p "$trust/approved-migrations"
  build_verify_container "$trust/$slug-verify-container" "$slug" "$trust" "$state" "$docker"
  build_runner          "$trust/$slug-migrate-runner"   "$slug" "$trust" "$state" "$clone" "$docker"
}

# The environment a suite sets for a run. FAKE_DOCKER_SCENARIO belongs to the test
# double, never to the gate: the gate reads no scenario, it only runs the helper.
gate_env() { # gate_env <scenario-file>
  printf 'FAKE_DOCKER_SCENARIO=%s' "$1"
}
