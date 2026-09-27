#!/usr/bin/env bash
# cswap-auto — run `cswap auto` (the account-switching poll loop) as a macOS
# LaunchAgent that starts at login, restarts if it dies, and logs to ./logs.
#
# Usage:
#   cswap.sh start                   install/refresh the LaunchAgent and start it
#   cswap.sh stop                    stop and unload (stays stopped across logins)
#   cswap.sh stop --keep-installed   stop current process only; KeepAlive/login restarts it
#   cswap.sh status [lines]          state + log tail (exit 0 running, 1 loaded, 2 not loaded)
#   cswap.sh run                     the poll loop itself — invoked by launchd, not by hand
#
# Flags for `cswap auto` live in cswap-auto.conf; restart to apply.
set -euo pipefail

# launchd label prefix shared by every agent from this repository; override per machine.
LABEL_PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
LABEL="$LABEL_PREFIX.cswap-auto"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"
OUT_LOG="$LOG_DIR/cswap-auto.log"
ERR_LOG="$LOG_DIR/cswap-auto.err.log"
PLIST_TEMPLATE="$ROOT/cswap-auto.plist.template"
AGENT_DIR="$HOME/Library/LaunchAgents"
DST_PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"

mkdir -p "$LOG_DIR"

is_loaded() {
  launchctl print "$SERVICE" >/dev/null 2>&1
}

# `launchctl bootout` returns before launchd has finished tearing the job down,
# and bootstrapping during that window fails with "Input/output error".
wait_until_unloaded() {
  local i
  for ((i = 0; i < 50; i++)); do
    is_loaded || return 0
    sleep 0.2
  done
  return 1
}

cmd_run() {
  local conf="$ROOT/cswap-auto.conf"

  CSWAP_BIN="${CSWAP_BIN:-$HOME/.local/bin/cswap}"
  # Flags passed to `cswap auto`. Space-separated, word-split on purpose.
  CSWAP_AUTO_ARGS="${CSWAP_AUTO_ARGS:-}"

  # shellcheck source=/dev/null
  [[ -f "$conf" ]] && source "$conf"

  if [[ ! -x "$CSWAP_BIN" ]]; then
    echo "[cswap-auto] ERROR: cswap not executable at $CSWAP_BIN" >&2
    exit 78 # EX_CONFIG - do not hot-loop on a broken install
  fi

  echo "[cswap-auto] $(date '+%Y-%m-%dT%H:%M:%S%z') starting: $CSWAP_BIN auto $CSWAP_AUTO_ARGS"

  # shellcheck disable=SC2086 # deliberate word splitting of the configured flags
  exec "$CSWAP_BIN" auto $CSWAP_AUTO_ARGS
}

# Escape a value for the replacement side of a sed s|||.
sed_escape() {
  printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'
}

# Render the committed template with this machine's label, folder and home.
render_plist() {
  sed -e "s|@LABEL@|$(sed_escape "$LABEL")|g" \
      -e "s|@DIR@|$(sed_escape "$ROOT")|g" \
      -e "s|@HOME@|$(sed_escape "$HOME")|g" \
      "$PLIST_TEMPLATE"
}

cmd_start() {
  mkdir -p "$AGENT_DIR"
  render_plist >"$DST_PLIST"

  # Replace any previous registration so an edited plist actually takes effect.
  if is_loaded; then
    launchctl bootout "$SERVICE" 2>/dev/null || true
    wait_until_unloaded || { echo "ERROR: $LABEL is still loaded after bootout" >&2; exit 1; }
  fi

  # Retry: launchd can still report the old job as in-flight for a moment.
  local attempt
  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$DST_PLIST" 2>/dev/null; then
      break
    fi
    if [[ $attempt -eq 5 ]]; then
      echo "ERROR: bootstrap failed for $DST_PLIST" >&2
      launchctl bootstrap "$DOMAIN" "$DST_PLIST" # re-run to surface the error
      exit 1
    fi
    sleep 0.5
  done
  launchctl enable "$SERVICE"
  launchctl kickstart "$SERVICE" >/dev/null

  echo "started: $LABEL"
  echo "  plist: $DST_PLIST"
  echo "  log:   $OUT_LOG"
}

cmd_stop() {
  local keep_installed=0
  [[ "${1:-}" == "--keep-installed" ]] && keep_installed=1

  if ! is_loaded; then
    echo "not loaded: $LABEL"
    exit 0
  fi

  if [[ $keep_installed -eq 1 ]]; then
    launchctl kill SIGTERM "$SERVICE" 2>/dev/null || true
    echo "signalled: $LABEL (still installed; KeepAlive may restart it)"
    exit 0
  fi

  launchctl bootout "$SERVICE" || true
  wait_until_unloaded || { echo "ERROR: $LABEL is still loaded" >&2; exit 1; }
  rm -f "$DST_PLIST"
  echo "stopped and unloaded: $LABEL"
}

cmd_status() {
  local tail_lines="${1:-15}"

  echo "label:     $LABEL"
  echo "plist:     $DST_PLIST $([[ -f "$DST_PLIST" ]] && echo '(installed)' || echo '(NOT installed)')"

  if ! is_loaded; then
    echo "state:     not loaded"
    echo "at login:  no"
    exit 2
  fi

  # Right after a kickstart the state is briefly "xpcproxy"/"spawn scheduled"
  # before it settles on "running". Give it a moment so status is not a false negative.
  local info="" state="" pid="" last=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    info="$(launchctl print "$SERVICE" 2>/dev/null || true)"
    state="$(awk -F'= ' '/^\tstate = /{print $2; exit}' <<<"$info")"
    [[ "$state" == "running" ]] && break
    sleep 0.2
  done

  if [[ -z "$info" ]]; then
    echo "state:     not loaded (disappeared while querying)"
    exit 2
  fi
  pid="$(awk -F'= ' '/^\tpid = /{print $2; exit}' <<<"$info")"
  last="$(awk -F'= ' '/last exit code = /{print $2; exit}' <<<"$info")"

  echo "state:     ${state:-unknown}"
  echo "pid:       ${pid:-none}"
  echo "last exit: ${last:-n/a}"
  echo "at login:  yes (RunAtLoad)"

  echo
  echo "--- $OUT_LOG (last $tail_lines) ---"
  [[ -s "$OUT_LOG" ]] && tail -n "$tail_lines" "$OUT_LOG" || echo "(empty)"

  if [[ -s "$ERR_LOG" ]]; then
    echo
    echo "--- $ERR_LOG (last $tail_lines) ---"
    tail -n "$tail_lines" "$ERR_LOG"
  fi

  # A pid means the job is spawned, even if the state label has not settled yet.
  [[ "$state" == "running" || -n "$pid" ]] && exit 0 || exit 1
}

case "${1:-}" in
  run)    cmd_run ;;
  start)  cmd_start ;;
  stop)   shift; cmd_stop "$@" ;;
  status) shift || true; cmd_status "$@" ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: cswap.sh run|start|stop [--keep-installed]|status [lines]

  run          exec `cswap auto` — long-running poll loop (what the LaunchAgent calls)
  start        install + load the LaunchAgent
  stop         unload the LaunchAgent (--keep-installed: keep the plist)
  status       agent state + recent log lines (default 15); exits 0 ok / 1 degraded / 2 down

environment: SYSTEM_TOOLS_LABEL_PREFIX (launchd label prefix,
             default local.system-tools; the label is <prefix>.cswap-auto)
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") run|start|stop [--keep-installed]|status [lines]" >&2
    exit 1
    ;;
esac
