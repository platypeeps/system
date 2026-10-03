#!/bin/sh
# Bounded reads of configured paths, for a volume a launchd job can stop
# reading without failing (sd:2541).
# Usage: volume-probe.sh status|check|test|help
#
# From 2026-10-01 15:50 to 07:31 the next morning every launchd job that
# opened a file under the external volume waited, while interactive sessions
# wrote to the same volume (sd:2537). Nothing reported it: each job ran out
# its own timeout, one at a time. This reads each configured path in a child
# that gets VOLUME_PROBE_TIMEOUT seconds, and names any path whose read is
# still waiting at the bound.
#
# TCC attributes a read to the executing binary, and a launchd job cannot
# answer a prompt (.claude/rules/macos-tcc.md). So the answer belongs to the
# context that runs this: from a terminal it is the terminal's access. The
# `status` that local-health-check runs from its launchd job is the answer
# that matters.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/../lib/config.sh"

TOOL=volume-probe

usage() {
  cat <<'USAGE'
volume-probe.sh — bounded reads of configured paths on an external volume.

Usage: volume-probe.sh <command>

  status   One line. Exits 0 when every path answered within the bound,
           3 when VOLUME_PROBE_PATHS is not set, 1 when a path was still
           waiting at the bound, is missing, or refused the read --
           local-health-check reads these codes.
  check    One line per path, then the status line, with the same exit codes.
  test     Run this folder's tests (extra args go to unittest).
  help     This text.

Configuration: $SYSTEM_TOOLS_CONFIG/volume-probe/.env or exported (copy
.env.example).
  VOLUME_PROBE_PATHS     paths to read, separated by ':'. A directory is
                         listed with ls; a file has its first byte read.
  VOLUME_PROBE_TIMEOUT   whole seconds each read may take (1-60, default 5).
                         Keep paths x timeout under the health check's
                         30-second status bound.
  VOLUME_PROBE_READER    optional command that reads one path, given as its
                         last argument, for a binary whose own grant is in
                         question (TCC grants are per binary).

The answer belongs to the context that runs it: run from a terminal, it
reports the terminal's access, not a launchd job's.
USAGE
}

# probe <path>: read one path in a bounded child; set vp_line to one line and
# vp_state to ok|blocked|missing|refused.
probe() {
  vp_path=$1
  vp_err="$vp_tmp/err"
  : > "$vp_err"
  # The child execs the reader, so the pid waited on and killed is the
  # process doing the read. The existence test runs inside the child too: on
  # a volume nobody can read, a stat can wait as long as an open.
  sh -c '
    [ -e "$1" ] || exit 66
    if [ -n "$2" ]; then exec $2 "$1"; fi
    if [ -d "$1" ]; then exec ls -a "$1"; fi
    exec head -c 1 "$1"
  ' volume-probe "$vp_path" "${VOLUME_PROBE_READER:-}" >/dev/null 2>"$vp_err" </dev/null &
  vp_pid=$!
  vp_waited=0
  while kill -0 "$vp_pid" 2>/dev/null; do
    [ "$vp_waited" -ge "$vp_timeout" ] && break
    sleep 1
    vp_waited=$((vp_waited + 1))
  done
  if kill -0 "$vp_pid" 2>/dev/null; then
    kill -TERM "$vp_pid" 2>/dev/null || true
    sleep 1
    kill -KILL "$vp_pid" 2>/dev/null || true
    sleep 1
    vp_state=blocked
    if kill -0 "$vp_pid" 2>/dev/null; then
      # Never `wait` on it: a read stuck in the kernel would hold this
      # status past the health check's bound.
      vp_line="blocked: $vp_path still waiting after ${vp_timeout}s; pid $vp_pid survived KILL"
    else
      wait "$vp_pid" 2>/dev/null || true
      vp_line="blocked: $vp_path still waiting after ${vp_timeout}s"
    fi
    return 0
  fi
  vp_rc=0
  wait "$vp_pid" || vp_rc=$?
  case "$vp_rc" in
    0) vp_state=ok; vp_line="ok: $vp_path" ;;
    66) vp_state=missing; vp_line="missing: $vp_path does not exist" ;;
    *) vp_state=refused
       vp_line="refused: $vp_path (exit $vp_rc: $(head -n 1 "$vp_err"))" ;;
  esac
}

# run_probes <verbose>: probe every configured path; exit 0, 1 or 3.
run_probes() {
  st_source_env "$TOOL"
  if [ -z "${VOLUME_PROBE_PATHS:-}" ]; then
    echo "volume-probe: not configured (VOLUME_PROBE_PATHS is not set)"
    return 3
  fi
  vp_timeout=${VOLUME_PROBE_TIMEOUT:-5}
  case "$vp_timeout" in
    ''|*[!0-9]*) vp_timeout=bad ;;
  esac
  if [ "$vp_timeout" = bad ] || [ "$vp_timeout" -lt 1 ] || [ "$vp_timeout" -gt 60 ]; then
    echo "volume-probe: FAIL: VOLUME_PROBE_TIMEOUT must be whole seconds from 1 to 60"
    return 1
  fi
  vp_tmp=$(mktemp -d "${TMPDIR:-/tmp}/volume-probe.XXXXXX")
  vp_count=0
  vp_bad=""
  vp_rest=$VOLUME_PROBE_PATHS
  while :; do
    case "$vp_rest" in
      *:*) vp_one=${vp_rest%%:*}; vp_rest=${vp_rest#*:} ;;
      *) vp_one=$vp_rest; vp_rest="" ;;
    esac
    if [ -n "$vp_one" ]; then
      vp_count=$((vp_count + 1))
      # The shell reports a killed child on stderr; the line says it better.
      probe "$vp_one" 2>/dev/null
      [ "$1" = verbose ] && echo "$vp_line"
      [ "$vp_state" = ok ] || vp_bad="${vp_bad:+$vp_bad; }$vp_line"
    fi
    [ -n "$vp_rest" ] || break
  done
  rm -rf "$vp_tmp"
  if [ -n "$vp_bad" ]; then
    echo "volume-probe: FAIL: $vp_bad"
    return 1
  fi
  echo "volume-probe: ok: $vp_count path(s) answered within ${vp_timeout}s"
  return 0
}

case "${1:-}" in
  status) [ "$#" -eq 1 ] || { usage >&2; exit 1; }; run_probes quiet || exit $? ;;
  check) [ "$#" -eq 1 ] || { usage >&2; exit 1; }; run_probes verbose || exit $? ;;
  test) shift; exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage ;;
  *) usage >&2; exit 1 ;;
esac
