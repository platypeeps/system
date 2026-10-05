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
            report needs <folder> install (<path>)  a LaunchAgent plist, or the
                    code that writes one, changed; never applied
            report needs migration (<paths>)  local-sd-db's schema changed;
                    sd-serve is then not restarted
            restart sd-serve    local-sd-db/ changed
            restart dashboard   local-project-dashboard/ changed
            restart runner      local-sd-runner/ changed
          Changes under tests/ and to *.md files restart nothing.
  apply   run the plan. sd-serve: skip with a report while a session is open
          on port 8769 (`lsof`), else `launchctl kickstart -k` and wait for a
          new listener. dashboard: kickstart -k, then `dashboard.sh health`.
          runner: `runner.sh restart` (drains), never kickstart. A service
          whose agent is not loaded is skipped. Stops at the first failed
          check and exits 1 naming it.
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

plan() {
  RANGE="$1..$2"
  git -C "$ROOT" rev-parse -q --verify "$1^{commit}" >/dev/null || { echo "deploy: not a commit: $1" >&2; exit 1; }
  git -C "$ROOT" rev-parse -q --verify "$2^{commit}" >/dev/null || { echo "deploy: not a commit: $2" >&2; exit 1; }
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
  schema=$(changed 'local-sd-db/sd_db/schema*' | tr '\n' ' ')
  if [ -n "$schema" ]; then
    echo "report needs migration (${schema% })"
  elif [ -n "$(changed local-sd-db/)" ]; then
    echo "restart sd-serve"
  fi
  [ -z "$(changed local-project-dashboard/)" ] || echo "restart dashboard"
  [ -z "$(changed local-sd-runner/)" ] || echo "restart runner"
}

# Exits 0 when launchd holds the agent; else says so and exits 1.
loaded() {
  launchctl print "gui/$(id -u)/$PREFIX.$1" >/dev/null 2>&1 && return 0
  echo "skip $1: $PREFIX.$1 is not loaded"
  return 1
}

fail() {
  echo "deploy: $1 failed; stopped" >&2
  exit 1
}

restart_serve() {
  loaded sd-serve || return 0
  # One satellite session is one TCP connection to the listener (serve.py).
  if [ -n "$(lsof -nP -iTCP:$SERVE_PORT -sTCP:ESTABLISHED -t 2>/dev/null || true)" ]; then
    echo "report sd-serve not restarted: a session is open on port $SERVE_PORT; rerun apply when it closes"
    return 0
  fi
  old=$(lsof -nP -iTCP:$SERVE_PORT -sTCP:LISTEN -t 2>/dev/null || true)
  launchctl kickstart -k "gui/$(id -u)/$PREFIX.sd-serve" || fail "sd-serve kickstart"
  waited=0
  while :; do
    new=$(lsof -nP -iTCP:$SERVE_PORT -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "$new" ] && [ "$new" != "$old" ]; then
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
  printf '%s\n' "$actions" | while read -r verb what rest; do
    case "$verb $what" in
      " ") ;;
      report*) echo "$verb $what $rest" ;;
      "restart sd-serve") restart_serve ;;
      "restart dashboard")
        loaded sd-dashboard || continue
        launchctl kickstart -k "gui/$(id -u)/$PREFIX.sd-dashboard" || fail "dashboard kickstart"
        sh "$ROOT/local-project-dashboard/dashboard.sh" health || fail "dashboard health"
        echo "restarted dashboard" ;;
      "restart runner")
        loaded sd-runner || continue
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
