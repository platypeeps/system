#!/bin/sh
# Health probe for the host google_workspace_mcp server (LaunchAgent
# <prefix>.google-workspace-mcp, 127.0.0.1:8083). The server itself lives in
# ~/repos/ai/google_workspace_mcp; this folder only watches it. The server can
# stop answering while launchd still reports it running, so KeepAlive never
# restarts it: `status` asks the server itself, with an MCP initialize,
# and `watch` restarts it once when that goes unanswered.
# Usage: workspace-mcp.sh status|watch|test|help
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

URL="${WORKSPACE_MCP_URL:-http://127.0.0.1:8083/mcp}"
# launchd label prefix shared by every agent from this repository; override per machine.
LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
LABEL="${WORKSPACE_MCP_LABEL:-$LABEL_PREFIX.google-workspace-mcp}"
TIMEOUT="${WORKSPACE_MCP_TIMEOUT:-10}"
GRACE="${WORKSPACE_MCP_GRACE:-90}"
RESTART_WAIT="${WORKSPACE_MCP_RESTART_WAIT:-150}"
POLL="${WORKSPACE_MCP_POLL:-10}"
STATE_DIR="${WORKSPACE_MCP_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/workspace-mcp}"
NOTIFY="${WORKSPACE_MCP_NOTIFY:-$DIR/../local-notify/notify.sh}"
NOTIFY_TIMEOUT="${WORKSPACE_MCP_NOTIFY_TIMEOUT:-30}"

usage() {
  cat <<'HELPEOF'
usage: workspace-mcp.sh status|watch|test|help

  status   POST one MCP initialize to the server; the whole probe,
           handshake and close included, is bounded by
           WORKSPACE_MCP_TIMEOUT seconds. Exits 0 when it answers HTTP 200
           with an mcp-session-id header and then accepts
           notifications/initialized on that session (the session is
           closed with DELETE after); 3 when there is nothing to check
           (no launchctl, or the LaunchAgent is not loaded here); 1 when
           the agent is loaded and the server does not answer --
           local-health-check reads these codes. Read-only: it never
           restarts anything.
  watch    what the cron job runs. `status`; on 1, keep probing every
           WORKSPACE_MCP_POLL seconds for up to WORKSPACE_MCP_GRACE, since
           a server launchd is still starting needs about 90s. Only when
           every probe in that window fails, restart the agent once
           (launchctl kickstart -k) and poll the same way for up to
           WORKSPACE_MCP_RESTART_WAIT. Windows count sleeps; each probe
           adds up to WORKSPACE_MCP_TIMEOUT. Exits 0
           when healthy, recovered, or nothing to check (also when the
           agent is booted out mid-run: no restart, no notice); 1 when the restart
           did not bring it back. The first unrecovered run of an outage
           notifies through local-notify with -c local,ntfy: the email
           channel goes through this same server, so it cannot carry news
           of its outage. Later runs of the same outage restart again but
           do not notify again; the cron wrapper still reports each exit 1.
  test     run the unittest suite (tests/) against stub launchctl, curl
           and notify; extra arguments go to unittest (-v).

env:
  WORKSPACE_MCP_URL           endpoint (default http://127.0.0.1:8083/mcp;
                              notify.sh reads the same variable)
  WORKSPACE_MCP_LABEL         LaunchAgent label (default
                              $SYSTEM_TOOLS_LABEL_PREFIX.google-workspace-mcp;
                              the prefix defaults to local.system-tools)
  WORKSPACE_MCP_TIMEOUT       seconds per probe (default 10)
  WORKSPACE_MCP_GRACE         seconds to keep probing before a restart (default 90)
  WORKSPACE_MCP_RESTART_WAIT  seconds to wait after a restart (default 150)
  WORKSPACE_MCP_POLL          seconds between probes after a restart (default 10)
  WORKSPACE_MCP_STATE_DIR     where the one-alert-per-outage marker lives
                              (default $XDG_STATE_HOME/workspace-mcp,
                              ~/.local/state/workspace-mcp)
  WORKSPACE_MCP_NOTIFY        notifier (default ../local-notify/notify.sh)
  WORKSPACE_MCP_NOTIFY_TIMEOUT  seconds before a stalled notifier is killed
                              and retried next run (default 30)
HELPEOF
}

stamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }

# Prints one line of reason and returns the status code, so `watch` can reuse
# it without the exit. 4 means alive but the session close failed: `status`
# reports it as 1, and `watch` reports it without a restart.
probe() {
  if ! command -v launchctl >/dev/null 2>&1; then
    echo "workspace-mcp: nothing to check: no launchctl on this machine"
    return 3
  fi
  if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "workspace-mcp: nothing to check: $LABEL is not loaded"
    return 3
  fi
  # One deadline covers all three calls below, so the probe as a whole stays
  # within TIMEOUT (to the second) and inside local-health-check's 30s bound.
  deadline=$(( $(date +%s) + TIMEOUT ))
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/workspace-mcp.XXXXXX")
  code=$(curl -sS -X POST "$URL" -D "$tmp/hdr" -o "$tmp/body" \
    --max-time "$TIMEOUT" -w '%{http_code}' \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"workspace-mcp-probe","version":"1.0"}}}' \
    2>"$tmp/err") && rc=0 || rc=$?
  sid=""
  [ -f "$tmp/hdr" ] && sid=$(tr -d '\r' < "$tmp/hdr" | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')
  err=$(tr '\n' ' ' < "$tmp/err" | head -c 200)
  rm -rf "$tmp"
  if [ "$rc" -ne 0 ]; then
    echo "workspace-mcp: $LABEL is loaded but $URL did not answer initialize within ${TIMEOUT}s (curl exit $rc: $err)"
    return 1
  fi
  if [ "$code" != 200 ] || [ -z "$sid" ]; then
    echo "workspace-mcp: $URL answered initialize with HTTP $code and no session id"
    return 1
  fi
  # Finish the handshake on that session, as every MCP client must, then
  # close it: a probe every 10 minutes would otherwise leave a session
  # behind each time. The server answered 202 and 200 to these on 2026-09-26.
  left=$(( deadline - $(date +%s) ))
  if [ "$left" -lt 1 ]; then
    echo "workspace-mcp: $URL answered initialize but left no time within ${TIMEOUT}s for notifications/initialized"
    return 1
  fi
  ncode=$(curl -sS -o /dev/null -X POST "$URL" --max-time "$left" -w '%{http_code}' \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' 2>/dev/null) && rc=0 || rc=$?
  # With no time left the close is skipped: the bound wins over the cleanup.
  left=$(( deadline - $(date +%s) ))
  closed=skipped
  if [ "$left" -ge 1 ]; then
    dcode=$(curl -sS -o /dev/null -X DELETE "$URL" --max-time "$left" -w '%{http_code}' \
      -H "mcp-session-id: $sid" 2>/dev/null) && drc=0 || drc=$?
    closed=tried
  fi
  case "$rc:$ncode" in
    0:2??) ;;
    *)
      echo "workspace-mcp: $URL answered initialize but not notifications/initialized (curl exit $rc, HTTP $ncode)"
      return 1 ;;
  esac
  if [ "$closed" = skipped ]; then
    echo "workspace-mcp: healthy: $URL answered initialize, but the probe left its session open: no time within ${TIMEOUT}s to close it"
    return 0
  fi
  # A close that fails leaves a session behind on every probe. The MCP spec
  # lets a server answer 405 when clients may not end sessions: not a fault.
  case "$drc:$dcode" in
    0:2??|0:405) ;;
    *)
      # 4, not 1: the server is alive, so `watch` must not restart it.
      echo "workspace-mcp: $URL did not close its session (curl exit $drc, HTTP $dcode)"
      return 4 ;;
  esac
  echo "workspace-mcp: healthy: $URL answered initialize"
  return 0
}

# Probe every POLL seconds, at most LIMIT / POLL times; returns 0 on the
# first answer, and 3 at once when the agent is no longer loaded (the uv-prune
# bootout), so no caller restarts or reports an agent paused on purpose. The
# bound counts sleeps, so wall time is at most LIMIT / POLL * (POLL + TIMEOUT).
# Sets `waited` and `line` for the caller.
poll_until_healthy() { # $1 = LIMIT seconds
  waited=0
  while [ "$waited" -lt "$1" ]; do
    sleep "$POLL"
    waited=$((waited + POLL))
    line=$(probe) && rc=0 || rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 3 ] && return 3
    [ "$rc" -eq 4 ] && return 4
  done
  return 1
}

# Signal a process and all its descendants, children first, so a killed
# notifier leaves no curl behind holding the job's output open.
kill_tree() { # $1 = pid
  for child in $(pgrep -P "$1" 2>/dev/null); do
    kill_tree "$child"
  done
  kill "$1" 2>/dev/null || :
}

# Run the notifier with a deadline: notify.sh's ntfy curl has none, and a
# push that stalls would hold this run, and the cron job's lock, forever.
# No GNU timeout on macOS, so a background watchdog kills the tree. Returns
# the notifier's status, or 124 when the watchdog killed it. The watchdog
# acts only when its sleep ends normally: cancelling it kills the sleep first,
# and a killed sleep must not mark a timeout.
notify_bounded() {
  flag="$STATE_DIR/notify.timedout"
  rm -f "$flag"
  sh "$NOTIFY" "$@" &
  npid=$!
  ( sleep "$NOTIFY_TIMEOUT" && : > "$flag" && kill_tree "$npid" ) &
  wpid=$!
  wait "$npid" && nrc=0 || nrc=$?
  kill_tree "$wpid"
  wait "$wpid" 2>/dev/null || :
  if [ -e "$flag" ]; then
    rm -f "$flag"
    return 124
  fi
  return "$nrc"
}

watch() {
  mkdir -p "$STATE_DIR"
  marker="$STATE_DIR/alerted"
  line=$(probe) && rc=0 || rc=$?
  echo "$(stamp) $line"
  # A failed close (4) is a leak, not a hang: report it, never restart.
  [ "$rc" -eq 4 ] && return 1
  if [ "$rc" -ne 1 ]; then
    [ "$rc" -eq 0 ] && rm -f "$marker"
    return 0
  fi
  # One unanswered probe is not a hang: a server launchd is still starting
  # takes about 90s, and a kickstart -k then would reset that startup and
  # drop every live session. Restart only when the whole window fails.
  poll_until_healthy "$GRACE" && rc=0 || rc=$?
  if [ "$rc" -eq 4 ]; then
    echo "$(stamp) $line; no restart"
    return 1
  fi
  if [ "$rc" -eq 0 ]; then
    echo "$(stamp) workspace-mcp: answered within the grace window (${waited}s); no restart"
    rm -f "$marker"
    return 0
  fi
  # Exit 0 like the first probe's 3: the cron wrapper reports any nonzero
  # exit as a failure, and an agent paused on purpose is not one.
  if [ "$rc" -eq 3 ]; then
    echo "$(stamp) $line; no restart"
    return 0
  fi
  echo "$(stamp) workspace-mcp: no answer for ${waited}s; restarting: launchctl kickstart -k gui/$(id -u)/$LABEL"
  launchctl kickstart -k "gui/$(id -u)/$LABEL" || \
    echo "$(stamp) workspace-mcp: kickstart exited nonzero; polling anyway"
  poll_until_healthy "$RESTART_WAIT" && rc=0 || rc=$?
  if [ "$rc" -eq 4 ]; then
    echo "$(stamp) workspace-mcp: answered ${waited}s after the restart"
    echo "$(stamp) $line"
    rm -f "$marker"
    return 1
  fi
  if [ "$rc" -eq 0 ]; then
    echo "$(stamp) workspace-mcp: recovered ${waited}s after the restart"
    rm -f "$marker"
    return 0
  fi
  if [ "$rc" -eq 3 ]; then
    echo "$(stamp) $line; no notice"
    return 0
  fi
  echo "$(stamp) $line"
  echo "$(stamp) workspace-mcp: still down ${waited}s after the restart"
  if [ -e "$marker" ]; then
    echo "$(stamp) workspace-mcp: already notified for this outage at $(cat "$marker")"
    return 1
  fi
  notify_bounded -t "workspace-mcp down" -p high -c local,ntfy -b \
    "$LABEL did not answer initialize at $URL, and one launchctl kickstart -k did not bring it back within ${waited}s. Gmail and Drive tools are down; see ~/repos/ai/google_workspace_mcp/logs/service.err.log." \
    && nrc=0 || nrc=$?
  case "$nrc" in
    0) stamp > "$marker" ;;
    124) echo "$(stamp) workspace-mcp: notify did not finish within ${NOTIFY_TIMEOUT}s and was killed; will try again next run" ;;
    *) echo "$(stamp) workspace-mcp: notify failed; will try again next run" ;;
  esac
  return 1
}

case "${1:-}" in
  status)
    # probe's 4 (a failed close) is broken to local-health-check.
    probe && rc=0 || rc=$?
    [ "$rc" -eq 4 ] && exit 1
    exit "$rc"
    ;;
  watch)
    watch
    ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
