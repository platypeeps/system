#!/bin/sh
# After a merge to this checkout, restart only the sd services the merge
# changed, each in its safe form, and report what a restart cannot apply:
# a LaunchAgent change (an install) or a schema change (a migration) (sd:2725).
# Usage: deploy.sh plan|apply <from-sha> <to-sha>|test|help
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
# sd-serve's default port; its plist passes no --port.
SERVE_PORT=8769

usage() {
  cat <<'HELPEOF'
usage: deploy.sh plan <from-sha> <to-sha> | apply <from-sha> <to-sha> | test | help

  plan    read `git diff --name-only from..to` in this checkout and print one
          action per line; an empty plan prints nothing and exits 0:
            report needs migration (<paths>); restart after migrate
                    local-sd-db's schema changed; nothing restarts
            report needs sd_db install: ...  local-sd-db/sd_db/ changed; the
                    dashboard and the runner import an installed copy
            report needs <folder> install (<path>)  a LaunchAgent plist, or the
                    code that writes one, changed; never applied
            restart sd-serve    local-sd-db/ changed
            restart dashboard   local-project-dashboard/ or local-sd-db/sd_db/
            restart runner      local-sd-runner/ or local-sd-db/sd_db/
          Changes under tests/ and to *.md files restart nothing.
  apply   print the reports, then refuse with exit 1 before any restart when
          `lsof` cannot answer for port 8769, or when the dashboard's or the
          runner's installed sd_db lacks this checkout's last library commit
          (sd_dashboard.runtime._library_lag, run under the interpreter its
          plist names). Then restart. sd-serve: skip with a report while a
          session is open on port 8769, else `launchctl kickstart -k` and
          wait for a new listener. dashboard: kickstart -k, then
          `dashboard.sh health`. runner: `runner.sh restart` (drains), never
          kickstart. A service whose agent is not loaded is skipped. Stops at
          the first failed check and exits 1 naming it.
  test    run the unittest suite (tests/); extra arguments go to unittest.

env:
  SYSTEM_TOOLS_LABEL_PREFIX  launchd label prefix (default local.system-tools)
  DEPLOY_WAIT                seconds to wait for sd-serve's listener (default 30)
HELPEOF
}

# The changed paths, minus tests and docs, limited to the pathspecs given.
changed() {
  git -C "$ROOT" diff --name-only "$RANGE" -- "$@" ':!*/tests/*' ':!*.md'
}

# One report per LaunchAgent plist, or plist-writing code, the range changed.
installs() {
  # ponytail: a plist is code in two tools (runtime.py, sd_runner/cli.py), so
  # this greps changed lines for launchd keys and the names that feed them; a
  # value computed elsewhere goes unseen. Upgrade: render each plist at both
  # commits and compare.
  { changed '*.plist' '*.plist.template'
    git -C "$ROOT" diff --name-only "$RANGE" \
      -G'plistlib|ProgramArguments|EnvironmentVariables|KeepAlive|RunAtLoad|ProcessType|ThrottleInterval|Standard(Out|Error)Path|_launch_environment|PASSED_THROUGH|LAUNCH_PATH|SD_(RUNNER|DASHBOARD)_PYTHON' \
      -- '*.py' '*.sh' ':!*/tests/*'
  } | sort -u | while read -r path; do
    echo "report needs ${path%%/*} install ($path)"
  done
}

plan() {
  RANGE="$1..$2"
  git -C "$ROOT" rev-parse -q --verify "$1^{commit}" >/dev/null || { echo "deploy: not a commit: $1" >&2; exit 1; }
  git -C "$ROOT" rev-parse -q --verify "$2^{commit}" >/dev/null || { echo "deploy: not a commit: $2" >&2; exit 1; }
  # New dashboard or runner code may read a schema that does not exist yet,
  # and migrate.py wants both stopped: a schema change holds every restart.
  schema=$(changed 'local-sd-db/sd_db/schema*' | tr '\n' ' ')
  [ -z "$schema" ] || echo "report needs migration (${schema% }); restart after migrate"
  # The dashboard and the runner import an installed sd_db, not this
  # checkout's: a library change reaches them only through an install.
  library=$(changed local-sd-db/sd_db/)
  [ -z "$library" ] || echo "report needs sd_db install: local-sd-db/sd-db.sh install <venv> for the dashboard's and the runner's interpreters, before apply"
  installs
  [ -z "$schema" ] || return 0
  [ -z "$(changed local-sd-db/)" ] || echo "restart sd-serve"
  [ -z "$library$(changed local-project-dashboard/)" ] || echo "restart dashboard"
  [ -z "$library$(changed local-sd-runner/)" ] || echo "restart runner"
}

# Exits 0 when launchd holds the agent, else 1, as runner.sh status asks.
held() {
  launchctl print "gui/$(id -u)/$PREFIX.$1" >/dev/null 2>&1
}

refuse() {
  echo "deploy: refused before any restart: $1" >&2
  exit 1
}

fail() {
  echo "deploy: $1 failed; stopped" >&2
  exit 1
}

# The pids lsof finds in one TCP state on sd-serve's port. lsof -t exits 1
# and prints nothing for no match; any other answer, or no lsof at all, says
# nothing about the port and fails.
serve_pids() {
  out=$(lsof -w -nP -iTCP:$SERVE_PORT -sTCP:$1 -t 2>&1) && { echo "$out"; return 0; }
  rc=$?
  [ "$rc" -eq 1 ] && [ -z "$out" ] && return 0
  echo "lsof exited $rc: $out" >&2
  return 1
}

# The dashboard's own startup check, _library_lag, run under the consumer's
# interpreter before its agent is killed rather than after.
LAG='import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/local-project-dashboard")
from sd_dashboard.runtime import _library_lag
_library_lag(Path(sys.argv[1]))'

library_current() { # agent, plist variable naming its interpreter
  plist="$HOME/Library/LaunchAgents/$PREFIX.$1.plist"
  py=$(plutil -extract "EnvironmentVariables.$2" raw -o - "$plist" 2>/dev/null) || py=""
  [ -n "$py" ] || refuse "$1: cannot read $2 from $plist"
  why=$("$py" -I -c "$LAG" "$ROOT" 2>&1) ||
    refuse "$1: $(printf '%s\n' "$why" | tail -1); provision: local-sd-db/sd-db.sh install $(dirname "$(dirname "$py")"), then rerun apply"
}

# Everything that can refuse runs here, before the first restart.
preflight() {
  case "$1" in
    sd-serve)
      held sd-serve || return 0
      # One satellite session is one TCP connection to the listener (serve.py).
      sessions=$(serve_pids ESTABLISHED) || refuse "sd-serve: lsof cannot tell whether a satellite session is open"
      old_listener=$(serve_pids LISTEN) || refuse "sd-serve: lsof cannot read the listener" ;;
    dashboard) ! held sd-dashboard || library_current sd-dashboard SD_DASHBOARD_PYTHON ;;
    runner) ! held sd-runner || library_current sd-runner SD_RUNNER_PYTHON ;;
  esac
}

restart_serve() {
  if [ -n "$sessions" ]; then
    echo "report sd-serve not restarted: a session is open on port $SERVE_PORT; rerun apply when it closes"
    return 0
  fi
  launchctl kickstart -k "gui/$(id -u)/$PREFIX.sd-serve" || fail "sd-serve kickstart"
  waited=0
  while :; do
    new=$(serve_pids LISTEN) || fail "sd-serve listener check (lsof)"
    if [ -n "$new" ] && [ "$new" != "$old_listener" ]; then
      echo "restarted sd-serve: listener pid $new"
      return 0
    fi
    [ "$waited" -lt "${DEPLOY_WAIT:-30}" ] || fail "sd-serve listener check (no new listener on port $SERVE_PORT)"
    sleep 1
    waited=$((waited + 1))
  done
}

apply() {
  actions=$(plan "$1" "$2")
  printf '%s\n' "$actions" | grep '^report' || true
  services=$(printf '%s\n' "$actions" | sed -n 's/^restart //p')
  for service in $services; do preflight "$service"; done
  for service in $services; do
    case "$service" in
      sd-serve) held sd-serve || { echo "skip sd-serve: $PREFIX.sd-serve is not loaded"; continue; }
        restart_serve ;;
      dashboard) held sd-dashboard || { echo "skip dashboard: $PREFIX.sd-dashboard is not loaded"; continue; }
        launchctl kickstart -k "gui/$(id -u)/$PREFIX.sd-dashboard" || fail "dashboard kickstart"
        sh "$ROOT/local-project-dashboard/dashboard.sh" health || fail "dashboard health"
        echo "restarted dashboard" ;;
      runner) held sd-runner || { echo "skip runner: $PREFIX.sd-runner is not loaded"; continue; }
        sh "$ROOT/local-sd-runner/runner.sh" restart || fail "runner restart"
        echo "restarted runner" ;;
    esac
  done
}

case "${1:-}" in
  plan|apply)
    [ $# -eq 3 ] || { usage >&2; exit 1; }
    "$1" "$2" "$3" ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage ;;
  '') usage >&2; exit 1 ;;
  *) echo "deploy: unknown command: $1" >&2; usage >&2; exit 1 ;;
esac
