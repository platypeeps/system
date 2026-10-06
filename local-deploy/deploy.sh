#!/bin/sh
# After a merge to this checkout, restart only the sd services the merge
# changed, each in its safe form, and report what a restart cannot apply:
# a LaunchAgent change (an install) or a schema change (a migration) (sd:2725).
# `upgrade` wraps the whole hub upgrade: pull, install sd_db, apply (sd:2812).
# Usage: deploy.sh plan|apply <from-sha> <to-sha>|upgrade [--from SHA]|test|help
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
PREFIX="${SYSTEM_TOOLS_LABEL_PREFIX:-local.system-tools}"
# sd-serve's default port; its plist passes no --port.
SERVE_PORT=8769
# The sha the last complete upgrade deployed; local disk, outside the checkout.
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/system/deploy/deployed"

usage() {
  cat <<'HELPEOF'
usage: deploy.sh plan <from-sha> <to-sha> | apply <from-sha> <to-sha> |
       upgrade [--from SHA] | test | help

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
          `lsof` cannot answer for port 8769, when the dashboard's or the
          runner's installed sd_db differs in content from this checkout's
          local-sd-db/sd_db (sd_dashboard.runtime._build_manifest on both,
          run under the interpreter its plist names), or when the runner's
          load limit refuses (local-sd-runner/sd_runner/load.py). Then
          restart. sd-serve: skip with a report while a session is open on
          port 8769, else `launchctl kickstart -k` and wait for a new
          listener. dashboard: kickstart -k, then `dashboard.sh health
          --wait DEPLOY_WAIT`. runner: `runner.sh restart` (drains), never
          kickstart. A service whose agent is not loaded is skipped. Stops at
          the first failed check and exits 1 naming it.
  upgrade [--from SHA]
          refuse unless this checkout is on main and clean; `git fetch
          origin`, then fast-forward to origin/main (refuse on divergence).
          From is the sha the last complete upgrade recorded in
          $XDG_STATE_HOME/system/deploy/deployed (default
          ~/.local/state/system/deploy/deployed); with no record, --from is
          required, and --from always overrides it. An empty plan records the
          new sha and exits 0. A migration report refuses, and so does the
          runner's load limit and an open sd-serve session, all before
          the first stop. A `needs sd_db install` report installs into each
          venv a loaded agent's plist names (`*_PYTHON`), and first stops
          every loaded sd agent ($PREFIX.sd-*.plist) whose interpreter
          lives there: the runner with `runner.sh stop` (drain), the others
          with `launchctl bootout`. Then `local-sd-db/sd-db.sh install
          <venv>`, apply, and a start of each stopped agent: sd-serve, the
          dashboard (`health --wait`), the runner (`runner.sh start`). A
          failure after the first stop leaves them stopped and names the
          rerun that finishes. It records the new sha only when
          every step passed, sd-serve was not held back by a session and no
          LaunchAgent install is reported, so a rerun replays the same range.
  test    run the unittest suite (tests/); extra arguments go to unittest.

env:
  SYSTEM_TOOLS_LABEL_PREFIX  launchd label prefix (default local.system-tools)
  DEPLOY_WAIT                seconds to wait for sd-serve's listener and the
                             dashboard's health (default 30)
  XDG_STATE_HOME             holds upgrade's record (default ~/.local/state)
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
  # commits and compare. local-deploy names those keys but writes no plist.
  { changed '*.plist' '*.plist.template'
    git -C "$ROOT" diff --name-only "$RANGE" \
      -G'plistlib|ProgramArguments|EnvironmentVariables|KeepAlive|RunAtLoad|ProcessType|ThrottleInterval|Standard(Out|Error)Path|_launch_environment|PASSED_THROUGH|LAUNCH_PATH|SD_(RUNNER|DASHBOARD)_PYTHON' \
      -- '*.py' '*.sh' ':!*/tests/*' ':!local-deploy/*'
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
  stopped_hint
  exit 1
}

fail() {
  echo "deploy: $1 failed; stopped" >&2
  stopped_hint
  exit 1
}

# STOPPED lists the agents upgrade stopped and has not started again. They
# stay stopped on a failure, so none runs on a half-replaced sd_db.
stopped_hint() {
  [ -z "${STOPPED:-}" ] || [ ! -s "$STOPPED" ] ||
    echo "deploy: left stopped: $(tr '\n' ' ' < "$STOPPED" | sed 's/ $//'); finish the upgrade with: sh $ROOT/local-deploy/deploy.sh upgrade --from $from" >&2
}

# One upgrade at a time, from the first check to the record: a second one
# could start the runner while the first still replaces sd_db. flock(2) on
# upgrade.lock, held by the descriptor the upgrade inherits through exec: the
# kernel drops it when the upgrade ends, so no lock outlives its holder.
LOCKER='import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit("deploy: refused before any restart: another upgrade holds " + sys.argv[1])
os.set_inheritable(fd, True)
os.execvp(sys.argv[2], sys.argv[2:])'


# The runner's load limit (sd_runner/load.py), under its interpreter; the
# module imports no sd_db, so it answers before an install too.
runner_load() {
  interpreter sd-runner SD_RUNNER_PYTHON
  why=$("$py" -I "$ROOT/local-sd-runner/sd_runner/load.py" 2>&1) || {
    reason=$(printf '%s\n' "$why" | sed -n 's/.*"reason": "\([^"]*\)".*/\1/p')
    refuse "runner: ${reason:-$(printf '%s\n' "$why" | tail -1)}"
  }
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

# The consumer's installed sd_db against this checkout's, file by file, with
# the dashboard's own build manifest. Not _library_lag: it compares commits,
# and a `sd-db.sh install` wheel records none, so it passes every such build.
LAG='import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/local-project-dashboard")
import sd_db
from sd_dashboard.runtime import _build_manifest
installed = dict(_build_manifest(Path(sd_db.__file__).resolve().parent))
checkout = dict(_build_manifest(Path(sys.argv[1]) / "local-sd-db/sd_db"))
differ = sorted(n for n in installed.keys() | checkout.keys() if installed.get(n) != checkout.get(n))
if differ:
    sys.exit("installed sd_db differs from this checkout in " + str(len(differ)) + " file(s): " + " ".join(differ[:5]))'

interpreter() { # agent, plist variable naming its interpreter
  plist="$HOME/Library/LaunchAgents/$PREFIX.$1.plist"
  py=$(plutil -extract "EnvironmentVariables.$2" raw -o - "$plist" 2>/dev/null) || py=""
  [ -n "$py" ] || refuse "$1: cannot read $2 from $plist"
}

library_current() { # agent, plist variable naming its interpreter
  interpreter "$1" "$2"
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
    runner)
      held sd-runner || return 0
      library_current sd-runner SD_RUNNER_PYTHON
      # Checked before any restart, not after sd-serve and the dashboard restarted.
      runner_load ;;
  esac
}

restart_serve() {
  if [ -n "$sessions" ]; then
    echo "report sd-serve not restarted: a session is open on port $SERVE_PORT; rerun apply when it closes"
    held_back=1
    return 0
  fi
  launchctl kickstart -k "gui/$(id -u)/$PREFIX.sd-serve" || fail "sd-serve kickstart"
  await_listener
  echo "restarted sd-serve: listener pid $new"
}

# Waits for a listener on sd-serve's port other than old_listener; sets new.
await_listener() {
  waited=0
  while :; do
    new=$(serve_pids LISTEN) || fail "sd-serve listener check (lsof)"
    if [ -n "$new" ] && [ "$new" != "$old_listener" ]; then
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
        sh "$ROOT/local-project-dashboard/dashboard.sh" health --wait "${DEPLOY_WAIT:-30}" || fail "dashboard health"
        echo "restarted dashboard" ;;
      runner) held sd-runner || { echo "skip runner: $PREFIX.sd-runner is not loaded"; continue; }
        sh "$ROOT/local-sd-runner/runner.sh" restart || fail "runner restart"
        echo "restarted runner" ;;
    esac
  done
}

# The interpreter dashboard.sh, runner.sh and sd-db.sh each run when the
# agent's plist names none.
DEFAULT_PYTHON="$HOME/repos/platypeeps/sd-ai-command-pack/.venv/bin/python"

# Every sd agent's label, from the plists launchd reads; not a fixed list.
agents() {
  for plist in "$HOME/Library/LaunchAgents/$PREFIX".sd-*.plist; do
    [ ! -e "$plist" ] || basename "$plist" .plist
  done
}

loaded() {
  launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1
}

stopped() {
  [ -s "$STOPPED" ] && grep -qxF "$1" "$STOPPED"
}

# The interpreter a `*_PYTHON` variable in the agent's plist names, or nothing.
named_python() {
  plutil -convert json -o - "$HOME/Library/LaunchAgents/$1.plist" 2>/dev/null |
    sed -n 's/.*"[A-Z_]*_PYTHON":"\([^"]*\)".*/\1/p' | sed 's#\\/#/#g'
}

# Where sd_db installs: the venv of each loaded or stopped agent whose plist
# names its interpreter (the dashboard's and the runner's).
venvs() {
  for label in $(agents); do
    loaded "$label" || stopped "$label" || continue
    named=$(named_python "$label")
    [ -z "$named" ] || dirname "$(dirname "$named")"
  done
}

# The loaded agents whose interpreter lives in a venv named on stdin: they
# must not run while sd_db there is replaced. The runner comes first, so it
# drains before anything else stops.
consumers() {
  targets=$(cat)
  for label in $(agents); do
    loaded "$label" || continue
    named=$(named_python "$label")
    venv=$(dirname "$(dirname "${named:-$DEFAULT_PYTHON}")")
    printf '%s\n' "$targets" | grep -qxF "$venv" || continue
    case "$label" in *.sd-runner) echo "0 $label" ;; *) echo "1 $label" ;; esac
  done | sort | cut -d' ' -f2-
}

stop_agent() {
  case "$1" in
    *.sd-runner) why=$(sh "$ROOT/local-sd-runner/runner.sh" stop 2>&1) ||
      fail "runner stop ($(printf '%s\n' "$why" | tail -3 | tr -s '\n ' '  '))" ;;
    *) launchctl bootout "gui/$(id -u)/$1" || fail "$1 bootout" ;;
  esac
  echo "$1" >> "$STOPPED"
  echo "stopped $1"
}

start_agent() {
  case "$1" in
    *.sd-runner)
      library_current sd-runner SD_RUNNER_PYTHON
      why=$(sh "$ROOT/local-sd-runner/runner.sh" start 2>&1) ||
        fail "runner start ($(printf '%s\n' "$why" | tail -3 | tr -s '\n ' '  '))" ;;
    *)
      [ "$1" != "$PREFIX.sd-dashboard" ] || library_current sd-dashboard SD_DASHBOARD_PYTHON
      launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$1.plist" || fail "$1 bootstrap"
      case "$1" in
        *.sd-serve) await_listener ;;
        *.sd-dashboard) sh "$ROOT/local-project-dashboard/dashboard.sh" health --wait "${DEPLOY_WAIT:-30}" ||
          fail "dashboard health" ;;
      esac ;;
  esac
  grep -vxF "$1" "$STOPPED" > "$STOPPED.$$" || true
  mv "$STOPPED.$$" "$STOPPED"
  echo "started $1"
}

# sd-serve first, the dashboard next, the runner last.
start_stopped() {
  [ -s "$STOPPED" ] || return 0
  for label in $(grep -v -e '\.sd-dashboard$' -e '\.sd-runner$' "$STOPPED") \
    $(grep '\.sd-dashboard$' "$STOPPED") $(grep '\.sd-runner$' "$STOPPED"); do
    start_agent "$label"
  done
  rm -f "$STOPPED"
}

upgrade() {
  from=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --from) [ $# -ge 2 ] || { usage >&2; exit 1; }; from="$2"; shift 2 ;;
      *) echo "deploy: unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
  done
  # The agents this upgrade stopped, one label a line; a rerun starts them.
  STOPPED="$(dirname "$STATE")/stopped"
  mkdir -p "$(dirname "$STATE")"
  [ "$(git -C "$ROOT" symbolic-ref -q --short HEAD)" = main ] || refuse "$ROOT is not on main"
  [ -z "$(git -C "$ROOT" status --porcelain)" ] || refuse "$ROOT has uncommitted or untracked files"
  git -C "$ROOT" fetch -q origin || fail "git fetch origin"
  git -C "$ROOT" merge-base --is-ancestor HEAD origin/main ||
    refuse "main and origin/main have diverged; reconcile them by hand"
  git -C "$ROOT" merge -q --ff-only origin/main || fail "fast-forward to origin/main"
  to=$(git -C "$ROOT" rev-parse HEAD)
  [ -n "$from" ] || from=$(cat "$STATE" 2>/dev/null) || true
  [ -n "$from" ] || refuse "no deployed sha recorded in $STATE; give --from SHA, the sha the services run now"
  actions=$(plan "$from" "$to")
  if [ -z "$actions" ] && [ ! -s "$STOPPED" ]; then
    echo "nothing to deploy from $from to $to"
    record "$to"
    return 0
  fi
  case "$actions" in *"report needs migration"*)
    printf '%s\n' "$actions" | grep '^report' || true
    refuse "the range needs a migration; migrate and restart by hand, then record it: deploy.sh upgrade --from $to" ;;
  esac
  # Every agent that runs from a venv the install replaces stops first: each
  # imports sd_db lazily and must never see a half-replaced library. Every
  # refusal that needs no new sd_db comes before the first stop.
  list=""; stopping=""
  case "$actions" in *"report needs sd_db install"*)
    # Assigned first, so a refusal inside them stops the script.
    list=$(venvs | sort -u)
    stopping=$(printf '%s\n' "$list" | consumers) ;;
  esac
  case "$actions
$stopping" in *"restart runner"*|*.sd-runner*)
    ! held sd-runner || runner_load ;;
  esac
  case "$actions
$stopping" in *"restart sd-serve"*|*.sd-serve*)
    if held sd-serve; then
      sessions=$(serve_pids ESTABLISHED) || refuse "sd-serve: lsof cannot tell whether a satellite session is open"
      [ -z "$sessions" ] || refuse "sd-serve: a session is open on port $SERVE_PORT; rerun upgrade when it closes"
      old_listener=$(serve_pids LISTEN) || refuse "sd-serve: lsof cannot read the listener"
    fi ;;
  esac
  for label in $stopping; do stop_agent "$label"; done
  if [ -n "$list" ]; then
    # A here-document keeps the loop in this shell and each path whole.
    while IFS= read -r venv; do
      [ -n "$venv" ] || continue
      sh "$ROOT/local-sd-db/sd-db.sh" install "$venv" || fail "sd_db install into $venv"
      echo "installed sd_db into $venv"
    done <<EOF
$list
EOF
  fi
  held_back=""
  apply "$from" "$to"
  start_stopped
  [ -z "$held_back" ] || { echo "deploy: sd-serve still runs the old code; $to not recorded; rerun upgrade" >&2; exit 1; }
  # A restart keeps the old LaunchAgent: recording would drop the report.
  case "$actions" in *"install ("*)
    echo "deploy: a LaunchAgent install is pending (reports above); $to not recorded; install it, then record it: deploy.sh upgrade --from $to" >&2
    exit 1 ;;
  esac
  record "$to"
}

record() {
  mkdir -p "$(dirname "$STATE")"
  printf '%s\n' "$1" > "$STATE.$$"
  mv "$STATE.$$" "$STATE"
  echo "deployed $1"
}

case "${1:-}" in
  plan|apply)
    [ $# -eq 3 ] || { usage >&2; exit 1; }
    "$1" "$2" "$3" ;;
  upgrade)
    # The fast-forward can rewrite this file while sh reads it: exit here, so
    # nothing past this line is read. The functions above, from before the
    # pull, run the whole upgrade; a change to deploy.sh applies next time.
    shift
    if [ -z "${DEPLOY_UPGRADE_LOCKED:-}" ]; then
      mkdir -p "$(dirname "$STATE")"
      DEPLOY_UPGRADE_LOCKED=1 exec "${PYTHON:-python3}" -c "$LOCKER" "$(dirname "$STATE")/upgrade.lock" \
        sh "$DIR/deploy.sh" upgrade "$@"
    fi
    upgrade "$@"
    exit 0 ;;
  test)
    shift
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@" ;;
  -h|--help|help) usage ;;
  '') usage >&2; exit 1 ;;
  *) echo "deploy: unknown command: $1" >&2; usage >&2; exit 1 ;;
esac
