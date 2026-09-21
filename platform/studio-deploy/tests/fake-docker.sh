#!/usr/bin/env bash
# Minimal docker stand-in for the deploy-gate tests.
#
# The gate's verification logic is the part finding 3 is about (bind the healthy
# check to the target app AND the full commit). Proving that must not require a
# live deployment, and a live test could not deterministically present a healthy
# container of another app at the same commit. So the container world is a
# scenario file and the gate is exercised against it.
#
# Scenario file (TSV, one container per line):
#   name <tab> coolify.name <tab> coolify.applicationId <tab> image <tab> coolify.commit <tab> health
#
# Any unexpected `run` (i.e. something trying to reach a database) fails loudly
# with a distinct exit code, so a test can assert that no database contact
# happened at all.
set -euo pipefail

SCEN="${FAKE_DOCKER_SCENARIO:?FAKE_DOCKER_SCENARIO must point at a scenario file}"

# The gate invokes "<docker> --context <ctx> <subcommand> ..."; skip the leading
# global options before reading the subcommand.
args=("$@")
while [ "${#args[@]}" -gt 0 ]; do
  case "${args[0]}" in
    --context) args=("${args[@]:2}") ;;
    *) break ;;
  esac
done
cmd="${args[0]:-}"
args=("${args[@]:1}")

case "$cmd" in
  ps)
    filt=""
    i=0
    while [ "$i" -lt "${#args[@]}" ]; do
      case "${args[$i]}" in
        --filter) filt="${args[$((i+1))]#label=coolify.name=}"; i=$((i+2)) ;;
        --format) i=$((i+2)) ;;
        *) i=$((i+1)) ;;
      esac
    done
    awk -F'\t' -v f="$filt" '$2==f {print $1}' "$SCEN"
    ;;

  inspect)
    tmpl=""; name=""
    i=0
    while [ "$i" -lt "${#args[@]}" ]; do
      case "${args[$i]}" in
        -f) tmpl="${args[$((i+1))]}"; i=$((i+2)) ;;
        *) name="${args[$i]}"; i=$((i+1)) ;;
      esac
    done
    row="$(awk -F'\t' -v n="$name" '$1==n {print; exit}' "$SCEN")"
    [ -n "$row" ] || exit 1
    # Extract by field index rather than `read`: tab is IFS whitespace, so read
    # collapses empty fields and silently shifts every later column.
    f() { printf '%s' "$row" | awk -F'\t' -v i="$1" '{print $i}'; }
    c_image="$(f 4)"; c_commit="$(f 5)"; c_health="$(f 6)"
    case "$tmpl" in
      *State.Health.Status*)
        printf '%s|%s|%s|%s|%s\n' "$c_health" "$c_image" "$c_commit" "$(f 2)" "$(f 3)" ;;
      *) printf '%s\n' "$c_health" ;;
    esac
    ;;

  run)
    echo "FAKE-DOCKER: unexpected 'run' — something tried to reach the database" >&2
    exit 97
    ;;

  *)
    echo "FAKE-DOCKER: unsupported subcommand '$cmd'" >&2
    exit 98
    ;;
esac
